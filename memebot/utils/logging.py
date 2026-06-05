"""Logging setup — one place, called once from main."""
from __future__ import annotations

import logging
import sys

_CONFIGURED = False


def setup_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    # Windows consoles default to cp1252 -> emojis/em-dashes become mojibake.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers[:] = [handler]
    # quiet noisy libs
    for noisy in ("websockets", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
