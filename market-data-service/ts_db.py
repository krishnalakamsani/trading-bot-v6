"""ts_db.py — TimescaleDB (PostgreSQL) client for the MDS service.

Owns:
  - Connection pool (asyncpg)
  - Schema initialisation (ticks + candles hypertables)
  - Write: insert_tick, insert_candle, upsert_candle
  - Read:  fetch_last_candles, fetch_candles_range

SQLite (trading.db) is NOT touched here — it stays for trades/config/strategies.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


def _dsn() -> str:
    return os.environ.get(
        "TIMESCALE_URL",
        "postgresql://tsdb:tsdb_pass@timescaledb:5432/marketdata",
    )


async def init_pool() -> None:
    global _pool
    if _pool is not None:
        return
    dsn = _dsn()
    logger.info(f"[TSDB] Connecting to TimescaleDB: {dsn.split('@')[-1]}")
    _pool = await asyncpg.create_pool(dsn=dsn, min_size=2, max_size=10)
    await _init_schema()
    logger.info("[TSDB] Connected and schema ready")


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("TimescaleDB pool not initialised — call init_pool() first")
    return _pool


# ── Schema ────────────────────────────────────────────────────────────────────

async def _init_schema() -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        # Enable TimescaleDB extension
        await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;")

        # Raw ticks — one row per Dhan poll
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS ticks (
                time                TIMESTAMPTZ     NOT NULL,
                symbol              TEXT            NOT NULL,
                ltp                 DOUBLE PRECISION NOT NULL,
                option_security_id  TEXT,
                option_ltp          DOUBLE PRECISION
            );
        """)
        try:
            await conn.execute(
                "SELECT create_hypertable('ticks', 'time', if_not_exists => TRUE);"
            )
        except Exception:
            pass  # already a hypertable

        # OHLC candles — one row per (symbol, timeframe, candle_open_time)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS candles (
                time                TIMESTAMPTZ     NOT NULL,
                symbol              TEXT            NOT NULL,
                timeframe_seconds   INTEGER         NOT NULL,
                open                DOUBLE PRECISION,
                high                DOUBLE PRECISION,
                low                 DOUBLE PRECISION,
                close               DOUBLE PRECISION,
                volume              INTEGER         DEFAULT 0
            );
        """)
        try:
            await conn.execute(
                "SELECT create_hypertable('candles', 'time', if_not_exists => TRUE);"
            )
        except Exception:
            pass

        # Unique constraint so upsert works
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS candles_unique
                ON candles (symbol, timeframe_seconds, time);
        """)

        # Retention: keep 90 days of ticks, 365 days of candles
        try:
            await conn.execute(
                "SELECT add_retention_policy('ticks', INTERVAL '90 days', if_not_exists => TRUE);"
            )
            await conn.execute(
                "SELECT add_retention_policy('candles', INTERVAL '365 days', if_not_exists => TRUE);"
            )
        except Exception:
            pass  # older TimescaleDB or community edition — skip


# ── Writes ────────────────────────────────────────────────────────────────────

async def insert_tick(
    *,
    time,           # datetime with tzinfo=UTC
    symbol: str,
    ltp: float,
    option_security_id: str | None = None,
    option_ltp: float | None = None,
) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO ticks (time, symbol, ltp, option_security_id, option_ltp)
            VALUES ($1, $2, $3, $4, $5)
            """,
            time, symbol.upper(), ltp, option_security_id, option_ltp,
        )


async def upsert_candle(
    *,
    time,           # candle open time (datetime UTC)
    symbol: str,
    timeframe_seconds: int,
    open: float,
    high: float,
    low: float,
    close: float,
    volume: int = 0,
) -> None:
    """Insert or update a candle. Called every tick to keep the live candle current."""
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO candles (time, symbol, timeframe_seconds, open, high, low, close, volume)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (symbol, timeframe_seconds, time)
            DO UPDATE SET
                high   = GREATEST(candles.high,  EXCLUDED.high),
                low    = LEAST   (candles.low,   EXCLUDED.low),
                close  = EXCLUDED.close,
                volume = candles.volume + EXCLUDED.volume
            """,
            time, symbol.upper(), int(timeframe_seconds),
            float(open), float(high), float(low), float(close), int(volume),
        )


# ── Reads ─────────────────────────────────────────────────────────────────────

async def fetch_last_candles(
    *,
    symbol: str,
    timeframe_seconds: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Return the last N closed candles, oldest first."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT time, open, high, low, close, volume
            FROM candles
            WHERE symbol = $1 AND timeframe_seconds = $2
            ORDER BY time DESC
            LIMIT $3
            """,
            symbol.upper(), int(timeframe_seconds), int(limit),
        )
    # Return oldest-first so callers can feed them into indicators in order
    result = []
    for r in reversed(rows):
        result.append({
            "ts":     r["time"].isoformat(),
            "open":   r["open"],
            "high":   r["high"],
            "low":    r["low"],
            "close":  r["close"],
            "volume": r["volume"],
        })
    return result


async def fetch_candles_range(
    *,
    symbol: str,
    timeframe_seconds: int,
    start,      # datetime UTC
    end,        # datetime UTC
    limit: int = 200_000,
) -> list[dict[str, Any]]:
    """Return candles in [start, end] inclusive, oldest first."""
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT time, open, high, low, close, volume
            FROM candles
            WHERE symbol = $1
              AND timeframe_seconds = $2
              AND time >= $3
              AND time <= $4
            ORDER BY time ASC
            LIMIT $5
            """,
            symbol.upper(), int(timeframe_seconds), start, end, int(limit),
        )
    return [
        {
            "ts":     r["time"].isoformat(),
            "open":   r["open"],
            "high":   r["high"],
            "low":    r["low"],
            "close":  r["close"],
            "volume": r["volume"],
        }
        for r in rows
    ]
