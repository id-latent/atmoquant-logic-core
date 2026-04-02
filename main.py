# ══════════════════════════════════════════════════════════════════════════════
# main.py
# ══════════════════════════════════════════════════════════════════════════════

"""
AQL FastAPI Entry Point
  • /health            → Railway health probe
  • /admin/reset-breaker → operator circuit breaker reset
  • /admin/scan-now    → force an immediate scan cycle
  • Background task    → engine.run_forever()
"""
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
from core.risk import CircuitBreaker

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("aql.main")

app          = FastAPI(title="AtmoQuant Logic", version="1.0.0")
engine       = AQLEngine()
BANKROLL_USD = float(os.getenv("BANKROLL_USD", "200.0"))


@app.on_event("startup")
async def _start() -> None:
    log.info("Launching AQL engine background task — bankroll=$%.2f", BANKROLL_USD)
    asyncio.create_task(engine.run_forever(bankroll_usd=BANKROLL_USD))


@app.get("/health")
async def health() -> JSONResponse:
    s = CircuitBreaker().state
    return JSONResponse({
        "status":             "degraded" if s.circuit_breaker_active else "ok",
        "circuit_breaker":    s.circuit_breaker_active,
        "consecutive_losses": s.consecutive_losses,
        "total_trades":       s.total_trades,
        "total_pnl_usd":      s.total_pnl_usd,
        "version":            engine.VERSION,
    })


@app.post("/admin/reset-breaker")
async def reset_breaker() -> JSONResponse:
    CircuitBreaker().manual_reset()
    return JSONResponse({"status": "ok", "message": "Circuit breaker reset."})


@app.get("/admin/scan-now")
async def scan_now() -> JSONResponse:
    asyncio.create_task(engine.run_scan_cycle(bankroll_usd=BANKROLL_USD))
    return JSONResponse({"status": "triggered"})


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
