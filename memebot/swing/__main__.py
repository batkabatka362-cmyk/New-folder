"""Entrypoint for the swing mean-reversion forward-test (PAPER).

  python -m memebot.swing

Reads .env (SWING_* knobs + OHLCV source), then runs the paper forward test until Ctrl-C. Fully separate
from the sniping bot (`python -m memebot`) — runs its own loop, its own book, its own swing_state.json.
Mean-reversion ONLY (momentum/breakout had no edge in the WL17 backtest). Crash-resilient autonomy:
`python -m memebot.supervisor --target memebot.swing`.
"""
from __future__ import annotations

import asyncio

from ..config import get_settings
from ..utils.logging import get_logger, setup_logging
from ..utils.singleton import InstanceLock, SingleInstanceError
from .runner import SwingRunner

log = get_logger("swing")


def main() -> None:
    setup_logging()
    s = get_settings()
    # single-instance guard: two swing processes on one swing_state.json would double-write the paper book
    # (the same overlap the sniper's DB lock prevents). Released automatically on exit/crash.
    lock = InstanceLock("swing_state.json.lock")
    try:
        lock.acquire()
    except SingleInstanceError as e:
        log.error("%s", e)
        return
    for _w in s.validate():                        # surface any config invariant warnings at startup
        log.warning("config: %s", _w)
    runner = SwingRunner(s)
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        log.info("swing forward-test stopped (Ctrl-C).")
    finally:
        lock.release()


if __name__ == "__main__":
    main()
