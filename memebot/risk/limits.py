"""Deterministic risk gate + kill-switch. Independent of any model/LLM.

In Phase 3 the Claude verdict becomes advisory and THIS layer still decides and
can veto. Phase 1 wires the core limits: max positions, per-trade size, daily
loss cap (trips the kill-switch), and per-token cooldown after a loss.
"""
from __future__ import annotations

import time
from collections import deque

from ..config import Settings
from ..utils.logging import get_logger

log = get_logger("risk")


def expected_return(conviction: float, tp_pct: float, sl_pct: float, round_trip_cost: float) -> float:
    """Cost-aware expected return of a trade, using conviction as a win-probability
    proxy: p*tp - (1-p)*sl - round_trip_cost. Gate entries on this being >= a floor
    so we don't take trades whose edge can't clear fees + slippage."""
    p = max(0.0, min(1.0, conviction))
    return p * tp_pct - (1.0 - p) * sl_pct - round_trip_cost


def kelly_fraction(p: float, tp_pct: float, sl_pct: float, kelly_mult: float = 0.5, cap: float = 1.0) -> float:
    """W6: fractional-Kelly bet fraction of the risk budget. p = win probability, b = tp/sl (the
    reward:risk odds). Full Kelly f* = p - (1-p)/b; we bet `kelly_mult` of that (half-Kelly by
    default for safety), clamped to [0, cap]. Returns 0 when the edge is non-positive (NEVER scale a
    negative-EV trade — Kelly maximizes log-growth only on a real edge). REQUIRES a CALIBRATED p
    (the P4 GBM) to be meaningful; the rule score is a weak proxy, so this is gated OFF by default."""
    p = max(0.0, min(1.0, p))
    if tp_pct <= 0 or sl_pct <= 0:
        return 0.0
    b = tp_pct / sl_pct
    f_star = p - (1.0 - p) / b
    return max(0.0, min(cap, kelly_mult * f_star))


def reeval_action(concentration: float | None, cut_pct: float, good_pct: float) -> str:
    """Decide what to do with an OPEN position given fresh holder concentration:
    'cut' (worsened -> exit fast), 'extend' (broadened -> let it run), 'hold' (no change).
    Unknown concentration -> hold (don't act on missing data)."""
    if concentration is None:
        return "hold"
    if concentration >= cut_pct:
        return "cut"
    if concentration <= good_pct:
        return "extend"
    return "hold"


class RiskManager:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.halted = False
        self.halt_reason = ""
        self._cooldown: dict[str, float] = {}
        self._consecutive_losses = 0
        # N3: realized PnL of the last `rolling_loss_window` closes -> halt on a deep net bleed (robust
        # to the alternating win/loss the consecutive counter misses). maxlen>=1 even when disabled.
        self._recent_pnls: deque[float] = deque(maxlen=max(1, settings.risk.rolling_loss_window))
        # N10: cumulative REALIZED PnL today + its intraday peak -> giveback halt (banked a winning day
        # then gave it back). REALIZED (not mark-to-market) so a single open position's unrealized
        # pump-and-fade can neither arm nor trip it, and it updates on every close regardless of book size.
        self._day_realized = 0.0
        self._day_realized_peak = 0.0
        self._day = -1                          # set on first can_open; UTC-day for streak + bleed + giveback reset

    @property
    def consecutive_losses(self) -> int:
        return self._consecutive_losses

    def halt(self, reason: str) -> None:
        if not self.halted:
            self.halted = True
            self.halt_reason = reason
            log.error("KILL-SWITCH TRIPPED: %s - no new positions", reason)

    def resume(self) -> bool:
        """Clear a halt (e.g. a manual /stop). Returns True if it had been halted."""
        was = self.halted
        if was:
            log.info("kill-switch cleared (was: %s)", self.halt_reason)
        self.halted = False
        self.halt_reason = ""
        return was

    def can_open(self, portfolio, price_map: dict[str, float], mint: str,
                 now: float | None = None) -> tuple[bool, str]:
        now = time.time() if now is None else now
        today = int(now // 86_400)                  # UTC-day rollover resets the loss streak + bleed window
        if today != self._day:
            self._day = today
            self._consecutive_losses = 0
            self._recent_pnls.clear()               # N3: a fresh day starts the rolling-PnL window clean
            self._day_realized = self._day_realized_peak = 0.0   # N10: fresh day resets the realized-PnL HWM
            if self.halted and self.halt_reason in ("loss_streak", "pnl_bleed", "giveback"):
                self.halted, self.halt_reason = False, ""
        dl = portfolio.daily_loss(price_map, now)   # same clock as the streak rollover above
        cap = self.s.risk.daily_loss_cap_sol
        if self._cooldown:                          # prune expired cooldowns so the dict can't grow unbounded
            self._cooldown = {m: t for m, t in self._cooldown.items() if t > now}
        # Auto-clear ONLY a daily-cap halt once the new day's loss is back under cap.
        # Any other (manual / future) halt reason stays sticky.
        if self.halted and self.halt_reason == "daily_loss_cap" and dl < cap:
            self.halted = False
            self.halt_reason = ""
            log.info("daily_loss_cap halt cleared on day rollover")
        if self.halted:
            return False, f"halted:{self.halt_reason}"
        if portfolio.has_position(mint):
            return False, "already_open"
        if portfolio.open_count() >= self.s.risk.max_positions:
            return False, "max_positions"
        if dl >= cap:
            self.halt("daily_loss_cap")
            return False, "daily_loss_cap"
        # N10: giveback halt — a WINNING day (REALIZED) that round-trips its banked gains. Arms only once
        # the day's peak realized PnL cleared giveback_arm_frac of initial equity (so a tiny gain can't
        # strangle the thin trade flow), then halts when that peak is given back by daily_giveback_frac.
        # REALIZED basis: a single open position's unrealized swing can neither arm nor trip it, and the
        # peak updates on every close (no dependence on the book being non-full). Cleared on day rollover.
        gf = self.s.risk.daily_giveback_frac
        if gf > 0.0:
            peak = self._day_realized_peak
            arm = self.s.risk.initial_sol * self.s.risk.giveback_arm_frac
            if peak >= arm and (peak - self._day_realized) >= gf * peak:
                self.halt("giveback")
                return False, "giveback"
        if now < self._cooldown.get(mint, 0.0):
            return False, "cooldown"
        return True, ""

    def position_size_sol(self, portfolio, equity: float, frac: float = 1.0,
                          liquidity_sol: float = 0.0, price_map: dict | None = None) -> float:
        """Equity-fraction sizing — compounds as equity grows — scaled by the brain's
        size fraction, then capped by the absolute per-trade ceiling, 95% of the cash
        balance, a slice of pool liquidity (to bound slippage), and the aggregate
        open-exposure ceiling (correlated-flush protection)."""
        r = self.s.risk
        target = equity * r.risk_per_trade_frac * max(0.0, min(1.0, frac))
        # D5 HARD per-trade ceiling: independent of how `target` is derived, one position's SOL
        # (= its full loss on a rug) can never exceed max_position_equity_frac of equity. This is
        # a robust survival invariant — it still binds if a future edit lets `frac`/`target` grow,
        # and (unlike the absolute max_position_sol) it TIGHTENS automatically as equity is drawn down.
        caps = [r.max_position_sol, equity * r.max_position_equity_frac, portfolio.sol_balance * 0.95]
        if liquidity_sol > 0:
            caps.append(liquidity_sol * r.max_liquidity_frac)
        # keep total open exposure <= equity * frac. Value positions at CURRENT price
        # (same basis as equity()), not cost — so the cap tracks real exposure, not entry cost.
        pm = price_map or {}
        deployed = sum(p.qty * pm.get(p.mint, p.avg_price_sol) for p in portfolio.positions.values())
        caps.append(max(0.0, equity * r.max_total_exposure_frac - deployed))
        size = min([target] + caps)
        return size if size > 1e-4 else 0.0

    def sol_at_risk(self, equity: float) -> dict:
        """D5 survival accounting: the worst-case SOL the config PERMITS to be lost AT THIS EQUITY /
        AT ENTRY TIME. A rug is -100% of a position, so per-trade SOL-at-risk == the per-trade size
        ceiling. The whole-book worst case (every open position rugs at once) is bounded by BOTH the
        per-trade ceiling × max positions AND the aggregate-exposure ceiling. NOTE this is a snapshot
        bound, not a continuous invariant: the exposure cap is enforced at OPEN on current price, so
        appreciation of open positions can push realized book-at-risk above this number — the
        daily-loss breaker (dl >= cap) is the continuous backstop. `book_within_daily_cap` uses a
        STRICT < to match that breaker's inclusive >= trip (a book rug == the cap WOULD trip it)."""
        r = self.s.risk
        per_trade = min(r.max_position_sol, equity * r.max_position_equity_frac)
        max_book_rug = min(per_trade * r.max_positions, equity * r.max_total_exposure_frac)
        return {
            "equity": equity,
            "per_trade_sol": per_trade,
            "per_trade_pct": (per_trade / equity if equity > 0 else 0.0),
            "max_book_rug_sol": max_book_rug,
            "max_book_rug_pct": (max_book_rug / equity if equity > 0 else 0.0),
            "daily_loss_cap_sol": r.daily_loss_cap_sol,
            "book_within_daily_cap": max_book_rug < r.daily_loss_cap_sol,
        }

    def _record_pnl(self, pnl_sol: float) -> None:
        """Feed a realized CLOSE PnL into the two daily survival trackers.
        N10: accumulate the day's realized PnL + its intraday peak (the giveback basis).
        N3: append to the rolling window and trip a 'pnl_bleed' halt when the last-N closes net to a
        loss >= max_rolling_loss_sol. Unlike the consecutive-loss streak, a single win does NOT lift
        either halt (only a day rollover / manual resume) — robust to the alternating {win, big loss}
        bleed the streak counter silently misses."""
        self._day_realized += pnl_sol                          # N10: cumulative realized PnL today
        if self._day_realized > self._day_realized_peak:
            self._day_realized_peak = self._day_realized       # ...and its intraday high-water mark
        cap = self.s.risk.max_rolling_loss_sol
        if cap <= 0 or self.s.risk.rolling_loss_window < 1:
            return
        self._recent_pnls.append(pnl_sol)
        if -sum(self._recent_pnls) >= cap:          # net loss over the window this deep
            self.halt("pnl_bleed")

    def on_loss(self, mint: str, pnl_sol: float = 0.0, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._cooldown[mint] = now + self.s.risk.per_token_cooldown_s
        self._consecutive_losses += 1
        n = self.s.risk.max_consecutive_losses
        if n > 0 and self._consecutive_losses >= n:
            self.halt("loss_streak")
        self._record_pnl(pnl_sol)

    def on_win(self, pnl_sol: float = 0.0) -> None:
        """A profitable/flat close resets the consecutive-loss streak and lifts a streak halt — but
        does NOT lift the N3 rolling-PnL bleed halt (that needs a day rollover)."""
        self._consecutive_losses = 0
        if self.halted and self.halt_reason == "loss_streak":
            self.halted, self.halt_reason = False, ""
        self._record_pnl(pnl_sol)
