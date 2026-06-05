"""PumpPortal WebSocket feed — the free discovery firehose.

ONE persistent connection. Subscribes to new tokens + migrations (both free).
Per-token trade streams (`subscribeTokenTrade`) are METERED on PumpPortal, so we
only subscribe a small watchlist on demand (Phase 4).

Docs: https://pumpportal.fun/data-api/real-time   URL: wss://pumpportal.fun/api/data

Messages are normalized into models.* events and pushed onto a bounded queue.
If the queue is full we DROP (and count) rather than stall the socket reader.
"""
from __future__ import annotations

import asyncio
import json

import websockets

from ..constants import SIDE_BUY, SIDE_SELL
from ..models import MigrationEvent, NewTokenEvent, TradeEvent
from ..utils.logging import get_logger

log = get_logger("feed.pumpportal")


def normalize(msg: dict):
    """Map a raw PumpPortal message to an event, or None (acks / unknown)."""
    tx = msg.get("txType")
    mint = msg.get("mint")
    if not tx or not mint:
        return None  # subscription ack or heartbeat

    if tx == "create":
        return NewTokenEvent(
            mint=mint,
            name=msg.get("name", ""),
            symbol=msg.get("symbol", ""),
            creator=msg.get("traderPublicKey", ""),
            uri=msg.get("uri", ""),
            pool=msg.get("pool", ""),
            bonding_curve_key=msg.get("bondingCurveKey", ""),
            market_cap_sol=float(msg.get("marketCapSol", 0) or 0),
            v_sol_in_curve=float(msg.get("vSolInBondingCurve", 0) or 0),
            v_tokens_in_curve=float(msg.get("vTokensInBondingCurve", 0) or 0),
            initial_buy_sol=float(msg.get("solAmount", 0) or 0),
            raw=msg,
        )
    if tx in (SIDE_BUY, SIDE_SELL):
        return TradeEvent(
            mint=mint,
            trader=msg.get("traderPublicKey", ""),
            side=tx,
            sol_amount=float(msg.get("solAmount", 0) or 0),
            token_amount=float(msg.get("tokenAmount", 0) or 0),
            market_cap_sol=float(msg.get("marketCapSol", 0) or 0),
            new_token_balance=float(msg.get("newTokenBalance", 0) or 0),
            raw=msg,
        )
    if tx in ("migrate", "migration"):
        return MigrationEvent(mint=mint, pool=msg.get("pool", ""), raw=msg)
    return None


class PumpPortalFeed:
    def __init__(self, url: str, out_queue: "asyncio.Queue", *,
                 subscribe_new: bool = True, subscribe_migration: bool = True,
                 api_key: str = "") -> None:
        self.url = url
        self.api_key = api_key            # ONLY needed for the metered subscribeTokenTrade stream
        self.out_queue = out_queue
        self.subscribe_new = subscribe_new
        self.subscribe_migration = subscribe_migration
        self._watchlist: set[str] = set()
        self._ws = None
        self._stop = False
        self.dropped = 0
        self.trade_sub_unavailable = False   # set True if PumpPortal rejects subscribeTokenTrade (unfunded key)

    def _connect_url(self) -> str:
        """PumpPortal requires the api-key in the URL for metered (token-trade) subs.
        Free new-token/migration subs work without it, so the key is appended only
        when one is configured."""
        if not self.api_key:
            return self.url
        sep = "&" if "?" in self.url else "?"
        return f"{self.url}{sep}api-key={self.api_key}"

    def _redact(self, text: str) -> str:
        return text.replace(self.api_key, "***") if self.api_key else text

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop:
            try:
                async with websockets.connect(
                    self._connect_url(), ping_interval=20, ping_timeout=20, max_size=2 ** 21
                ) as ws:
                    self._ws = ws
                    await self._resubscribe(ws)
                    backoff = 1.0
                    log.info("PumpPortal connected (new=%s migration=%s watch=%d)",
                             self.subscribe_new, self.subscribe_migration, len(self._watchlist))
                    async for raw in ws:
                        self._handle(raw)
            except asyncio.CancelledError:
                self._stop = True
                raise
            except Exception as e:  # noqa: BLE001 — reconnect on any socket error
                log.warning("PumpPortal WS error: %s — reconnecting in %.0fs", self._redact(str(e)), backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                self._ws = None

    @staticmethod
    def _is_trade_sub_rejected(msg: dict) -> bool:
        """True if PumpPortal refused subscribeTokenTrade because the api-key's wallet is unfunded
        (needs >= 0.02 SOL). Both the 'errors: Minimum balance not met' and the 'only available when
        connecting with an API key funded...' messages indicate the same unfunded-key cause."""
        blob = f"{msg.get('errors', '')} {msg.get('message', '')}".lower()
        return ("minimum balance not met" in blob
                or ("subscribetokentrade" in blob and ("funded" in blob or "sol" in blob)))

    async def _resubscribe(self, ws) -> None:
        if self.subscribe_new:
            await ws.send(json.dumps({"method": "subscribeNewToken"}))
        if self.subscribe_migration:
            await ws.send(json.dumps({"method": "subscribeMigration"}))
        if self._watchlist and not self.trade_sub_unavailable:
            await ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": list(self._watchlist)}))

    async def watch_token_trades(self, mints: list[str]) -> None:
        """Subscribe to per-token trade stream (METERED — small watchlist only). No-op once PumpPortal
        has told us the api-key is unfunded, so we stop hammering a sub it will only reject."""
        if self.trade_sub_unavailable:
            return
        new = [m for m in mints if m not in self._watchlist]
        if not new:
            return
        self._watchlist.update(new)
        if self._ws is not None:
            await self._ws.send(json.dumps({"method": "subscribeTokenTrade", "keys": new}))

    async def unwatch_token_trades(self, mints: list[str]) -> None:
        """Unsubscribe tokens that aged out of the watchlist (bounds metered cost)."""
        drop = [m for m in mints if m in self._watchlist]
        if not drop:
            return
        if self._ws is not None:
            # send BEFORE forgetting them: a failed send leaves them in _watchlist so the
            # next reconcile retries the unsubscribe (else they keep billing until reconnect).
            await self._ws.send(json.dumps({"method": "unsubscribeTokenTrade", "keys": drop}))
        self._watchlist.difference_update(drop)

    @property
    def watched(self) -> set[str]:
        return set(self._watchlist)

    def _handle(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(msg, dict):
            return
        ev = normalize(msg)
        if ev is None:
            # PumpPortal sends acks / errors here (no txType). Catch the unfunded-key rejection of
            # subscribeTokenTrade ONCE, so the bot stops retrying a sub it will only reject + tells the
            # operator the exact fix (the G3 tape stays dark until the api-key wallet holds >= 0.02 SOL).
            if not self.trade_sub_unavailable and self._is_trade_sub_rejected(msg):
                self.trade_sub_unavailable = True
                self._watchlist.clear()      # stop the watchlist diff from re-trying rejected subscribes
                log.warning("G3 DISABLED by PumpPortal: subscribeTokenTrade needs an api-key wallet funded "
                            "with >= 0.02 SOL (got '%s'). The tape signals (bundle/smart-money/creator-dump) "
                            "stay DARK until you fund the PumpPortal wallet; the bot will stop re-subscribing.",
                            self._redact(str(msg.get("errors") or msg.get("message")))[:160])
            return
        try:
            self.out_queue.put_nowait(ev)
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped % 100 == 1:
                log.warning("event queue full — dropped %d events", self.dropped)

    def stop(self) -> None:
        self._stop = True
