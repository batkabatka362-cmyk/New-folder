"""The swing universe — established, currently-liquid Solana memecoins to mean-revert.

v1 is a curated list of liquid survivors (the WL17 backtest set, minus the faded stress-test names that
were only there to prove survivorship-robustness — we do NOT want to live-trade tokens that are bleeding to
zero). A per-token liquidity sanity check (Solana Tracker) prunes any that have since gone illiquid, so a
faded name silently drops out instead of being dip-bought into oblivion. A fully dynamic top-by-liquidity
universe is the v2 upgrade; for forward-testing the edge a vetted list is honest and safe.
"""
from __future__ import annotations

from ..utils.logging import get_logger

log = get_logger("swing.universe")

# Currently-liquid, established Solana memecoins (mints verified against the Solana Tracker /chart API).
DEFAULT_UNIVERSE = {
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "POPCAT": "7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr",
    "MEW": "MEW1gQWJ3nEXg2qgERiKu7FAFj79PHvQVREQUzScPP5",
    "GIGA": "63LfDmNb3MQ8mw9MtZ2To9bEA2M71kZUUGq5tiJxcqj9",
    "PNUT": "2qEHjDLDLbuBgRYvsxhc5D6uDWAivNFZGan56P1tpump",
    "FARTCOIN": "9BB6NFEcjBCtnNLFko2FqVQBq8HHM13kCyYcdQbgpump",
    "TRUMP": "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN",
    # WL21: more verified currently-liquid established names (>$250K LP) to widen entry opportunities for
    # the forward test — the deep-dip entry is selective, so more tokens = more chances for the edge to fire.
    "GOAT": "CzLSujWBLFsSjncfkh59rUFqvafWcY5tzedWJSuypump",
    "PENGU": "2zMMhcVQEXDtdE6vsFS7S7D5oUodfJHE8vd1gnBouauv",
    "ZEREBRO": "8x5VqbHA8D7NkD52uNuS5nnt3PwA8pLD34ymskeSo2Wn",
    "ARC": "61V8vBaqAGMpgDQi4JcAwo1dmBGHsyhzodcPqnEVpump",
    "GRASS": "Grass7B4RdKfBCjTKgSqnXkqjwiGvQyFbuSCUJr3XXjs",
    "SPX": "J3NKxxXZcnNiMjKw9hYb2K4LUxgwB6t1FtPtQVsv3KFr",
    # WL30: 8 more verified currently-liquid established names (all >$280K LP, 2000 4h bars) to ACCELERATE
    # the forward-proof — the selective ~18% dip fires rarely, so 22 tokens ~= 1.5x the entry opportunities
    # of 14. The runtime liquid_universe prune + the chart-empty skip drop any that later go illiquid/dead.
    "MOODENG": "ED5nyyWEzpPPiWimP8vYm7sD7TD3LAt3Q3gRTWHzPJBY",
    "RETARDIO": "6ogzHhzdrQr9Pgv6hZ2MNze7UrzBMAFyBBWUYp1Fhitx",
    "BILLY": "3B5wuUrMEi5yATD7on46hKfej3pfmd7t1RKgrsN3pump",
    "MANEKI": "25hAyBQfoDhfWx9ay6rarbgvWGwDdNqcHsXS3jQ3mTDJ",
    "DADDY": "4Cnk9EPnW5ixfLZatCPJjDB1PUtcRpVVgTQukm9epump",
    "VINE": "6AJcP7wuLwmRYLBNbi825wgguaPsWzPBEHcHndpRpump",
    "GME": "8wXtPeU6557ETkp9WHFY1n1EcU6NxDvbAggHGsMYiHsB",
    "CHILLHOUSE": "DitHyRMQiSDhn5cnKMJV2CDDt6sVct96YrECiM49pump",
}


async def liquid_universe(client, base: dict | None = None, min_liquidity_usd: float = 50_000.0) -> dict:
    """Prune the curated universe to tokens whose current pool liquidity clears `min_liquidity_usd`
    (so a faded name that's gone illiquid drops out). Uses the Solana Tracker /tokens risk endpoint's
    pool data; on any error keeps the token (fail-open — a data hiccup must not empty the universe).
    Returns {symbol: mint}."""
    base = base or DEFAULT_UNIVERSE
    if client is None:
        return dict(base)
    kept = {}
    for sym, mint in base.items():
        try:
            r = await client._client.get(f"{client.base_url}/tokens/{mint}")
            liq = None
            if r.status_code == 200:
                pools = r.json().get("pools") or []
                if pools and isinstance(pools[0], dict):
                    liq = (pools[0].get("liquidity") or {}).get("usd")
            if liq is None or float(liq) >= min_liquidity_usd:
                kept[sym] = mint
            else:
                log.info("swing universe: dropping %s (liquidity $%.0f < $%.0f)", sym, float(liq), min_liquidity_usd)
        except Exception as e:  # noqa: BLE001 — fail-open: keep the token if the check errors
            log.debug("liquidity check failed for %s: %s", sym, type(e).__name__)
            kept[sym] = mint
    return kept
