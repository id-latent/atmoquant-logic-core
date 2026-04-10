# ==============================================================================
# main.py — FastAPI Entry Point (FIXED v2)
# ==============================================================================
from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

# Support local development dengan .env file
# Di Railway, env vars di-inject langsung — load_dotenv() tidak berbahaya jika file tidak ada
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv opsional

from config.settings import settings
from core.engine import AQLEngine
from core.location_registry import registry_summary
from notifications.notifier import close_http_client

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("aql.main")

BANKROLL_USD = float(os.getenv("BANKROLL_USD", "200.0"))
engine       = AQLEngine()


def _handle_task_exception(task: asyncio.Task) -> None:
    """Log exception dari background task yang tidak di-await."""
    try:
        exc = task.exception()
        if exc:
            log.critical(
                "Background task '%s' crash: %s",
                task.get_name(), exc, exc_info=exc,
            )
    except asyncio.CancelledError:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    reg = registry_summary()
    log.info("AQL v2.0.1 starting | bankroll=$%.2f | cities=%d", BANKROLL_USD, reg["total"])
    task = asyncio.create_task(
        engine.run_forever(bankroll_usd=BANKROLL_USD),
        name="aql-engine",
    )
    task.add_done_callback(_handle_task_exception)
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        log.info("Engine task cancelled.")
    await close_http_client()


app = FastAPI(title="AtmoQuant Logic", version="2.0.1", lifespan=lifespan)


@app.get("/health")
async def health() -> JSONResponse:
    s   = engine.breaker.state
    reg = registry_summary()
    return JSONResponse({
        "status":             "degraded" if s.circuit_breaker_active else "ok",
        "version":            "2.0.1",
        "circuit_breaker":    s.circuit_breaker_active,
        "consecutive_losses": s.consecutive_losses,
        "total_trades":       s.total_trades,
        "total_pnl_usd":      s.total_pnl_usd,
        "open_positions":     len(engine.tracker.get_open_positions()),
        "cache_entries":      engine.cache.get_stats()["total_entries"],
        "cities_tracked":     reg["total"],
    })


@app.post("/admin/reset-breaker")
async def reset_breaker() -> JSONResponse:
    engine.breaker.manual_reset()
    log.warning("Circuit breaker reset via admin endpoint.")
    return JSONResponse({"status": "ok", "message": "Circuit breaker reset."})


@app.get("/admin/scan-now")
async def scan_now() -> JSONResponse:
    asyncio.create_task(engine.run_scan_cycle(bankroll_usd=BANKROLL_USD))
    return JSONResponse({"status": "triggered"})


@app.get("/admin/positions")
async def get_positions() -> JSONResponse:
    return JSONResponse(engine.tracker.get_summary())


@app.get("/admin/cache-stats")
async def cache_stats() -> JSONResponse:
    return JSONResponse(engine.cache.get_stats())


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
