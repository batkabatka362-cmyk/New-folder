"""E2 — data-quality / training-readiness report.

Answers the only question that matters before training a model: do we actually
have enough COMPARABLE labeled data, with both classes? On the free tier most
pump.fun tokens die or age out fast, so few mints are tracked long enough for a
fixed forward horizon — this report makes that reality visible instead of
training a model on a handful of survivorship-biased rows.

    python -m memebot.backtest.dataquality
"""
from __future__ import annotations

import argparse
from collections import defaultdict

from ..config import get_settings
from .label import _load_all_obs, fixed_horizon_labels

HORIZONS = [("5m", 300.0), ("30m", 1800.0), ("2h", 7200.0)]


def report(rows) -> dict:
    """Pure: summarize tracking coverage + labelable counts/label-balance per horizon."""
    by_mint: dict[str, list] = defaultdict(list)
    for mint, ts, _price, _f in rows:
        by_mint[mint].append(ts)
    durations = sorted(max(v) - min(v) for v in by_mint.values()) if by_mint else []
    out = {
        "observations": len(rows),
        "distinct_mints": len(by_mint),
        "median_track_s": round(durations[len(durations) // 2], 0) if durations else 0.0,
        "max_track_s": round(durations[-1], 0) if durations else 0.0,
    }
    for name, hz in HORIZONS:
        out[f"tracked_ge_{name}"] = sum(1 for d in durations if d >= hz)
        labeled = fixed_horizon_labels(rows, hz, up=0.5, tol_s=max(60.0, hz * 0.3))
        pos = sum(lbl for _m, _f, lbl, _fwd in labeled)
        out[f"labelable_{name}"] = len(labeled)
        out[f"pos_rate_{name}"] = round(pos / len(labeled), 3) if labeled else 0.0

    # P2: the IN-DISTRIBUTION, death-aware view — exactly what the GBM (P4) will train on.
    # `indexed` = snapshots with a real DexScreener pool (liquidity>0); death-aware labeling
    # rescues witnessed collapses (label 0) that plain horizon labeling would silently drop.
    indexed = [r for r in rows if float(r[3].get("liquidity_usd", 0.0)) > 0]
    out["indexed_observations"] = len(indexed)
    out["indexed_mints"] = len({r[0] for r in indexed})
    da = fixed_horizon_labels(rows, 300.0, up=0.5, tol_s=120.0, death_drop=0.9, indexed_only=True)
    da_pos = sum(lbl for _m, _f, lbl, _fwd in da)
    out["labelable_5m_indexed"] = len(da)
    out["pos_5m_indexed"] = da_pos
    out["neg_5m_indexed"] = len(da) - da_pos
    out["pos_rate_5m_indexed"] = round(da_pos / len(da), 3) if da else 0.0
    return out


def trainable(r: dict, min_mints: int = 200) -> bool:
    """A GBM is worth training with a few hundred labelable mints AND both classes. We check
    the 5-MINUTE horizon: it's the scalp-relevant window AND the only one with data on the
    free tier (median token is tracked ~41s; almost none survive to a 30m/2h horizon).

    P2: prefer the IN-DISTRIBUTION, death-aware count when present — training on the unbiased
    population (not just survivors) is the whole point of P2/P4. Falls back to the legacy count
    for older DBs without observations."""
    n_indexed = r.get("labelable_5m_indexed", 0)
    if n_indexed:                                  # P2 in-distribution count available -> use it
        n, rate = n_indexed, r.get("pos_rate_5m_indexed", 0.0)
    else:                                          # older DB without observations -> legacy count
        n, rate = r.get("labelable_5m", 0), r.get("pos_rate_5m", 0.0)
    return n >= min_mints and 0.1 <= rate <= 0.9


def main() -> None:
    ap = argparse.ArgumentParser(description="Report training-data quality / readiness.")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()
    s = get_settings()
    rows = _load_all_obs(args.db or s.db_path)
    if not rows:
        print("No logged observations yet — run the bot to collect data.")
        return
    r = report(rows)
    print("== memebot data-quality ==")
    for k, v in r.items():
        print(f"  {k:18} {v}")
    print("\nA GBM needs a few hundred labelable mints with BOTH classes (~10-90% positive).")
    print("verdict:", "looks trainable (try `python -m memebot.backtest.train_gbm`)" if trainable(r)
          else "NOT enough comparable labeled data yet -- keep collecting (or enable G3 for dense "
               "real-time curve data). Most free-tier tokens die/age out before a fixed horizon.")
    print(f"  (in-distribution labelable_5m = {r.get('labelable_5m_indexed', 0)} mints; this is the "
          "honest count P4 trains on -- the legacy labelable_5m is inflated by curve-only rows.)")


if __name__ == "__main__":
    main()
