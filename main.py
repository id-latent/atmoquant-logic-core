# ==============================================================================
# main.py — FastAPI Entry Point
# ==============================================================================
from __future__ import annotations

import asyncio
import logging
import os
import sys

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from config.settings import settings
from core.engine import AQLEngine
from core.location_registry import registry_summary
from core.market_cache import MarketCache
from core.position_tracker import PositionTracker
from core.risk import CircuitBreaker

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("aql.main")

app          = FastAPI(title="AtmoQuant Logic", version="2.0.0")
engine       = AQLEngine()
BANKROLL_USD = float(os.getenv("BANKROLL_USD", "200.0"))


@app.on_event("startup")
async def _start() -> None:
    reg = registry_summary()
    log.info(
        "AQL v2.0.0 starting | bankroll=$%.2f | cities=%d",
        BANKROLL_USD, reg["total"],
    )
    asyncio.create_task(
        engine.run_forever(bankroll_usd=BANKROLL_USD)
    )


@app.get("/health")
async def health() -> JSONResponse:
    s       = CircuitBreaker().state
    tracker = PositionTracker()
    cache   = MarketCache()
    reg     = registry_summary()

    return JSONResponse({
        "status":              "degraded" if s.circuit_breaker_active else "ok",
        "version":             "2.0.0",
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
    CircuitBreaker().manual_reset()
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
    """List semua open positions."""
    tracker = PositionTracker()
    summary = tracker.get_summary()
    return JSONResponse(summary)


@app.get("/admin/cache-stats")
async def cache_stats() -> JSONResponse:
    """Stats market analysis cache."""
    cache = MarketCache()
    return JSONResponse(cache.get_stats())


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
    )
