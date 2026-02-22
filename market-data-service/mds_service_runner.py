"""mds_service_runner.py — starts both the collector and the REST API.

Two async tasks run concurrently:
  1. MarketDataService — polls Dhan, writes ticks+candles to TimescaleDB
  2. Uvicorn           — serves the FastAPI REST endpoints on port 8002
"""
import asyncio
import logging
import os
import signal

import uvicorn

logger = logging.getLogger(__name__)


async def _run():
    # ── Dhan credentials ──────────────────────────────────────────────────────
    dhan_access = os.environ.get("DHAN_ACCESS_TOKEN", "")
    dhan_client = os.environ.get("DHAN_CLIENT_ID", "")

    if not dhan_access or not dhan_client:
        logger.error("[MDS] DHAN_ACCESS_TOKEN / DHAN_CLIENT_ID not set — cannot start")
        return

    from dhan_api import DhanAPI
    from market_data_service import MarketDataService
    from mds_api import app

    dhan = DhanAPI(dhan_access, dhan_client)
    mds  = MarketDataService(dhan)

    # ── graceful shutdown ─────────────────────────────────────────────────────
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    # ── start collector ───────────────────────────────────────────────────────
    await mds.start()

    # ── start REST API ────────────────────────────────────────────────────────
    cfg = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8002,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(cfg)
    api_task = asyncio.create_task(server.serve(), name="mds_api")

    logger.info("[MDS] Collector + REST API running on :8002")

    try:
        await stop_event.wait()
    finally:
        server.should_exit = True
        await api_task
        await mds.stop()
        logger.info("[MDS] Shutdown complete")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
