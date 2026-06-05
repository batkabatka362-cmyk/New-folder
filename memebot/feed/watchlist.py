"""Watchlist manager for the METERED PumpPortal per-token trade stream.

OFF by default (TRADE_STREAM_ENABLED=false). The trade stream costs SOL, so the
bot will NOT enable it autonomously — you turn it on knowingly after funding a
PumpPortal API key. When on, it keeps a bounded top-N watchlist of the freshest
active tokens (diffing subscribe/unsubscribe each cycle) so tick-level features
(real volume spike, unique-buyer breadth) become available for those tokens.
"""
from __future__ import annotations

import asyncio

from ..utils.logging import get_logger

log = get_logger("feed.watchlist")


def select_watchlist(states, size: int) -> list[str]:
    """Top-`size` mints by most-recent activity (freshest last_ts first)."""
    ordered = sorted(states, key=lambda s: s.last_ts, reverse=True)
    return [s.mint for s in ordered[:size]]


def diff_watchlist(current: set[str], desired: set[str]) -> tuple[list[str], list[str]]:
    """(to_subscribe, to_unsubscribe) — bounds metered cost to the desired set."""
    return sorted(desired - current), sorted(current - desired)


class WatchlistManager:
    def __init__(self, feed, registry, settings) -> None:
        self.feed = feed
        self.registry = registry
        self.s = settings
        # requires BOTH the explicit opt-in AND an API key (the stream is metered)
        self.enabled = bool(settings.trade_stream_enabled and settings.pumpportal_api_key)

    async def run(self) -> None:
        if not self.enabled:
            return
        log.warning("PumpPortal trade-stream watchlist ON (size=%d) — this stream is METERED and spends SOL",
                    self.s.watchlist_size)
        while True:
            await asyncio.sleep(self.s.watchlist_interval_s)
            if getattr(self.feed, "trade_sub_unavailable", False):
                log.warning("watchlist stopping: PumpPortal refused the trade stream (api-key wallet "
                            "needs >= 0.02 SOL). Fund it + restart, or set TRADE_STREAM_ENABLED=false.")
                return                                      # PumpPortal won't serve it -> don't spin
            try:
                desired = set(select_watchlist(
                    self.registry.active(self.s.watch_max_age_s, self.s.watch_limit),
                    self.s.watchlist_size,
                ))
                to_add, to_remove = diff_watchlist(self.feed.watched, desired)
                if to_add:
                    await self.feed.watch_token_trades(to_add)
                if to_remove:
                    await self.feed.unwatch_token_trades(to_remove)
                if to_add or to_remove:
                    log.info("watchlist: now watching %d tokens' trade streams (+%d/-%d)",
                             len(self.feed.watched), len(to_add), len(to_remove))
            except Exception:  # noqa: BLE001
                log.exception("watchlist error")
