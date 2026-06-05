"""RugCheck.xyz risk cross-check (B1) — a free, independent token-risk second opinion before a buy.

RugCheck aggregates mint/freeze authority, LP status, holder concentration, and a proprietary
scam-pattern database into a single risk report. We already compute most of those ourselves, so this
is a CROSS-CHECK: a second opinion that can catch edge cases / novel scam patterns our own gate
misses. Read-only public summary endpoint — no key, spends nothing.

Last-line + paper-safe: run right before a (paper) buy, only on actual buy candidates (cheap). It
degrades to None (= "no opinion") on any error, timeout, non-200, or unparseable body, so it can NEVER
block a buy on its OWN failure — only on a positively-returned RugCheck 'danger' verdict. An external
service must not silently strangle the bot, so failure is always a no-op.
"""
from __future__ import annotations

import httpx

from ..utils.logging import get_logger

log = get_logger("feed.rugcheck")


class RugCheckClient:
    def __init__(self, base_url: str = "https://api.rugcheck.xyz", timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "RugCheckClient":
        self._client = httpx.AsyncClient(timeout=self.timeout, trust_env=False)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def report(self, mint: str) -> dict | None:
        """{'danger': bool, 'risks': [names], 'score': float|None} or None on any failure / no opinion."""
        if self._client is None:
            return None
        try:
            r = await self._client.get(f"{self.base_url}/v1/tokens/{mint}/report/summary")
            if r.status_code != 200:
                return None
            return self._parse(r.json())
        except Exception as e:  # noqa: BLE001 — TOTAL degrade-to-None: an external risk service must
            log.debug("rugcheck report failed: %s", type(e).__name__)  # NEVER strangle the bot on its own failure
            return None

    @staticmethod
    def _parse(data) -> dict | None:
        """Defensive parse — tolerant of schema drift. RugCheck returns a `risks` list of
        {name, level (danger|warn|info), ...} plus a score; we trust the explicit 'danger' LEVEL
        as the veto signal (score semantics have shifted across their API versions, so it stays
        advisory only). None when the body isn't a usable report."""
        if not isinstance(data, dict):
            return None
        risks = data.get("risks")
        risks = risks if isinstance(risks, list) else []
        names, danger = [], False
        for rk in risks:
            if not isinstance(rk, dict):
                continue
            names.append(str(rk.get("name", "")))
            if str(rk.get("level", "")).lower() == "danger":
                danger = True
        score = data.get("score_normalised", data.get("score"))
        return {
            "danger": danger,
            "risks": names,
            "score": float(score) if isinstance(score, (int, float)) else None,
        }
