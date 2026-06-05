"""Image scam-scorer (WL6, user insight) — judge a token's IMAGE the way a human trader does.

The user's edge: at LAUNCH they spot scams by the IMAGE / name / duplication — a signal no on-chain
number captures. Our market gates already filter the launch-time dupe spam (so name+symbol reuse does
NOT separate on the tokens we actually trade — we bleed on LOW-reuse mints a count can't flag), but a
scammy-LOOKING image (impersonation, low-effort, recycled, misleading) is exactly what a count misses
and a human eye catches. This sends a GATE-PASSED candidate's image to a vision model (local Ollama
llava = free, or a cloud vision endpoint) for a 0..1 scam score.

LOG-ONLY (the doctrine + the user's call: measure before any veto). The score rides the dataset as a
non-FEATURE_NAMES key (`image_scam_score`) onto the trade's entry features, so the image->outcome
separation can be calibrated forward from real labels — never a blind veto.

Defensive by construction: ANY failure (missing/unreachable uri, no image field, oversized image,
model down, unparsable reply, timeout) -> None, and it NEVER blocks a buy or raises into the pipeline.
Cached per mint (the image never changes). Cost-gated by the caller to gate-passed candidates only
(a few per minute), so even a paid cloud backend stays cheap.
"""
from __future__ import annotations

import base64
import json
import logging

log = logging.getLogger(__name__)

# Constrain the vision model to a parsable verdict (Ollama `format` = JSON schema / structured output).
_SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "scam_score": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["scam_score"],
}

_PROMPT = (
    "You are a Solana pump.fun memecoin risk screener judging ONLY the token's IMAGE (logo/art) for "
    "scam/rug likelihood — the way an experienced trader spots a fake at a glance. Rate scam_score from "
    "0.0 to 1.0:\n"
    "  ~0.0  original, effortful, coherent art with no deception\n"
    "  ~0.5  generic / low-effort / recycled-looking, ambiguous\n"
    "  ~1.0  obvious scam: impersonates a known brand/person/project or a major token's logo, a "
    "screenshot/QR/contract-address bait, misleading 'official' claims, or zero-effort copy\n"
    "Judge the IMAGE only (not price). Return JSON {\"scam_score\": <0..1>, \"reason\": \"<short>\"}."
)


def _clamp01(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:                       # NaN
        return None
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


def parse_score(reply: str):
    """Pull scam_score out of a vision reply (structured JSON, or the outermost {...} a chatty model
    wraps it in). Returns a clamped 0..1 float, or None when it can't be read (defensive)."""
    if not reply:
        return None
    try:
        obj = json.loads(reply)
    except (ValueError, TypeError):
        i, j = reply.find("{"), reply.rfind("}")
        if i < 0 or j <= i:
            return None
        try:
            obj = json.loads(reply[i:j + 1])
        except (ValueError, TypeError):
            return None
    if not isinstance(obj, dict):
        return None
    return _clamp01(obj.get("scam_score"))


def resolve_url(raw: str, gateway: str) -> str:
    """Map an ipfs:// (or bare-CID-ish) reference to an HTTP gateway URL; pass http(s) through."""
    u = (raw or "").strip()
    if not u:
        return ""
    if u.startswith("ipfs://"):
        return gateway.rstrip("/") + "/" + u[len("ipfs://"):].lstrip("/")
    return u


class ImageScamScorer:
    """Vision-model 0..1 scam score for a token's image. LOG-only; fully optional + defensive."""

    def __init__(self, *, enabled: bool, host: str, model: str, timeout_s: float = 30.0,
                 gateway: str = "https://ipfs.io/ipfs/", max_image_bytes: int = 5_000_000) -> None:
        self.enabled = bool(enabled) and bool(model)
        self.host = (host or "").rstrip("/")
        self.model = model
        self.timeout_s = float(timeout_s)
        self.gateway = gateway
        self.max_image_bytes = int(max_image_bytes)
        self._cache: dict[str, float | None] = {}
        self._client = None            # lazy httpx.AsyncClient (import-guarded; httpx is a runtime dep)

    def _http(self):
        if self._client is None:
            import httpx                # local import: keep module import dependency-free for the test runner
            self._client = httpx.AsyncClient(timeout=self.timeout_s, trust_env=False, follow_redirects=True)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def score(self, mint: str, uri: str):
        """0..1 scam score for the mint's image, or None. Cached per mint; never raises."""
        if not self.enabled or not mint or not uri:
            return None
        if mint in self._cache:
            return self._cache[mint]
        result = None
        try:
            result = await self._score(uri)
        except Exception as e:  # noqa: BLE001  — a screener must never break the buy path
            log.debug("image scam-score failed for %s: %s", mint[:8], e)
            result = None
        self._cache[mint] = result
        return result

    async def _score(self, uri: str):
        img_url = await self._image_url(uri)
        if not img_url:
            return None
        b64 = await self._image_b64(img_url)
        if not b64:
            return None
        return await self._vision(b64)

    async def _image_url(self, uri: str) -> str:
        """GET the metadata JSON the launch uri points at and return its `image` field (as a URL)."""
        r = await self._http().get(resolve_url(uri, self.gateway))
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            return ""
        return resolve_url(str(data.get("image") or ""), self.gateway)

    async def _image_b64(self, url: str):
        r = await self._http().get(resolve_url(url, self.gateway))
        r.raise_for_status()
        content = r.content
        if not content or len(content) > self.max_image_bytes:
            return None
        return base64.b64encode(content).decode("ascii")

    async def _vision(self, b64: str):
        """Ollama-compatible /api/generate vision call -> a clamped 0..1 scam score (or None)."""
        payload = {
            "model": self.model,
            "prompt": _PROMPT,
            "images": [b64],
            "stream": False,
            "format": _SCORE_SCHEMA,
            "options": {"temperature": 0.0},
        }
        r = await self._http().post(f"{self.host}/api/generate", json=payload)
        r.raise_for_status()
        body = r.json()
        return parse_score(body.get("response", "") if isinstance(body, dict) else "")
