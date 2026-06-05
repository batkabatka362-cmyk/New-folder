"""Virtual portfolio: SOL balance, open positions, realized PnL, exit logic.

Cost basis is fee-inclusive (the `sol` on a buy Fill already includes fees), so
average entry price is the true break-even-before-exit-costs price.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..constants import MODE_HOLD, MODE_SCALP
from ..execution.base import Fill
from ..utils.logging import get_logger

log = get_logger("portfolio")


@dataclass(slots=True)
class ExitParams:
    """Per-mode exit rules (tunable; backtest before trusting)."""

    scalp_tp_pct: float = 0.40
    scalp_sl_pct: float = 0.18
    scalp_max_hold_s: float = 180.0
    hold_tp_pct: float = 3.0
    hold_sl_pct: float = 0.35
    hold_trail_pct: float = 0.30        # trail from peak once in profit
    hold_max_hold_s: float = 86_400.0
    breakeven_arm_pct: float = 0.12     # P3: arm sooner so a small poke up latches protection on a late entry
    breakeven_floor_pct: float = 0.10   # ...then exit near the ~9.5% round-trip cost, so an armed-breakeven exit is ~flat
    trail_after_arm_pct: float = 0.04   # P8: once armed, exit this far off the peak. Tightened 0.06->0.04 from the exitlab sweep over 113 logged price paths: net -0.585 vs baseline -0.06's -2.501 (~4x less bad, win 42% vs 40%) — be_trail dominates exits, so a tighter ratchet banks more of the peak before the fade. Still net-NEGATIVE (the leak is entries/data, not exits); re-sweep as data grows.
    scalp_stall_s: float = 75.0         # P3: a scalp still <=break-even after this is a bleeder -> exit "stall"
    partial_tp_pct: float = 0.15        # P3: take a partial profit here (ABOVE the ~9.5% round-trip cost) to lock SOL
    partial_tp_frac: float = 0.7        # WL2 (exitlab, 234 paths): 0.5->0.7 — banks more of the win on the partial, lifting win 52%->56% (+9 winners, 0 lost) + median fwd +0.010->+0.042, broad across 92/234 paths (NOT outlier-driven). Robust tuning, re-sweep as data grows. (trail_after_arm_pct stays 0.04 — its net edge was 3-path overfit.)
    liq_collapse_frac: float = 0.5      # W2: force-close a HOLD when its pool drains below this fraction of ENTRY liquidity (a rug the -35% price SL gaps past — exit on the pool drain, the real rug tell)
    # P7 (user doctrine: "ersdeltei baiwal orson dvngeer ashig hiigeed vldsen hesgiig tsaash ywuulj
    # aldagdalgvi vldene" — when RISKY, recover the PRINCIPAL as profit and free-roll the rest with
    # zero capital at risk). The de-risk is a principal-recovery partial, triggered on a live RISK
    # tell (sell-pressure on the tape / holder concentration), at a LOWER profit bar than the clean
    # partial so capital is secured sooner.
    derisk_tp_pct: float = 0.08         # a RISKY held position books a principal-recovery partial once up >= this (lower bar than partial_tp_pct=0.15)
    derisk_max_frac: float = 0.9        # cap the principal-recovery sell here so a free-roll (house-money) tail ALWAYS rides
    # N5 PROACTIVE principal-recovery: bank principal once up >= this %, WITHOUT waiting for a risk tell
    # (the most literal "scale out FAST to bank profit, free-roll the rest" doctrine). 0 = OFF (the live
    # derisk stays risk-gated). PRICE-ONLY, so it is faithfully replayable in the exitlab — set it from
    # the exitlab sweep, never by feel (aggressive early de-risk caps the rare 10x; it's give-back MITIGATION).
    derisk_proactive_pct: float = 0.0   # proactive principal-recovery trigger (0 = off; e.g. 0.30 = bank at +30%) — the SHARED-latch variant (preempts the P3 partial). WL4: now wired LIVE (was exitlab-only/dead); kept OFF by default — the exitlab sweep's gain over the partial was net+median but win-rate-neutral + 55% top-3-concentrated, so enable only after forward confirmation, never by feel.
    # A3 TAKE-INITIAL (the spec's #1 survival rule): once up >= this, recover the PRINCIPAL (sell enough
    # to bank the original cost basis) and ride the remainder as risk-free HOUSE MONEY — so a later rug
    # can never zero a winner. OWN latch (initial_taken), so it COMPOSES with the early P3 partial rather
    # than being preempted: a small bank at partial_tp_pct, then full principal recovered here at 2x.
    # Default 1.0 = +100% = 2x (a high, conservative bar: rarely fires, barely caps upside since ~half
    # rides). 0 disables. PRICE-ONLY (faithfully replayable in the exitlab).
    take_initial_pct: float = 1.0
    sell_pressure_bsr: float = 0.7      # buy/sell ratio (DexScreener h1) below this on a HELD position, with price rolling over = sells dominating = a rug worked on the tape (the user's "ariljaan deer rug" tell). 0 disables.
    sell_pressure_cut: bool = False     # also CUT a non-winning held position on sell-pressure (OFF: hourly bsr is noisy + the SL/liq_collapse own the deep downside; on = an early rug-tape exit)


@dataclass(slots=True)
class Position:
    mint: str
    symbol: str
    mode: str
    qty: float = 0.0
    cost_sol: float = 0.0               # fee-inclusive remaining cost basis
    opened_ts: float = field(default_factory=time.time)
    peak_price_sol: float = 0.0
    entry_price_sol: float = 0.0        # P10 #11: stable MFE/MAE reference (entry price; avg_price_sol -> 0 at close)
    trough_price_sol: float = 0.0       # P10 #11: lowest price seen while held (max adverse excursion)
    tp_override: float | None = None    # brain-chosen take-profit (0/None => mode default)
    sl_override: float | None = None    # brain-chosen stop-loss   (0/None => mode default)
    entry_note: str = ""                # the verdict reasoning at entry (for reflection)
    entry_risk_flags: list = field(default_factory=list)  # P10 #4: the brain's own at-entry concerns -> reflection
    breakeven_armed: bool = False       # latched once the position has been comfortably in profit
    partial_taken: bool = False         # P3: a partial take-profit has already been booked
    initial_taken: bool = False         # A3: the "recover principal at 2x" take-initial has fired (own latch)
    derisk_taken: bool = False          # P7 fix: the RISK-flag principal-recovery de-risk has fired (own latch, composes with partial/initial — a clean partial must NOT block a later risk-triggered de-risk)
    entry_features: dict = field(default_factory=dict)  # P2: feature vector at entry -> trade_outcomes
    entry_score: float = 0.0            # P2: the scorer's value at entry
    creator: str = ""                   # N9: creator wallet -> correlated-cohort exposure cap
    setup_type: str = ""                # the situation class at entry -> setup-specific reflection/recall
    verdict_source: str = ""            # P10b #1: which decider opened this (haiku/sonnet/ollama/rule) -> brain-vs-rule A/B
    verdict_conviction: float = 0.0     # P10b #1: the decider's conviction at entry
    realized_cost_sol: float = 0.0      # P3 accounting: cost basis removed by partial sells so far
    realized_proceeds_sol: float = 0.0  # ...cumulative proceeds across partials...
    realized_pnl_sol: float = 0.0       # ...cumulative realized PnL — the FULL round-trip is booked at close

    def should_take_partial(self, price_sol: float, params: ExitParams) -> bool:
        """P3: take a ONE-TIME partial profit once up >= partial_tp_pct (which sits ABOVE the
        ~9.5% round-trip cost, so the booked half is net-positive), then keep the remainder
        protected by the armed breakeven-trail (survival-first: never round-trip a winner to a
        loss — NOT a loose free-roll). Disabled when partial_tp_frac<=0."""
        if self.partial_taken or params.partial_tp_frac <= 0.0:
            return False
        return self.pnl_pct(price_sol) >= params.partial_tp_pct

    def should_take_initial(self, price_sol: float, params: ExitParams) -> bool:
        """A3: time to recover the PRINCIPAL — up >= take_initial_pct (default 2x) and not yet taken.
        One-time per position via its OWN latch (initial_taken), independent of the P3 partial so the
        two compose. Disabled when take_initial_pct <= 0."""
        if self.initial_taken or params.take_initial_pct <= 0.0:
            return False
        return self.pnl_pct(price_sol) >= params.take_initial_pct

    def should_take_proactive_derisk(self, price_sol: float, params: ExitParams) -> bool:
        """N5 (WL4): bank the full PRINCIPAL proactively once up >= derisk_proactive_pct, WITHOUT
        waiting for a risk tell — then free-roll the rest as house money. Shares the P3-partial latch
        (mutually exclusive with the clean partial, exactly as exitlab models it). OFF by default
        (derisk_proactive_pct=0.0); price-only, so it stays faithfully exitlab-replayable."""
        if self.partial_taken or params.derisk_proactive_pct <= 0.0:
            return False
        return self.pnl_pct(price_sol) >= params.derisk_proactive_pct

    def derisk_fraction(self, price_sol: float, sell_cost_pct: float, max_frac: float = 0.9) -> float:
        """P7: the fraction of qty to sell NOW so net proceeds recover the remaining cost basis (the
        PRINCIPAL) — leaving the rest as a risk-free, house-money runner. Solve
        f * qty * price * (1 - sell_cost) = cost_sol for f, then clamp to (0, max_frac] so a free-roll
        tail ALWAYS rides. When the gain is too small to fully recover principal within the cap, we
        sell `max_frac` (bank most of it, free-roll a small tail) — still survival-first. Caller gates
        on pnl >= derisk_tp_pct, so the position is already in profit when this is used."""
        if self.qty <= 0 or price_sol <= 0:
            return 0.0
        gross_per_token = price_sol * max(0.0, 1.0 - sell_cost_pct)
        if gross_per_token <= 0:
            return 0.0
        f = self.cost_sol / (self.qty * gross_per_token)   # fraction whose net proceeds == remaining principal
        return max(0.0, min(max_frac, f))

    @property
    def avg_price_sol(self) -> float:
        return (self.cost_sol / self.qty) if self.qty > 0 else 0.0

    def pnl_pct(self, price_sol: float) -> float:
        ap = self.avg_price_sol
        return (price_sol - ap) / ap if ap > 0 else 0.0

    def should_exit(self, price_sol: float, params: ExitParams, now: float | None = None) -> tuple[bool, str]:
        now = time.time() if now is None else now   # `or` would treat now=0.0 as unset

        if price_sol > self.peak_price_sol:
            self.peak_price_sol = price_sol
        # P10 #11: track the lowest price seen too (max adverse excursion), for exit-timing reflection
        if price_sol > 0 and (self.trough_price_sol <= 0 or price_sol < self.trough_price_sol):
            self.trough_price_sol = price_sol
        pnl = self.pnl_pct(price_sol)
        age = now - self.opened_ts
        scalp = self.mode == MODE_SCALP
        # brain-chosen tp/sl override the mode defaults (0/None => default)
        tp = self.tp_override or (params.scalp_tp_pct if scalp else params.hold_tp_pct)
        sl = self.sl_override or (params.scalp_sl_pct if scalp else params.hold_sl_pct)

        # breakeven protection: once comfortably up, don't let it round-trip into a loss.
        # Only fires ABOVE the stop-loss band so a true SL breach is still recorded as "sl".
        if pnl >= params.breakeven_arm_pct:
            self.breakeven_armed = True
        # P3 arm-trail: once armed, a tight ratchet off the peak catches the fade early
        # (the move we missed is over) — applies to SCALP too, which otherwise never trails.
        if self.breakeven_armed and self.peak_price_sol > 0 and \
                (self.peak_price_sol - price_sol) / self.peak_price_sol >= params.trail_after_arm_pct:
            return True, "be_trail"
        if self.breakeven_armed and -sl < pnl <= params.breakeven_floor_pct:
            # honest label: an armed give-back above ~entry is "breakeven";
            # if it slipped to a real loss it's a protective stop-out, label "sl"
            return True, "breakeven" if pnl >= 0.0 else "sl"

        if pnl >= tp:
            return True, "tp"
        if pnl <= -sl:
            return True, "sl"
        if scalp:
            # P3 loss-aware stall: a scalp still under water past the stall window is a bleeder
            if age >= params.scalp_stall_s and pnl <= 0.0:
                return True, "stall"
            if age >= params.scalp_max_hold_s:
                return True, "timeout"
        else:  # MODE_HOLD — trailing stop only after we've been in profit
            if self.peak_price_sol > self.avg_price_sol > 0:
                drawdown = (self.peak_price_sol - price_sol) / self.peak_price_sol
                if drawdown >= params.hold_trail_pct:
                    return True, "trail"
            if age >= params.hold_max_hold_s:
                return True, "timeout"
        return False, ""


@dataclass(slots=True)
class ClosedTrade:
    mint: str
    symbol: str
    mode: str
    cost_sol: float
    proceeds_sol: float
    pnl_sol: float
    pnl_pct: float
    opened_ts: float
    closed_ts: float
    reason: str
    entry_note: str = ""
    entry_features: dict = field(default_factory=dict)  # P2: carried through for trade_outcomes
    entry_score: float = 0.0
    setup_type: str = ""                # situation class at entry -> setup-specific lesson tagging
    entry_risk_flags: list = field(default_factory=list)  # P10 #4: the brain's at-entry concerns -> reflection
    peak_pct: float = 0.0               # P10 #11: max favorable excursion vs entry (did we exit too early?)
    trough_pct: float = 0.0             # P10 #11: max adverse excursion vs entry (how deep did it dip?)
    verdict_source: str = ""            # P10b #1: decider that opened it -> brain-vs-rule A/B on the CLEAN book
    verdict_conviction: float = 0.0     # P10b #1: decider conviction at entry


class Portfolio:
    DUST = 1e-9

    def __init__(self, initial_sol: float) -> None:
        self.initial_sol = initial_sol
        self.sol_balance = initial_sol
        self.realized_pnl = 0.0
        self.positions: dict[str, Position] = {}
        self.closed: list[ClosedTrade] = []
        self.day_start_equity = initial_sol
        self._day = int(time.time() // 86_400)

    # ── queries ──────────────────────────────────────────────────────────────
    def has_position(self, mint: str) -> bool:
        return mint in self.positions

    def open_count(self) -> int:
        return len(self.positions)

    def equity(self, price_map: dict[str, float]) -> float:
        unreal = sum(p.qty * price_map.get(p.mint, p.avg_price_sol) for p in self.positions.values())
        return self.sol_balance + unreal

    def daily_loss(self, price_map: dict[str, float], now: float | None = None) -> float:
        """Positive number = SOL lost so far today vs start-of-day equity."""
        self._roll_day(price_map, now)
        return max(0.0, self.day_start_equity - self.equity(price_map))

    def _roll_day(self, price_map: dict[str, float], now: float | None = None) -> None:
        now = time.time() if now is None else now   # one clock — callers (can_open) may inject `now`
        today = int(now // 86_400)
        if today != self._day:
            self._day = today
            self.day_start_equity = self.equity(price_map)

    # ── mutations ──────────────────────────────────────────────────────────────
    def apply_buy(self, fill: Fill, *, symbol: str, mode: str,
                  tp_override: float | None = None, sl_override: float | None = None,
                  note: str = "", features: dict | None = None, score: float = 0.0,
                  creator: str = "", setup_type: str = "", risk_flags: list | None = None,
                  verdict_source: str = "", verdict_conviction: float = 0.0) -> bool:
        if not fill.ok or fill.tokens <= 0:
            return False
        if fill.sol > self.sol_balance + self.DUST:
            return False
        self.sol_balance -= fill.sol
        pos = self.positions.get(fill.mint)
        if pos is None:
            pos = Position(fill.mint, symbol, mode, peak_price_sol=fill.price_sol,
                           entry_price_sol=fill.price_sol, trough_price_sol=fill.price_sol,
                           tp_override=tp_override, sl_override=sl_override, entry_note=note,
                           entry_risk_flags=list(risk_flags or []),
                           entry_features=dict(features or {}), entry_score=score, creator=creator,
                           setup_type=setup_type, verdict_source=verdict_source,
                           verdict_conviction=verdict_conviction)
            self.positions[fill.mint] = pos
        pos.qty += fill.tokens
        pos.cost_sol += fill.sol
        return True

    def apply_sell(self, fill: Fill, *, reason: str = "", max_return_mult: float = 0.0) -> float | None:
        """Apply a (possibly partial) sell. Returns realized PnL in SOL, or None.

        FILL-SANITY GUARD (`max_return_mult`>0): a single round-trip booking proceeds above
        `max_return_mult`x its cost basis is almost certainly a PRICING GLITCH — e.g. a position
        bought on the bonding curve (curve SOL price) and sold post-migration off DexScreener
        (`price_native`), two unreconciled SOL scales — not real alpha. Left unclamped, ONE such fill
        can be ~100% of the book's reported PnL and poison both the go-live gate and the training
        labels. We clamp the proceeds to the cap and log loudly; 0 disables (legacy behavior)."""
        pos = self.positions.get(fill.mint)
        if pos is None or not fill.ok or fill.tokens <= 0:
            return None
        sold = min(fill.tokens, pos.qty)
        portion = sold / pos.qty if pos.qty > 0 else 0.0
        cost_removed = pos.cost_sol * portion
        proceeds = fill.sol
        if max_return_mult > 0 and cost_removed > 0 and proceeds > cost_removed * max_return_mult:
            log.warning("fill-sanity: %s sell proceeds %.4f SOL >> %.0fx cost %.4f — clamping "
                        "(pricing glitch, not alpha)", fill.mint[:8], proceeds, max_return_mult, cost_removed)
            proceeds = cost_removed * max_return_mult
        pnl = proceeds - cost_removed

        self.sol_balance += proceeds
        self.realized_pnl += pnl
        pos.qty -= sold
        pos.cost_sol -= cost_removed
        # P3/P2 accounting: accumulate every slice so a partially-exited trade books the FULL
        # round-trip into ClosedTrade — not just the final slice. Without this, trade_outcomes
        # and the PnL stats would understate any trade that took a partial take-profit first.
        pos.realized_cost_sol += cost_removed
        pos.realized_proceeds_sol += proceeds
        pos.realized_pnl_sol += pnl

        if pos.qty <= self.DUST:
            total_cost, total_proceeds = pos.realized_cost_sol, pos.realized_proceeds_sol
            total_pnl = pos.realized_pnl_sol
            # P10 #11: max favorable / adverse excursion vs the entry price (qty is 0 now, so
            # avg_price_sol is unusable — that's why entry_price_sol is captured at apply_buy).
            ref = pos.entry_price_sol
            peak_pct = (pos.peak_price_sol / ref - 1.0) if ref > 0 else 0.0
            trough_pct = (pos.trough_price_sol / ref - 1.0) if ref > 0 and pos.trough_price_sol > 0 else 0.0
            self.closed.append(ClosedTrade(
                mint=pos.mint, symbol=pos.symbol, mode=pos.mode,
                cost_sol=total_cost, proceeds_sol=total_proceeds, pnl_sol=total_pnl,
                pnl_pct=(total_pnl / total_cost if total_cost > 0 else 0.0),
                opened_ts=pos.opened_ts, closed_ts=time.time(), reason=reason,
                entry_note=pos.entry_note,
                entry_features=dict(pos.entry_features), entry_score=pos.entry_score,
                setup_type=pos.setup_type, entry_risk_flags=list(pos.entry_risk_flags),
                peak_pct=peak_pct, trough_pct=trough_pct,
                verdict_source=pos.verdict_source, verdict_conviction=pos.verdict_conviction,
            ))
            del self.positions[fill.mint]
        return pnl
