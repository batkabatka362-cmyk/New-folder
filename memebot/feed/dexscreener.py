"""DexScreener REST client — free, no API key. Enrichment + paper-fill pricing.

Endpoints used (data tier ~300 req/min):
  GET /token-pairs/v1/{chain}/{tokenAddress}   -> all pools for a token
  GET /tokens/v1/{chain}/{addr1,addr2,...}     -> pools for up to 30 tokens (batch)
  GET /latest/dex/search?q=...                 -> search

No official websocket -> we poll. Pre-migration pump.fun tokens may not appear
here yet; for those, price comes from the bonding curve / PumpPortal.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from ..utils.logging import get_logger
from ..utils.rate_limit import AsyncRateLimiter

log = get_logger("feed.dexscreener")
CHAIN = "solana"


@dataclass(slots=True)
class PairSnapshot:
    mint: str
    symbol: str
    pair_address: str
    dex_id: str
    price_usd: float
    price_native: float          # price in SOL
    liquidity_usd: float
    volume_h1: float
    volume_h24: float
    buys_h1: int
    sells_h1: int
    price_change_m5: float
    price_change_h1: float
    market_cap: float
    fdv: float
    pair_created_at: int
    buys_m5: int = 0             # P10b SR4: fresh 5-minute breadth (dataset-only, advisory)
    sells_m5: int = 0

    @property
    def buy_sell_ratio_h1(self) -> float:
        if self.sells_h1 == 0:
            return float(self.buys_h1) if self.buys_h1 else 0.0
        return self.buys_h1 / self.sells_h1

    @property
    def buy_sell_ratio_m5(self) -> float:
        if self.sells_m5 == 0:
            return float(self.buys_m5) if self.buys_m5 else 0.0
        return self.buys_m5 / self.sells_m5

    @property
    def vol_to_mcap_pct(self) -> float:
        return (self.volume_h1 / self.market_cap * 100.0) if self.market_cap > 0 else 0.0


def _to_pair(p: dict) -> PairSnapshot | None:
    base = p.get("baseToken") or {}
    txns_h1 = (p.get("txns") or {}).get("h1") or {}
    txns_m5 = (p.get("txns") or {}).get("m5") or {}          # P10b SR4: fresh short-window breadth
    vol = p.get("volume") or {}
    pc = p.get("priceChange") or {}
    liq = p.get("liquidity") or {}
    mint = base.get("address")
    if not mint:
        return None
    return PairSnapshot(
        mint=mint,
        symbol=base.get("symbol", ""),
        pair_address=p.get("pairAddress", ""),
        dex_id=p.get("dexId", ""),
        price_usd=float(p.get("priceUsd") or 0),
        price_native=float(p.get("priceNative") or 0),
        liquidity_usd=float(liq.get("usd") or 0),
        volume_h1=float(vol.get("h1") or 0),
        volume_h24=float(vol.get("h24") or 0),
        buys_h1=int(txns_h1.get("buys") or 0),
        sells_h1=int(txns_h1.get("sells") or 0),
        price_change_m5=float(pc.get("m5") or 0),
        price_change_h1=float(pc.get("h1") or 0),
        market_cap=float(p.get("marketCap") or 0),
        fdv=float(p.get("fdv") or 0),
        pair_created_at=int(p.get("pairCreatedAt") or 0),
        buys_m5=int(txns_m5.get("buys") or 0),
        sells_m5=int(txns_m5.get("sells") or 0),
    )


def _best(pairs: list[PairSnapshot]) -> PairSnapshot | None:
    """Most-liquid pool is the reference for price/size impact."""
    return max(pairs, key=lambda x: x.liquidity_usd, default=None)


class DexScreenerClient:
    def __init__(self, base_url: str, rate_per_min: float = 280.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._limiter = AsyncRateLimiter(rate_per_min)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "DexScreenerClient":
        self._client = httpx.AsyncClient(timeout=10.0, headers={"Accept": "application/json"})
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None):
        assert self._client is not None, "use 'async with DexScreenerClient(...)'"
        await self._limiter.acquire()
        try:
            r = await self._client.get(f"{self.base_url}{path}", params=params)
            if r.status_code == 429:
                # Cool down so the rest of the cycle doesn't cascade more 429s.
                retry = r.headers.get("Retry-After", "")
                cooldown = float(retry) if retry.isdigit() else 3.0
                log.warning("DexScreener 429 on %s — cooling down %.1fs", path, cooldown)
                await asyncio.sleep(cooldown)
                return None
            r.raise_for_status()
            return r.json()
        # P9-harden: r.json() decodes with stdlib json -> raises json.JSONDecodeError
        # (a ValueError, NOT an httpx.HTTPError) on a 200 with a non-JSON body
        # (Cloudflare/edge HTML interstitial, truncated body). Without ValueError here
        # that escapes _get and aborts the whole eval cycle. Degrade to None instead
        # (mint absent from `covered` -> retried next cycle).
        except (httpx.HTTPError, ValueError) as e:
            log.debug("DexScreener GET %s failed: %s", path, e)
            return None

    async def token_pairs(self, mint: str) -> list[PairSnapshot]:
        data = await self._get(f"/token-pairs/v1/{CHAIN}/{mint}")
        if not isinstance(data, list):
            return []
        return [p for p in (_to_pair(x) for x in data) if p]

    async def snapshot(self, mint: str) -> PairSnapshot | None:
        """Best (most-liquid) pool snapshot for a single mint."""
        return _best(await self.token_pairs(mint))

    async def snapshots(self, mints: list[str]) -> tuple[dict[str, PairSnapshot], set[str]]:
        """Batch up to 30 mints/call. Returns (best-pool-per-mint, covered) where
        `covered` = mints whose request chunk SUCCEEDED. This lets callers tell a
        fetch failure (mint absent from `covered`) from a genuinely unpriced token
        (in `covered` but absent from the dict) — critical so a flaky request isn't
        misread as dozens of rugs in the backtest/labeler."""
        out: dict[str, PairSnapshot] = {}
        covered: set[str] = set()
        for i in range(0, len(mints), 30):
            chunk = mints[i:i + 30]
            data = await self._get(f"/tokens/v1/{CHAIN}/{','.join(chunk)}")
            if not isinstance(data, list):
                continue   # chunk failed -> these mints are NOT covered (not "rugged")
            covered.update(chunk)
            by_mint: dict[str, list[PairSnapshot]] = {}
            for x in data:
                p = _to_pair(x)
                if p:
                    by_mint.setdefault(p.mint, []).append(p)
            for m, ps in by_mint.items():
                best = _best(ps)
                if best:
                    out[m] = best
        return out, covered
