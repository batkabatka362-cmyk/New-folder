"""Offline backtest: replay logged candidates through the scorer + forward outcome.

  python -m memebot.backtest.simulate [--min-age 3600] [--db PATH] [--threshold X]

For each logged candidate (one per mint, earliest), reconstruct it from its
stored feature vector, re-run the rule gate + scorer + entry threshold, and for
would-be ENTRIES compute the net return from the token's CURRENT DexScreener
price minus a round-trip fee+slippage cost. Reports entries, win-rate, average
net return, total simulated PnL, split by mode.

LIMITATION: we have one entry price and one forward price, not the intrabar
path, so this CANNOT model TP/SL ordering — it is a hold-to-now approximation.
Use it to sanity-check entry selection (does a higher score correlate with a
better forward outcome?), not as a precise PnL forecast.
"""
from __future__ import annotations

import argparse
import asyncio
import time

from ..config import get_settings
from ..constants import MODE_HOLD, MODE_SCALP
from ..feed.dexscreener import DexScreenerClient
from ..filter import rules
from ..filter.features import BREAKOUT_CODE, EMA_CODE, TREND_DIR_CODE
from ..models import Candidate
from ..risk.limits import expected_return
from ..signals.scoring import Scorer
from ..utils.money import round_trip_cost_pct


def _tri_back(v: float) -> bool | None:
    return None if v < 0 else (v >= 0.5)


def candidate_from_features(feats: dict, price_usd: float, mint: str = "", symbol: str = "") -> Candidate:
    """Rebuild a Candidate from a stored feature dict (inverse of build_features)."""
    c = Candidate(mint=mint, symbol=symbol)
    c.price_usd = price_usd
    c.liquidity_usd = float(feats.get("liquidity_usd", 0.0))
    c.market_cap_usd = float(feats.get("market_cap_usd", 0.0))
    c.vol_to_mcap_pct = float(feats.get("vol_to_mcap_pct", 0.0))
    c.buy_sell_ratio = float(feats.get("buy_sell_ratio", 0.0))
    c.unique_buyers = int(feats.get("unique_buyers", 0))
    c.features["vol_spike"] = float(feats.get("vol_spike", 0.0))
    c.features["vol_h1"] = float(feats.get("vol_h1", 0.0))
    c.mode = MODE_HOLD if float(feats.get("is_hold", 0.0)) >= 0.5 else MODE_SCALP
    c.mint_revoked = _tri_back(float(feats.get("mint_revoked", -1.0)))
    c.freeze_revoked = _tri_back(float(feats.get("freeze_revoked", -1.0)))
    lp = float(feats.get("lp_burned_pct", -1.0))
    c.lp_burned_pct = None if lp < 0 else lp
    top = float(feats.get("top5_concentration_pct", -1.0))
    c.top5_concentration_pct = None if top < 0 else top
    # P8 trajectory/velocity: restore so build_features re-derives identical scalars (GBM round-trip).
    c.price_change_m5 = float(feats.get("price_change_m5", 0.0))
    c.price_change_h1 = float(feats.get("price_change_h1", 0.0))
    c.features["age_s"] = float(feats.get("age_min", 0.0)) * 60.0
    _back = lambda code, v: next((k for k, x in code.items() if x == v), None)  # noqa: E731
    c.features["trend"] = {
        "dir": _back(TREND_DIR_CODE, float(feats.get("trend_dir", -2.0))) or "unknown",
        "chg_pct": float(feats.get("trend_chg_pct", 0.0)),
        "off_high_pct": float(feats.get("off_high_pct", 0.0)),
    }
    c.features["tech"] = {
        "ema_signal": _back(EMA_CODE, float(feats.get("ema_signal", -2.0))) or "n/a",
        "breakout": _back(BREAKOUT_CODE, float(feats.get("breakout", 0.0))) or "none",
        # P10b SR6: round-trip res/sup distance so build_features re-derives identical scalars
        "res_dist_pct": float(feats.get("res_dist_pct", 0.0)),
        "sup_dist_pct": float(feats.get("sup_dist_pct", 0.0)),
    }
    c.features["liq_trend"] = {
        "dir": _back(TREND_DIR_CODE, float(feats.get("liq_trend_dir", -2.0))) or "unknown",
        "chg_pct": float(feats.get("liq_trend_chg_pct", 0.0)),
    }
    chp = float(feats.get("creator_holding_pct", -1.0))
    c.features["creator_holding_pct"] = None if chp < 0 else chp
    ss = float(feats.get("sniper_share", -1.0))
    c.features["sniper_share"] = None if ss < 0 else ss
    cd = float(feats.get("creator_dump_ratio", -1.0))
    c.features["creator_dump_ratio"] = None if cd < 0 else cd
    bs = float(feats.get("bundle_share", -1.0))
    c.features["bundle_share"] = None if bs < 0 else bs
    sms = float(feats.get("smart_money_share", -1.0))
    c.features["smart_money_share"] = None if sms < 0 else sms
    c.features["buyer_growth"] = float(feats.get("buyer_growth", 0.0))   # N13: signed; 0.0 = neutral/unknown
    return c


def _ev_tp_sl(mode: str, ep) -> tuple[float, float]:
    """The tp/sl an entry's expected-return gate should use — mirrors Bot._try_open: a HOLD's
    realistic win is the TRAIL width (the +300% ceiling is never realized), not hold_tp_pct."""
    if mode == MODE_SCALP:
        return ep.scalp_tp_pct, ep.scalp_sl_pct
    return min(ep.hold_tp_pct, ep.hold_trail_pct), ep.hold_sl_pct


def simulate(rows, prices: dict, scorer: Scorer, settings, entry_threshold: float,
             min_expected_return: float | None = None) -> dict:
    """rows: list[(mint, logged_price_usd, features_dict)]; prices: mint -> current price.
    min_expected_return (D4): None = NO EV gate (default; backward-compatible for existing callers).
    A numeric value — INCLUDING 0.0 or a negative floor — applies the LIVE gate `expected_return(
    score, tp, sl, rtc) >= value`, using the score as the conviction proxy (the live rule-fallback;
    Bot._try_open always applies this gate, so 0.0 still filters negative-EV entries — matching live)."""
    rtc = round_trip_cost_pct(settings.fees)
    entries: list[tuple[float, str]] = []
    considered = 0
    ev_skipped = 0
    for mint, logged_price, feats in rows:
        c = candidate_from_features(feats, logged_price, mint=mint)
        rules.evaluate(c, settings)
        if not c.rule_passed:
            continue
        scorer.score(c)
        considered += 1
        if c.score < entry_threshold:
            continue
        if min_expected_return is not None:
            tp, sl = _ev_tp_sl(c.mode, settings.exit)
            if expected_return(c.score, tp, sl, rtc) < min_expected_return:
                ev_skipped += 1
                continue
        cur = prices.get(mint, 0.0)
        fwd = (cur / logged_price - 1.0) if logged_price > 0 else -1.0   # missing price = rugged
        entries.append((max(-1.0, fwd - rtc), c.mode))   # a position can't lose more than 100%

    def _agg(items):
        n = len(items)
        wins = sum(1 for r, _ in items if r > 0)
        total = sum(r for r, _ in items)
        return {"entries": n, "win_rate": (wins / n if n else 0.0),
                "avg_net_return": (total / n if n else 0.0), "total_net_return": total}

    out = {"considered": considered, "round_trip_cost_pct": rtc, "ev_skipped": ev_skipped}
    out.update(_agg(entries))
    out["scalp"] = _agg([e for e in entries if e[1] == MODE_SCALP])
    out["hold"] = _agg([e for e in entries if e[1] == MODE_HOLD])
    return out


async def _run(args) -> None:
    from .label import _load_rows   # reuse: one sample per mint, min-age filter

    s = get_settings()
    rows = _load_rows(args.db or s.db_path, args.min_age, time.time())
    if not rows:
        print("No eligible candidates yet — run the bot to collect data.")
        return
    mints = sorted({m for m, _, _ in rows})
    async with DexScreenerClient(s.dexscreener_base_url) as dex:
        snaps, covered = await dex.snapshots(mints)
    failed = [m for m in mints if m not in covered]
    if failed:
        print(f"  ({len(failed)} mints dropped — price fetch failed, not counted as rugs)")
    rows = [r for r in rows if r[0] in covered]      # fetch failures are NOT rugs
    if not rows:
        print("All price fetches failed — try again.")
        return
    prices = {m: (snaps[m].price_usd if m in snaps else 0.0) for m in covered}

    from ..filter.gbm import GBMScorer
    scorer = Scorer(s, GBMScorer.load(s.gbm_model_path))
    thr = args.threshold if args.threshold is not None else (
        s.gbm_entry_threshold if scorer.kind == "gbm" else s.entry_threshold)
    min_ev = args.min_ev if args.min_ev is not None else s.min_expected_return
    res = simulate(rows, prices, scorer, s, thr, min_expected_return=min_ev)

    print(f"backtest over {len(rows)} mints | scorer={scorer.kind} | entry>={thr} | "
          f"min_ev={min_ev} | round-trip cost={res['round_trip_cost_pct']*100:.1f}%")
    print(f"  considered (gate-passed): {res['considered']}  ev_skipped: {res['ev_skipped']}")
    for k in ("", "scalp", "hold"):
        block = res if k == "" else res[k]
        label = k or "ALL"
        print(f"  {label:5} entries={block['entries']} win%={block['win_rate']*100:.1f} "
              f"avg_net={block['avg_net_return']*100:+.1f}% total_net={block['total_net_return']*100:+.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser(description="Offline backtest over logged candidates.")
    ap.add_argument("--db", default=None)
    ap.add_argument("--min-age", type=float, default=3600.0)
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--min-ev", type=float, default=None,
                    help="D4: also require expected_return >= this (default: config min_expected_return)")
    asyncio.run(_run(ap.parse_args()))


if __name__ == "__main__":
    main()
