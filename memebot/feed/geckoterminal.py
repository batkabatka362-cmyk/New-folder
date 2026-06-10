"""GeckoTerminal (by CoinGecko) — KEYLESS OHLCV for the swing forward-test (WL23).

The swing runner polls its universe every scan; on the Solana Tracker free tier (~2,500 req/mo) that
budget is exhausted in days (14 tokens x 48 scans/day ~= 20k/mo). GeckoTerminal's public API is FREE, needs
NO KEY, and allows ~30 calls/min — the right source for the high-frequency OHLCV polling. We keep Solana
Tracker for the lower-frequency universe-liquidity check + the weekly discovery + the sniper rugged-veto.

Endpoints (https://api.geckoterminal.com/api/v2):
- GET /networks/solana/tokens/{mint}/pools          -> the token's pools (we take the top one, cached)
- GET /networks/solana/pools/{pool}/ohlcv/{tf}?aggregate=N  -> [[ts,o,h,l,c,v], ...]
Defensive: any failure returns [] (the runner treats empty as 'skip this token this scan').
"""
from __future__ import annotations

import httpx

from ..utils.logging import get_logger

log = get_logger("feed.geckoterminal")

# our interval -> (GeckoTerminal timeframe, aggregate)
_TF = {"1h": ("hour", 1), "4h": ("hour", 4), "1d": ("day", 1), "15m": ("minute", 15), "1m": ("minute", 1)}


class GeckoTerminalClient:
    def __init__(self, base_url: str = "https://api.geckoterminal.com/api/v2", timeout: float = 12.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._pool: dict[str, str] = {}        # mint -> top pool address (stable, cached for the process)

    async def __aenter__(self) -> "GeckoTerminalClient":
        self._client = httpx.AsyncClient(timeout=self.timeout, trust_env=False,
                                         headers={"accept": "application/json"})
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def pool_for(self, mint: str) -> str | None:
        """The token's top pool address (cached). None on failure."""
        if mint in self._pool:
            return self._pool[mint]
        if self._client is None or not mint:
            return None
        try:
            r = await self._client.get(f"{self.base_url}/networks/solana/tokens/{mint}/pools")
            if r.status_code != 200:
                return None
            data = r.json().get("data") or []
            if not data:
                return None
            addr = (data[0].get("attributes") or {}).get("address")
            if addr:
                self._pool[mint] = addr
            return addr
        except Exception as e:  # noqa: BLE001
            log.debug("geckoterminal pool_for failed: %s", type(e).__name__)
            return None

    async def chart(self, mint: str, interval: str = "4h") -> list[dict]:
        """OHLCV bars for a token, ASCENDING by time (latest last), in the swing engine's dict shape.
        Empty list on any failure."""
        if self._client is None or not mint:
            return []
        pool = await self.pool_for(mint)
        if not pool:
            return []
        tf, agg = _TF.get(interval, ("hour", 4))
        try:
            r = await self._client.get(f"{self.base_url}/networks/solana/pools/{pool}/ohlcv/{tf}",
                                       params={"aggregate": agg, "limit": 1000})
            if r.status_code != 200:
                return []
            rows = (((r.json().get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
            bars = []
            for row in rows:
                if isinstance(row, list) and len(row) >= 5 and row[4]:
                    bars.append({"time": float(row[0]), "open": float(row[1]), "high": float(row[2]),
                                 "low": float(row[3]), "close": float(row[4]),
                                 "volume": float(row[5]) if len(row) > 5 else 0.0})
            bars.sort(key=lambda b: b["time"])     # GeckoTerminal returns newest-first; the engine needs ascending
            return bars
        except Exception as e:  # noqa: BLE001
            log.debug("geckoterminal chart failed: %s", type(e).__name__)
            return []
