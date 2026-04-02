# ══════════════════════════════════════════════════════════════════════════════
# settings.py = Kode Pengaturan Parameter
# ══════════════════════════════════════════════════════════════════════════════

"""
AQL Configuration — All credentials read strictly from environment variables.
Never hardcode secrets.
"""
import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AQLSettings:
    # ── Polymarket ─────────────────────────────────────────────────────────
    POLY_GAMMA_BASE: str          = "https://gamma-api.polymarket.com"
    POLY_CLOB_BASE: str           = "https://clob.polymarket.com"
    POLY_PRIVATE_KEY: str         = field(default_factory=lambda: os.environ["POLY_PRIVATE_KEY"])
    POLY_CHAIN_ID: int            = 137  # Polygon Mainnet

    # ── Open-Meteo (no key required) ───────────────────────────────────────
    OPENMETEO_BASE: str           = "https://api.open-meteo.com/v1"

    # ── Discord ────────────────────────────────────────────────────────────
    DISCORD_WEBHOOK_URL: str      = field(default_factory=lambda: os.environ["DISCORD_WEBHOOK_URL"])
    DISCORD_BOT_NAME: str         = "AQL NODE"
    DISCORD_AVATAR_URL: str       = "https://i.imgur.com/AtmoQuantLogo.png"

    # ── Risk Parameters ────────────────────────────────────────────────────
    KELLY_FRACTION: float         = 0.25    # Fractional Kelly (conservative)
    MIN_EDGE_PCT: float           = 0.05    # 5% minimum edge after friction
    TRADING_FEE_PCT: float        = 0.017   # 1.7% Polymarket fee
    GAS_FEE_USD: float            = 0.05    # Estimated gas cost per trade
    MAX_POSITION_USD: float       = 50.0    # Hard cap per trade
    CIRCUIT_BREAKER_LOSSES: int   = 3       # Consecutive losses before halt

    # ── Consensus Engine ───────────────────────────────────────────────────
    TRIPLE_LOCK_VARIANCE_C: float = 1.0     # Max inter-model variance (°C)
    DECISION_PHASE_HOURS: list    = field(default_factory=lambda: [0, 12])  # 00z, 12z
    ENTRY_WINDOW_HOURS_BEFORE: int = 13     # Target 12–14h before resolution

    # ── Runtime ────────────────────────────────────────────────────────────
    POLL_INTERVAL_SECONDS: int    = 900     # 15-minute monitoring loop
    STATE_FILE: str               = "data/state.json"
    LOG_LEVEL: str                = os.getenv("LOG_LEVEL", "INFO")


settings = AQLSettings()
