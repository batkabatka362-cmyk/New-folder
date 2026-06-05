"""AI5 — winner-characteristics study.

On the fixed-horizon labeled data, compare the feature distributions of WINNERS
(forward return >= --up within the horizon) vs LOSERS, so we can see which early
signals actually separate the coins that run from the ones that bleed — and feed
that back into the setup classifier. Pure `winner_characteristics()` is unit-tested;
the CLI prints a ranked table.

    python -m memebot.backtest.winners [--horizon 300 --up 0.5 --tol 90]
"""
from __future__ import annotations

import argparse
import statistics

from ..config import get_settings
from ..filter.features import FEATURE_NAMES
from .label import _load_all_obs, fixed_horizon_labels


def winner_characteristics(labeled: list) -> tuple[dict, int, int]:
    """labeled = [(mint, features, label, fwd)]. Returns {feature: {winner_median,
    loser_median, lift}}, n_winners, n_losers. `lift` = winner_median / loser_median
    (how much higher the signal runs in winners; >1 favors winners)."""
    wins = [f for _m, f, lbl, _fwd in labeled if lbl == 1]
    losers = [f for _m, f, lbl, _fwd in labeled if lbl == 0]
    out = {}
    for name in FEATURE_NAMES:
        wv = [float(f.get(name, 0.0)) for f in wins]
        lv = [float(f.get(name, 0.0)) for f in losers]
        wm = statistics.median(wv) if wv else 0.0
        lm = statistics.median(lv) if lv else 0.0
        if lm != 0:
            lift = wm / lm
        elif wm != 0:
            lift = float("inf")
        else:
            lift = 1.0
        out[name] = {"winner_median": wm, "loser_median": lm, "lift": lift}
    return out, len(wins), len(losers)


def _rank_key(item) -> float:
    lift = item[1]["lift"]
    if lift in (0.0, float("inf")):
        return 1e9
    return abs(lift - 1.0)            # distance from "no difference"


def main() -> None:
    ap = argparse.ArgumentParser(description="Study which early signals separate winners from losers.")
    ap.add_argument("--db", default=None)
    ap.add_argument("--horizon", type=float, default=300.0)
    ap.add_argument("--up", type=float, default=0.5)
    ap.add_argument("--tol", type=float, default=90.0)
    args = ap.parse_args()
    s = get_settings()
    rows = _load_all_obs(args.db or s.db_path)
    labeled = fixed_horizon_labels(rows, args.horizon, args.up, args.tol)
    if len(labeled) < 20:
        print(f"only {len(labeled)} labeled mints at +{args.horizon:.0f}s — collect more data first.")
        return
    chars, nw, nl = winner_characteristics(labeled)
    print(f"== winner-characteristics @ +{args.horizon:.0f}s (>= +{args.up * 100:.0f}%) ==")
    print(f"winners={nw}  losers={nl}  (base rate {nw / (nw + nl) * 100:.0f}%)\n")
    print(f"{'feature':>24} {'winner_med':>12} {'loser_med':>12} {'lift':>7}")
    for name, d in sorted(chars.items(), key=_rank_key, reverse=True):
        lift = d["lift"]
        ls = "inf" if lift == float("inf") else f"{lift:.2f}"
        print(f"{name:>24} {d['winner_median']:>12.4g} {d['loser_median']:>12.4g} {ls:>7}")
    print("\nRead: features where winner_med >> loser_med (lift>>1) are the early tells of a runner; "
          "feed the strongest into the setup classifier (AI7/rules) and the scorer.")


if __name__ == "__main__":
    main()
