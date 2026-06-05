"""Aggregate trade ticks into fixed-interval OHLCV candles per mint."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(slots=True)
class Candle:
    ts: int            # bucket start (epoch seconds)
    open: float
    high: float
    low: float
    close: float
    volume_sol: float
    trades: int


class CandleBuilder:
    def __init__(self, interval_s: int = 5, maxlen: int = 720) -> None:
        self.interval = interval_s
        self.candles: deque[Candle] = deque(maxlen=maxlen)

    def add(self, ts: float, price: float, volume_sol: float) -> None:
        if price <= 0:
            return
        bucket = int(ts // self.interval) * self.interval
        if self.candles and self.candles[-1].ts == bucket:
            c = self.candles[-1]
            c.high = max(c.high, price)
            c.low = min(c.low, price)
            c.close = price
            c.volume_sol += volume_sol
            c.trades += 1
        else:
            self.candles.append(Candle(bucket, price, price, price, price, volume_sol, 1))

    def closes(self) -> list[float]:
        return [c.close for c in self.candles]

    def volumes(self) -> list[float]:
        return [c.volume_sol for c in self.candles]

    def to_dataframe(self):
        import pandas as pd

        if not self.candles:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume_sol", "trades"])
        df = pd.DataFrame([c.__dict__ for c in self.candles])
        df["dt"] = pd.to_datetime(df["ts"], unit="s")
        return df.set_index("dt")
