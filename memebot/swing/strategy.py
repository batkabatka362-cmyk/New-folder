"""The mean-reversion signal — pure, deterministic, testable (the heart of the swing mode).

Buy when price dips DEEP below its SMA (oversold), sell when it reverts back above the SMA. This is the
exact rule validated in backtest/swing_lab (WL17): it beats buy-and-hold 15/15 on liquid established
memecoins, robust to survivorship/fees/glitches, and the param gradient is interpretable (deeper dips win).
No network, no state beyond the inputs — so it is unit-tested directly and shared by the live engine.
"""
from __future__ import annotations


def sma(values: list[float], window: int) -> float | None:
    """Simple moving average of the last `window` values, or None if there aren't enough."""
    if window <= 0 or len(values) < window:
        return None
    return sum(values[-window:]) / window


def mean_rev_position(closes: list[float], *, window: int, dip_k: float, exit_k: float = 0.0,
                      pos: int = 0) -> int:
    """Desired position (0=flat, 1=long) given the close series (latest = closes[-1]) and the CURRENT
    position. Enter long when price < SMA*(1-dip_k) (a deep oversold dip); exit when price > SMA*(1+exit_k)
    (reversion). Holds otherwise. Returns the current pos unchanged until there's enough history for the
    SMA — never acts on a partial window."""
    s = sma(closes, window)
    if s is None or s <= 0:
        return pos
    price = closes[-1]
    if pos == 0 and price < s * (1.0 - dip_k):
        return 1
    if pos == 1 and price > s * (1.0 + exit_k):
        return 0
    return pos


def dip_depth(closes: list[float], window: int) -> float | None:
    """How far below the SMA the latest price sits, as a fraction (0.18 = 18% below). Used for ranking
    candidates (deeper dip = stronger signal per the WL17 gradient) and for logging. None if no SMA yet
    or price is at/above the SMA (not a dip)."""
    s = sma(closes, window)
    if s is None or s <= 0:
        return None
    depth = (s - closes[-1]) / s
    return depth if depth > 0 else 0.0
