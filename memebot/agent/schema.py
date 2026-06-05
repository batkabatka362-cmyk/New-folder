"""Structured outputs for the brain: a trade Verdict and a ReflectionNote.

These JSON schemas are sent to Claude via `output_config.format` (structured
outputs). Per the API constraints, they avoid numeric min/max and string
length constraints and set additionalProperties: false on every object.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from ..constants import MODE_SCALP

# setup-type tag tokens let lessons be SETUP-specific (a 'creator_gold' loss is recalled for future
# creator_gold candidates), closing the classify -> decide -> reflect -> recall loop. 'hold' is reused
# for safe_hold; 'unproven' carries no lesson tag (default skip-lean, nothing to learn).
_ALLOWED_TAGS = {"scalp", "hold", "rug", "global", "gold", "momentum", "risky",
                 # P10b LM5: exit-reason tokens so a lesson can be keyed on HOW the trade ended
                 # (and recalled for a live candidate showing the matching condition).
                 "trail", "timeout", "breakeven", "fade", "miss"}
_SETUP_TAGS = {"creator_gold": "gold", "safe_hold": "hold", "momentum": "momentum", "risky": "risky"}


def setup_tag(setup_type: str | None) -> str:
    """Map a setup_type (from signals.setup.setup_type) to its lesson-tag token, or '' (no tag)."""
    return _SETUP_TAGS.get(setup_type or "", "")


def _clamp(x: float, lo: float, hi: float) -> float:
    # NaN/Inf must fail SAFE to the floor: a junk LLM value like conviction=NaN would
    # otherwise survive max(lo, min(hi, NaN)) as hi (max-size BUY). Reject non-finite.
    if not math.isfinite(x):
        return lo
    return max(lo, min(hi, x))


def _canon_tag(raw) -> str:
    """Snap a free-form lesson tag onto the known vocabulary (so recall works).
    Keeps matching tokens of a compound tag ('rug-pull' -> 'rug', 'scalp_exit' ->
    'scalp'); anything with no known token -> 'global'."""
    toks = {t for t in re.split(r"[-_\s]+", str(raw).strip().lower()) if t}
    matched = toks & _ALLOWED_TAGS
    return "-".join(sorted(matched)) if matched else "global"


# P10b LM1: a deterministic actionability gate so recall slots carry only SPECIFIC, reusable lessons —
# not platitudes ("be careful", "manage risk") or bare category words. No LLM call; a light heuristic.
_PLATITUDES = {"be careful", "stay disciplined", "manage risk", "trade carefully", "be patient",
               "do your research", "dyor", "be cautious", "stay safe", "trust the process",
               "be selective", "stay focused", "be disciplined", "manage your risk"}
_ACTION_CUES = ("when", "if ", "skip", "avoid", "sell", "hold", "buy", "cut ", "take", "demand",
                "wait", "exit", "enter", "size ", "trail", "stop", "never", "always", "prefer",
                "require", "trim", "scale", "dump", "rug", "veto", "raise", "lower", "tighten")


def is_actionable_lesson(lesson: str) -> bool:
    """True only if the lesson is specific enough to be useful: long enough, not a bare tag/platitude,
    and carries a signal+action shape (a conditional or an action cue). Purely additive gatekeeping at
    the store boundary — it cannot affect a trade decision or any gate."""
    t = (lesson or "").strip().lower()
    if len(t.split()) < 5:                       # too short to name a signal AND an action
        return False
    if t.strip(" .") in _PLATITUDES:
        return False
    if t.strip(" .") in _ALLOWED_TAGS:           # a bare category word ("rug", "scalp")
        return False
    return any(cue in t for cue in _ACTION_CUES)


def _num(x, default: float = 0.0) -> float:
    """Coerce a raw (LLM-supplied) value to float, failing SAFE to `default` on junk.
    A local model can ignore the schema and emit conviction='high' / size_pct=None;
    a bare float() would raise and (under the parallel gather) drop the whole batch."""
    try:
        return float(x if x is not None else default)
    except (TypeError, ValueError):
        return default


def _bounded(x, lo: float, hi: float) -> float:
    """Accept a value only inside [lo, hi]; anything else (or junk) -> 0.0.

    Used for tp/sl: a 0 means 'use the mode default', so a degenerate or
    unit-confused advisory value (e.g. 0.0005 or 18 instead of 0.18) is
    rejected rather than driving an instant stop-out or an unreachable target.
    """
    try:
        v = float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return v if lo <= v <= hi else 0.0


# ── Verdict (the trade decision) ─────────────────────────────────────────────
VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["buy", "skip", "watch"]},
        "mode": {"type": "string", "enum": ["scalp", "hold"]},
        "conviction": {"type": "number", "description": "0..1 confidence in this call"},
        "size_pct": {"type": "number", "description": "0..1 fraction of the max position size to deploy"},
        "tp_pct": {"type": "number", "description": "take-profit as a fraction, e.g. 0.4 = +40%. 0 = use default"},
        "sl_pct": {"type": "number", "description": "stop-loss as a fraction, e.g. 0.18 = -18%. 0 = use default"},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
        "primary_signal": {
            "type": "string",
            "enum": ["holder_concentration", "creator_track_record", "momentum_breadth",
                     "safety_authority", "liquidity", "smart_money_tape", "trend", "lessons", "none"],
            "description": "the single signal that most drove this call; 'none' if nothing was decisive (then SKIP)",
        },
        "reasoning": {"type": "string", "description": "one or two sentences of rationale"},
    },
    "required": ["action", "mode", "conviction", "size_pct", "tp_pct", "sl_pct", "primary_signal", "reasoning"],
}

# P10 #10: the allowed decisive-signal labels (kept in sync with VERDICT_SCHEMA's enum). Used to
# clamp the parsed value so a non-conforming local model can't inject a free-form string.
_PRIMARY_SIGNALS = frozenset({
    "holder_concentration", "creator_track_record", "momentum_breadth", "safety_authority",
    "liquidity", "smart_money_tape", "trend", "lessons", "none",
})


@dataclass(slots=True)
class Verdict:
    action: str = "skip"            # buy | skip | watch
    mode: str = MODE_SCALP          # scalp | hold
    conviction: float = 0.0         # 0..1
    size_pct: float = 0.0           # 0..1 of max position size
    tp_pct: float = 0.0             # 0 => use mode default
    sl_pct: float = 0.0             # 0 => use mode default
    risk_flags: list[str] = field(default_factory=list)
    primary_signal: str = "none"    # P10 #10: the single signal the brain says most drove this call
    reasoning: str = ""
    source: str = "rule"            # haiku | sonnet | rule
    recalled_lessons: list = field(default_factory=list)  # P10b #14: lessons recalled into THIS decision (advisory metadata; never feeds a gate/score)

    @classmethod
    def from_dict(cls, d: dict, source: str) -> "Verdict":
        ps = str(d.get("primary_signal", "none")).strip().lower()
        return cls(
            action=str(d.get("action", "skip")),
            mode=str(d.get("mode", MODE_SCALP)),
            conviction=_clamp(_num(d.get("conviction")), 0.0, 1.0),
            size_pct=_clamp(_num(d.get("size_pct")), 0.0, 1.0),
            tp_pct=_bounded(d.get("tp_pct", 0.0), 0.05, 10.0),   # else 0 => mode default
            sl_pct=_bounded(d.get("sl_pct", 0.0), 0.05, 0.95),   # else 0 => mode default
            risk_flags=[str(x) for x in (d.get("risk_flags") or [])],
            primary_signal=ps if ps in _PRIMARY_SIGNALS else "none",
            reasoning=str(d.get("reasoning", ""))[:500],
            source=source,
        )


# ── ReflectionNote (a lesson written to memory after a closed trade) ──────────
REFLECTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "lesson": {"type": "string", "description": "a concrete, reusable lesson for future trades"},
        "tag": {"type": "string", "description": "short category, e.g. 'scalp', 'hold', 'rug', 'global'"},
        "confidence": {"type": "number", "description": "0..1 how strongly this generalizes"},
    },
    "required": ["lesson", "tag"],
}


@dataclass(slots=True)
class ReflectionNote:
    lesson: str
    tag: str = "global"
    confidence: float = 0.5

    @classmethod
    def from_dict(cls, d: dict) -> "ReflectionNote":
        return cls(
            lesson=str(d.get("lesson", "")).strip()[:600],
            tag=_canon_tag(d.get("tag", "global")),
            confidence=_clamp(_num(d.get("confidence"), 0.5), 0.0, 1.0),
        )
