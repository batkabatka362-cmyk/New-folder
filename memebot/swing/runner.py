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
        )
        self.engine = SwingEngine(self.p, initial_sol=settings.swing_initial_sol)
        self.universe: dict[str, str] = {}
        self._last_bar: dict[str, float] = {}      # mint -> last bar time stepped (per-candle bookkeeping)

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
            closes = [float(b["close"]) for b in bars]
            ts = float(bars[-1].get("time", 0.0))
            price_map[mint] = closes[-1]
            # only advance per-candle bookkeeping (bars_held / time-stop) when a NEW bar has closed
            new_bar = ts > self._last_bar.get(mint, 0.0)
            res = self.engine.step(mint, sym, closes, ts) if True else None
            if new_bar:
                self._last_bar[mint] = ts
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
                except Exception as e:  # noqa: BLE001 — a scan failure must not kill the forward test
                    log.warning("swing scan error: %s", type(e).__name__)
                await asyncio.sleep(self.s.swing_scan_interval_s)
