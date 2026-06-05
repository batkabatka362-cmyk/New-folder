"""Shared dataclasses passed between layers (feed -> filter -> decision -> exec)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .constants import MODE_SCALP


@dataclass(slots=True)
class NewTokenEvent:
    """A freshly created pump.fun token (PumpPortal `subscribeNewToken`)."""

    mint: str
    name: str = ""
    symbol: str = ""
    creator: str = ""
    uri: str = ""
    pool: str = ""
    bonding_curve_key: str = ""
    market_cap_sol: float = 0.0
    v_sol_in_curve: float = 0.0
    v_tokens_in_curve: float = 0.0
    initial_buy_sol: float = 0.0
    ts: float = field(default_factory=time.time)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TradeEvent:
    """A single buy/sell on a token (PumpPortal `subscribeTokenTrade`)."""

    mint: str
    trader: str
    side: str                 # SIDE_BUY | SIDE_SELL
    sol_amount: float = 0.0
    token_amount: float = 0.0
    market_cap_sol: float = 0.0
    new_token_balance: float = 0.0
    ts: float = field(default_factory=time.time)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MigrationEvent:
    """A token graduated off the bonding curve to a DEX pool."""

    mint: str
    pool: str = ""
    ts: float = field(default_factory=time.time)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Candidate:
    """A token under consideration: features + scores flow through the cascade."""

    mint: str
    symbol: str = ""
    mode: str = MODE_SCALP            # MODE_SCALP | MODE_HOLD
    creator: str = ""                 # N9: creator wallet -> correlated-cohort exposure cap
    # market snapshot (from DexScreener / curve)
    price_usd: float = 0.0
    price_sol: float = 0.0
    liquidity_usd: float = 0.0
    market_cap_usd: float = 0.0
    vol_to_mcap_pct: float = 0.0
    buy_sell_ratio: float = 0.0
    unique_buyers: int = 0
    price_change_m5: float = 0.0      # DexScreener 5-min price change % (short-term momentum/trend)
    price_change_h1: float = 0.0      # DexScreener 1-hour price change % (medium-term trend)
    # safety snapshot (from Helius/RPC; None == not yet checked)
    mint_revoked: Optional[bool] = None
    freeze_revoked: Optional[bool] = None
    lp_burned_pct: Optional[float] = None
    top5_concentration_pct: Optional[float] = None
    token2022_risk: Optional[str] = None   # A1: a dangerous Token-2022 extension (hook/fee/delegate/frozen) -> hard veto; None = none detected / not token-2022 / unknown
    # scoring
    rule_passed: bool = False
    rule_reasons: list[str] = field(default_factory=list)
    score: float = 0.0                # 0..1 momentum/quality
    needs_llm: bool = False           # escalate to Claude (Phase 3)
    ts: float = field(default_factory=time.time)
    features: dict[str, Any] = field(default_factory=dict)
