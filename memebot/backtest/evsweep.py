"""D4 — EV-margin entry sweep: at a FIXED entry threshold, sweep the minimum expected-return
buffer (`min_expected_return`) and report entries / win% / net per buffer, so we can pick a
defensive edge cushion that filters out marginal-EV entries without starving the book.

    python -m memebot.backtest.evsweep [--lo 0.0 --hi 0.10 --step 0.02 --threshold X --min-age 1800]

Mirrors sweep.py (fetch forward prices once, then re-run the simulator per buffer). The EV gate
uses the score as a conviction proxy — the same thing the live rule-fallback does — so the sweep
reflects what the bot would actually admit. Caveat: only as good as the data; thin/imbalanced
free-tier data gives a weak signal. Re-run as data accrues.
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
from .sweep import threshold_grid as ev_grid   # same grid math (lo..hi step), reused for the EV axis


def pick_best_ev(results, min_entries: int):
    """Pure: results = list of (ev_buffer, sim_result_dict). Return the (ev, avg_net, win_rate,
    entries) with the BEST average net return among buffers that admitted >= min_entries (so a
    buffer that crowned itself on one lucky trade is ignored), or None if none qualify."""
    best = None
    for ev, r in results:
        if r["entries"] >= min_entries and (best is None or r["avg_net_return"] > best[1]):
            best = (ev, r["avg_net_return"], r["win_rate"], r["entries"])
    return best


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
    thr = args.threshold if args.threshold is not None else (
        s.gbm_entry_threshold if scorer.kind == "gbm" else s.entry_threshold)

    print(f"EV sweep over {len(rows)} mints | scorer={scorer.kind} | entry>={thr} | "
          f"round-trip cost={round_trip_cost_pct(s.fees) * 100:.1f}%")
    print(f"{'min_ev':>7} {'entries':>8} {'ev_skip':>8} {'win%':>6} {'avg_net%':>9} {'total_net%':>11}")
    sweep = []
    for ev in ev_grid(args.lo, args.hi, args.step):
        r = simulate(rows, prices, scorer, s, thr, min_expected_return=ev)
        print(f"{ev:>7.3f} {r['entries']:>8} {r['ev_skipped']:>8} {r['win_rate']*100:>6.1f} "
              f"{r['avg_net_return']*100:>+9.1f} {r['total_net_return']*100:>+11.1f}")
        sweep.append((ev, r))
    best = pick_best_ev(sweep, args.min_entries)
    if best:
        print(f"\nbest avg-net EV buffer: {best[0]:.3f} "
              f"(avg_net {best[1]*100:+.1f}%, win% {best[2]*100:.1f}, entries {best[3]})")
    else:
        print(f"\nno EV buffer produced >= {args.min_entries} entries — collect more data or lower --lo.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Sweep the min expected-return buffer at a fixed entry threshold.")
    ap.add_argument("--db", default=None)
    ap.add_argument("--lo", type=float, default=0.0)
    ap.add_argument("--hi", type=float, default=0.10)
    ap.add_argument("--step", type=float, default=0.02)
    ap.add_argument("--threshold", type=float, default=None, help="fixed entry threshold (default: config)")
    ap.add_argument("--min-age", type=float, default=1800.0)
    ap.add_argument("--min-entries", type=int, default=10)
    asyncio.run(_run(ap.parse_args()))


if __name__ == "__main__":
    main()
