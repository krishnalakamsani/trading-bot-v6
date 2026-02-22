"""market_data_service.py — fetches ticks from Dhan, stores in TimescaleDB.

Collects ALL configured indices simultaneously.
Only runs during market hours (9:15 AM – 3:30 PM IST, Mon–Fri).
When market is closed it sleeps and checks every 60 s.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import List

from candle_builder import CandleBuilder

logger = logging.getLogger(__name__)

# All timeframes built for every index simultaneously
_TIMEFRAMES = [5, 15, 30, 60, 300, 900]

# IST offset
_IST = timezone(timedelta(hours=5, minutes=30))


def _is_market_open() -> bool:
    """True if current IST time is within market hours on a weekday."""
    now_ist = datetime.now(_IST)
    if now_ist.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    market_open  = now_ist.replace(hour=9,  minute=15, second=0, microsecond=0)
    market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now_ist <= market_close


def _all_symbols() -> List[str]:
    """Return all index symbols from the shared indices config."""
    try:
        from indices import get_available_indices
        return [s.upper() for s in get_available_indices()]
    except Exception:
        # Fallback if indices.py not available
        return ["NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY"]


class MarketDataService:
    """Polls Dhan for every configured index; writes ticks + candles to TimescaleDB."""

    def __init__(self, dhan_api) -> None:
        self.dhan = dhan_api
        self.running = False
        self._task: asyncio.Task | None = None
        # builders[symbol][timeframe] = CandleBuilder
        self._builders: dict[str, dict[int, CandleBuilder]] = {}
        self._error_backoff: float = 0.0

    async def start(self) -> None:
        if self.running:
            return
        from ts_db import init_pool
        await init_pool()
        self.running = True
        self._task = asyncio.create_task(self._loop(), name="mds_collector")
        logger.info(f"[MDS] Started — collecting: {_all_symbols()}")

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
        logger.info("[MDS] Stopped")

    # ── main loop ─────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        import os
        poll_s = float(os.environ.get("MDS_POLL_SECONDS", "1.0") or "1.0")
        poll_s = max(0.25, min(5.0, poll_s))

        while self.running:
            try:
                if not _is_market_open():
                    # Log once per minute so we know it's alive but not spamming
                    now_ist = datetime.now(_IST)
                    logger.info(
                        f"[MDS] Market closed ({now_ist.strftime('%H:%M IST, %A')}) "
                        f"— sleeping 60s"
                    )
                    await asyncio.sleep(60)
                    continue

                symbols = _all_symbols()
                now     = datetime.now(timezone.utc)

                # Fetch all indices concurrently
                ltps = await asyncio.gather(
                    *[self._fetch_ltp(sym) for sym in symbols],
                    return_exceptions=True,
                )

                for symbol, ltp in zip(symbols, ltps):
                    if isinstance(ltp, Exception) or not ltp or ltp <= 0:
                        continue
                    await self._save_tick(symbol=symbol, ltp=ltp, ts=now)
                    await self._update_candles(symbol=symbol, ltp=ltp, ts=now)

                self._error_backoff = 0.0
                await asyncio.sleep(poll_s)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._error_backoff = min(60.0, (self._error_backoff or 1.0) * 2)
                logger.error(
                    f"[MDS] Loop error: {e} — backoff {self._error_backoff:.0f}s"
                )
                await asyncio.sleep(self._error_backoff)

    # ── helpers ───────────────────────────────────────────────────────────────

    async def _fetch_ltp(self, symbol: str) -> float | None:
        try:
            ltp = await asyncio.to_thread(self.dhan.get_index_ltp, symbol)
            return float(ltp) if ltp and float(ltp) > 0 else None
        except Exception as e:
            logger.debug(f"[MDS] fetch {symbol} error: {e}")
            return None

    async def _save_tick(self, *, symbol: str, ltp: float, ts: datetime) -> None:
        try:
            from ts_db import insert_tick
            await insert_tick(time=ts, symbol=symbol, ltp=ltp)
        except Exception as e:
            logger.debug(f"[MDS] insert_tick {symbol} error: {e}")

    async def _update_candles(self, *, symbol: str, ltp: float, ts: datetime) -> None:
        if symbol not in self._builders:
            self._builders[symbol] = {
                tf: CandleBuilder(symbol, tf) for tf in _TIMEFRAMES
            }

        from ts_db import upsert_candle
        for tf, builder in self._builders[symbol].items():
            closed = builder.on_tick(ltp, ts)
            live = builder.live
            if live:
                try:
                    await upsert_candle(
                        time=live.open_time, symbol=symbol,
                        timeframe_seconds=tf,
                        open=live.open, high=live.high,
                        low=live.low,  close=live.close,
                        volume=live.volume,
                    )
                except Exception as e:
                    logger.debug(f"[MDS] upsert candle {symbol}/{tf}s error: {e}")

            if closed:
                logger.info(
                    f"[MDS] {symbol}/{tf}s closed — "
                    f"O={closed.open} H={closed.high} "
                    f"L={closed.low} C={closed.close}"
                )
