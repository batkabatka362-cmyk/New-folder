"""Local LLM backend via Ollama — free, offline, no API key.

Same interface as AnthropicLLM (decide/reflect) so the brain is agnostic.
Uses Ollama's /api/chat with structured output (`format` = our JSON schema),
so the model is constrained to emit a valid Verdict / ReflectionNote.

No prompt caching and no Haiku/Sonnet routing here — one local model, and it's
free, so `supports_escalation = False` (the brain skips the Sonnet re-ask).
"""
from __future__ import annotations

import json

import httpx

from ..config import Settings
from ..utils.logging import get_logger
from .schema import REFLECTION_SCHEMA, VERDICT_SCHEMA

log = get_logger("agent.local_llm")


def _loads(text: str) -> dict | None:
    """Parse JSON, tolerating any stray prose around the object (e.g. a
    thinking model's preamble) by falling back to the outermost {...}."""
    text = (text or "").strip()
    if not text:
        return None
    # P9-harden: only an object is a usable reply. A model that ignores `format` can
    # emit valid-but-non-object JSON (a list/scalar); returning that raw would make
    # Verdict.from_dict's d.get(...) raise AttributeError and bypass the rule-fallback.
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        i, j = text.find("{"), text.rfind("}")
        if 0 <= i < j:
            try:
                obj = json.loads(text[i:j + 1])
                return obj if isinstance(obj, dict) else None
            except json.JSONDecodeError:
                return None
        return None


class OllamaLLM:
    name = "ollama"
    supports_escalation = False
    # embedding-only models can't serve /api/chat — never pick one as a fallback
    _EMBED_HINTS = ("embed", "bge", "minilm", "arctic")

    def __init__(self, host: str, model: str, timeout: float = 60.0, max_tokens: int = 512) -> None:
        self.host = host.rstrip("/")
        self.model = model
        self.name = f"ollama:{model}"
        self.base_source = self.name        # source label for verdicts (no escalation tier on a single model)
        self.max_tokens = max(256, int(max_tokens))   # floor so the JSON verdict fits
        self._timeout = timeout
        # P9-harden: open the client LAZILY (on first chat), not in __init__. DualLocalLLM.create
        # builds a `deep` instance to probe its resolved model and discards it when it collapses
        # to the fast model; an eager client there would leak (never aclose()d). A discarded
        # instance that never chats now never opens a client.
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        # localhost Ollama must never be routed through an ambient HTTP(S)_PROXY
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.host, timeout=self._timeout, trust_env=False)
        return self._client

    @classmethod
    def create(cls, settings: Settings) -> "OllamaLLM | None":
        return cls._create_named(settings, settings.ollama_model)

    @classmethod
    def _create_named(cls, settings: Settings, want: str, *, strict: bool = False) -> "OllamaLLM | None":
        """Resolve `want` against the models Ollama actually has pulled.

        strict=False (PRIMARY model): legacy behavior — exact match, else a same-family model,
        else the first chat model. strict=True (OPTIONAL deep confirm model): return None when
        `want` (exact or same-family) is not pulled, so a missing confirm model degrades to a
        single-model brain instead of silently grabbing an unrelated chat model.
        """
        host = settings.ollama_host.rstrip("/")
        try:
            with httpx.Client(trust_env=False, timeout=3.0) as c:   # ignore env proxies for localhost
                r = c.get(f"{host}/api/tags")
                r.raise_for_status()
                models = [m.get("name", "") for m in (r.json().get("models") or [])]
        except (httpx.HTTPError, ValueError):
            log.info("Ollama not reachable at %s -> local LLM unavailable", host)
            return None
        chat = [m for m in models if not any(k in m.lower() for k in cls._EMBED_HINTS)]
        if not chat:
            log.warning("Ollama has no chat-capable model pulled (e.g. `ollama pull gemma3:4b`)")
            return None
        if want in chat:
            model = want
        else:
            # accept a same-family match (e.g. "gemma3:4b" wanted, "gemma3:latest" present)
            family = want.split(":")[0]
            match = next((m for m in chat if m.split(":")[0] == family), None)
            if match is None:
                if strict:
                    log.info("Ollama model '%s' (and its family) not pulled -> not used", want)
                    return None
                match = chat[0]
            model = match
            log.info("Ollama model '%s' not found; using '%s' (chat models: %s)", want, model, chat)
        log.info("local LLM enabled via Ollama (model=%s)", model)
        return cls(host, model, settings.llm_timeout_s, settings.llm_max_tokens)

    async def _chat(self, constitution: str, user_text: str, schema: dict) -> dict | None:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": constitution},
                {"role": "user", "content": user_text},
            ],
            "stream": False,
            "format": schema,                       # structured output (JSON schema)
            "options": {"temperature": 0.2, "num_predict": self.max_tokens},
        }
        r = await self._get_client().post("/api/chat", json=payload)
        r.raise_for_status()
        j = r.json() or {}
        if j.get("done_reason") == "length":        # truncated mid-JSON -> _loads will fail
            log.warning("ollama output truncated at num_predict=%s (raise LLM_MAX_TOKENS)", self.max_tokens)
        content = (j.get("message") or {}).get("content", "")
        return _loads(content)

    async def decide(self, constitution: str, user_text: str, *, escalate: bool = False) -> dict | None:
        try:
            return await self._chat(constitution, user_text, VERDICT_SCHEMA)
        except Exception as e:  # noqa: BLE001 — never let an LLM error reach the loop
            log.warning("ollama decide error: %s", e)
            return None

    async def reflect(self, constitution: str, user_text: str) -> dict | None:
        try:
            return await self._chat(constitution, user_text, REFLECTION_SCHEMA)
        except Exception as e:  # noqa: BLE001
            log.warning("ollama reflect error: %s", e)
            return None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()


class DualLocalLLM:
    """Two local Ollama models as a fast-screen + deep-confirm cascade — the LOCAL analogue of the
    cloud Haiku->Sonnet routing, so the brain code path is identical (supports_escalation=True).

    A FAST screener (e.g. gemma3:4b) runs on every decide(); a DEEPER model (e.g. qwen3:8b) is
    consulted ONLY when the brain escalates a low-conviction buy (gated by the per-minute budget),
    and for reflection (quality path, off the hot path). The eval loop's parallelism (llm_parallel)
    is what raises throughput; the deep model only sharpens marginal buys, it does not add speed.

    Composes two OllamaLLM instances (reuse, not a rewrite). create() degrades GRACEFULLY to a bare
    single OllamaLLM whenever the confirm model is empty / unpulled / identical to the fast model.
    """

    name = "ollama-dual"
    supports_escalation = True

    def __init__(self, fast: "OllamaLLM", deep: "OllamaLLM") -> None:
        self.fast = fast
        self.deep = deep
        self.name = f"ollama-dual:{fast.model}+{deep.model}"
        self.base_source = fast.name        # fast-screen verdicts labelled by the fast model
        self.escalated_source = deep.name   # deep-confirm verdicts labelled by the deep model

    @classmethod
    def create(cls, settings: Settings) -> "OllamaLLM | DualLocalLLM | None":
        fast = OllamaLLM.create(settings)                 # primary (legacy family-match) resolution
        if fast is None:
            return None                                   # Ollama unreachable / no chat model
        confirm = (settings.ollama_confirm_model or "").strip()
        if not confirm:
            return fast                                   # single-model local brain (no escalation)
        deep = OllamaLLM._create_named(settings, confirm, strict=True)
        if deep is None:
            log.info("confirm model '%s' not pulled -> single-model local brain", confirm)
            return fast                                   # graceful fallback
        if deep.model == fast.model:
            # distinct config names can resolve to the SAME pulled model (Ollama family match),
            # which config.validate() can't see — warn so the silent dual->single downgrade is visible.
            log.warning("confirm model '%s' resolved to the same Ollama model as fast (%s) -> dual "
                        "escalation is OFF; pull a distinct deeper model", confirm, fast.model)
            return fast
        log.info("dual local brain: fast=%s confirm=%s", fast.model, deep.model)
        return cls(fast, deep)

    async def decide(self, constitution: str, user_text: str, *, escalate: bool = False) -> dict | None:
        llm = self.deep if escalate else self.fast
        return await llm.decide(constitution, user_text, escalate=False)

    async def reflect(self, constitution: str, user_text: str) -> dict | None:
        return await self.deep.reflect(constitution, user_text)   # quality path -> deeper model

    async def aclose(self) -> None:
        await self.fast.aclose()
        await self.deep.aclose()
