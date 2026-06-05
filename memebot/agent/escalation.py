"""Cost/latency gate: decide which candidates are worth a Claude call.

LLMs are OFF the per-token hot path. Only candidates in the UNCERTAIN
confidence band (above the entry floor but below slam-dunk) are escalated, and
only within a hard per-minute budget, deduped per mint. Slam-dunks act locally
on the rule score; weak candidates are skipped — neither needs the LLM.
"""
from __future__ import annotations

import time
from collections import deque


class Escalator:
    def __init__(self, per_min: int, high_conf: float, entry_threshold: float,
                 dedupe_window_s: float = 120.0) -> None:
        self.per_min = per_min
        self.high_conf = high_conf
        self.entry = entry_threshold
        self.window = dedupe_window_s
        self._calls: deque[float] = deque()
        self._seen: dict[str, float] = {}

    def _trim(self, now: float) -> None:
        while self._calls and now - self._calls[0] > 60.0:
            self._calls.popleft()
        # prune dedupe entries past the window so _seen can't grow unbounded
        if self._seen:
            cutoff = now - self.window
            self._seen = {m: t for m, t in self._seen.items() if t > cutoff}

    def allow(self, score: float, mint: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        # uncertain band only: [entry, high_conf)
        if score < self.entry or score >= self.high_conf:
            return False
        self._trim(now)
        if len(self._calls) >= self.per_min:
            return False
        if now - self._seen.get(mint, 0.0) < self.window:
            return False
        self._calls.append(now)
        self._seen[mint] = now
        return True

    def try_reserve(self, now: float | None = None) -> bool:
        """Reserve ONE budget slot, no band/dedupe checks — for a Sonnet re-ask
        on a candidate already admitted by allow(). Counts the second call
        against llm_per_min so a hard call can't double the rate cap."""
        now = time.time() if now is None else now
        self._trim(now)
        if len(self._calls) >= self.per_min:
            return False
        self._calls.append(now)
        return True
