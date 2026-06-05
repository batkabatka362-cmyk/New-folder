"""Rigorous winner-vs-rug SEPARATION analysis (P8) — the honest answer to "does any signal work?".

`calibrate.suggest_thresholds` compares per-class MEDIANS, which the audit flagged as under-powered
and mis-specified (a median compare hides overlap and is fooled by skew). This computes, per feature,
the proper distribution-free discrimination metric: single-feature AUC = P(a random winner's value >
a random rug's value), via the Mann-Whitney rank statistic. 0.5 = no separation; the further from
0.5, the more that feature ALONE ranks winners apart from rugs. Unknown sentinels (-1 safety / -2
categorical) are excluded so missing data can't fake a separation.

Use it to know — statistically, not by eyeballing medians — whether the static entry features really
don't separate (the standing finding), and whether the new trajectory/velocity/liquidity-trend
features DO once forward data accrues. That answer decides if a free-data entry edge exists at all.

  python -m memebot.backtest.separation --horizon 300
"""
from __future__ import annotations

import argparse
import statistics

from ..config import get_settings
from .dataset import build_dataset
from .label import _load_all_obs

# features worth testing for winner-vs-rug discrimination (static + the P7/P8 velocity signals)
_TEST_FEATURES = [
    "liquidity_usd", "market_cap_usd", "vol_to_mcap_pct", "buy_sell_ratio", "vol_h1", "vol_spike",
    "top5_concentration_pct", "sell_pressure", "bsr_h1", "creator_launches", "creator_holding_pct",
    "price_change_m5", "price_change_h1", "trend_chg_pct", "off_high_pct", "vol_per_min", "age_min",
    "buyer_growth",   # N13 breadth velocity
    "res_dist_pct", "sup_dist_pct",   # P10b SR6: support/resistance distance (now in FEATURE_NAMES)

    "trend_dir", "ema_signal", "breakout", "liq_trend_dir", "liq_trend_chg_pct",
    "sniper_share", "creator_dump_ratio", "bundle_share", "smart_money_share",   # G3 tape signals
]
_SAFETY = {"mint_revoked", "freeze_revoked", "lp_burned_pct", "top5_concentration_pct",
           "creator_holding_pct", "sniper_share", "creator_dump_ratio", "bundle_share", "smart_money_share"}
_CATEGORICAL = {"trend_dir", "ema_signal", "breakout", "liq_trend_dir"}


def _known(feat: str, v: float) -> bool:
    """Exclude the unknown sentinels so missing data can't fabricate a separation."""
    if feat in _SAFETY and v < 0:
        return False
    if feat in _CATEGORICAL and v == -2.0:
        return False
    return True


def single_feature_auc(winner_vals: list[float], rug_vals: list[float]) -> float:
    """AUC = P(winner value > rug value) + 0.5*P(tie), the Mann-Whitney rank statistic. 0.5 = no
    separation; >0.5 = the value runs HIGHER in winners; <0.5 = HIGHER in rugs. O(nw*nr) — fine here."""
    nw, nr = len(winner_vals), len(rug_vals)
    if nw == 0 or nr == 0:
        return 0.5
    gt = 0.0
    for w in winner_vals:
        for r in rug_vals:
            if w > r:
                gt += 1.0
            elif w == r:
                gt += 0.5
    return gt / (nw * nr)


def separation_report(classified: list[dict], *, min_per_class: int = 8) -> dict:
    """Per-feature AUC of winner vs rug, strongest discrimination first. `strength` = |AUC-0.5|*2 in
    [0,1] (0 = noise, 1 = perfect). `ready` is False until both classes clear min_per_class."""
    winners = [r for r in classified if r.get("outcome") == "winner"]
    rugs = [r for r in classified if r.get("outcome") == "rug"]
    if len(winners) < min_per_class or len(rugs) < min_per_class:
        return {"ready": False, "winners": len(winners), "rugs": len(rugs), "features": []}
    feats = []
    for feat in _TEST_FEATURES:
        wv = [float(r["features"][feat]) for r in winners
              if feat in r["features"] and _known(feat, float(r["features"][feat]))]
        rv = [float(r["features"][feat]) for r in rugs
              if feat in r["features"] and _known(feat, float(r["features"][feat]))]
        if len(wv) < min_per_class or len(rv) < min_per_class:
            continue
        auc = single_feature_auc(wv, rv)
        feats.append({
            "feature": feat, "auc": auc, "strength": abs(auc - 0.5) * 2.0,
            "winner_median": statistics.median(wv), "rug_median": statistics.median(rv),
            "nw": len(wv), "nr": len(rv),
        })
    feats.sort(key=lambda x: x["strength"], reverse=True)
    return {"ready": True, "winners": len(winners), "rugs": len(rugs), "features": feats}


def verdict(strength: float, nw: int, nr: int, *, min_n: int = 15, sep_strength: float = 0.30) -> str:
    """A blunt, honest read. Needs enough samples AND a real effect to claim a separator; small n is
    always INCONCLUSIVE (don't trade on noise)."""
    if nw < min_n or nr < min_n:
        return "inconclusive (thin)"
    if strength >= sep_strength:
        return "SEPARATES"
    return "overlaps"


def main() -> None:
    ap = argparse.ArgumentParser(description="Rigorous winner-vs-rug single-feature AUC separation.")
    ap.add_argument("--db", default=None)
    ap.add_argument("--horizon", type=float, default=300.0)
    ap.add_argument("--tol", type=float, default=900.0)
    ap.add_argument("--min-per-class", type=int, default=8)
    args = ap.parse_args()
    s = get_settings()
    rows = _load_all_obs(args.db or s.db_path)
    rep = separation_report(build_dataset(rows, args.horizon, args.tol), min_per_class=args.min_per_class)
    if not rep["ready"]:
        print(f"Not ready: winners={rep['winners']} rugs={rep['rugs']} (need >= {args.min_per_class} each). "
              "Run the bot longer.")
        return
    print(f"\nwinner-vs-rug separation ({rep['winners']} winners / {rep['rugs']} rugs) "
          "| AUC 0.5 = noise; >0.5 higher-in-winners, <0.5 higher-in-rugs\n")
    print(f"  {'feature':22} {'AUC':>5} {'strength':>8} {'win_med':>11} {'rug_med':>11} {'n(w/r)':>9}   verdict")
    for f in rep["features"]:
        print(f"  {f['feature']:22} {f['auc']:>5.2f} {f['strength']:>8.2f} {f['winner_median']:>11.2f} "
              f"{f['rug_median']:>11.2f} {f['nw']:>4}/{f['nr']:<4}   {verdict(f['strength'], f['nw'], f['nr'])}")
    best = rep["features"][0] if rep["features"] else None
    if best and verdict(best["strength"], best["nw"], best["nr"]) == "SEPARATES":
        print(f"\n>> {best['feature']} separates (AUC {best['auc']:.2f}) — a real entry signal to weight. Confirm forward.")
    else:
        print("\n>> No feature separates with a real effect yet — the static-entry-edge finding holds; "
              "watch the velocity/liquidity-trend features as forward data accrues.")


if __name__ == "__main__":
    main()
