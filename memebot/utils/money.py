"""Money math: SOL/lamports conversion, slippage and stacked-fee modeling.

Fees stack on pump.fun and they are large — modeling them faithfully is what
keeps paper PnL honest. See Fees in config.py.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import Fees
from ..constants import LAMPORTS_PER_SOL


def lamports_to_sol(lamports: int) -> float:
    return lamports / LAMPORTS_PER_SOL


def sol_to_lamports(sol: float) -> int:
    return int(round(sol * LAMPORTS_PER_SOL))


def estimate_slippage_pct(trade_sol: float, liquidity_sol: float, base_pct: float,
                          impact_k: float = 0.5, cap_pct: float = 0.5) -> float:
    """Linear price-impact model on top of a base slippage.

    Thin bonding-curve liquidity means a meaningful order moves the price. We
    approximate impact as proportional to (trade size / available liquidity).
    For size-aware accuracy later, replace with a Jupiter /quote route price.
    """
    if liquidity_sol <= 0:
        return cap_pct
    impact = impact_k * (trade_sol / liquidity_sol)
    return min(cap_pct, base_pct + impact)


@dataclass(slots=True)
class FillResult:
    tokens: float          # tokens bought (buy) / sold (sell)
    sol: float             # net SOL spent (buy) / received (sell)
    fee_sol: float         # total fees incl. flat priority fee
    eff_price_sol: float   # effective per-token price after slippage
    slippage_pct: float


def round_trip_cost_pct(fees: Fees) -> float:
    """In-and-out cost as a return fraction: both legs pay pct fees + slippage.
    Used by the expected-return entry filter and the backtest."""
    return 2.0 * sell_cost_pct(fees)


def sell_cost_pct(fees: Fees) -> float:
    """One-way (sell-leg) cost as a return fraction: pct fees + slippage (the flat priority
    fee is tiny and size-dependent, so it's excluded here). Used to size a principal-recovery
    de-risk partial — how much of the position to sell so net proceeds clear the cost basis."""
    return fees.pumpportal_pct + fees.pumpfun_curve_pct + fees.base_slippage_pct


def _pct_fee(fees: Fees) -> float:
    return fees.pumpportal_pct + fees.pumpfun_curve_pct


def simulate_buy(sol_in: float, price_sol: float, slippage_pct: float, fees: Fees) -> FillResult:
    """Buy with a budget of `sol_in` SOL (everything-in: fees come out of it)."""
    pct = _pct_fee(fees)
    flat = fees.priority_fee_sol
    notional = max(0.0, (sol_in - flat)) / (1.0 + pct)   # SOL actually swapped
    fee_sol = sol_in - notional - 0.0  # = notional*pct + flat (algebraically)
    eff_price = price_sol * (1.0 + slippage_pct)
    tokens = notional / eff_price if eff_price > 0 else 0.0
    return FillResult(tokens=tokens, sol=sol_in, fee_sol=fee_sol,
                      eff_price_sol=eff_price, slippage_pct=slippage_pct)


def simulate_sell(tokens: float, price_sol: float, slippage_pct: float, fees: Fees,
                  include_migration: bool = False) -> FillResult:
    """Sell `tokens`; returns NET SOL received after slippage + stacked fees."""
    pct = _pct_fee(fees)
    flat = fees.priority_fee_sol + (fees.migration_fee_sol if include_migration else 0.0)
    eff_price = price_sol * (1.0 - slippage_pct)
    gross = tokens * eff_price
    fee_sol = gross * pct + flat
    net = max(0.0, gross - fee_sol)
    return FillResult(tokens=tokens, sol=net, fee_sol=fee_sol,
                      eff_price_sol=eff_price, slippage_pct=slippage_pct)
