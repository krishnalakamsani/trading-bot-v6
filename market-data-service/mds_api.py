"""mds_api.py — FastAPI REST server for the Market Data Service.

Endpoints consumed by the backend's mds_client.py:
  GET /health
  GET /v1/candles/last?symbol=NIFTY&timeframe_seconds=5&limit=2
  GET /v1/candles/range?symbol=NIFTY&timeframe_seconds=5&start=...&end=...
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse

from ts_db import fetch_last_candles, fetch_candles_range, init_pool

logger = logging.getLogger(__name__)

app = FastAPI(title="Market Data Service", version="1.0.0")


@app.on_event("startup")
async def _startup():
    await init_pool()
    logger.info("[API] TimescaleDB pool ready")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/v1/candles/last")
async def candles_last(
    symbol: str = Query(..., description="Index symbol e.g. NIFTY"),
    timeframe_seconds: int = Query(..., description="Candle size in seconds"),
    limit: int = Query(2, ge=1, le=20000),
):
    """Return the last N candles, oldest first."""
    try:
        candles = await fetch_last_candles(
            symbol=symbol,
            timeframe_seconds=timeframe_seconds,
            limit=limit,
        )
        return JSONResponse({"candles": candles})
    except Exception as e:
        logger.error(f"[API] /v1/candles/last error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/candles/range")
async def candles_range(
    symbol: str = Query(...),
    timeframe_seconds: int = Query(...),
    start: str = Query(..., description="ISO8601 UTC start timestamp"),
    end: str   = Query(..., description="ISO8601 UTC end timestamp"),
    limit: int = Query(200000, ge=1, le=200000),
):
    """Return candles in [start, end] range, oldest first."""
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt   = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid timestamp: {e}")

    try:
        candles = await fetch_candles_range(
            symbol=symbol,
            timeframe_seconds=timeframe_seconds,
            start=start_dt,
            end=end_dt,
            limit=limit,
        )
        return JSONResponse({"candles": candles})
    except Exception as e:
        logger.error(f"[API] /v1/candles/range error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
