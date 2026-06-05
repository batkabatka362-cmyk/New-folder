"""The autonomous trading brain: perceive -> recall -> reason -> decide -> reflect.

Claude (Haiku default, Sonnet escalation) is the reasoning layer; it is ADVISORY.
The deterministic risk layer (risk/) still gates and can veto every decision.
Degrades gracefully to a rule-based fallback when no API key / anthropic package.
"""
