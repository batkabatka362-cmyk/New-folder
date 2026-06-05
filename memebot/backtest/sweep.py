"""Data-driven entry-threshold tuning: backtest a grid of entry thresholds over
the collected candidates and report which clears the most net PnL.

    python -m memebot.backtest.sweep [--lo 0.4 --hi 0.8 --step 0.05 --min-age 1800]

Fetches forward prices once, then re-runs the simulator at each threshold (no
re-fetch). Use it to pick ENTRY_THRESHOLD as data accrues. Caveat: only as good
as the data — thin/imbalanced free-tier data gives a weak signal.
"""
from __future__ import annotations

import argparse
import asyncio
import time

from ..config import get_settings
from ..feed.dexscreener import DexScreenerClient
from ..filter.gbm import GBMScorer
from ..signals.scoring import Scorer
from ..utils.money import round_trip_cost_pct
from .label import _load_rows
from .simulate import simulate


def threshold_grid(lo: float, hi: float, step: float) -> list[float]:
    n = int(round((hi - lo) / step)) + 1
    return [round(lo + k * step, 4) for k in range(max(1, n)) if lo + k * step <= hi + 1e-9]


async def _run(args) -> None:
    s = get_settings()
    rows = _load_rows(args.db or s.db_path, args.min_age, time.time())
    if not rows:
        print("No eligible candidates yet — run the bot to collect data.")
        return
    mints = sorted({m for m, _, _ in rows})
    async with DexScreenerClient(s.dexscreener_base_url) as dex:
        snaps, covered = await dex.snapshots(mints)
    rows = [r for r in rows if r[0] in covered]
    if not rows:
        print("All price fetches failed — try again.")
        return
    prices = {m: (snaps[m].price_usd if m in snaps else 0.0) for m in covered}
    scorer = Scorer(s, GBMScorer.load(s.gbm_model_path))

    print(f"sweep over {len(rows)} mints | scorer={scorer.kind} | "
          f"round-trip cost={round_trip_cost_pct(s.fees) * 100:.1f}%")
    print(f"{'thr':>5} {'entries':>8} {'win%':>6} {'avg_net%':>9} {'total_net%':>11}")
    best = None
    for thr in threshold_grid(args.lo, args.hi, args.step):
        r = simulate(rows, prices, scorer, s, thr)
        print(f"{thr:>5.2f} {r['entries']:>8} {r['win_rate']*100:>6.1f} "
              f"{r['avg_net_return']*100:>+9.1f} {r['total_net_return']*100:>+11.1f}")
        if r["entries"] >= args.min_entries and (best is None or r["total_net_return"] > best[1]):
            best = (thr, r["total_net_return"], r["win_rate"], r["entries"])
    if best:
        print(f"\nbest total-net threshold: {best[0]:.2f} "
              f"(total_net {best[1]*100:+.1f}%, win% {best[2]*100:.1f}, entries {best[3]})")
    else:
        print(f"\nno threshold produced >= {args.min_entries} entries — collect more data.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Sweep entry thresholds over logged candidates.")
    ap.add_argument("--db", default=None)
    ap.add_argument("--lo", type=float, default=0.40)
    ap.add_argument("--hi", type=float, default=0.80)
    ap.add_argument("--step", type=float, default=0.05)
    ap.add_argument("--min-age", type=float, default=1800.0)
    ap.add_argument("--min-entries", type=int, default=10)
    asyncio.run(_run(ap.parse_args()))


if __name__ == "__main__":
    main()
