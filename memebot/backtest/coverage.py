"""Per-feature data-COVERAGE report (N6) — which FEATURE_NAMES columns actually carry signal in the
logged population, and which are DARK (constant / all-default) and would enter a GBM as dead weight.

The point: a feature can be wired end-to-end (in FEATURE_NAMES, narrated to the brain) yet be CONSTANT
in every dataset built today — e.g. the G3-tape columns (bundle_share / smart_money_share /
creator_dump_ratio) are all -1 on the no-stream population. If such a column enters a GBM, its
importance is ~0 but a reader can mistake the wiring for real "bundle detection." This report makes
the dark reality visible BEFORE training, so honesty is enforced at the data layer.

Offline, no network, no lightgbm — reads the logged `observations` price history.

  python -m memebot.backtest.coverage [--db PATH] [--indexed-only] [--min-coverage 0.01]
"""
from __future__ import annotations

import argparse

from ..config import get_settings
from ..filter.features import FEATURE_NAMES, dark_features, feature_coverage, row_from_feats
from .label import _load_all_obs


def main() -> None:
    ap = argparse.ArgumentParser(description="Per-feature coverage / dark-column report over logged observations.")
    ap.add_argument("--db", default=None)
    ap.add_argument("--indexed-only", action="store_true",
                    help="only the IN-DISTRIBUTION rows (liquidity_usd>0) the live GBM actually scores")
    ap.add_argument("--min-coverage", type=float, default=0.01,
                    help="flag a feature DARK below this fraction of known (non-default) values")
    args = ap.parse_args()

    s = get_settings()
    rows = _load_all_obs(args.db or s.db_path)
    if args.indexed_only:
        rows = [r for r in rows if float(r[3].get("liquidity_usd", 0.0)) > 0]
    if not rows:
        print("No logged observations yet — run the bot to collect data.")
        return

    matrix = [row_from_feats(f) for _mint, _ts, _price, f in rows]
    cov = feature_coverage(matrix)
    dark = set(dark_features(cov, min_coverage=args.min_coverage))

    print(f"\nfeature coverage over {len(matrix)} observation rows"
          f"{' (indexed-only)' if args.indexed_only else ''}\n")
    print(f"  {'feature':24} {'known':>7} {'coverage':>9}   flag")
    for name in FEATURE_NAMES:                 # load-bearing order
        c = cov[name]
        flag = "DARK (constant)" if c["constant"] else ("DARK" if name in dark else "")
        print(f"  {name:24} {c['known']:>7} {c['coverage'] * 100:>8.1f}%   {flag}")
    if dark:
        print(f"\n{len(dark)} DARK feature(s) — they add NOTHING to a GBM; their importance is not signal:")
        print(f"  {sorted(dark)}")
        print("Populating the G3-tape columns (bundle_share / smart_money_share / creator_dump_ratio) "
              "needs the METERED trade stream to accrue; do not train as if they were live.")
    else:
        print("\nNo dark features — every column carries some signal.")


if __name__ == "__main__":
    main()
