"""Metadata-DUPLICATION tell (user insight) — a name/ticker reused across many mints is a scam-factory.

The user named source + name + image + DUPLICATION as the hallmark of scam coins: an operation spams
many tokens that REUSE the same branding (name / ticker / image). The token's name + symbol are already
captured on the free discovery WS (NewTokenEvent) and then dropped; counting how many DISTINCT mints
share a normalized name+symbol gives a free, no-network serial-rugger tell that complements the creator
launch count + the funder cluster (a fresh creator wallet + a fresh funder but the SAME branding still
ties the factory together).

SOFT / advisory only, and deliberately NOISY-aware: popular memes (PEPE, DOGE...) are reused
LEGITIMATELY by unrelated devs, so a high reuse count alone is not proof — it is one more small graded
signal the brain weighs, logged for the data to calibrate, NEVER a hard veto. (Image-level duplication
would need the off-chain uri JSON — a later add; name+symbol is the free first cut.)

Loaded once from storage at startup, then incremented in-memory per NewTokenEvent (mirrors CreatorHistory).
"""
from __future__ import annotations


def _key(name: str | None, symbol: str | None) -> str:
    """Normalized branding key — case/space-insensitive name+symbol. '' for an empty name (uncounted)."""
    n = (name or "").strip().lower()
    s = (symbol or "").strip().lower()
    return f"{n}|{s}" if n else ""


class MetadataRegistry:
    def __init__(self, max_keys: int = 200_000) -> None:
        self._counts: dict[str, int] = {}
        # P9-harden: bound the dict (same shape as CreatorHistory). record() is on the
        # always-on ingest hot path; distinct branding strings accumulate ~with total mints
        # seen, so without a cap this grows unbounded over a multi-day run.
        self._max = max(1, int(max_keys))

    def load(self, counts: dict[str, int]) -> None:
        """Seed from a {normalized-key: distinct-mint-count} map (storage.name_symbol_counts())."""
        self._counts = {k: int(v) for k, v in counts.items() if k}

    def record(self, name: str | None, symbol: str | None) -> None:
        """Count a newly-discovered token's branding (called on each NewTokenEvent)."""
        k = _key(name, symbol)
        if k:
            self._counts[k] = self._counts.get(k, 0) + 1
            if len(self._counts) > self._max:
                self._prune()

    def _prune(self) -> None:
        """Drop the lowest-count (singleton = no duplication signal) keys on overflow. reuse_count
        only carries signal when > 1 (the name_reuse_warn tell), so evicting count==1 keys is
        lossless for the feature; an evicted key re-seeds from the DB on restart."""
        ranked = sorted(self._counts.items(), key=lambda kv: kv[1])
        for k, _ in ranked[: max(1, len(self._counts) // 10)]:
            del self._counts[k]

    def reuse_count(self, name: str | None, symbol: str | None) -> int:
        """How many mints (incl. this one) share this normalized name+symbol. 0 for an empty name."""
        k = _key(name, symbol)
        return self._counts.get(k, 0) if k else 0
