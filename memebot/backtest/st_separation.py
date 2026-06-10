"""WL15 — does the Solana Tracker (Axiom-style) risk data SEPARATE winners from rugs on OUR realized book?

The WL14 signals (st_risk_score / st_top10 / st_bundlers_pct / st_dev_pct / st_snipers_pct / st_insiders_pct)
log forward at entry, but that accrues slowly. This tool gives an EARLY read by BACKFILLING the current
risk data for the mints we already traded and pairing it with their realized PnL.

  python -m memebot.backtest.st_separation                # backfill (cached) + per-signal AUC
  python -m memebot.backtest.st_separation --limit 200    # cap how many mints to fetch this run
  python -m memebot.backtest.st_separation --recent-days 3 # also break out the least look-ahead-contaminated subset

HONEST CAVEAT — this is INDICATIVE, not confirmation. The risk data is fetched NOW, not at our entry time;
for a token that already rugged, today's concentration/dev/bundle reflect the POST-rug state, which
INFLATES separation (look-ahead). So treat it as a NEGATIVE SCREEN: if a signal doesn't separate even WITH
that look-ahead help (AUC ~0.5), it won't separate live either — kill it. If it does separate, it's a
HYPOTHESIS to confirm on the clean forward-logged st_* data (signal_separation), never a veto on its own.

Backfilled values are cached in the `st_backfill` table so re-runs don't re-spend API calls. Off without
SOLANATRACKER_ENABLED + a key (it reuses the same client as the live bot).
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import time

from ..config import get_settings
from ..feed.solanatracker import SolanaTrackerClient
from .signal_separation import separate

_SIGNALS = (
    ("score", "st_risk_score", True),       # 1-10 risk (higher = worse)
    ("top10", "st_top10", True),            # top-10 concentration % (higher = worse)
    ("bundlers_pct", "st_bundlers_pct", True),   # coordinated-wallet supply % (the #1 rug tell)
    ("dev_pct", "st_dev_pct", True),        # dev/creator holding % (higher = more dump risk)
    ("snipers_pct", "st_snipers_pct", True),
    ("insiders_pct", "st_insiders_pct", True),
)
_GLITCH_RET_MULT = 20.0


def _ensure_cache(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS st_backfill (
        mint TEXT PRIMARY KEY, ts REAL, rugged INTEGER, score REAL, top10 REAL,
        bundlers_pct REAL, dev_pct REAL, snipers_pct REAL, insiders_pct REAL)""")
    conn.commit()


def _book(conn: sqlite3.Connection) -> dict[str, tuple[float, float]]:
    """{mint: (net_pnl_sol, max_ret)} from trade_outcomes; pricing-glitch round-trips dropped."""
    rows = conn.execute("SELECT mint, pnl_sol, pnl_pct, ts FROM trade_outcomes").fetchall()
    agg: dict[str, list] = {}
    for mint, pnl_sol, pnl_pct, ts in rows:
        if pnl_pct is not None and (1.0 + float(pnl_pct)) > _GLITCH_RET_MULT:
            agg.pop(mint, None)
            continue
        a = agg.setdefault(mint, [0.0, 0.0])
        a[0] += float(pnl_sol or 0.0)
        a[1] = max(a[1], float(ts or 0.0))
    return {m: (v[0], v[1]) for m, v in agg.items()}


async def backfill(conn: sqlite3.Connection, mints: list[str], s) -> int:
    done = {r[0] for r in conn.execute("SELECT mint FROM st_backfill").fetchall()}
    todo = [m for m in mints if m not in done]
    if not todo:
        return 0
    n = 0
    async with SolanaTrackerClient(s.solanatracker_api_key, s.solanatracker_base_url) as cl:
        for m in todo:
            r = await cl.risk(m)
            if r is None:
                # cache a NULL row so we don't retry a 404 mint every run
                conn.execute("INSERT OR REPLACE INTO st_backfill(mint, ts) VALUES (?, ?)", (m, time.time()))
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO st_backfill(mint, ts, rugged, score, top10, bundlers_pct, "
                    "dev_pct, snipers_pct, insiders_pct) VALUES (?,?,?,?,?,?,?,?,?)",
                    (m, time.time(), int(bool(r.get("rugged"))), r.get("score"), r.get("top10"),
                     r.get("bundlers_pct"), r.get("dev_pct"), r.get("snipers_pct"), r.get("insiders_pct")))
                n += 1
            conn.commit()
            await asyncio.sleep(0.35)   # ~3 req/s rate-limit courtesy
    return n


def _report(conn: sqlite3.Connection, book: dict, label: str) -> None:
    cache = {r[0]: r for r in conn.execute(
        "SELECT mint, score, top10, bundlers_pct, dev_pct, snipers_pct, insiders_pct FROM st_backfill").fetchall()}
    idx = {"score": 1, "top10": 2, "bundlers_pct": 3, "dev_pct": 4, "snipers_pct": 5, "insiders_pct": 6}
    print(f"\n=== {label} ===")
    print(f"  {'signal':16} {'n':>4} {'win_med':>9} {'lose_med':>9} {'AUC':>6}  verdict")
    for key, disp, hiw in _SIGNALS:
        pairs = []
        for mint, (net, _ts) in book.items():
            row = cache.get(mint)
            if not row or row[idx[key]] is None:
                continue
            pairs.append((float(row[idx[key]]), net > 0))
        r = separate(pairs, hiw)
        if r["n"] < 8:
            print(f"  {disp:16} {r['n']:>4} {'—':>9} {'—':>9} {'—':>6}  too few")
            continue
        wm = "n/a" if r["win_med"] is None else f"{r['win_med']:.3g}"
        lm = "n/a" if r["lose_med"] is None else f"{r['lose_med']:.3g}"
        v = ("SEPARATES (confirm forward!)" if r["auc"] >= 0.6 else "noise -> kill")
        print(f"  {disp:16} {r['n']:>4} {wm:>9} {lm:>9} {r['auc']:>6.2f}  {v}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=250, help="max mints to FETCH this run (cost cap)")
    ap.add_argument("--recent-days", type=float, default=3.0, help="also break out this least-contaminated subset")
    args = ap.parse_args()
    s = get_settings()
    if not (s.solanatracker_enabled and s.solanatracker_api_key):
        print("SOLANATRACKER_ENABLED + SOLANATRACKER_API_KEY required.")
        return
    conn = sqlite3.connect(s.db_path)
    try:
        _ensure_cache(conn)
        book = _book(conn)
        # fetch most-recent mints first (least look-ahead contamination), capped by --limit
        mints = sorted(book, key=lambda m: book[m][1], reverse=True)[:args.limit]
        got = asyncio.run(backfill(conn, mints, s))
        print(f"backfilled {got} new mints (cached); book has {len(book)} traded mints.")
        print("CAVEAT: risk data is CURRENT, not entry-time -> look-ahead INFLATES separation. NEGATIVE "
              "screen only: ~0.5 even here = dead; >=0.6 = a hypothesis to confirm on forward st_* data.")
        _report(conn, book, "ALL traded mints (most look-ahead-contaminated)")
        cutoff = time.time() - args.recent_days * 86400.0
        recent = {m: v for m, v in book.items() if v[1] >= cutoff}
        if recent:
            _report(conn, recent, f"RECENT {args.recent_days:g}d subset (least contaminated, smaller n)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
