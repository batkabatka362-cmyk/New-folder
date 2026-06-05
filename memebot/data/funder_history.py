"""Dev-wallet FUNDING-SOURCE clustering (N12) — re-link rotated creators by their common funder.

The strongest documented free-data anti-rug signal the research said we were MISSING is coordinated /
concealed operator identity. `creator_spam_launches` counts tokens per CREATOR wallet, but a savvy
operator rotates to a FRESH creator wallet per token — defeating the count — while funding each one
from a single treasury. Resolving the FUNDER of a creator wallet (who sent it its first SOL) re-links
those rotated identities: a funder that has spawned many distinct creators (each launching tokens) is
a serial rug factory hiding behind wallet rotation.

The denylist problem (CEX hot wallets / pump.fun rails / faucets legitimately fund thousands of
wallets, creating giant false clusters) is solved WITHOUT a hand-maintained address list: a funder
above `serial_max` distinct creators is treated as INFRASTRUCTURE and excluded — only a plausible
operator BAND [serial_min, serial_max] earns the penalty.

SOFT / advisory (graded scam term, never a hard veto) and UNPROVEN — calibrate the band from labeled
outcomes before trusting it. Funders are resolved off the hot path and persisted (a wallet's funder
is immutable), so the cluster graph accrues across restarts.
"""
from __future__ import annotations

from collections import defaultdict


class FunderRegistry:
    def __init__(self, max_creators: int = 200_000) -> None:
        self._funder_of: dict[str, str] = {}                       # creator -> funder
        self._creators: dict[str, set[str]] = defaultdict(set)     # funder -> {distinct creators}
        # P9-harden: a creator's funder is immutable, so both maps only ever grow; bound them.
        # Resolution is rate-limited off the hot path (funder_batch_size/pass), so this is a slow
        # grower, but unbounded over a multi-week run without a cap.
        self._max = max(1, int(max_creators))

    def load(self, rows) -> None:
        """Seed from persisted (creator, funder) rows (cross-restart cluster graph)."""
        for creator, funder in rows:
            if creator and funder:
                self._funder_of[creator] = funder
                self._creators[funder].add(creator)

    def record(self, creator: str, funder: str) -> None:
        """Cache a resolved creator->funder link (idempotent; first resolution wins — a funder is immutable)."""
        if not creator or not funder or creator in self._funder_of:
            return
        self._funder_of[creator] = funder
        self._creators[funder].add(creator)
        if len(self._funder_of) > self._max:
            self._prune()

    def _prune(self) -> None:
        """Evict ENTIRE smallest clusters (a funder + all its creators atomically) on overflow, so the
        cluster-size count of every REMAINING funder stays EXACT — unlike dropping individual creators,
        which would silently corrupt funder_creator_count. The smallest clusters are below
        funder_serial_min (= no serial-rugger signal), so this is near-lossless; the only cost is that
        an evicted funder which later funds again restarts its count from 1 (a rare, advisory-only
        undercount, re-seedable from the persisted graph on restart)."""
        ranked = sorted(self._creators.items(), key=lambda kv: len(kv[1]))
        target = max(1, len(self._funder_of) // 10)   # free at least ~10% of tracked creators
        freed = 0
        for funder, creators in ranked:
            if freed >= target:
                break
            for c in creators:
                self._funder_of.pop(c, None)
            freed += len(creators)
            del self._creators[funder]

    def resolved(self, creator: str | None) -> bool:
        return bool(creator) and creator in self._funder_of

    def funder_of(self, creator: str | None) -> str | None:
        return self._funder_of.get(creator or "")

    def funder_creator_count(self, creator: str | None) -> int | None:
        """How many DISTINCT creators share this creator's funder (the cluster size). None when the
        creator's funder is not yet resolved — unknown, so no penalty is applied on missing data."""
        f = self._funder_of.get(creator or "")
        return len(self._creators[f]) if f else None

    def snapshot(self) -> list[tuple]:
        """(creator, funder) rows to persist."""
        return [(c, f) for c, f in self._funder_of.items()]
