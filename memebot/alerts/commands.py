"""Telegram command listener: pull-based monitoring + control.

Long-polls getUpdates (no python-telegram-bot dependency, just httpx) and
answers /pnl /positions /status /stop /resume /help. Only the configured
chat_id is honored — messages from any other chat are ignored.

The command logic is a PURE function (render_command) so it's unit-testable
without a network; the listener is a thin transport around it.
"""
from __future__ import annotations

import asyncio

import httpx

from ..portfolio.pnl import compute_stats
from ..utils.logging import get_logger

log = get_logger("alerts.commands")

HELP = (
    "*memebot commands*\n"
    "/pnl - equity, balance, realized PnL, win-rate\n"
    "/positions - open positions and their PnL\n"
    "/status - mode, brain, scorer, health\n"
    "/stop - halt opening new positions\n"
    "/resume - clear a manual halt\n"
    "/help - this message"
)


def render_command(text: str, *, portfolio, risk, settings, price_map: dict,
                   scorer_kind: str, backend: str, tracked: int) -> str | None:
    """Map a command string to a reply (or None to ignore). May mutate risk
    (/stop, /resume) — that's the only side effect."""
    parts = text.strip().split()
    if not parts:
        return None
    cmd = parts[0].lower().lstrip("/").split("@")[0]   # tolerate "/pnl@botname"

    if cmd in ("pnl", "stats"):
        eq = portfolio.equity(price_map)
        st = compute_stats(portfolio.closed)
        roi = (eq / portfolio.initial_sol - 1.0) * 100.0 if portfolio.initial_sol else 0.0
        return (
            f"*PnL*\nequity: {eq:.4f} SOL ({roi:+.1f}%)\n"
            f"balance: {portfolio.sol_balance:.4f} SOL\n"
            f"realized: {portfolio.realized_pnl:+.4f} SOL\n"
            f"open positions: {portfolio.open_count()}\n{st.as_line()}"
        )
    if cmd in ("positions", "pos"):
        if not portfolio.positions:
            return "No open positions."
        lines = ["*Positions*"]
        for mint, p in portfolio.positions.items():
            price = price_map.get(mint, p.avg_price_sol)
            lines.append(
                f"{p.symbol or mint[:6]} [{p.mode}] {p.pnl_pct(price) * 100:+.1f}% "
                f"qty={p.qty:.0f} avg={p.avg_price_sol:.8f}"
            )
        return "\n".join(lines)
    if cmd in ("status", "mode"):
        health = f"HALTED ({risk.halt_reason})" if risk.halted else "active"
        return (
            f"*Status*\nmode: {settings.mode} | {health}\n"
            f"brain: {backend} | scorer: {scorer_kind}\n"
            f"tracked: {tracked} | open: {portfolio.open_count()}"
        )
    if cmd == "stop":
        risk.halt("manual /stop")
        return "🛑 Halted - no new positions will open. Send /resume to clear."
    if cmd == "resume":
        return "▶️ Resumed - new positions allowed." if risk.resume() else "Was not halted."
    if cmd in ("help", "start"):
        return HELP
    return None   # unrecognized -> ignore


class TelegramCommands:
    def __init__(self, settings, alerter, bot) -> None:
        self.s = settings
        self.alerter = alerter
        self.bot = bot
        self.enabled = bool(settings.telegram_bot_token and settings.telegram_chat_id)
        self._offset = 0

    async def run(self) -> None:
        if not self.enabled:
            return
        base = f"https://api.telegram.org/bot{self.s.telegram_bot_token}"
        async with httpx.AsyncClient(timeout=40.0) as client:
            await self._prime_offset(client, base)   # skip the backlog queued while offline
            log.info("Telegram command listener active")
            while True:
                try:
                    r = await client.get(
                        f"{base}/getUpdates", params={"offset": self._offset, "timeout": 30}
                    )
                    data = r.json() or {}
                    if not data.get("ok"):           # 409/401/429 etc. -> back off, don't busy-spin
                        log.warning("telegram getUpdates not ok: %s", data.get("description"))
                        await asyncio.sleep(5)
                        continue
                    for u in data.get("result", []):
                        await self._handle(u, client, base)
                        self._offset = u["update_id"] + 1   # advance only after handling
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — keep polling through transient errors
                    log.warning("telegram poll error: %s", e)
                    await asyncio.sleep(5)

    async def _prime_offset(self, client: "httpx.AsyncClient", base: str) -> None:
        """Discard updates queued while the bot was offline (esp. stale /stop /resume),
        so a control command sent during downtime isn't replayed against current intent."""
        try:
            r = await client.get(f"{base}/getUpdates", params={"offset": -1, "timeout": 0})
            res = (r.json() or {}).get("result", [])
            if res:
                self._offset = res[-1]["update_id"] + 1
        except Exception as e:  # noqa: BLE001 — accept a one-time replay rather than fail to start
            log.warning("telegram prime-offset failed: %s", e)

    async def _handle(self, update: dict, client: "httpx.AsyncClient", base: str) -> None:
        msg = update.get("message") or update.get("edited_message") or {}
        text = msg.get("text", "")
        chat = str((msg.get("chat") or {}).get("id", ""))
        if not text or chat != str(self.s.telegram_chat_id):   # security: only the owner's chat
            return
        try:
            reply = render_command(
                text, portfolio=self.bot.portfolio, risk=self.bot.risk, settings=self.s,
                price_map=self.bot._price_map(), scorer_kind=self.bot.scorer.kind,
                backend=(self.bot.llm.name if self.bot.llm is not None else "rule-fallback"),
                tracked=len(self.bot.registry),
            )
        except Exception as e:  # noqa: BLE001 — a bad command must not drop the update or kill the poll
            log.warning("command render failed: %s", e)
            return
        if reply is None:
            return
        try:
            await client.post(f"{base}/sendMessage",
                              json={"chat_id": chat, "text": reply, "parse_mode": "Markdown"})
        except httpx.HTTPError as e:
            log.warning("telegram reply failed: %s", e)
