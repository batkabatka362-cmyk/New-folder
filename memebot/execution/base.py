"""Execution backend interface + Fill result.

The whole point: strategy code talks ONLY to ExecutionBackend. Switching from
paper to live = swapping the backend (PaperBackend -> JupiterBackend /
PumpPortalBackend in Phase 5), with no change to strategy/risk code.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(slots=True)
class Fill:
    mint: str
    side: str                # "buy" | "sell"
    tokens: float
    sol: float               # buy: SOL spent (incl. fees); sell: net SOL received
    price_sol: float         # effective per-token fill price
    fee_sol: float
    slippage_pct: float
    ok: bool = True
    reason: str = ""
    ts: float = field(default_factory=time.time)


class ExecutionBackend(ABC):
    name: str = "base"

    @abstractmethod
    async def get_price_sol(self, mint: str) -> float | None:
        ...

    @abstractmethod
    async def buy(self, mint: str, sol_in: float) -> Fill:
        ...

    @abstractmethod
    async def sell(self, mint: str, tokens: float, *, include_migration: bool = False) -> Fill:
        ...
