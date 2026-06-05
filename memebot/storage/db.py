"""SQLite (WAL) logging of tokens, candidates, trades, and equity snapshots.

Writes are serialized behind a lock and pushed to a worker thread so they never
block the asyncio event loop. Parquet export (storage/archive.py) comes in
Phase 4.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time

from ..utils.logging import get_logger

log = get_logger("storage.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    mint         TEXT PRIMARY KEY,
    symbol       TEXT,
    name         TEXT,
    creator      TEXT,
    created_ts   REAL,
    first_seen_ts REAL,
    migrated     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS candidates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL,
    mint         TEXT,
    symbol       TEXT,
    mode         TEXT,
    rule_passed  INTEGER,
    score        REAL,
    price_usd    REAL,
    liquidity_usd REAL,
    vol_to_mcap_pct REAL,
    buy_sell_ratio REAL,
    reasons      TEXT,
    features     TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL,
    mint         TEXT,
    symbol       TEXT,
    side         TEXT,
    mode         TEXT,
    sol          REAL,
    tokens       REAL,
    price_sol    REAL,
    fee_sol      REAL,
    slippage_pct REAL,
    reason       TEXT
);

CREATE TABLE IF NOT EXISTS equity (
    ts             REAL,
    equity_sol     REAL,
    sol_balance    REAL,
    realized_pnl   REAL,
    open_positions INTEGER
);

CREATE TABLE IF NOT EXISTS lessons (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL,
    tag          TEXT,
    confidence   REAL,
    lesson       TEXT
);

CREATE TABLE IF NOT EXISTS verdicts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL,
    mint         TEXT,
    symbol       TEXT,
    action       TEXT,
    mode         TEXT,
    conviction   REAL,
    size_pct     REAL,
    tp_pct       REAL,
    sl_pct       REAL,
    source       TEXT,
    reasoning    TEXT,
    risk_flags   TEXT,
    primary_signal TEXT,
    recalled_lessons TEXT,
    score        REAL
);

CREATE TABLE IF NOT EXISTS calibration_suggestions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL,
    source       TEXT,            -- 'median' (suggest_thresholds) | 'auc' (separation_report)
    feature      TEXT,
    winner_val   REAL,
    rug_val      REAL,
    strength     REAL,            -- separation (median) or AUC strength
    nw           INTEGER,
    nr           INTEGER,
    verdict      TEXT
);

-- P2: the UNBIASED in-distribution log. `candidates` only ever recorded rule-gate
-- SURVIVORS, which biased every dataset toward winners. `observations` records EVERY
-- indexed snapshot (incl. gate-failers and tokens decaying toward death) so the GBM
-- (P4) can finally be trained on the population it actually scores live.
CREATE TABLE IF NOT EXISTS observations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL,
    mint          TEXT,
    symbol        TEXT,
    mode          TEXT,
    rule_passed   INTEGER,
    score         REAL,
    price_usd     REAL,
    liquidity_usd REAL,
    market_cap_usd REAL,
    vol_h1        REAL,
    features      TEXT
);

-- P2: the cleanest supervised signal — the ACTUAL entry features of a trade we took
-- mapped to its ACTUAL realized outcome. No survivorship, no horizon approximation.
CREATE TABLE IF NOT EXISTS trade_outcomes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL,
    mint          TEXT,
    symbol        TEXT,
    mode          TEXT,
    entry_ts      REAL,
    entry_score   REAL,
    pnl_sol       REAL,
    pnl_pct       REAL,
    reason        TEXT,
    hold_s        REAL,
    entry_features TEXT,
    verdict_source TEXT,
    verdict_conviction REAL
);

-- P8 REGRET dataset: a token we OBSERVED and did NOT buy whose forward price subsequently cleared
-- the "winner" bar. The counterfactual complement to trade_outcomes (what we DID) — so reflection
-- can ask "what filter wrongly rejected this?". One row per (mint, observation), deduped by obs_id.
-- detect_price is the price AT DETECTION (a single DexScreener point), NOT a true intra-window peak.
CREATE TABLE IF NOT EXISTS missed_winners (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL,        -- when this miss was DETECTED (replay time)
    obs_id        INTEGER,     -- the observations.id we judged (dedupe key)
    mint          TEXT,
    symbol        TEXT,
    mode          TEXT,
    entry_ts      REAL,        -- observation ts (when we saw + skipped it)
    entry_price   REAL,        -- observations.price_usd at skip time
    detect_price  REAL,        -- forward price at detection time (one point, not the window max)
    fwd_return    REAL,        -- detect_price/entry_price - 1.0, the realized regret-so-far
    horizon_s     REAL,        -- entry_ts -> detection age
    rule_passed   INTEGER,     -- did it clear the cheap market gate? (was it even rankable?)
    score         REAL,        -- entry score at skip time
    reflected     INTEGER DEFAULT 0,   -- 1 once a lesson has been queued (spam guard)
    entry_features TEXT        -- the observation's features JSON (the traits to learn)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_miss_obs ON missed_winners(obs_id);
CREATE INDEX IF NOT EXISTS idx_miss_mint ON missed_winners(mint);

-- P8: bounds the regret replay — each judged observation is re-priced AT MOST ONCE (the candidate
-- query excludes already-judged obs_ids). The replay drains oldest-in-window first (ORDER BY id ASC)
-- so rows are judged before they age out; if inflow exceeds the per-pass batch the surplus is judged
-- on later passes (anything older than max_age_s ages out unjudged — acceptable, off the hot path).
CREATE TABLE IF NOT EXISTS miss_judged (
    obs_id  INTEGER PRIMARY KEY,
    ts      REAL,
    verdict TEXT          -- 'winner' | 'flat'
);

-- P8 SMART-MONEY (copy-trade): per-wallet CROSS-TOKEN realized PnL track record, persisted so a
-- wallet's edge accrues across restarts (it only shows over many trades). Fed by the G3 tape.
CREATE TABLE IF NOT EXISTS wallets (
    wallet  TEXT PRIMARY KEY,
    pnl     REAL,
    closed  INTEGER,
    wins    INTEGER,
    ts      REAL
);

-- N12 FUNDER CLUSTERING: the resolved funding source of each creator wallet, persisted (a wallet's
-- funder is immutable), so the rotated-creator cluster graph accrues across restarts.
CREATE TABLE IF NOT EXISTS creator_funders (
    creator TEXT PRIMARY KEY,
    funder  TEXT,
    ts      REAL
);

-- READINESS MONITOR: a durable time-series of the go-live GATE metrics, so the trajectory (is the
-- CLEAN book trending toward net-positive?) is queryable across restarts.
CREATE TABLE IF NOT EXISTS readiness_log (
    ts            REAL,
    n             INTEGER,
    net           REAL,
    win_rate      REAL,
    profit_factor REAL,
    labelable     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_trades_mint ON trades(mint);
CREATE INDEX IF NOT EXISTS idx_cand_mint ON candidates(mint);
CREATE INDEX IF NOT EXISTS idx_obs_mint ON observations(mint);
CREATE INDEX IF NOT EXISTS idx_obs_ts ON observations(ts);  -- P9: time-windowed reads + safe retention prune
CREATE INDEX IF NOT EXISTS idx_outcomes_mint ON trade_outcomes(mint);
"""


class Storage:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def connect(self) -> None:
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()
        log.info("SQLite ready at %s", self.path)

    def _migrate(self) -> None:
        """P9-harden: generic forward-migration. executescript(_SCHEMA) uses CREATE TABLE IF NOT
        EXISTS, so on an OLD DB file any column added to a table's _SCHEMA definition after it
        shipped is NOT created — the next writer that names it raises 'no such column'. Instead of
        one hardcoded ALTER, ADD every column that the current _SCHEMA defines but the live table
        lacks. Expected columns are derived from _SCHEMA itself (built in a throwaway in-memory DB)
        so this never drifts from the schema. Names come from our own sqlite_master/PRAGMA (no
        injection); only '<col> <type>' is added (SQLite forbids ADD COLUMN with PK/UNIQUE/non-const
        default — and such columns are always in the original CREATE, never a later addition)."""
        assert self._conn is not None
        ref = sqlite3.connect(":memory:")
        try:
            ref.executescript(_SCHEMA)
            for (table,) in ref.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                want = {r[1]: r[2] for r in ref.execute(f"PRAGMA table_info({table})").fetchall()}
                have = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
                for col, coltype in want.items():
                    if col not in have:
                        try:
                            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
                            log.info("schema migrate: added %s.%s %s", table, col, coltype)
                        except sqlite3.OperationalError:
                            pass  # raced / already present
        finally:
            ref.close()

    def close(self) -> None:
        if self._conn is not None:
            with self._lock:
                self._conn.commit()
                self._conn.close()
                self._conn = None

    # ── sync writers (run in a worker thread) ──────────────────────────────────
    def _exec(self, sql: str, params: tuple) -> None:
        with self._lock:
            conn = self._conn
            if conn is None:
                # Connection closed during shutdown while this write was in flight.
                log.debug("storage closed; dropping late write")
                return
            conn.execute(sql, params)
            conn.commit()

    def _exec_many(self, sql: str, seq: list) -> None:
        with self._lock:
            conn = self._conn
            if conn is None:
                log.debug("storage closed; dropping late batch write")
                return
            conn.executemany(sql, seq)
            conn.commit()

    def _upsert_token(self, mint, symbol, name, creator, created_ts, first_seen_ts, migrated):
        self._exec(
            """INSERT INTO tokens(mint,symbol,name,creator,created_ts,first_seen_ts,migrated)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(mint) DO UPDATE SET
                 symbol=excluded.symbol, name=excluded.name, migrated=excluded.migrated""",
            (mint, symbol, name, creator, created_ts, first_seen_ts, int(migrated)),
        )

    # ── async API ───────────────────────────────────────────────────────────────
    async def upsert_token(self, st) -> None:
        await asyncio.to_thread(
            self._upsert_token, st.mint, st.symbol, st.name, st.creator,
            st.created_ts, st.first_seen_ts, st.migrated,
        )

    async def log_candidate(self, c, features_json: str = "{}") -> None:
        await asyncio.to_thread(
            self._exec,
            """INSERT INTO candidates(ts,mint,symbol,mode,rule_passed,score,price_usd,
                 liquidity_usd,vol_to_mcap_pct,buy_sell_ratio,reasons,features)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (c.ts, c.mint, c.symbol, c.mode, int(c.rule_passed), c.score, c.price_usd,
             c.liquidity_usd, c.vol_to_mcap_pct, c.buy_sell_ratio,
             json.dumps(c.rule_reasons), features_json),
        )

    async def log_observation(self, c, features_json: str = "{}") -> None:
        """P2: record one in-distribution observation per indexed snapshot, regardless of the
        rule gate. `rule_passed`/`score` are as known at the cheap MARKET-gate stage (before the
        post-survivor safety re-gate), which is exactly the population-wide view we want."""
        await asyncio.to_thread(
            self._exec,
            """INSERT INTO observations(ts,mint,symbol,mode,rule_passed,score,price_usd,
                 liquidity_usd,market_cap_usd,vol_h1,features)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (c.ts, c.mint, c.symbol, c.mode, int(c.rule_passed), c.score, c.price_usd,
             c.liquidity_usd, c.market_cap_usd, float(c.features.get("vol_h1", 0.0)), features_json),
        )

    async def log_trade_outcome(self, ct) -> None:
        """P2: persist a closed trade's entry features -> realized PnL (a perfect, if small,
        in-distribution supervised row for the trades we actually took)."""
        await asyncio.to_thread(
            self._exec,
            """INSERT INTO trade_outcomes(ts,mint,symbol,mode,entry_ts,entry_score,
                 pnl_sol,pnl_pct,reason,hold_s,entry_features,verdict_source,verdict_conviction)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ct.closed_ts, ct.mint, ct.symbol, ct.mode, ct.opened_ts, ct.entry_score,
             ct.pnl_sol, ct.pnl_pct, ct.reason, max(0.0, ct.closed_ts - ct.opened_ts),
             json.dumps(ct.entry_features or {}),
             getattr(ct, "verdict_source", ""), getattr(ct, "verdict_conviction", 0.0)),  # P10b #1
        )

    async def log_trade(self, *, mint, symbol, side, mode, sol, tokens, price_sol,
                        fee_sol, slippage_pct, reason="") -> None:
        await asyncio.to_thread(
            self._exec,
            """INSERT INTO trades(ts,mint,symbol,side,mode,sol,tokens,price_sol,
                 fee_sol,slippage_pct,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), mint, symbol, side, mode, sol, tokens, price_sol,
             fee_sol, slippage_pct, reason),
        )

    async def log_equity(self, *, equity_sol, sol_balance, realized_pnl, open_positions) -> None:
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO equity(ts,equity_sol,sol_balance,realized_pnl,open_positions) VALUES(?,?,?,?,?)",
            (time.time(), equity_sol, sol_balance, realized_pnl, open_positions),
        )

    async def log_verdict(self, *, mint, symbol, v, score: float = 0.0) -> None:
        # P10 #4/#10 + P10b #14: persist the brain's at-entry concerns, the cited decisive signal,
        # the lessons recalled into this decision, and the candidate score (the latter so a logged
        # SKIP carries its score for later separation analysis). Lists serialized to JSON TEXT.
        flags = json.dumps(list(getattr(v, "risk_flags", []) or []))
        primary = getattr(v, "primary_signal", "") or ""
        recalled = json.dumps(list(getattr(v, "recalled_lessons", []) or []))
        await asyncio.to_thread(
            self._exec,
            """INSERT INTO verdicts(ts,mint,symbol,action,mode,conviction,size_pct,
                 tp_pct,sl_pct,source,reasoning,risk_flags,primary_signal,recalled_lessons,score)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), mint, symbol, v.action, v.mode, v.conviction, v.size_pct,
             v.tp_pct, v.sl_pct, v.source, v.reasoning, flags, primary, recalled, score),
        )

    async def log_calibration_suggestions(self, rows: list[tuple]) -> None:
        """P10b CAL1: append-only audit log of each calibrate cycle's advisory winner-vs-rug findings
        (median suggestions + AUC separation), so calibration is queryable over time, not just transient
        log lines + one deduped lesson. rows = [(source, feature, winner_val, rug_val, strength, nw, nr, verdict)].
        Advisory audit only — NEVER auto-applied to any gate/config."""
        if not rows:
            return
        ts = time.time()
        await asyncio.to_thread(self._exec_many,
            """INSERT INTO calibration_suggestions(ts,source,feature,winner_val,rug_val,strength,nw,nr,verdict)
                 VALUES(?,?,?,?,?,?,?,?,?)""",
            [(ts, *r) for r in rows])

    async def log_lesson(self, *, lesson, tag, confidence) -> None:
        await asyncio.to_thread(
            self._exec,
            "INSERT INTO lessons(ts,tag,confidence,lesson) VALUES(?,?,?,?)",
            (time.time(), tag, confidence, lesson),
        )

    # ── P8 miss-learning (regret) ────────────────────────────────────────────────
    def _log_missed_winner(self, params: tuple) -> int | None:
        with self._lock:
            conn = self._conn
            if conn is None:
                return None
            cur = conn.execute(
                """INSERT OR IGNORE INTO missed_winners(ts,obs_id,mint,symbol,mode,entry_ts,
                     entry_price,detect_price,fwd_return,horizon_s,rule_passed,score,reflected,
                     entry_features) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
                params,
            )
            conn.commit()
            return cur.lastrowid if cur.rowcount else None   # None => obs_id already recorded (UNIQUE no-op)

    async def log_missed_winner(self, *, obs_id, mint, symbol, mode, entry_ts, entry_price,
                                detect_price, fwd_return, horizon_s, rule_passed, score,
                                features_json) -> int | None:
        """P8: record a skipped token whose forward return cleared the winner bar. Returns the new
        row id, or None if obs_id was already recorded (so the caller skips re-queuing a reflection)."""
        return await asyncio.to_thread(
            self._log_missed_winner,
            (time.time(), obs_id, mint, symbol, mode, entry_ts, entry_price, detect_price,
             fwd_return, horizon_s, int(rule_passed), score, features_json),
        )

    async def mark_miss_judged(self, obs_id: int, verdict: str) -> None:
        """P8: mark an observation as re-priced so the replay never re-fetches it (bounds cost)."""
        await asyncio.to_thread(
            self._exec,
            "INSERT OR IGNORE INTO miss_judged(obs_id,ts,verdict) VALUES(?,?,?)",
            (obs_id, time.time(), verdict),
        )

    def _fetch_miss_candidates(self, min_age_s: float, max_age_s: float, limit: int, now: float) -> list[dict]:
        lo, hi = now - max_age_s, now - min_age_s   # in-window: old enough to have moved, not stale
        with self._lock:
            conn = self._conn
            if conn is None:
                return []
            rows = conn.execute(
                """
                SELECT o.id, o.ts, o.mint, o.symbol, o.mode, o.rule_passed,
                       o.score, o.price_usd, o.features
                FROM observations o
                WHERE o.ts BETWEEN ? AND ?
                  AND o.price_usd > 0
                  AND o.liquidity_usd > 0
                  AND o.mint NOT IN (SELECT DISTINCT mint FROM trades WHERE side = 'buy')
                  AND o.id NOT IN (SELECT obs_id FROM miss_judged)
                ORDER BY o.id ASC
                LIMIT ?
                """,
                (lo, hi, limit),
            ).fetchall()
        cols = ("obs_id", "ts", "mint", "symbol", "mode", "rule_passed", "score", "price_usd", "features")
        return [dict(zip(cols, r)) for r in rows]

    async def miss_candidates(self, *, min_age_s: float, max_age_s: float, limit: int, now: float) -> list[dict]:
        """P8: observed-but-not-bought mints in the maturity window, excluding ones we bought or
        already judged. Each carries its entry price + features for the forward-return check."""
        return await asyncio.to_thread(self._fetch_miss_candidates, min_age_s, max_age_s, limit, now)

    # ── N1 forward-tracking (label completion) ───────────────────────────────────
    def _fetch_tracking_candidates(self, window_s: float, revisit_s: float, limit: int, now: float) -> list[dict]:
        win_lo = now - window_s            # first obs no older than this -> still maturing toward the horizon
        revisit_hi = now - revisit_s       # latest obs older than this -> due for a fresh forward point
        with self._lock:
            conn = self._conn
            if conn is None:
                return []
            rows = conn.execute(
                """
                SELECT o.mint, MAX(o.symbol) AS symbol, MAX(o.mode) AS mode, MAX(o.ts) AS last_ts
                FROM observations o
                WHERE o.price_usd > 0
                  AND o.liquidity_usd > 0
                  AND o.mint NOT IN (SELECT DISTINCT mint FROM trades WHERE side = 'buy')
                GROUP BY o.mint
                HAVING MIN(o.ts) >= ? AND MAX(o.ts) <= ?
                ORDER BY last_ts ASC
                LIMIT ?
                """,
                (win_lo, revisit_hi, limit),
            ).fetchall()
        return [dict(zip(("mint", "symbol", "mode", "last_ts"), r)) for r in rows]

    async def tracking_candidates(self, *, window_s: float, revisit_s: float, limit: int, now: float) -> list[dict]:
        """N1: INDEXED, not-bought mints whose FIRST observation is within window_s and whose LATEST
        observation is older than revisit_s — i.e., they left the live active set before the label
        horizon and need a fresh forward price point. Most-stale first (fair rotation under the batch
        cap). The revisit_s guard excludes still-active mints (eval logs them every cycle)."""
        return await asyncio.to_thread(self._fetch_tracking_candidates, window_s, revisit_s, limit, now)

    def _tracking_stats(self, horizon_s: float) -> dict:
        """N1 funnel: how many distinct INDEXED mints have a forward path long enough to label —
        the binding constraint on GBM/separation. `labelable` = span (last-first) >= horizon_s."""
        with self._lock:
            conn = self._conn
            if conn is None:
                return {}
            try:
                row = conn.execute(
                    """
                    SELECT COUNT(*) AS mints,
                           SUM(CASE WHEN n >= 2 THEN 1 ELSE 0 END) AS multi,
                           SUM(CASE WHEN span >= ? THEN 1 ELSE 0 END) AS labelable
                    FROM (
                        SELECT mint, COUNT(*) AS n, MAX(ts) - MIN(ts) AS span
                        FROM observations
                        WHERE price_usd > 0 AND liquidity_usd > 0
                        GROUP BY mint
                    )
                    """,
                    (horizon_s,),
                ).fetchone()
            except sqlite3.OperationalError:
                return {}
        if row is None:
            return {}
        return {"indexed_mints": row[0] or 0, "multi_obs": row[1] or 0, "labelable": row[2] or 0}

    async def tracking_stats(self, *, horizon_s: float) -> dict:
        return await asyncio.to_thread(self._tracking_stats, horizon_s)

    def _winners_traits(self, limit: int) -> dict:
        with self._lock:
            conn = self._conn
            if conn is None:
                return {}
            rows = conn.execute(
                "SELECT fwd_return, rule_passed, score, entry_features "
                "FROM missed_winners ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        if not rows:
            return {}
        import statistics
        from collections import Counter
        fwd = [r[0] for r in rows]
        gate_failed = sum(1 for r in rows if not r[1])   # missed BECAUSE the gate rejected it
        liq, vol, bsr = [], [], []
        skip_hist: Counter = Counter()                   # P10b #4: per-filter regret attribution
        for r in rows:
            try:
                f = json.loads(r[3] or "{}")
            except (ValueError, TypeError):
                continue
            if f.get("liquidity_usd"):
                liq.append(float(f["liquidity_usd"]))
            if f.get("vol_h1"):
                vol.append(float(f["vol_h1"]))
            if f.get("bsr_h1"):
                bsr.append(float(f["bsr_h1"]))
            skip_hist[f.get("skip_reason", "unknown")] += 1
        med = lambda xs: statistics.median(xs) if xs else 0.0   # noqa: E731
        return {
            "n": len(rows),
            "median_fwd_pct": med(fwd) * 100.0,
            "gate_failed_frac": gate_failed / len(rows),
            "median_liq_usd": med(liq),
            "median_vol_h1": med(vol),
            "median_bsr": med(bsr),
            "skip_reasons": dict(skip_hist),             # which filter rejected each missed winner
        }

    async def winners_traits(self, limit: int = 200) -> dict:
        """P8: compact traits profile of recent missed-winners for the report + meta-reflection.
        gate_failed_frac is load-bearing: HIGH => the hard gates wrongly reject winners (calibrate
        the gates); LOW => winners cleared the gate but ranking/entry/brain skipped them (decision
        layer too strict). Degrades to {} on no rows."""
        return await asyncio.to_thread(self._winners_traits, limit)

    def _miss_verdict_counts(self) -> dict:
        with self._lock:
            conn = self._conn
            if conn is None:
                return {}
            try:
                rows = conn.execute("SELECT verdict, COUNT(*) FROM miss_judged GROUP BY verdict").fetchall()
            except sqlite3.OperationalError:
                return {}
        return {str(v): int(n) for v, n in rows}

    async def miss_verdict_counts(self) -> dict:
        """P10b #3: the judged-miss outcome mix {winner/fade/flat/glitch: n}. Anchors regret to its
        true base rate — most 'misses' are fades/deaths, so a skip was usually correct. Read-only."""
        return await asyncio.to_thread(self._miss_verdict_counts)

    def _missed_winner_mints(self) -> set:
        with self._lock:
            conn = self._conn
            if conn is None:
                return set()
            rows = conn.execute("SELECT DISTINCT mint FROM missed_winners").fetchall()
        return {r[0] for r in rows}

    async def missed_winner_mints(self) -> set:
        """P8: the set of mints already recorded as missed winners — used to seed the replay's
        per-pass `seen` so a token observed (and re-judged) across multiple passes records ONE
        missed_winner row, keeping winners_traits a token-level (not per-observation) profile."""
        return await asyncio.to_thread(self._missed_winner_mints)

    def creator_launch_counts(self) -> dict:
        """{creator: launch_count} over all stored tokens — seeds CreatorHistory at startup
        (the serial-spam/rug tell). Sync; called once before the async loops start."""
        with self._lock:
            conn = self._conn
            if conn is None:
                return {}
            rows = conn.execute(
                "SELECT creator, COUNT(*) FROM tokens WHERE creator IS NOT NULL AND creator != '' "
                "GROUP BY creator"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def name_symbol_counts(self) -> dict:
        """{normalized 'name|symbol': distinct-mint-count} over all stored tokens — seeds the
        MetadataRegistry (the branding-duplication / scam-factory tell). Sync; called once at startup."""
        with self._lock:
            conn = self._conn
            if conn is None:
                return {}
            try:
                rows = conn.execute(
                    "SELECT LOWER(TRIM(name))||'|'||LOWER(TRIM(symbol)), COUNT(DISTINCT mint) "
                    "FROM tokens WHERE TRIM(COALESCE(name,'')) != '' GROUP BY 1"
                ).fetchall()
            except sqlite3.OperationalError:
                return {}
        return {r[0]: r[1] for r in rows}

    def save_wallets(self, rows) -> None:
        """P8 smart-money: upsert the per-wallet PnL track record (sync; called periodically)."""
        with self._lock:
            conn = self._conn
            if conn is None:
                return
            conn.executemany(
                "INSERT OR REPLACE INTO wallets(wallet,pnl,closed,wins,ts) VALUES(?,?,?,?,?)",
                [(w, pnl, closed, wins, time.time()) for (w, pnl, closed, wins) in rows],
            )
            conn.commit()

    def load_wallets(self) -> list:
        """P8 smart-money: (wallet, pnl, closed, wins) rows to seed the ledger at startup."""
        with self._lock:
            conn = self._conn
            if conn is None:
                return []
            try:
                return conn.execute("SELECT wallet,pnl,closed,wins FROM wallets").fetchall()
            except sqlite3.OperationalError:
                return []

    def save_readiness(self, book: dict, labelable: int) -> None:
        """Append one go-live readiness snapshot (sync; called from the hourly monitor loop)."""
        with self._lock:
            conn = self._conn
            if conn is None:
                return
            pf = book.get("profit_factor", 0.0)
            pf = 1e9 if pf == float("inf") else float(pf)        # SQLite can't store inf
            conn.execute(
                "INSERT INTO readiness_log(ts,n,net,win_rate,profit_factor,labelable) VALUES(?,?,?,?,?,?)",
                (time.time(), int(book.get("n", 0)), float(book.get("net", 0.0)),
                 float(book.get("win_rate", 0.0)), pf, int(labelable)),
            )
            conn.commit()

    def readiness_history(self, limit: int = 24) -> list:
        """Recent readiness snapshots (oldest-first) — the trajectory. [] on an old DB."""
        with self._lock:
            conn = self._conn
            if conn is None:
                return []
            try:
                rows = conn.execute(
                    "SELECT ts,n,net,win_rate,profit_factor,labelable FROM readiness_log "
                    "ORDER BY ts DESC, rowid DESC LIMIT ?", (max(0, int(limit)),)   # rowid breaks ts ties
                ).fetchall()
            except sqlite3.OperationalError:
                return []
            return list(reversed(rows))

    def save_creator_funders(self, rows) -> None:
        """N12: persist resolved (creator, funder) links (sync; called from the funder loop)."""
        with self._lock:
            conn = self._conn
            if conn is None or not rows:
                return
            conn.executemany(
                "INSERT OR REPLACE INTO creator_funders(creator,funder,ts) VALUES(?,?,?)",
                [(creator, funder, time.time()) for (creator, funder) in rows],
            )
            conn.commit()

    def load_creator_funders(self) -> list:
        """N12: (creator, funder) rows to seed the cluster graph at startup."""
        with self._lock:
            conn = self._conn
            if conn is None:
                return []
            try:
                return conn.execute("SELECT creator,funder FROM creator_funders").fetchall()
            except sqlite3.OperationalError:
                return []

    def unresolved_creators(self, limit: int) -> list:
        """N12: recent distinct creator wallets NOT yet funder-resolved (newest first, bounded). The
        funder loop resolves these off the hot path. [] on a DB without the tables."""
        limit = max(0, int(limit))      # a NEGATIVE limit would become SQLite LIMIT -1 = UNBOUNDED -> RPC storm
        if limit == 0:
            return []
        with self._lock:
            conn = self._conn
            if conn is None:
                return []
            try:
                rows = conn.execute(
                    """SELECT DISTINCT creator FROM tokens
                       WHERE creator IS NOT NULL AND creator != ''
                         AND creator NOT IN (SELECT creator FROM creator_funders)
                       ORDER BY rowid DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
            return [r[0] for r in rows]

    def mint_creators(self) -> dict:
        """{mint: creator} over all stored tokens — joins classified outcomes back to the creator
        for the P8 creator-reputation skill. Sync (called inside the calibrate worker thread)."""
        with self._lock:
            conn = self._conn
            if conn is None:
                return {}
            rows = conn.execute(
                "SELECT mint, creator FROM tokens WHERE creator IS NOT NULL AND creator != ''"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def _fetch_lessons_full(self, limit: int) -> list[dict]:
        with self._lock:
            conn = self._conn
            if conn is None:
                return []
            rows = conn.execute(
                "SELECT lesson, tag, confidence, ts FROM lessons ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{"lesson": r[0], "tag": r[1], "confidence": r[2] or 0.0, "ts": r[3] or 0.0}
                for r in reversed(rows)]

    async def lessons_full(self, limit: int = 1000) -> list[dict]:
        """Lessons with confidence + ts (newest-last) — for the curation/validation pass (de-dup +
        reinforcement scoring + decay). Wider limit than recent_lessons since it folds duplicates."""
        return await asyncio.to_thread(self._fetch_lessons_full, limit)
