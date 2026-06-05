"""Offline stats over logged candidates (no live calls).

  python -m memebot.backtest.replay [db_path]

Reports how many candidates were logged, the rule-pass rate, and the score
distribution — a quick sanity check on what the live loop is actually seeing.
"""
from __future__ import annotations

import sqlite3
import sys

from ..config import get_settings


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else get_settings().db_path
    conn = sqlite3.connect(path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        if not n:
            print(f"{path}: no candidates logged yet — run the bot to collect data.")
            return
        with_feats = conn.execute(
            "SELECT COUNT(*) FROM candidates WHERE features IS NOT NULL AND features != '{}'"
        ).fetchone()[0]
        buckets = conn.execute(
            """SELECT CAST(score*10 AS INT) AS b, COUNT(*) FROM candidates
               GROUP BY b ORDER BY b"""
        ).fetchall()
        trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    finally:
        conn.close()

    print(f"db: {path}")
    print(f"candidates logged : {n}  (gate-passed survivors only — rejects aren't logged)")
    print(f"with features     : {with_feats}  (usable for training)")
    print(f"trades logged     : {trades}")
    print("score distribution (bucket = score*10):")
    for b, cnt in buckets:
        lo = (b or 0) / 10
        print(f"  {lo:.1f}-{lo + 0.1:.1f}: {'#' * min(50, cnt)} {cnt}")


if __name__ == "__main__":
    main()
