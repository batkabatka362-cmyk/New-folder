"""Solana Tracker risk/holder cross-check (WL14) — the Axiom-style filter DATA we can't compute on
free on-chain data.

The literature's #1 detectable rug tell is HOLDER CONCENTRATION + coordinated/bundled wallets (snipers,
insiders) — and the pro Axiom/GMGN traders filter on exactly those. We cannot derive bundle/sniper/
insider percentages from the free PumpPortal WS + public RPC (they need trade-tape/fund-flow analysis).
Solana Tracker's Data API DOES return them: `GET /tokens/{mint}` carries a `risk` object with `rugged`,
a 1-10 `score`, `snipers`, `insiders`, `top10` concentration, and a `risks[]` list of {name, level}.

Free tier ~2,500 req/month at 3 req/s, so this is COST-GATED to actual buy candidates only (a few/day)
and cached per mint. Needs a free `x-api-key` (SOLANATRACKER_API_KEY); OFF without it. Last-line +
paper-safe: degrades to None (= "no opinion") on any error/timeout/non-200/unparseable body, so it can
NEVER block a buy on its own failure — only on a positively-returned high-risk/rugged verdict.
"""
from __future__ import annotations

import httpx

from ..utils.logging import get_logger

log = get_logger("feed.solanatracker")


def _pct(v):
    """Pull a percentage out of a field that may be a number or an object like {totalPercentage: ...}."""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for k in ("totalPercentage", "percentage", "total", "count"):
            x = v.get(k)
            if isinstance(x, (int, float)):
                return float(x)
    return None


class SolanaTrackerClient:
    def __init__(self, api_key: str, base_url: str = "https://data.solanatracker.io",
                 timeout: float = 6.0) -> None:
        self.api_key = api_key or ""
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "SolanaTrackerClient":
        self._client = httpx.AsyncClient(timeout=self.timeout, trust_env=False,
                                         headers={"x-api-key": self.api_key} if self.api_key else {})
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def risk(self, mint: str) -> dict | None:
        """{'rugged', 'score', 'top10', 'snipers_pct', 'insiders_pct', 'danger', 'risks':[names]} or None
        on any failure / missing key. `danger` = rugged OR any danger-level risk in the report."""
        if self._client is None or not self.api_key or not mint:
            return None
        try:
            r = await self._client.get(f"{self.base_url}/tokens/{mint}")
            if r.status_code != 200:
                return None
            return self._parse(r.json())
        except Exception as e:  # noqa: BLE001 — TOTAL degrade-to-None: an external risk service must
            log.debug("solanatracker risk failed: %s", type(e).__name__)  # NEVER strangle the bot
            return None

    @staticmethod
    def _parse(data) -> dict | None:
        """Defensive parse of the /tokens/{mint} `risk` object — tolerant of schema drift. None when
        there's no usable risk block."""
        if not isinstance(data, dict):
            return None
        risk = data.get("risk")
        if not isinstance(risk, dict):
            return None
        names, danger = [], bool(risk.get("rugged"))
        for rk in (risk.get("risks") if isinstance(risk.get("risks"), list) else []):
            if not isinstance(rk, dict):
                continue
            names.append(str(rk.get("name", "")))
            if str(rk.get("level", "")).lower() in ("danger", "high"):
                danger = True
        score = risk.get("score")
        return {
            "rugged": bool(risk.get("rugged")),
            "score": float(score) if isinstance(score, (int, float)) else None,   # 1-10 (higher = riskier)
            "top10": _pct(risk.get("top10")),
            "snipers_pct": _pct(risk.get("snipers")),
            "insiders_pct": _pct(risk.get("insiders")),
            "bundlers_pct": _pct(risk.get("bundlers")),   # coordinated multi-wallet supply % — literature's #1 rug tell
            "dev_pct": _pct(risk.get("dev")),             # dev/creator current holding %
            "danger": danger,
            "risks": names,
        }
