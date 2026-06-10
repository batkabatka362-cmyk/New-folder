"""Entrypoint for the swing mean-reversion forward-test (PAPER).

  python -m memebot.swing

Reads .env (SOLANATRACKER_* for OHLCV + the SWING_* knobs), then runs the paper forward test until
Ctrl-C. Fully separate from the sniping bot (`python -m memebot`) — runs its own loop, its own book,
its own swing_state.json. Mean-reversion ONLY (momentum/breakout had no edge in the WL17 backtest).
"""
from __future__ import annotations

import asyncio

from ..config import get_settings
from ..utils.logging import get_logger, setup_logging
from .runner import SwingRunner

log = get_logger("swing")


def main() -> None:
    setup_logging()
    s = get_settings()
    runner = SwingRunner(s)
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        log.info("swing forward-test stopped (Ctrl-C).")


if __name__ == "__main__":
    main()
