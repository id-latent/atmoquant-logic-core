# ==============================================================================
# main.py — FastAPI Entry Point (FIXED)
# ==============================================================================
"""
Fixes:
  BUG #6  : CircuitBreaker / PositionTracker / MarketCache diinstansiasi baru
            setiap HTTP request. Sekarang pakai instance dari engine (singleton).
  BUG #13 : @app.on_event("startup") deprecated di FastAPI >= 0.93.
            Diganti dengan lifespan context manager.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from config.settings import settings
from core.engine import AQLEngine
from core.location_registry import registry_summary

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("aql.main")

BANKROLL_USD = float(os.getenv("BANKROLL_USD", "200.0"))

# Singleton engine — dibuat sekali, dipakai oleh semua route
engine = AQLEngine()


# FIX BUG #13: Gunakan lifespan, bukan @app.on_event("startup") yang deprecated
@asynccontextmanager
async def lifespan(app: FastAPI):
    reg = registry_summary()
    log.info(
        "AQL v2.0.1 starting | bankroll=$%.2f | cities=%d",
        BANKROLL_USD, reg["total"],
    )
    task = asyncio.create_task(
        engine.run_forever(bankroll_usd=BANKROLL_USD)
    )
    yield
    # Shutdown: batalkan task background
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        log.info("Engine task cancelled — shutdown selesai.")


app = FastAPI(
    title="AtmoQuant Logic",
    version="2.0.1",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> JSONResponse:
    # FIX BUG #6: Pakai instance dari engine, bukan buat baru setiap request
    s       = engine.breaker.state
    tracker = engine.tracker
    cache   = engine.cache
    reg     = registry_summary()

    return JSONResponse({
        "status":              "degraded" if s.circuit_breaker_active else "ok",
        "version":             "2.0.1",
        "circuit_breaker":     s.circuit_breaker_active,
        "consecutive_losses":  s.consecutive_losses,
        "total_trades":        s.total_trades,
        "total_pnl_usd":       s.total_pnl_usd,
        "open_positions":      len(tracker.get_open_positions()),
        "cache_entries":       cache.get_stats()["total_entries"],
        "cities_tracked":      reg["total"],
    })


@app.post("/admin/reset-breaker")
async def reset_breaker() -> JSONResponse:
    # FIX BUG #6: Pakai instance engine
    engine.breaker.manual_reset()
    log.warning("Circuit breaker reset via admin endpoint.")
    return JSONResponse({
        "status":  "ok",
        "message": "Circuit breaker reset.",
    })


@app.get("/admin/scan-now")
async def scan_now() -> JSONResponse:
    asyncio.create_task(
        engine.run_scan_cycle(bankroll_usd=BANKROLL_USD)
    )
    return JSONResponse({"status": "triggered"})


@app.get("/admin/positions")
async def get_positions() -> JSONResponse:
    # FIX BUG #6: Pakai instance engine
    summary = engine.tracker.get_summary()
    return JSONResponse(summary)


@app.get("/admin/cache-stats")
async def cache_stats() -> JSONResponse:
    # FIX BUG #6: Pakai instance engine
    return JSONResponse(engine.cache.get_stats())


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
    )
