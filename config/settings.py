# ══════════════════════════════════════════════════════════════════════════════
# settings.py = Kode Pengaturan Parameter
# ══════════════════════════════════════════════════════════════════════════════

import os
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class AQLSettings:
    # ── Polymarket ─────────────────────────────────────────────────────────
    POLY_GAMMA_BASE: str          = "https://gamma-api.polymarket.com"
    POLY_CLOB_BASE: str           = "https://clob.polymarket.com"
    POLY_PRIVATE_KEY: str         = field(default_factory=lambda: os.environ["POLY_PRIVATE_KEY"])
    POLY_CHAIN_ID: int            = 137 

    # ── Open-Meteo ─────────────────────────────────────────────────────────
    OPENMETEO_BASE: str           = "https://api.open-meteo.com/v1"

    # ── Discord Multi-Channel (4 Routes) ───────────────────────────────────
    TERMINAL_WEBHOOK_URL: str     = field(default_factory=lambda: os.environ["TERMINAL_WEBHOOK_URL"])
    WEATHER_WEBHOOK_URL: str      = field(default_factory=lambda: os.environ["WEATHER_WEBHOOK_URL"])
    ALERTS_WEBHOOK_URL: str       = field(default_factory=lambda: os.environ["ALERTS_WEBHOOK_URL"])
    TRADE_WEBHOOK_URL: str        = field(default_factory=lambda: os.environ["TRADE_WEBHOOK_URL"])
    
    DISCORD_BOT_NAME: str         = "AQL NODE"
    DISCORD_AVATAR_URL: str       = "https://i.imgur.com/AtmoQuantLogo.png"

    # ── Risk Parameters ────────────────────────────────────────────────────
    KELLY_FRACTION: float         = 0.25    
    MIN_EDGE_PCT: float           = 0.05    
    TRADING_FEE_PCT: float        = 0.017   
    GAS_FEE_USD: float            = 0.05    
    MAX_POSITION_USD: float       = 50.0    
    CIRCUIT_BREAKER_LOSSES: int   = 3       

    # ── Consensus Engine ───────────────────────────────────────────────────
    TRIPLE_LOCK_VARIANCE_C: float = 1.0     
    DECISION_PHASE_HOURS: list    = field(default_factory=lambda: [0, 12])  
    ENTRY_WINDOW_HOURS_BEFORE: int = 13     

    # ── Runtime ────────────────────────────────────────────────────────────
    POLL_INTERVAL_SECONDS: int    = 900     
    STATE_FILE: str               = "data/state.json"
    LOG_LEVEL: str                = os.getenv("LOG_LEVEL", "INFO")

settings = AQLSettings()
