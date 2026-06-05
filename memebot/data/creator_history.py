"""Per-creator launch history — a serial-spam / rug-factory tell.

A legit project dev launches one or two tokens; a pump-and-dump operation spams hundreds (the live
DB has creators with 600-700+ launches). The number of tokens a creator wallet has launched is thus
a cheap, FREE, behavioral risk signal (we already store `creator` on every token — 100% populated).

This is a SOFT signal (graded penalty the brain/scorer weighs), NOT a hard veto: the live data is
noisy (our worst rugs came from 65- and 67-launch creators, but a +237% WINNER came from a 20-launch
creator, and low-launch rugs exist too), so a hard launch-count veto would block winners and starve
the already-thin supply. Capture it, weigh it softly, and calibrate a threshold from labeled data.

Loaded once from storage at startup, then incremented in-memory as new tokens are discovered.
"""
from __future__ import annotations


class CreatorHistory:
    def __init__(self, max_creators: int = 200_000) -> None:
        self._counts: dict[str, int] = {}
        # P9-harden: bound the dict (mirrors SmartMoney.max_wallets). record() is on the
        # always-on ingest hot path and pump.fun mints thousands of mostly-distinct creators
        # a day, so without a cap this grows unbounded over a multi-day autonomous run.
        self._max = max(1, int(max_creators))

    def load(self, counts: dict[str, int]) -> None:
        """Seed from a {creator: launch_count} map (e.g. storage.creator_launch_counts())."""
        self._counts = {k: int(v) for k, v in counts.items() if k}

    def record(self, creator: str | None) -> None:
        """Count a newly-discovered token's creator (called on each NewTokenEvent)."""
        if creator:
            self._counts[creator] = self._counts.get(creator, 0) + 1
            if len(self._counts) > self._max:
                self._prune()

    def _prune(self) -> None:
        """Drop the lowest-count (single-launch = least-signal) creators on overflow. The
        high-launch serial-spam creators (the actual signal, weighed only above
        creator_spam_launches) are never near the eviction floor, so this can't grow
        unbounded yet preserves the signal; an evicted creator re-seeds from the DB on restart."""
        ranked = sorted(self._counts.items(), key=lambda kv: kv[1])
        for c, _ in ranked[: max(1, len(self._counts) // 10)]:
            del self._counts[c]

    def launch_count(self, creator: str | None) -> int:
        """How many tokens this creator wallet has launched (0 if unknown/empty)."""
        return self._counts.get(creator or "", 0)
