"""Immutable protocol / chain constants (not user-tunable)."""
from __future__ import annotations

# pump.fun on-chain program (mainnet). Official integration is on-chain only;
# there is no official Python REST/trade API — we use PumpPortal for the feed.
PUMP_FUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Solana well-knowns
WSOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL = 1_000_000_000
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# Addresses to EXCLUDE from holder-concentration math (or safe tokens look
# falsely whale-heavy). Extend with known CEX hot wallets as you find them.
BURN_ADDRESSES = frozenset(
    {
        "1nc1nerator11111111111111111111111111111111",  # incinerator
        "11111111111111111111111111111111",             # system / null-ish
    }
)

# Event type tags emitted by the feed normalizer.
EV_NEW_TOKEN = "new_token"
EV_TRADE = "trade"
EV_MIGRATION = "migration"

# Trade sides
SIDE_BUY = "buy"
SIDE_SELL = "sell"

# Trade modes
MODE_SCALP = "scalp"
MODE_HOLD = "hold"
