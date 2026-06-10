"""The swing PAPER engine — a self-contained mean-reversion ledger (cash + positions + realized book).

Deliberately NOT the pump.fun PaperBackend/Portfolio: those model bonding-curve pricing and TP/SL/trail
exits, which are wrong for liquid-token mean-reversion. This is a clean, network-free ledger the runner
feeds prices into, so the whole fill/exit policy is unit-tested. Fees mirror the swing_lab backtest: a
round-trip `fee_pct` split half on entry and half on exit. PAPER-ONLY.
"""
from __future__ import annotations

from dataclasses import dataclass

from .strategy import dip_depth, mean_rev_position


@dataclass(frozen=True)
class SwingParams:
    window: int = 24
    dip_k: float = 0.18           # enter when price < SMA*(1-dip_k); deeper dips scored stronger (WL17)
    exit_k: float = 0.0           # exit when price > SMA*(1+exit_k)
    fee_pct: float = 0.01         # round-trip liquid-pair fee (split half/half per side)
    size_sol: float = 1.0         # SOL deployed per entry
    max_positions: int = 5
    max_hold_bars: int = 0        # 0 = no time stop; else force-exit after this many bars held (regime guard)
    stop_k: float = 0.0           # 0 = no stop; else hard-exit if price falls >= this frac below ENTRY (falling-knife cap)
    slippage_bps: float = 0.0     # extra per-SIDE fill haircut in bps (dip-buys fill into falling books)
    regime_window: int = 0        # long-SMA downtrend veto window (0 = off); skip a dip below the long SMA
    regime_tol: float = 0.0       # allow price this frac below the long SMA before vetoing the dip
    max_total_exposure_sol: float = 0.0   # cap aggregate deployed SOL across positions (0 = only the count cap)
    rolling_loss_halt_sol: float = 0.0    # halt NEW entries once recent realized loss <= -this (0 = off)
    loss_halt_lookback: int = 20          # how many recent closed trades the halt sums


@dataclass
class SwingPosition:
    mint: str
    symbol: str
    qty: float
    entry_price: float
    entry_ts: float
    sol_in: float
    dip_at_entry: float
    bars_held: int = 0


@dataclass
class SwingClosed:
    mint: str
    symbol: str
    entry_price: float
    exit_price: float
    sol_in: float
    sol_out: float
    pnl_sol: float
    pnl_pct: float
    entry_ts: float
    exit_ts: float
    reason: str


class SwingEngine:
    def __init__(self, params: SwingParams, initial_sol: float = 10.0) -> None:
        self.p = params
        self.cash = float(initial_sol)
        self.initial = float(initial_sol)
        self.positions: dict[str, SwingPosition] = {}
        self.closed: list[SwingClosed] = []

    # ---- portfolio kill-switch (correlated memecoins -> cap the bad-day loss) -------------------------
    def _can_open(self) -> bool:
        """Gate NEW entries on the count cap, an aggregate-exposure cap, and a rolling-realized-loss halt
        (stop averaging into a regime we're losing in). Holding/exiting existing positions is unaffected."""
        if len(self.positions) >= self.p.max_positions:
            return False
        if self.p.max_total_exposure_sol > 0:
            deployed = sum(p.sol_in for p in self.positions.values())
            if deployed + self.p.size_sol > self.p.max_total_exposure_sol:
                return False
        if self.p.rolling_loss_halt_sol > 0 and self.closed:
            recent = self.closed[-self.p.loss_halt_lookback:]
            if sum(c.pnl_sol for c in recent) <= -self.p.rolling_loss_halt_sol:
                return False
        return True

    # ---- paper fills (fee split half per side + slippage per side, matching the backtest) ------------
    def _enter(self, mint: str, symbol: str, closes: list[float], ts: float) -> SwingPosition | None:
        price = closes[-1]
        if price <= 0 or mint in self.positions or not self._can_open():
            return None
        if self.cash < self.p.size_sol:
            return None
        half = self.p.fee_pct / 2.0
        slip = self.p.slippage_bps / 10000.0
        qty = self.p.size_sol * (1.0 - half) * (1.0 - slip) / price   # entry-side fee + slippage (worse fill)
        self.cash -= self.p.size_sol
        pos = SwingPosition(mint=mint, symbol=symbol, qty=qty, entry_price=price, entry_ts=ts,
                            sol_in=self.p.size_sol, dip_at_entry=dip_depth(closes, self.p.window) or 0.0)
        self.positions[mint] = pos
        return pos

    def _exit(self, mint: str, price: float, ts: float, reason: str) -> SwingClosed | None:
        pos = self.positions.pop(mint, None)
        if pos is None or price <= 0:
            return None
        half = self.p.fee_pct / 2.0
        slip = self.p.slippage_bps / 10000.0
        sol_out = pos.qty * price * (1.0 - half) * (1.0 - slip)        # exit-side fee + slippage
        self.cash += sol_out
        pnl = sol_out - pos.sol_in
        closed = SwingClosed(mint=mint, symbol=pos.symbol, entry_price=pos.entry_price, exit_price=price,
                             sol_in=pos.sol_in, sol_out=sol_out, pnl_sol=pnl,
                             pnl_pct=(pnl / pos.sol_in if pos.sol_in else 0.0),
                             entry_ts=pos.entry_ts, exit_ts=ts, reason=reason)
        self.closed.append(closed)
        return closed

    # ---- the per-token step the runner calls with fresh bars -----------------------------------------
    def step(self, mint: str, symbol: str, closes: list[float], ts: float) -> tuple[str, object] | None:
        """Apply the mean-reversion rule to one token's latest close series. Returns ('enter'|'exit', obj)
        when a (paper) trade fired, else None. The runner is responsible for only calling this once per
        new bar (so bars_held + the time-stop count bars, not polls)."""
        if not closes or closes[-1] <= 0:
            return None
        held = self.positions.get(mint)
        cur = 1 if held else 0
        if held:
            held.bars_held += 1
            if self.p.max_hold_bars and held.bars_held >= self.p.max_hold_bars:
                c = self._exit(mint, closes[-1], ts, "time_stop")
                return ("exit", c) if c else None
            # hard stop / max-adverse-excursion: cap the falling-knife left tail BEFORE the reversion check
            # (mean-reversion has unbounded downside until a reversion that may never come on a dying token).
            if self.p.stop_k > 0 and held.entry_price > 0 and \
                    (held.entry_price - closes[-1]) / held.entry_price >= self.p.stop_k:
                c = self._exit(mint, closes[-1], ts, "stop")
                return ("exit", c) if c else None
        want = mean_rev_position(closes, window=self.p.window, dip_k=self.p.dip_k,
                                 exit_k=self.p.exit_k, pos=cur,
                                 regime_window=self.p.regime_window, regime_tol=self.p.regime_tol)
        if want == 1 and cur == 0:
            pos = self._enter(mint, symbol, closes, ts)
            return ("enter", pos) if pos else None
        if want == 0 and cur == 1:
            c = self._exit(mint, closes[-1], ts, "reversion")
            return ("exit", c) if c else None
        return None

    # ---- accounting ----------------------------------------------------------------------------------
    def equity(self, price_map: dict[str, float]) -> float:
        """Cash + open positions marked at the supplied current prices (mint->price)."""
        held = sum(p.qty * price_map.get(m, p.entry_price) for m, p in self.positions.items())
        return self.cash + held

    def stats(self) -> dict:
        n = len(self.closed)
        wins = [c for c in self.closed if c.pnl_sol > 0]
        gross_win = sum(c.pnl_sol for c in wins)
        gross_loss = -sum(c.pnl_sol for c in self.closed if c.pnl_sol <= 0)
        realized = sum(c.pnl_sol for c in self.closed)
        return {
            "closed": n,
            "win_rate": (len(wins) / n) if n else 0.0,
            "realized_sol": realized,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0),
            "open": len(self.positions),
            "cash": self.cash,
        }
