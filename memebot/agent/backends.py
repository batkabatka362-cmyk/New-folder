"""Pick the brain's LLM backend from config.

LLM_BACKEND:
  auto  (default) — try LOCAL Ollama first, then cloud Claude, else rule-fallback
  local           — Ollama only
  cloud           — Anthropic Claude only
  off             — no LLM (pure rule-based)

Returns an object exposing decide()/reflect()/name/supports_escalation, or None
(in which case the brain runs in rule-fallback mode).
"""
from __future__ import annotations

from ..config import Settings
from ..utils.logging import get_logger
from .llm import AnthropicLLM
from .local_llm import DualLocalLLM

log = get_logger("agent.backends")


def make_llm(settings: Settings):
    backend = (settings.llm_backend or "auto").strip().lower()
    if backend == "off":
        log.info("LLM_BACKEND=off -> brain runs rule-only")
        return None
    # DualLocalLLM.create returns a 2-model cascade when a deeper confirm model is pulled, and
    # transparently degrades to a single OllamaLLM (or None) otherwise — so both 'local' and 'auto'
    # get dual-when-available, single-when-not, with no extra branching here.
    if backend == "local":
        return DualLocalLLM.create(settings)
    if backend == "cloud":
        return AnthropicLLM.create(settings)
    # auto: prefer local (free/offline), then cloud, else rule-fallback
    return DualLocalLLM.create(settings) or AnthropicLLM.create(settings)
