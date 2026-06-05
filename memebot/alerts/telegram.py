"""Telegram alerts via an internal queue + worker coroutine.

`send()` is non-blocking (enqueue only) so alerting never sits on the trading
hot path. If no token/chat_id is configured it degrades to console logging.
Uses the Telegram Bot HTTP API directly (no heavy SDK needed for sending).
"""
from __future__ import annotations

import asyncio

import httpx

from ..utils.logging import get_logger

log = get_logger("alerts.telegram")


class TelegramAlerter:
    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1000)

    def send(self, text: str) -> None:
        if not self.enabled:
            log.info("[alert] %s", text)
            return
        try:
            self._queue.put_nowait(text)
        except asyncio.QueueFull:
            log.warning("telegram queue full, dropping alert")

    async def run(self) -> None:
        if not self.enabled:
            log.info("Telegram disabled (no token/chat_id) — alerts go to console")
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        async with httpx.AsyncClient(timeout=10.0) as client:
            while True:
                text = await self._queue.get()
                try:
                    await client.post(url, json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": True,
                    })
                except Exception as e:  # noqa: BLE001 — alerting must never kill the bot
                    log.warning("telegram send failed: %s", e)
                finally:
                    self._queue.task_done()
