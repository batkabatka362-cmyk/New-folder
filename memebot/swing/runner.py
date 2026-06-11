"""The swing forward-test runner — polls the universe, runs the mean-reversion engine on PAPER, persists.

This is the live (paper) proof of the WL17 edge: every `swing_scan_interval_s` it pulls each universe
token's OHLCV, steps the engine (enter on a deep dip, exit on reversion), marks the book to market, and
snapshots state to swing_state.json so the forward test survives restarts. It logs every fill + a periodic
equity line so the realized book can be compared to the backtest. PAPER-ONLY — it never places a real
order; there is no live path here at all.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import AsyncExitStack

from ..backtest.swing_discover import run_discovery
from ..backtest.swing_lab import load_bars
from ..feed.geckoterminal import GeckoTerminalClient
from ..feed.solanatracker import SolanaTrackerClient
from ..utils.logging import get_logger
from .engine import SwingClosed, SwingEngine, SwingParams, SwingPosition
from .promote import decide_promotion
from .universe import liquid_universe

log = get_logger("swing.runner")
_STATE = "swing_state.json"
_LIVE_PARAMS = "swing_live_params.json"   # WL25: a promoted config overrides the .env window/dip_k here
_REPLAY_CAP = 50          # max closed bars to replay after a poll gap (bounds a long-outage catch-up)


def _reconstruct(cls, d: dict):
    """Build a dataclass from a dict keeping only its CURRENT fields (schema-drift safe: unknown keys are
    dropped; a missing REQUIRED field raises TypeError, which _load catches to start fresh)."""
    import dataclasses
    valid = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in valid})


def _load_live_params() -> dict:
    """A self-improvement PROMOTION (swing_live_params.json) overrides the .env window/dip_k. Empty/absent
    -> use the config defaults. Delete the file to revert to the .env config."""
    if not os.path.exists(_LIVE_PARAMS):
        return {}
    try:
        d = json.load(open(_LIVE_PARAMS))
        return {"window": int(d["window"]), "dip_k": float(d["dip_k"])} if "window" in d and "dip_k" in d else {}
    except (ValueError, OSError, KeyError, TypeError):
        return {}


class SwingRunner:
    def __init__(self, settings) -> None:
        self.s = settings
        live = _load_live_params()                 # a promoted config (if any) overrides the .env strategy params
        if live:
            log.info("swing: using PROMOTED params from %s (window=%d dip_k=%.2f) — delete the file to revert",
                     _LIVE_PARAMS, live["window"], live["dip_k"])
        self.p = SwingParams(
            window=live.get("window", settings.swing_window), dip_k=live.get("dip_k", settings.swing_dip_k),
            exit_k=settings.swing_exit_k,
            fee_pct=settings.swing_fee_pct, size_sol=settings.swing_size_sol,
            max_positions=settings.swing_max_positions, max_hold_bars=settings.swing_max_hold_bars,
            stop_k=settings.swing_stop_k, slippage_bps=settings.swing_slippage_bps,
            regime_window=settings.swing_regime_window, regime_tol=settings.swing_regime_tol,
            max_total_exposure_sol=settings.swing_max_total_exposure_sol,
            rolling_loss_halt_sol=settings.swing_rolling_loss_halt_sol,
            loss_halt_lookback=settings.swing_loss_halt_lookback,
        )
        self.engine = SwingEngine(self.p, initial_sol=settings.swing_initial_sol)
        self.universe: dict[str, str] = {}
        self._last_bar: dict[str, float] = {}      # mint -> last bar time stepped (per-candle bookkeeping)
        self._last_discover = 0.0                  # last autonomous strategy-discovery run (epoch s)
        self._promote_streak: dict = {}            # WL25: challenger -> consecutive winning discovery runs

    # ---- state persistence (so the forward test survives restarts) -----------------------------------
    def _save(self) -> None:
        try:
            state = {
                "cash": self.engine.cash,
                "initial": self.engine.initial,            # restore the equity-log baseline on resume
                "positions": [vars(p) for p in self.engine.positions.values()],
                "closed": [vars(c) for c in self.engine.closed],
                "last_bar": self._last_bar,
                "last_discover": self._last_discover,       # don't re-fire discovery on every restart
                "promote_streak": self._promote_streak,     # WL25 self-improvement persistence
            }
            # ATOMIC write: dump to a temp file then os.replace, so a crash mid-write can never leave a
            # truncated swing_state.json that the next _load would choke on.
            tmp = _STATE + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(state, fh)
            os.replace(tmp, _STATE)
        except OSError as e:
            log.debug("swing state save failed: %s", type(e).__name__)

    def _load(self) -> None:
        if not os.path.exists(_STATE):
            return
        try:
            st = json.load(open(_STATE))
        except (ValueError, OSError):
            log.warning("swing state file unreadable/corrupt — starting fresh")
            return
        try:
            # SCHEMA-DRIFT SAFE: filter each dict to the dataclass's CURRENT fields (a code change that
            # adds/removes a field must NOT crash-loop the supervisor via a TypeError on **unpacking).
            self.engine.cash = float(st.get("cash", self.engine.cash))
            self.engine.initial = float(st.get("initial", self.engine.initial))
            self.engine.positions = {p["mint"]: _reconstruct(SwingPosition, p) for p in st.get("positions", [])}
            self.engine.closed = [_reconstruct(SwingClosed, c) for c in st.get("closed", [])]
            self._last_bar = {k: float(v) for k, v in st.get("last_bar", {}).items()}
            self._last_discover = float(st.get("last_discover", 0.0))
            self._promote_streak = dict(st.get("promote_streak", {}))
        except (TypeError, KeyError, ValueError) as e:
            log.warning("swing state schema drift on load (%s) — starting fresh", type(e).__name__)
            self.engine.cash = self.engine.initial
            self.engine.positions, self.engine.closed, self._last_bar, self._promote_streak = {}, [], {}, {}
            return
        log.info("swing: resumed state (cash=%.3f, open=%d, closed=%d)",
                 self.engine.cash, len(self.engine.positions), len(self.engine.closed))

    async def _scan_once(self, client) -> None:
        price_map = {}
        # scan the universe PLUS any HELD position whose token has dropped out of it — a held position must
        # keep being managed (stop/time-stop/exit) even after the liquidity filter drops its token, or it
        # would be stranded with no exit.
        items = list(self.universe.items())
        u_mints = set(self.universe.values())
        items += [(p.symbol, m) for m, p in self.engine.positions.items() if m not in u_mints]
        for sym, mint in items:
            bars = await client.chart(mint, self.s.swing_interval)
            if len(bars) < 2:                                  # need a closed bar + the forming bar
                continue
            closes_all = [float(b["close"]) for b in bars]
            ts = float(bars[-1].get("time", 0.0))
            price_map[mint] = closes_all[-1]                  # mark-to-market every poll (forming bar ok for equity)
            last = self._last_bar.get(mint, 0.0)
            latest_closed_ts = float(bars[-2].get("time", ts))
            if last <= 0.0:
                # First sighting: set the baseline to the latest CLOSED bar, do NOT act on history (no
                # back-entering on an old dip). We only act on bars that close after we start watching.
                self._last_bar[mint] = latest_closed_ts
                continue
            # Step ONCE per CLOSED candle, on the CLOSED series (drop the still-forming last bar) so decisions
            # never repaint intra-bar and bars_held counts BARS not polls. REPLAY every closed bar newer than
            # the last one we stepped, oldest-first + capped — a multi-hour outage closes several bars at once,
            # and collapsing them to one step would lose closed-bar decisions + under-count bars_held (a held
            # position still gets its stop/exit on any data; entries no-op below the SMA window inside step()).
            new_idx = [k for k in range(len(bars) - 1) if float(bars[k].get("time", 0.0)) > last][-_REPLAY_CAP:]
            for k in new_idx:
                res = self.engine.step(mint, sym, closes_all[:k + 1], float(bars[k].get("time", ts)))
                self._last_bar[mint] = float(bars[k].get("time", ts))
                if res:
                    kind, obj = res
                    if kind == "enter":
                        log.info("swing ENTER %s @ %.6g (dip %.0f%% below SMA, size %.2f SOL)",
                                 sym, obj.entry_price, obj.dip_at_entry * 100, obj.sol_in)
                    else:
                        log.info("swing EXIT  %s @ %.6g -> PnL %+.3f SOL (%+.1f%%) [%s]",
                                 sym, obj.exit_price, obj.pnl_sol, obj.pnl_pct * 100, obj.reason)
            await asyncio.sleep(self.s.swing_poll_sleep_s)   # space calls under the OHLCV source's rate limit
        eq = self.engine.equity(price_map)
        st = self.engine.stats()
        log.info("swing book | equity %.3f SOL (start %.1f) | open %d | closed %d win%% %.0f pf %.2f realized %+.3f",
                 eq, self.engine.initial, st["open"], st["closed"], st["win_rate"] * 100,
                 st["profit_factor"], st["realized_sol"])
        self._save()

    async def _discover(self) -> None:
        """WL19 autonomous self-research: re-run the walk-forward strategy discovery, record the
        OOS-validated strategies to swing_strategies.json, and LOG the best vs the live config. ADVISORY
        only — it never auto-changes live params (same safe-gate discipline as the GBM retrain). Heavy +
        off the hot path; a failure must never kill the forward test."""
        data = await load_bars(self.s, self.s.swing_interval)
        data = {k: v for k, v in data.items() if v and len(v) > 100}
        if not data:
            return
        rows = await asyncio.to_thread(run_discovery, data, max(self.s.swing_fee_pct, 0.02), 2.0, 0.7,
                                       self.s.swing_slippage_bps)
        passed = [(g, name, r) for g, name, r, p in rows if p]
        try:
            json.dump({"validated": [{"name": n, "test_gmean": round(g, 3),
                                      "train_gmean": round(r["train_gmean"], 3),
                                      "oos_beat": f"{r['test_beat']}/{r['n']}"} for g, n, r in passed]},
                      open("swing_strategies.json", "w"))
        except OSError:
            pass
        if passed:
            g, name, _ = passed[0]
            log.info("swing discovery | %d/%d strategies OOS-validated | best: %s (test_gmean %.2f) "
                     "| live: SMA%d dip%.0f%%",
                     len(passed), len(rows), name, g, self.s.swing_window, self.s.swing_dip_k * 100)
        else:
            log.info("swing discovery | 0/%d strategies passed the walk-forward gate this run", len(rows))
        # WL25 SELF-IMPROVEMENT: promote a challenger that beats live by a margin for enough consecutive runs.
        new_params, self._promote_streak, note = decide_promotion(
            rows, self.s.swing_window, self.s.swing_dip_k, self._promote_streak,
            margin=self.s.swing_promote_margin, streak_needed=self.s.swing_promote_streak)
        if new_params and self.s.swing_promote_enabled:
            try:
                json.dump(new_params, open(_LIVE_PARAMS, "w"))
                log.warning("swing SELF-IMPROVE: %s -> wrote %s (effective on next restart; delete to revert)",
                            note, _LIVE_PARAMS)
            except OSError:
                pass
        else:
            log.info("swing promotion: %s", note)
        self._save()

    async def _prewarm_pools(self, client) -> None:
        """GeckoTerminal needs a pool address per token (one extra call). Resolve them ALL once at startup,
        spaced under the ~30/min limit, so the per-scan chart() is a single OHLCV call — the first scan
        then never bursts 2 calls/token (pool_for + ohlcv) over the rate limit. No-op for non-GT clients."""
        if not hasattr(client, "pool_for"):
            return
        n = 0
        for mint in list(self.universe.values()):
            try:
                if await client.pool_for(mint):
                    n += 1
            except Exception:  # noqa: BLE001 — a warm-up miss just means that token resolves on its first scan
                pass
            await asyncio.sleep(self.s.swing_poll_sleep_s)
        log.info("swing: pre-warmed %d/%d GeckoTerminal pools", n, len(self.universe))

    async def run(self) -> None:
        use_gt = self.s.swing_ohlcv_source == "geckoterminal"
        st_ok = self.s.solanatracker_enabled and bool(self.s.solanatracker_api_key)
        if not use_gt and not st_ok:
            log.error("swing needs an OHLCV source: SWING_OHLCV_SOURCE=geckoterminal (keyless) or a Solana Tracker key.")
            return
        self._load()
        async with AsyncExitStack() as stack:
            # Solana Tracker (if a key exists) does the low-frequency universe-liquidity check + the weekly
            # discovery; GeckoTerminal (keyless) serves the HIGH-frequency per-scan OHLCV so the ST free tier
            # isn't blown. Either alone is enough to run.
            st = None
            if st_ok:
                st = await stack.enter_async_context(
                    SolanaTrackerClient(self.s.solanatracker_api_key, self.s.solanatracker_base_url))
            chart_client = await stack.enter_async_context(GeckoTerminalClient()) if use_gt else st
            self.universe = await liquid_universe(st, min_liquidity_usd=self.s.swing_min_liquidity_usd)
            await self._prewarm_pools(chart_client)        # resolve GeckoTerminal pools ONCE (spaced) so the
            log.info("swing mode LIVE (paper) | %d tokens | %s bars | OHLCV=%s | SMA%d dip%.0f%% | size %.2f SOL | scan %.0fs",
                     len(self.universe), self.s.swing_interval, self.s.swing_ohlcv_source, self.p.window,
                     self.p.dip_k * 100, self.p.size_sol, self.s.swing_scan_interval_s)
            while True:
                try:
                    await self._scan_once(chart_client)
                    if self.s.swing_discover_enabled and self.s.swing_discover_interval_s > 0:
                        now = time.time()
                        if now - self._last_discover >= self.s.swing_discover_interval_s:
                            self._last_discover = now
                            await self._discover()         # autonomous self-research (advisory; ST-sourced)
                except Exception as e:  # noqa: BLE001 — a scan/discovery failure must not kill the forward test
                    log.warning("swing loop error: %s", type(e).__name__)
                await asyncio.sleep(self.s.swing_scan_interval_s)
