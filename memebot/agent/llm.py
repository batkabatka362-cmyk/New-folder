"""Anthropic SDK wrapper for the brain.

- AsyncAnthropic, structured output via `output_config.format` (no prefill).
- cache_control on the frozen constitution (system prefix); volatile per-call
  data stays in the user message. NOTE: prompt caching only engages once the
  prefix exceeds the model's minimum cacheable length (~1024-4096 tokens). The
  current constitution is below that, so caching is currently a no-op (and the
  cache_read/write debug below will read 0) — the marker is harmless and makes
  caching kick in automatically if the constitution grows.
- Model routing: Haiku 4.5 default, Sonnet 4.6 for hard/large decisions.
- Small max_tokens (a verdict, not prose). SDK auto-retries 429/5xx.
- create() returns None when no key / package -> brain uses rule fallback.

Exact model IDs (no date suffix): claude-haiku-4-5, claude-sonnet-4-6.
"""
from __future__ import annotations

import json
import os

from ..config import Settings
from ..utils.logging import get_logger
from .schema import REFLECTION_SCHEMA, VERDICT_SCHEMA

log = get_logger("agent.llm")


class AnthropicLLM:
    name = "claude"
    supports_escalation = True   # can re-ask Sonnet on a hard call
    base_source = "haiku"        # source label for the default (Haiku) verdict
    escalated_source = "sonnet"  # ...and the escalated (Sonnet) verdict

    def __init__(self, client, settings: Settings) -> None:
        self.client = client
        self.haiku = settings.haiku_model
        self.sonnet = settings.sonnet_model
        self.max_tokens = settings.llm_max_tokens
        self.name = f"claude:{settings.haiku_model}+{settings.sonnet_model}"

    @classmethod
    def create(cls, settings: Settings) -> "AnthropicLLM | None":
        key = settings.anthropic_api_key or os.getenv("ANTHROPIC_API_KEY")
        if not key:
            log.info("no ANTHROPIC_API_KEY -> brain runs in rule-fallback mode")
            return None
        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            log.warning("anthropic package not installed -> rule-fallback mode (pip install anthropic)")
            return None
        log.info("Claude brain enabled (haiku=%s, sonnet=%s)", settings.haiku_model, settings.sonnet_model)
        # P9-harden: bound the per-request timeout (honor llm_timeout_s, which until now
        # only the Ollama path used) and cap SDK auto-retries, so a slow/hung Anthropic
        # request can't stall the eval loop for the SDK default (~600s) x retries.
        return cls(
            AsyncAnthropic(api_key=key, timeout=settings.llm_timeout_s, max_retries=1),
            settings,
        )

    async def _call(self, model: str, constitution: str, user_text: str, schema: dict, max_tokens: int) -> dict | None:
        resp = await self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": constitution, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_text}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
        u = resp.usage
        log.debug(
            "%s in=%s cache_read=%s cache_write=%s out=%s",
            model, u.input_tokens,
            getattr(u, "cache_read_input_tokens", 0), getattr(u, "cache_creation_input_tokens", 0),
            u.output_tokens,
        )
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
        if not text:
            return None
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            log.warning("%s returned non-JSON output", model)
            return None
        # P9-harden: a model can emit valid-but-non-object JSON (a list/scalar). Verdict/
        # ReflectionNote.from_dict call d.get(...) and would raise AttributeError on a
        # non-dict, bypassing the clean rule-fallback. Treat non-dict as "no reply".
        return obj if isinstance(obj, dict) else None

    async def decide(self, constitution: str, user_text: str, *, escalate: bool = False) -> dict | None:
        model = self.sonnet if escalate else self.haiku
        try:
            return await self._call(model, constitution, user_text, VERDICT_SCHEMA, self.max_tokens)
        except Exception as e:  # noqa: BLE001 — never let an LLM error reach the loop
            log.warning("decide error (%s): %s", model, e)
            return None

    async def reflect(self, constitution: str, user_text: str) -> dict | None:
        try:
            return await self._call(self.sonnet, constitution, user_text, REFLECTION_SCHEMA, self.max_tokens)
        except Exception as e:  # noqa: BLE001
            log.warning("reflect error: %s", e)
            return None

    async def aclose(self) -> None:
        try:
            await self.client.close()
        except Exception:  # noqa: BLE001
            pass
