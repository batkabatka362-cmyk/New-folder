"""Lightweight, dependency-free indicators for the rule layer.

Richer indicators (RSI/MACD/breakouts) can use the `ta` library on
CandleBuilder.to_dataframe() in Phase 2; these pure-python helpers keep the
hot path fast and import-free.
"""
from __future__ import annotations


def sma(values: list[float], period: int) -> float:
    if period <= 0 or not values:   # values[-0:] would silently average the whole series
        return 0.0
    window = values[-period:]
    return sum(window) / len(window)


def ema(values: list[float], period: int) -> float:
    if period <= 0 or not values:   # period == -1 would divide by zero below
        return 0.0
    k = 2.0 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def volume_spike_ratio(volumes: list[float], lookback: int = 10) -> float:
    """Latest bucket volume divided by trailing average (excludes latest)."""
    if len(volumes) < 2:
        return 0.0
    latest = volumes[-1]
    trailing = volumes[-(lookback + 1):-1]
    avg = sum(trailing) / len(trailing) if trailing else 0.0
    return latest / avg if avg > 0 else 0.0
