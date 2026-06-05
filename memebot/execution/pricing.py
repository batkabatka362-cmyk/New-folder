"""Price + liquidity source used for paper fills (and, later, live sizing).

Resolution order:
  - migrated / listed tokens  -> DexScreener best pool (price_native = SOL price)
  - pre-migration tokens      -> bonding-curve price from TokenState (trade-derived)
Liquidity is returned in SOL so the slippage model can estimate price impact.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..data.token_state import TokenRegistry
from ..feed.dexscreener import DexScreenerClient
from ..utils.logging import get_logger

log = get_logger("execution.pricing")


@dataclass(slots=True)
class Quote:
    mint: str
    price_sol: float
    liquidity_sol: float
    price_usd: float
    source: str


class PriceSource:
    def __init__(self, registry: TokenRegistry, dex: DexScreenerClient | None = None) -> None:
        self.registry = registry
        self.dex = dex

    async def quote(self, mint: str) -> Quote | None:
        st = self.registry.get(mint)

        # Post-migration / listed: DexScreener is authoritative.
        if self.dex is not None and (st is None or st.migrated):
            snap = await self.dex.snapshot(mint)
            if snap and snap.price_native > 0:
                sol_usd = (snap.price_usd / snap.price_native) if snap.price_native else 0.0
                liq_sol = (snap.liquidity_usd / sol_usd) if sol_usd > 0 else 0.0
                return Quote(mint, snap.price_native, liq_sol, snap.price_usd, "dexscreener")

        # Pre-migration: bonding-curve price from observed trades.
        if st is not None and st.last_price_sol > 0:
            liq_sol = st.v_sol_in_curve or 0.0
            return Quote(mint, st.last_price_sol, liq_sol, 0.0, "curve")

        # Last resort: try DexScreener even if we thought it was pre-migration.
        if self.dex is not None:
            snap = await self.dex.snapshot(mint)
            if snap and snap.price_native > 0:
                sol_usd = (snap.price_usd / snap.price_native) if snap.price_native else 0.0
                liq_sol = (snap.liquidity_usd / sol_usd) if sol_usd > 0 else 0.0
                return Quote(mint, snap.price_native, liq_sol, snap.price_usd, "dexscreener")
        return None
