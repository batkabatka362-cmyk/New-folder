"""Paper-vs-live FILL-DIVERGENCE harness (N11) — the missing G1 prerequisite.

The ONE number that gates real money: how much does the PAPER backend (which fills instantly at the
decision-time price) OVERSTATE a LIVE fill? Live, there is latency between the decision and the
on-chain fill (detect -> build tx -> submit -> confirm), during which the price moves — and on
memecoins it tends to move AGAINST you (adverse selection). This harness estimates that by re-reading
each traded mint's LOGGED observation price series and comparing the decision-time price to the price
`polls` observations LATER — the price a delayed live fill would actually face.

A POSITIVE buy divergence (price rose after we decided to buy) and a NEGATIVE sell divergence (price
fell after we decided to sell) are ADVERSE: paper books a better fill than live would get. Summed over
both legs, that is the EXTRA round-trip cost latency imposes, which we compare to the ~9.5% modeled
round-trip cushion and (via pnl_reconstruct) the CLEAN realized book.

HONEST CAVEAT: a ~3s-poll "price N polls later" is a WEAK LOWER BOUND on live adverse selection — real
MEV / sandwiching / mempool front-running is strictly worse. So a benign result proves nothing; but a
BAD result (latency already eats the cushion) is a cheap, hard kill-argument against free-data G1.

  python -m memebot.backtest.fill_divergence [--db PATH] [--polls 2]
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
from collections import defaultdict

from ..config import get_settings
from ..utils.money import round_trip_cost_pct


def _load_trades(path: str) -> list:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(
            "SELECT ts, mint, side, price_sol FROM trades WHERE price_sol > 0 ORDER BY ts ASC"
        ).fetchall()
    finally:
        conn.close()


def _load_series(path: str) -> dict:
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT mint, ts, price_usd FROM observations WHERE price_usd > 0 ORDER BY mint, ts ASC"
        ).fetchall()
    finally:
        conn.close()
    by: dict[str, list] = defaultdict(list)
    for mint, ts, price in rows:
        by[mint].append((float(ts or 0.0), float(price)))
    return by


def _idx_at_or_before(series: list, ts: float) -> int | None:
    """Index of the last observation at or before ts (the price the decision saw), or None if ts
    precedes the whole series. Series is sorted oldest-first."""
    lo = None
    for i, (t, _p) in enumerate(series):
        if t <= ts:
            lo = i
        else:
            break
    return lo


def fill_divergence(trades: list, series_by_mint: dict, polls: int = 2) -> dict:
    """Per-trade signed divergence between the decision-time price and the price `polls` observations
    later. trades: iterable of (ts, mint, side, price_sol). Returns the raw per-side lists + match count.
    NOTE: this is a per-LEG aggregate (all buys vs all sells), NOT a per-round-trip pairing — so
    `extra_round_trip_cost` downstream is the mean adverse drag across legs, not a matched buy/sell stat."""
    buys: list[float] = []
    sells: list[float] = []
    for ts, mint, side, _price in trades:
        s = series_by_mint.get(mint)
        if not s:
            continue
        i = _idx_at_or_before(s, float(ts or 0.0))
        if i is None:
            continue
        j = i + max(1, polls)
        if j >= len(s):
            continue                       # no logged price far enough ahead -> can't estimate latency
        p0, p1 = s[i][1], s[j][1]
        if p0 <= 0 or p1 <= 0:
            continue
        (buys if side == "buy" else sells).append(p1 / p0 - 1.0)
    return {"buys": buys, "sells": sells, "matched": len(buys) + len(sells), "polls": max(1, polls)}


def _stats(xs: list) -> dict:
    if not xs:
        return {"n": 0, "mean": 0.0, "median": 0.0}
    return {"n": len(xs), "mean": sum(xs) / len(xs), "median": statistics.median(xs)}


def summarize(div: dict, rtc: float) -> dict:
    """The honest G1 number: the EXTRA round-trip cost latency imposes + how much of the modeled
    round-trip cushion it eats. buy_adverse = +mean(buy div) (price rose after buy decision = pay more);
    sell_adverse = -mean(sell div) (price fell after sell decision = get less)."""
    b, s = _stats(div["buys"]), _stats(div["sells"])
    buy_adverse = b["mean"]
    sell_adverse = -s["mean"]
    extra_rt = buy_adverse + sell_adverse
    return {
        "buy": b, "sell": s,
        "buy_adverse": buy_adverse, "sell_adverse": sell_adverse,
        "extra_round_trip_cost": extra_rt,
        "round_trip_cushion": rtc,
        "eats_cushion_frac": (extra_rt / rtc if rtc > 0 else 0.0),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Estimate paper-vs-live fill divergence from logged prices.")
    ap.add_argument("--db", default=None)
    ap.add_argument("--polls", type=int, default=2, help="observations after the trade to use as the live-fill price")
    args = ap.parse_args()
    s = get_settings()
    trades = _load_trades(args.db or s.db_path)
    if not trades:
        print("No trades logged yet — run the paper bot to collect fills.")
        return
    series = _load_series(args.db or s.db_path)
    rtc = round_trip_cost_pct(s.fees)
    div = fill_divergence(trades, series, polls=args.polls)
    if div["matched"] == 0:
        print("No trades had a logged price far enough ahead to estimate latency — collect longer-tracked data.")
        return
    r = summarize(div, rtc)
    print(f"\nfill divergence over {div['matched']} matched fills (price {div['polls']} polls after decision)\n")
    print(f"  buys  n={r['buy']['n']:>4}  mean {r['buy']['mean'] * 100:+.2f}%  median {r['buy']['median'] * 100:+.2f}%"
          f"   (adverse = price rose after buy)")
    print(f"  sells n={r['sell']['n']:>4}  mean {r['sell']['mean'] * 100:+.2f}%  median {r['sell']['median'] * 100:+.2f}%"
          f"   (adverse = price fell after sell)")
    print(f"\n  implied EXTRA round-trip cost from latency: {r['extra_round_trip_cost'] * 100:+.2f}%"
          f"  (buy {r['buy_adverse'] * 100:+.2f}% + sell {r['sell_adverse'] * 100:+.2f}%)")
    print(f"  modeled round-trip cushion: {rtc * 100:.1f}%  ->  latency eats {r['eats_cushion_frac'] * 100:.0f}% of it")
    print("\nThis is a WEAK LOWER BOUND (real MEV/sandwich/mempool is worse). A benign number proves "
          "nothing; a number that eats the cushion is a hard argument AGAINST free-data G1. "
          "Compare to `python -m memebot.backtest.pnl_reconstruct` (the CLEAN realized book).")


if __name__ == "__main__":
    main()
