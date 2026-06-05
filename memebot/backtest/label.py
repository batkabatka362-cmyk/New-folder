"""Build a labeled training dataset from logged candidates + forward outcomes.

For each logged candidate (with features), we fetch the token's CURRENT price
from DexScreener and compare it to the price logged at decision time:

    forward_return = current_price / logged_price - 1
    label = 1 if forward_return >= --up   else 0
    (a token DexScreener can no longer price = delisted/rugged = label 0)

Only candidates older than --min-age are used (so there is a real forward
window). Output is a CSV of FEATURE_NAMES + 'label', ready for train_gbm.py.

  python -m memebot.backtest.label --min-age 3600 --up 0.5 --out dataset.csv
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sqlite3
import time

from ..config import get_settings
from ..feed.dexscreener import DexScreenerClient
from ..filter.features import FEATURE_NAMES, row_from_feats


def _load_rows(path: str, min_age: float, now: float):
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            """SELECT mint, ts, price_usd, features FROM candidates
               WHERE features IS NOT NULL AND features != '{}' AND price_usd > 0
               ORDER BY ts ASC, id ASC"""        # explicit time order (don't rely on rowid)
        ).fetchall()
    finally:
        conn.close()
    # ONE independent sample per mint (the earliest observation) — the live loop
    # logs a row per cycle, so keeping all of them would leak correlated rows of
    # the same token across the train/val split and over-weight long-lived mints.
    out = []
    seen: set[str] = set()
    for mint, ts, price_usd, feats in rows:
        if now - (ts or 0) < min_age or mint in seen:
            continue
        try:
            f = json.loads(feats)
        except (TypeError, ValueError):
            continue
        seen.add(mint)
        out.append((mint, float(price_usd), f))
    return out


def _query_price_history(conn, table: str):
    """SELECT the (mint, ts, price_usd, features) price history from one table, or [] if the
    table doesn't exist (older DB without the P2 `observations` table)."""
    try:
        return conn.execute(
            f"""SELECT mint, ts, price_usd, features FROM {table}
                WHERE features IS NOT NULL AND features != '{{}}' AND price_usd > 0
                ORDER BY ts ASC, id ASC"""        # explicit time order (don't rely on rowid)
        ).fetchall()
    except sqlite3.OperationalError:
        return []                                 # table absent


def _load_all_obs(path: str, source: str = "observations"):
    """ALL logged observations (mint, ts, price_usd, features) with a positive price — the
    per-mint price history that fixed-horizon labeling reads forward returns from.

    source="observations" (P2 default): prefer the UNBIASED `observations` table (every indexed
        snapshot, incl. gate-failers and tokens decaying toward death); fall back to `candidates`
        for older DB files that predate the table — so old data still labels.
    source="candidates": read the survivor-only `candidates` table directly. Used by the E3
        cadence retrain to PRESERVE its historical population; P4 will deliberately switch it to
        the in-distribution `observations` set (with --indexed-only / --death-drop)."""
    conn = sqlite3.connect(path)
    try:
        if source == "candidates":
            rows = _query_price_history(conn, "candidates")
        else:
            rows = _query_price_history(conn, "observations")
            if not rows:
                rows = _query_price_history(conn, "candidates")
    finally:
        conn.close()
    out = []
    for mint, ts, price_usd, feats in rows:
        try:
            f = json.loads(feats)
        except (TypeError, ValueError):
            continue
        out.append((mint, float(ts or 0), float(price_usd), f))
    return out


def fixed_horizon_labels(rows, horizon_s: float, up: float, tol_s: float,
                         death_drop: float | None = None, indexed_only: bool = False):
    """Label each mint's EARLIEST observation by its forward return at ~+horizon_s, read
    from the SAME mint's later LOGGED prices (offline, no network). Comparable across mints
    (one fixed horizon), unlike a variable 'price now'. Returns (mint, features, label, fwd_ret).

    indexed_only (P2): only consider snapshots with liquidity_usd>0 — the IN-DISTRIBUTION
        population the live GBM actually scores (excludes pre-index curve-only rows).
    death_drop (P2): death-aware recall. A mint with no logged price near the horizon is
        normally DROPPED (we never fabricate an outcome). But if we OBSERVED its price collapse
        by >= death_drop (e.g. 0.9 = -90%) within the window, that is a real, logged loser —
        label it 0 instead of dropping it. Still never fabricated: only a witnessed collapse."""
    from collections import defaultdict
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
        best = None
        worst_ret = 0.0                    # most negative forward return seen within the window
        for ts, price, _ in obs:
            if ts <= t0:
                continue
            if ts <= target + tol_s:       # inside the forward window (+tol) -> track the trough
                worst_ret = min(worst_ret, price / p0 - 1.0)
            if abs(ts - target) <= tol_s and (best is None or abs(ts - target) < abs(best[0] - target)):
                best = (ts, price)
        if best is not None:
            fwd = best[1] / p0 - 1.0
            out.append((mint, feats, 1 if fwd >= up else 0, fwd))
        elif death_drop is not None and worst_ret <= -abs(death_drop):
            out.append((mint, feats, 0, worst_ret))   # witnessed collapse = honest negative
    return out


async def _build(args) -> None:
    s = get_settings()
    now = time.time()
    rows = _load_rows(args.db or s.db_path, args.min_age, now)
    if not rows:
        print("No eligible candidates (need features + price + age >= --min-age). "
              "Run the bot longer to collect data.")
        return

    mints = sorted({m for m, _, _ in rows})
    print(f"{len(rows)} samples across {len(mints)} mints; fetching current prices...")

    async with DexScreenerClient(s.dexscreener_base_url) as dex:
        snaps, covered = await dex.snapshots(mints)
    failed = [m for m in mints if m not in covered]
    if failed:
        print(f"dropping {len(failed)} mints (price fetch failed) — they are NOT labeled as rugs")
    rows = [r for r in rows if r[0] in covered]     # fetch failure != delisted; don't fabricate negatives
    if not rows:
        print("All price fetches failed — try again.")
        return
    # covered-but-absent = genuinely unpriced/delisted = 0 (a real rug -> label 0)
    prices = {m: (snaps[m].price_usd if m in snaps else 0.0) for m in covered}

    pos = 0
    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(FEATURE_NAMES + ["label"])
        for mint, logged_price, f in rows:
            cur = prices.get(mint, 0.0)
            fwd_ret = (cur / logged_price - 1.0) if logged_price > 0 else -1.0
            label = 1 if fwd_ret >= args.up else 0
            pos += label
            w.writerow(row_from_feats(f) + [label])

    print(f"wrote {len(rows)} rows to {args.out}  (positives: {pos}, {pos / len(rows) * 100:.1f}%)")
    if pos < 20 or len(rows) - pos < 20:
        print("WARNING: very few positives or negatives — collect more data before training.")


def _build_offline(args) -> None:
    """E1: fixed-horizon labeling from the LOGGED price history (offline, no network)."""
    s = get_settings()
    rows = _load_all_obs(args.db or s.db_path)
    if not rows:
        print("No logged observations with a price — run the bot to collect data.")
        return
    labeled = fixed_horizon_labels(rows, args.horizon, args.up, args.tol,
                                   death_drop=(args.death_drop or None), indexed_only=args.indexed_only)
    if not labeled:
        print(f"No mints have a logged price within ±{args.tol:.0f}s of +{args.horizon:.0f}s "
              "— collect longer-tracked data or widen --tol.")
        return
    pos = sum(lbl for _, _, lbl, _ in labeled)
    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(FEATURE_NAMES + ["label"])
        for _mint, f, lbl, _fwd in labeled:
            w.writerow(row_from_feats(f) + [lbl])
    print(f"wrote {len(labeled)} fixed-horizon (+{args.horizon:.0f}s) rows to {args.out}  "
          f"(positives: {pos}, {pos / len(labeled) * 100:.1f}%)")
    if pos < 20 or len(labeled) - pos < 20:
        print("WARNING: very few positives or negatives — collect more data before training.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Label logged candidates by forward return.")
    ap.add_argument("--db", default=None, help="SQLite path (default: config DB_PATH)")
    ap.add_argument("--min-age", type=float, default=3600.0, help="min seconds since logged (online mode)")
    ap.add_argument("--up", type=float, default=0.5, help="forward return for a positive label (0.5 = +50%%)")
    ap.add_argument("--horizon", type=float, default=0.0,
                    help="fixed forward horizon in seconds from the LOGGED history (0 = fetch current price online)")
    ap.add_argument("--tol", type=float, default=900.0, help="tolerance window (s) around the horizon price")
    ap.add_argument("--indexed-only", action="store_true",
                    help="P2: only label IN-DISTRIBUTION snapshots (liquidity_usd>0) — what the live GBM scores")
    ap.add_argument("--death-drop", type=float, default=0.0,
                    help="P2: death-aware recall — a witnessed >=this-fraction collapse in-window labels 0 (0=off, e.g. 0.9)")
    ap.add_argument("--out", default="dataset.csv")
    args = ap.parse_args()
    if args.horizon > 0:
        _build_offline(args)          # E1: comparable fixed-horizon labels, no network
    else:
        asyncio.run(_build(args))     # legacy: forward return vs current online price


if __name__ == "__main__":
    main()
