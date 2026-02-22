"""market_data_service.py — fetches ticks from Dhan, stores in TimescaleDB.

Responsibilities:
  - Poll Dhan every ~1s for index LTP
  - Write every tick to TimescaleDB `ticks` table
  - Build OHLC candles in memory per all supported timeframes
  - Upsert live candle to TimescaleDB on every tick
  - Log closed candles on period rollover

Does NOT touch SQLite — that stays for trades/config/strategies.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from candle_builder import CandleBuilder

logger = logging.getLogger(__name__)

_TIMEFRAMES = [5, 15, 30, 60, 300, 900]


class MarketDataService:
    def __init__(self, dhan_api) -> None:
        self.dhan = dhan_api
        self.running = False
        self._task: asyncio.Task | None = None
        self._builders: dict[str, dict[int, CandleBuilder]] = {}
        self._error_backoff: float = 0.0

    async def start(self) -> None:
        if self.running:
            return
        from ts_db import init_pool
        await init_pool()
        self.running = True
        self._task = asyncio.create_task(self._loop(), name="mds_collector")
        logger.info("[MDS] MarketDataService started")

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        from ts_db import close_pool
        await close_pool()
        logger.info("[MDS] MarketDataService stopped")

    async def _loop(self) -> None:
        poll_s = float(os.environ.get("MDS_POLL_SECONDS", "1.0") or "1.0")
        poll_s = max(0.25, min(5.0, poll_s))

        while self.running:
            try:
                symbol = (os.environ.get("SELECTED_INDEX") or "NIFTY").upper()
                now    = datetime.now(timezone.utc)

                index_ltp = await self._fetch_ltp(symbol)

                if index_ltp and index_ltp > 0:
                    await self._save_tick(symbol=symbol, ltp=index_ltp, ts=now)
                    await self._update_candles(symbol=symbol, ltp=index_ltp, ts=now)

                self._error_backoff = 0.0
                await asyncio.sleep(poll_s)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._error_backoff = min(60.0, (self._error_backoff or 1.0) * 2)
                logger.error(f"[MDS] Loop error: {e} — backoff {self._error_backoff:.0f}s")
                await asyncio.sleep(self._error_backoff)

    async def _fetch_ltp(self, symbol: str) -> float | None:
        try:
            ltp = await asyncio.to_thread(self.dhan.get_index_ltp, symbol)
            return float(ltp) if ltp else None
        except Exception as e:
            logger.debug(f"[MDS] Dhan fetch error: {e}")
            return None

    async def _save_tick(self, *, symbol: str, ltp: float, ts: datetime) -> None:
        try:
            from ts_db import insert_tick
            await insert_tick(time=ts, symbol=symbol, ltp=ltp)
        except Exception as e:
            logger.debug(f"[MDS] insert_tick error: {e}")

    async def _update_candles(self, *, symbol: str, ltp: float, ts: datetime) -> None:
        if symbol not in self._builders:
            self._builders[symbol] = {tf: CandleBuilder(symbol, tf) for tf in _TIMEFRAMES}

        from ts_db import upsert_candle
        for tf, builder in self._builders[symbol].items():
            closed = builder.on_tick(ltp, ts)
            live = builder.live
            if live:
                try:
                    await upsert_candle(
                        time=live.open_time, symbol=symbol, timeframe_seconds=tf,
                        open=live.open, high=live.high, low=live.low, close=live.close,
                        volume=live.volume,
                    )
                except Exception as e:
                    logger.debug(f"[MDS] upsert candle error ({tf}s): {e}")

            if closed:
                logger.info(
                    f"[MDS] Candle closed {symbol}/{tf}s "
                    f"O={closed.open} H={closed.high} L={closed.low} C={closed.close}"
                )
