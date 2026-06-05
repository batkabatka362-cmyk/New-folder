"""Jupiter quote-based honeypot check (A2) — a QUOTE-ONLY sell-route test, NO real money.

A honeypot lets you BUY but not SELL (or taxes the sell to death). We detect it WITHOUT executing any
trade: fetch a BUY quote (SOL -> token) and a SELL quote (token -> SOL). If the buy routes but the sell
does NOT, or the instant round-trip implies an extreme tax, the token is a honeypot / high-tax trap ->
veto the buy.

Quote-only & paper-safe: this calls Jupiter's QUOTE API (read-only price routing), never the swap/
execute API. It needs no key and spends nothing. It degrades to None (= "no detected honeypot") on any
error, timeout, or unroutable token, so it can NEVER block a buy on its own failure (only on a
positively-detected honeypot signal). A token Jupiter can't even BUY yet (too fresh / not aggregated)
is treated as "can't assess" -> None, NOT a false honeypot veto.
"""
from __future__ import annotations

import httpx

from ..utils.logging import get_logger

log = get_logger("feed.jupiter")

SOL_MINT = "So11111111111111111111111111111111111111112"


class JupiterClient:
    def __init__(self, base_url: str = "https://quote-api.jup.ag", timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "JupiterClient":
        self._client = httpx.AsyncClient(timeout=self.timeout, trust_env=False)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int = 1500) -> dict | None:
        """One Jupiter quote (read-only). None on any failure -> the caller degrades to 'unknown'."""
        if self._client is None:
            return None
        try:
            r = await self._client.get(
                f"{self.base_url}/v6/quote",
                params={"inputMint": input_mint, "outputMint": output_mint,
                        "amount": int(amount), "slippageBps": slippage_bps},
            )
            if r.status_code != 200:
                return None
            return r.json()
        except Exception as e:  # noqa: BLE001 — TOTAL degrade-to-None: an external quote service must
            log.debug("jupiter quote failed: %s", type(e).__name__)   # NEVER break the bot on its own failure
            return None

    async def honeypot_check(self, mint: str, *, sol_amount: float = 0.05,
                             min_roundtrip_keep: float = 0.5) -> str | None:
        """A2: a veto reason (str) if `mint` looks like a honeypot, else None.

        None covers BOTH "safe" and "can't assess" (unroutable / API down) — the safe default for a
        check that must never block a buy on its own failure. Veto only on a CLEAR signal: a token that
        BUYS but won't SELL, or whose instant round-trip recovers < `min_roundtrip_keep` of the SOL in
        (an extreme stealth tax)."""
        lamports = max(1, int(sol_amount * 1e9))
        buy = await self._quote(SOL_MINT, mint, lamports)
        try:
            tokens = int((buy or {}).get("outAmount") or 0)
        except (TypeError, ValueError):
            tokens = 0
        if tokens <= 0:
            return None                       # can't even buy via Jupiter yet -> can't assess, NOT a veto
        sell = await self._quote(mint, SOL_MINT, tokens)
        try:
            sol_back = int((sell or {}).get("outAmount") or 0)
        except (TypeError, ValueError):
            sol_back = 0
        if not sell or sol_back <= 0:
            return "honeypot_no_sell_route"   # buys but won't sell = classic honeypot
        keep = sol_back / lamports            # fraction of SOL recovered on an instant round-trip
        if keep < min_roundtrip_keep:
            return f"honeypot_high_tax(keep={keep:.2f})"
        return None
