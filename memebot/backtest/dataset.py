"""Turn raw logs into a WELL-CLASSIFIED, ready-to-use dataset (P8 data pipeline).

`label.py` produces a BINARY (winner / not) column for GBM training. This goes further: it reads the
unbiased `observations` population (every indexed snapshot, deduped to ONE earliest row per mint —
the same group-safe choice as label.py), reconstructs each mint's forward path OFFLINE from its own
later logged prices, and classifies the outcome into a 6-way taxonomy so you can SEE what separates a
winner from a rug — not just count rows. The export carries the FULL feature set (FEATURE_NAMES plus
the P7/P8 dataset-only signals sell_pressure / bsr_h1 / creator_launches), the realized peak/trough/
final returns, and the outcome class — ready to analyse, calibrate thresholds from, or train on.

Outcome classes (the "classify very well" part):
  rug    — a witnessed collapse: forward trough <= -rug_drop (the cohort the price SL gaps past)
  dead   — lost the price thread in-window without a witnessed collapse (delisted / untracked)
  winner — peaked >= +up AND still held >= +win_hold at the horizon (ran and KEPT it)
  fade   — peaked >= +up but round-tripped below win_hold by the horizon (the pump-and-give-back trap)
  loser  — no pump, faded to <= -loss_band at the horizon (a slow bleeder, not a rug)
  flat   — none of the above: a small move either way

  python -m memebot.backtest.dataset --horizon 300 --out classified.csv
"""
from __future__ import annotations

import argparse
import csv
import statistics
from collections import defaultdict

from ..config import get_settings
from ..filter.features import FEATURE_NAMES
from .label import _load_all_obs

# dataset-only signals (NOT in FEATURE_NAMES) worth carrying for study / calibration
_EXTRA_COLS = ["sell_pressure", "bsr_h1", "creator_launches"]
_RETURN_COLS = ["peak_ret", "trough_ret", "final_ret"]
CLASSES = ("winner", "fade", "loser", "flat", "rug", "dead", "glitch")


def classify_outcome(peak_ret: float, trough_ret: float, final_ret: float, priced_at_horizon: bool,
                     *, up: float = 0.5, rug_drop: float = 0.8, win_hold: float = 0.15,
                     loss_band: float = 0.25, glitch_cap: float = 20.0) -> str:
    """Classify one mint's forward outcome. Order matters: an absurd forward return (>= glitch_cap,
    e.g. +1900%) is a PRICING GLITCH (the curve-buy vs DexScreener-sell SOL-scale mismatch), NOT a
    real winner — quarantined so it can't poison creator-reputation / calibration / GBM labels. A
    witnessed collapse is a RUG even if it pumped first or we later lost its price; an
    unpriced-at-horizon row that did NOT collapse is DEAD (untracked), never a fake winner/loser."""
    if glitch_cap > 0 and (peak_ret >= glitch_cap or final_ret >= glitch_cap):
        return "glitch"
    if trough_ret <= -abs(rug_drop):
        return "rug"
    if not priced_at_horizon:
        return "dead"
    if peak_ret >= up:
        return "winner" if final_ret >= win_hold else "fade"
    if final_ret <= -abs(loss_band):
        return "loser"
    return "flat"


def forward_outcomes(rows, horizon_s: float, tol_s: float, *, indexed_only: bool = True):
    """Per-mint forward path from the mint's OWN later logged prices (offline, no network). Uses the
    EARLIEST observation as the entry (group-safe, like label.py), then tracks the best/worst forward
    return inside [t0, t0+horizon+tol] and the return nearest the horizon. Returns a list of dicts
    with the entry features + peak/trough/final returns + whether it was priced near the horizon."""
    by_mint: dict[str, list] = defaultdict(list)
    for mint, ts, price, feats in rows:
        if price > 0 and (not indexed_only or float(feats.get("liquidity_usd", 0.0)) > 0):
            by_mint[mint].append((ts, price, feats))
    out = []
    for mint, obs in by_mint.items():
        obs.sort(key=lambda x: x[0])
        t0, p0, feats = obs[0]
        if p0 <= 0:
            continue
        target = t0 + horizon_s
        rets = [(ts, price / p0 - 1.0) for ts, price, _ in obs if t0 < ts <= target + tol_s]
        peak_ret = max((r for _, r in rets), default=0.0)
        trough_ret = min((r for _, r in rets), default=0.0)
        near = [(abs(ts - target), r) for ts, r in rets if abs(ts - target) <= tol_s]
        priced = bool(near)
        final_ret = min(near)[1] if priced else 0.0   # the return at the obs closest to the horizon
        out.append({"mint": mint, "features": feats, "peak_ret": peak_ret,
                    "trough_ret": trough_ret, "final_ret": final_ret, "priced_at_horizon": priced,
                    "t0": t0})   # P10b CAL3: entry ts -> recency-weighted creator reputation (additive; not in FEATURE_NAMES)
    return out


def build_dataset(rows, horizon_s: float, tol_s: float, *, indexed_only: bool = True,
                  up: float = 0.5, rug_drop: float = 0.8, win_hold: float = 0.15,
                  loss_band: float = 0.25) -> list[dict]:
    """Forward outcomes + the 6-way class label, one row per mint — the ready-to-use dataset."""
    recs = forward_outcomes(rows, horizon_s, tol_s, indexed_only=indexed_only)
    for r in recs:
        r["outcome"] = classify_outcome(r["peak_ret"], r["trough_ret"], r["final_ret"],
                                        r["priced_at_horizon"], up=up, rug_drop=rug_drop,
                                        win_hold=win_hold, loss_band=loss_band)
        r["win"] = 1 if r["outcome"] == "winner" else 0   # convenience binary (GBM-compatible)
    return recs


def _feat(rec: dict, name: str) -> float:
    return float(rec["features"].get(name, 0.0) or 0.0)


def summarize(recs: list[dict]) -> dict:
    """Class distribution + per-class median of the load-bearing features — the 'what separates a
    winner from a rug' table. Pure; returns {} on no rows."""
    if not recs:
        return {}
    by_class: dict[str, list] = defaultdict(list)
    for r in recs:
        by_class[r["outcome"]].append(r)
    cols = ["liquidity_usd", "market_cap_usd", "vol_h1", "buy_sell_ratio",
            "top5_concentration_pct", "sell_pressure", "creator_launches",
            # P10b CAL2 fix: the velocity/trajectory features that calibrate._WATCH ALSO inspects — they
            # MUST have a per-class median here or suggest_thresholds reads 0.0 and they can never emit a
            # suggestion (the whole point of CAL2). Keep this list a superset of _WATCH's feature names.
            "vol_to_mcap_pct", "vol_per_min", "off_high_pct", "price_change_m5", "price_change_h1",
            "trend_chg_pct", "buyer_growth", "liq_trend_chg_pct"]
    med = lambda xs: statistics.median(xs) if xs else 0.0   # noqa: E731
    summary = {"n": len(recs), "classes": {}}
    for cls in CLASSES:
        group = by_class.get(cls, [])
        if not group:
            continue
        summary["classes"][cls] = {
            "n": len(group),
            "frac": len(group) / len(recs),
            "median_fwd_pct": med([r["final_ret"] for r in group]) * 100.0,
            **{c: med([_feat(r, c) for r in group]) for c in cols},
        }
    return summary


def write_csv(recs: list[dict], path: str) -> None:
    header = (["mint"] + FEATURE_NAMES + _EXTRA_COLS + _RETURN_COLS + ["outcome", "win"])
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in recs:
            w.writerow(
                [r["mint"]]
                + [_feat(r, name) for name in FEATURE_NAMES]
                + [_feat(r, name) for name in _EXTRA_COLS]
                + [r["peak_ret"], r["trough_ret"], r["final_ret"]]
                + [r["outcome"], r["win"]]
            )


def _print_summary(summary: dict) -> None:
    if not summary:
        print("No classified rows — collect longer-tracked data or widen --tol.")
        return
    print(f"\n{summary['n']} classified mints:")
    cols = ["liquidity_usd", "vol_h1", "buy_sell_ratio", "top5_concentration_pct",
            "sell_pressure", "creator_launches"]
    print(f"  {'class':7} {'n':>5} {'frac':>6} {'fwd%':>7}  " + "  ".join(f"{c[:10]:>10}" for c in cols))
    for cls in CLASSES:
        c = summary["classes"].get(cls)
        if not c:
            continue
        print(f"  {cls:7} {c['n']:>5} {c['frac'] * 100:>5.1f}% {c['median_fwd_pct']:>6.0f}%  "
              + "  ".join(f"{c[col]:>10.1f}" for col in cols))
    print("\nRead it as: compare the WINNER row to the RUG row -> a feature whose medians diverge is a "
          "real separator worth weighting; one that overlaps is noise. Calibrate thresholds from this.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Classify logged observations into a ready-to-use dataset.")
    ap.add_argument("--db", default=None, help="SQLite path (default: config DB_PATH)")
    ap.add_argument("--horizon", type=float, default=300.0, help="forward horizon in seconds (default 300 = 5m)")
    ap.add_argument("--tol", type=float, default=900.0, help="tolerance window (s) around the horizon")
    ap.add_argument("--up", type=float, default=0.5, help="peak forward return for a winner/fade (0.5 = +50%%)")
    ap.add_argument("--rug-drop", type=float, default=0.8, help="trough drop for a RUG (0.8 = -80%%)")
    ap.add_argument("--win-hold", type=float, default=0.15, help="held forward return to stay a WINNER vs FADE")
    ap.add_argument("--loss-band", type=float, default=0.25, help="faded forward return for a LOSER (0.25 = -25%%)")
    ap.add_argument("--all", action="store_true", help="include pre-index curve-only rows (default: indexed-only)")
    ap.add_argument("--out", default="classified.csv")
    args = ap.parse_args()

    s = get_settings()
    rows = _load_all_obs(args.db or s.db_path)
    if not rows:
        print("No logged observations with a price — run the bot to collect data.")
        return
    recs = build_dataset(rows, args.horizon, args.tol, indexed_only=not args.all,
                         up=args.up, rug_drop=args.rug_drop, win_hold=args.win_hold, loss_band=args.loss_band)
    if not recs:
        print(f"No mints have a logged forward price within ±{args.tol:.0f}s of +{args.horizon:.0f}s "
              "— collect longer-tracked data or widen --tol.")
        return
    write_csv(recs, args.out)
    print(f"wrote {len(recs)} classified rows to {args.out}")
    _print_summary(summarize(recs))


if __name__ == "__main__":
    main()
