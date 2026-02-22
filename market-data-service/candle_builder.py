"""candle_builder.py — builds OHLC candles from raw ticks.

One CandleBuilder instance per (symbol, timeframe_seconds).
On every tick it updates the live candle in TimescaleDB.
When the candle period rolls over it returns the closed candle.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _floor_ts(dt: datetime, timeframe_seconds: int) -> datetime:
    """Floor a datetime to the nearest candle boundary."""
    epoch = int(dt.timestamp())
    floored = epoch - (epoch % timeframe_seconds)
    return datetime.fromtimestamp(floored, tz=timezone.utc)


@dataclass
class LiveCandle:
    open_time: datetime
    open:  float
    high:  float
    low:   float
    close: float
    volume: int = 0

    def update(self, ltp: float) -> None:
        self.high  = max(self.high, ltp)
        self.low   = min(self.low,  ltp)
        self.close = ltp
        self.volume += 1


class CandleBuilder:
    """Aggregates ticks into OHLC candles for one (symbol, timeframe)."""

    def __init__(self, symbol: str, timeframe_seconds: int) -> None:
        self.symbol = symbol.upper()
        self.timeframe_seconds = timeframe_seconds
        self._live: Optional[LiveCandle] = None

    def on_tick(self, ltp: float, ts: datetime) -> Optional[LiveCandle]:
        """Feed a tick. Returns the *closed* candle if the period rolled over, else None.

        The live candle is updated in-place every tick so TimescaleDB always
        has the current partial candle (useful for real-time charts).
        """
        candle_open = _floor_ts(ts, self.timeframe_seconds)

        if self._live is None:
            # First tick ever
            self._live = LiveCandle(
                open_time=candle_open,
                open=ltp, high=ltp, low=ltp, close=ltp,
            )
            return None

        if candle_open > self._live.open_time:
            # Period rolled over — close previous candle, open new one
            closed = self._live
            self._live = LiveCandle(
                open_time=candle_open,
                open=ltp, high=ltp, low=ltp, close=ltp,
            )
            logger.debug(
                f"[CANDLE] {self.symbol}/{self.timeframe_seconds}s closed: "
                f"O={closed.open} H={closed.high} L={closed.low} C={closed.close}"
            )
            return closed

        # Same period — just update
        self._live.update(ltp)
        return None

    @property
    def live(self) -> Optional[LiveCandle]:
        return self._live
