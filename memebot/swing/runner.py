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

from ..backtest.swing_discover import run_discovery
from ..backtest.swing_lab import load_bars
from ..feed.solanatracker import SolanaTrackerClient
from ..utils.logging import get_logger
from .engine import SwingClosed, SwingEngine, SwingParams, SwingPosition
from .universe import liquid_universe

log = get_logger("swing.runner")
_STATE = "swing_state.json"


class SwingRunner:
    def __init__(self, settings) -> None:
        self.s = settings
        self.p = SwingParams(
            window=settings.swing_window, dip_k=settings.swing_dip_k, exit_k=settings.swing_exit_k,
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

    # ---- state persistence (so the forward test survives restarts) -----------------------------------
    def _save(self) -> None:
        try:
            state = {
                "cash": self.engine.cash,
                "positions": [vars(p) for p in self.engine.positions.values()],
                "closed": [vars(c) for c in self.engine.closed],
                "last_bar": self._last_bar,
            }
            json.dump(state, open(_STATE, "w"))
        except OSError as e:
            log.debug("swing state save failed: %s", type(e).__name__)

    def _load(self) -> None:
        if not os.path.exists(_STATE):
            return
        try:
            st = json.load(open(_STATE))
        except (ValueError, OSError):
            return
        self.engine.cash = float(st.get("cash", self.engine.cash))
        self.engine.positions = {p["mint"]: SwingPosition(**p) for p in st.get("positions", [])}
        self.engine.closed = [SwingClosed(**c) for c in st.get("closed", [])]
        self._last_bar = {k: float(v) for k, v in st.get("last_bar", {}).items()}
        log.info("swing: resumed state (cash=%.3f, open=%d, closed=%d)",
                 self.engine.cash, len(self.engine.positions), len(self.engine.closed))

    async def _scan_once(self, client) -> None:
        price_map = {}
        for sym, mint in self.universe.items():
            bars = await client.chart(mint, self.s.swing_interval)
            if len(bars) < self.p.window + 1:
                continue
            closes_all = [float(b["close"]) for b in bars]
            ts = float(bars[-1].get("time", 0.0))
            price_map[mint] = closes_all[-1]                  # mark-to-market every poll (forming bar ok for equity)
            # Decide EXACTLY ONCE per CLOSED candle: act only when a new bar has appeared (the previous one
            # just closed), and decide on the CLOSED series (drop the still-forming last bar). This stops
            # entries/exits repainting intra-bar and makes bars_held count BARS, not 30-min polls — matching
            # the backtest's one-decision-per-closed-bar contract. (Prior `if True` stepped every poll: a
            # max_hold_bars time-stop fired ~8x early and entries fired on transient mid-candle prints.)
            new_bar = ts > self._last_bar.get(mint, 0.0)
            res = None
            if new_bar:
                self._last_bar[mint] = ts
                closed = closes_all[:-1]
                if len(closed) >= self.p.window:
                    res = self.engine.step(mint, sym, closed, float(bars[-2].get("time", ts)))
            if res:
                kind, obj = res
                if kind == "enter":
                    log.info("swing ENTER %s @ %.6g (dip %.0f%% below SMA, size %.2f SOL)",
                             sym, obj.entry_price, obj.dip_at_entry * 100, obj.sol_in)
                else:
                    log.info("swing EXIT  %s @ %.6g -> PnL %+.3f SOL (%+.1f%%) [%s]",
                             sym, obj.exit_price, obj.pnl_sol, obj.pnl_pct * 100, obj.reason)
            await asyncio.sleep(0.35)        # rate-limit courtesy
        eq = self.engine.equity(price_map)
        st = self.engine.stats()
        log.info("swing book | equity %.3f SOL (start %.1f) | open %d | closed %d win%% %.0f pf %.2f realized %+.3f",
                 eq, self.engine.initial, st["open"], st["closed"], st["win_rate"] * 100,
                 st["profit_factor"], st["realized_sol"])
        self._save()

    async def _discover(self, client) -> None:
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
                     "| live: SMA%d dip%.0f%% (advisory — live params unchanged)",
                     len(passed), len(rows), name, g, self.s.swing_window, self.s.swing_dip_k * 100)
        else:
            log.info("swing discovery | 0/%d strategies passed the walk-forward gate this run", len(rows))

    async def run(self) -> None:
        if not (self.s.solanatracker_enabled and self.s.solanatracker_api_key):
            log.error("swing mode needs SOLANATRACKER_ENABLED + a key (it sources OHLCV from /chart).")
            return
        self._load()
        async with SolanaTrackerClient(self.s.solanatracker_api_key, self.s.solanatracker_base_url) as client:
            self.universe = await liquid_universe(client)
            log.info("swing mode LIVE (paper) | %d tokens | %s bars | SMA%d dip%.0f%% | size %.2f SOL | scan %.0fs",
                     len(self.universe), self.s.swing_interval, self.p.window, self.p.dip_k * 100,
                     self.p.size_sol, self.s.swing_scan_interval_s)
            while True:
                try:
                    await self._scan_once(client)
                    if self.s.swing_discover_enabled and self.s.swing_discover_interval_s > 0:
                        now = time.time()
                        if now - self._last_discover >= self.s.swing_discover_interval_s:
                            self._last_discover = now
                            await self._discover(client)   # autonomous self-research (advisory)
                except Exception as e:  # noqa: BLE001 — a scan/discovery failure must not kill the forward test
                    log.warning("swing loop error: %s", type(e).__name__)
                await asyncio.sleep(self.s.swing_scan_interval_s)
