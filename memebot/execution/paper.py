"""PaperBackend — simulated fills with realistic slippage + stacked fees.

No keypair, no signing, no broadcast. Fills are priced against the real
market (PriceSource) so paper PnL approximates what a live order would get.
Portfolio bookkeeping lives in portfolio/ — this backend only produces Fills.
"""
from __future__ import annotations

from ..config import Fees
from ..utils.logging import get_logger
from ..utils.money import estimate_slippage_pct, simulate_buy, simulate_sell
from .base import ExecutionBackend, Fill
from .pricing import PriceSource

log = get_logger("execution.paper")


class PaperBackend(ExecutionBackend):
    name = "paper"

    def __init__(self, prices: PriceSource, fees: Fees, max_slippage_pct: float = 0.15) -> None:
        self.prices = prices
        self.fees = fees
        self.max_slippage_pct = max_slippage_pct

    async def get_price_sol(self, mint: str) -> float | None:
        q = await self.prices.quote(mint)
        return q.price_sol if q else None

    async def buy(self, mint: str, sol_in: float) -> Fill:
        q = await self.prices.quote(mint)
        if q is None or q.price_sol <= 0:
            return Fill(mint, "buy", 0.0, 0.0, 0.0, 0.0, 0.0, ok=False, reason="no_price")
        slip = estimate_slippage_pct(sol_in, q.liquidity_sol, self.fees.base_slippage_pct)
        if slip > self.max_slippage_pct:
            return Fill(mint, "buy", 0.0, 0.0, q.price_sol, 0.0, slip, ok=False,
                        reason=f"slippage_too_high({slip:.2%})")
        r = simulate_buy(sol_in, q.price_sol, slip, self.fees)
        return Fill(mint, "buy", r.tokens, r.sol, r.eff_price_sol, r.fee_sol, r.slippage_pct,
                    reason=q.source)

    async def sell(self, mint: str, tokens: float, *, include_migration: bool = False) -> Fill:
        q = await self.prices.quote(mint)
        if q is None or q.price_sol <= 0:
            return Fill(mint, "sell", 0.0, 0.0, 0.0, 0.0, 0.0, ok=False, reason="no_price")
        # sell size in SOL terms for slippage estimate
        trade_sol = tokens * q.price_sol
        slip = estimate_slippage_pct(trade_sol, q.liquidity_sol, self.fees.base_slippage_pct)
        r = simulate_sell(tokens, q.price_sol, slip, self.fees, include_migration=include_migration)
        return Fill(mint, "sell", r.tokens, r.sol, r.eff_price_sol, r.fee_sol, r.slippage_pct,
                    reason=q.source)
