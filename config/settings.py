# ==============================================================================
# settings.py — Kode Pengaturan Parameter (FIXED)
# ==============================================================================
"""
Fixes:
  BUG #16 : DECISION_PHASE_HOURS: list  →  list[int]  (type hint lebih ketat)
  IMPROVEMENT: Semua field env var sekarang menggunakan os.getenv() dengan
               fallback kosong dan validasi eksplisit, sehingga error message
               lebih jelas saat variabel belum di-set.
  IMPROVEMENT: GAS_FEE_USD tidak pernah dipakai di kode manapun — diberi
               catatan deprecated.
  IMPROVEMENT: MAX_CONCURRENT_PER_CITY tidak dipakai secara konsisten —
               diberi catatan.
  NOTE BUG #8: TRIPLE_LOCK_VARIANCE_C = 1.0 masih valid. Sebelumnya ini
               diartikan sebagai "max range ≤ 1°C", sekarang setelah fix
               consensus.py diartikan "max std dev ≤ 1°C". Std dev lebih
               ketat dari range — dengan 3-4 model, range 2°C bisa
               menghasilkan std dev ~0.8°C. Nilai 1.0 masih reasonable,
               tapi operator mungkin perlu menyesuaikan setelah data live.
"""

import os
from dataclasses import dataclass, field
from typing import Optional


def _require_env(key: str) -> str:
    """
    Ambil variabel lingkungan. Raise ValueError yang informatif jika tidak ada.
    Lebih baik dari KeyError bawaan Python yang tidak menjelaskan solusinya.
    """
    val = os.environ.get(key)
    if not val:
        raise ValueError(
            f"Environment variable '{key}' tidak di-set atau kosong.\n"
            f"Tambahkan ke file .env atau Railway → Service → Variables."
        )
    return val


@dataclass
class AQLSettings:

    # ── Polymarket ─────────────────────────────────────────────────────────────
    POLY_GAMMA_BASE: str           = "https://gamma-api.polymarket.com"
    POLY_CLOB_BASE: str            = "https://clob.polymarket.com"
    POLY_PRIVATE_KEY: str          = field(
        default_factory=lambda: _require_env("POLY_PRIVATE_KEY")
    )
    POLY_CHAIN_ID: int             = 137

    # ── Open-Meteo ─────────────────────────────────────────────────────────────
    OPENMETEO_BASE: str            = "https://api.open-meteo.com/v1"

    # ── Discord 4-Channel ──────────────────────────────────────────────────────
    TERMINAL_WEBHOOK_URL: str      = field(
        default_factory=lambda: _require_env("TERMINAL_WEBHOOK_URL")
    )
    WEATHER_WEBHOOK_URL: str       = field(
        default_factory=lambda: _require_env("WEATHER_WEBHOOK_URL")
    )
    TRADE_WEBHOOK_URL: str         = field(
        default_factory=lambda: _require_env("TRADE_WEBHOOK_URL")
    )
    ALERTS_WEBHOOK_URL: str        = field(
        default_factory=lambda: _require_env("ALERTS_WEBHOOK_URL")
    )
    DISCORD_BOT_NAME: str          = "AQL NODE"
    DISCORD_AVATAR_URL: str        = "https://i.imgur.com/AtmoQuantLogo.png"

    # ── Risk Parameters ────────────────────────────────────────────────────────
    KELLY_FRACTION: float          = 0.25
    TRADING_FEE_PCT: float         = 0.017
    # GAS_FEE_USD: tidak pernah dipakai di kode manapun — deprecated
    # Dihapus dari kalkulasi EV untuk menghindari confusion.
    MAX_POSITION_USD: float        = 50.0
    CIRCUIT_BREAKER_LOSSES: int    = 3

    # ── Dynamic MIN_EDGE per Tier ──────────────────────────────────────────────
    MIN_EDGE_TIER1: float          = 0.05   # NYC, London, Dallas (ketat)
    MIN_EDGE_TIER2: float          = 0.06   # Chicago, Seoul, Sydney
    MIN_EDGE_TIER3: float          = 0.075  # Emerging markets (lebih ketat)

    # ── Quad-Lock Consensus ────────────────────────────────────────────────────
    # NOTE BUG #8: Sekarang ini adalah threshold untuk STANDARD DEVIATION (σ)
    # antar model, bukan range (max-min) seperti sebelumnya.
    # σ ≤ 1.0°C berarti semua model dalam kisaran ~±2°C (2σ) dari mean.
    # Operator bisa turunkan ke 0.8 untuk seleksi lebih ketat.
    TRIPLE_LOCK_VARIANCE_C: float  = 1.0

    # FIX BUG #16: Gunakan list[int] bukan list (untyped)
    DECISION_PHASE_HOURS: list[int] = field(
        default_factory=lambda: [0, 12]
    )

    # ── Golden Hour Guard ──────────────────────────────────────────────────────
    # Format: (open_min, open_max) dalam jam sebelum market close
    GOLDEN_HOUR_US: tuple          = (2, 10)
    # FIX BUG #11: Canada region tersendiri — window sama seperti US
    GOLDEN_HOUR_CANADA: tuple      = (2, 10)
    GOLDEN_HOUR_EUROPE: tuple      = (3, 12)
    GOLDEN_HOUR_ASIA: tuple        = (4, 14)
    GOLDEN_HOUR_OTHER: tuple       = (3, 12)

    # Kelly multiplier per Golden Hour status
    GOLDEN_HOUR_OPEN_MULT: float   = 1.0    # Kondisi optimal
    GOLDEN_HOUR_WARN_MULT: float   = 0.7    # Kurang optimal (jauh)
    GOLDEN_HOUR_NEAR_MULT: float   = 0.5    # Dekat close (likuiditas tipis)

    # Hard limits
    MAX_HOURS_TO_CLOSE: float      = 20.0   # Skip jika > 20 jam
    MIN_HOURS_TO_CLOSE: float      = 1.0    # Skip jika < 1 jam

    # ── Adaptive Liquidity ─────────────────────────────────────────────────────
    LIQUIDITY_BASE_MULTI: float    = 500.0  # Base untuk multi-outcome
    LIQUIDITY_BASE_BINARY: float   = 300.0  # Base untuk binary YES/NO
    LIQUIDITY_BASE_RANGE: float    = 200.0  # Base untuk range binary
    LIQUIDITY_HARD_FLOOR: float    = 100.0  # Tidak pernah di bawah ini

    # ── Position & Trade Limits ────────────────────────────────────────────────
    MAX_TRADES_PER_CITY: int       = 2      # Max 2 trades per kota per scan
    MAX_CONCURRENT_CITIES: int     = 5      # Max concurrent market processing
    # MAX_CONCURRENT_PER_CITY tidak diimplementasikan sebagai semaphore
    # terpisah di engine.py saat ini — nilainya mengikuti MAX_TRADES_PER_CITY
    MAX_CONCURRENT_PER_CITY: int   = 2

    # ── Market Cache ───────────────────────────────────────────────────────────
    CACHE_REANALYZE_CYCLES: int    = 2      # Re-analyze setiap 2 scan (30 menit)
    CACHE_PRICE_CHANGE_PCT: float  = 0.03   # Re-analyze jika harga berubah >3%

    # ── Exit Strategy / Stop Loss ──────────────────────────────────────────────
    STOP_LOSS_ENABLED: bool        = True
    STOP_LOSS_PCT: float           = 0.50   # Sell jika harga turun 50% dari entry
    TAKE_PROFIT_PCT: float         = 1.50   # Sell jika harga naik 150% dari entry
    EXIT_RETRY_DELAY_SEC: float    = 2.0    # Delay sebelum retry sell yang gagal

    # ── Volume Signal ──────────────────────────────────────────────────────────
    VOLUME_WARNING_ENABLED: bool   = True
    VOLUME_SPIKE_THRESHOLD: float  = 3.0    # Alert jika volume naik 300%
    VOLUME_KELLY_REDUCTION: float  = 0.6    # Kurangi Kelly 40% jika berlawanan

    # ── Big Edge Alert ─────────────────────────────────────────────────────────
    BIG_EDGE_THRESHOLD: float      = 0.15   # Alert Discord jika edge > 15%

    # ── Bankroll Safety ────────────────────────────────────────────────────────
    MINIMUM_BANKROLL_HALT: float   = 15.0
    MINIMUM_BANKROLL_WARNING: float = 50.0

    # ── Notifications ──────────────────────────────────────────────────────────
    HOURLY_HEARTBEAT: bool         = True
    WEEKLY_REPORT_DAY: int         = 0      # 0 = Senin (weekday() == 0)
    MODEL_DISAGREE_THRESHOLD: float = 3.0   # Alert jika model beda > 3°C

    # ── Runtime ────────────────────────────────────────────────────────────────
    POLL_INTERVAL_SECONDS: int     = 900    # 15 menit
    STATE_FILE: str                = "data/state.json"
    LOG_LEVEL: str                 = os.getenv("LOG_LEVEL", "INFO")

    def __post_init__(self) -> None:
        """Validasi nilai-nilai kritis setelah init."""
        if not (0 < self.KELLY_FRACTION <= 1):
            raise ValueError(f"KELLY_FRACTION harus antara 0 dan 1, dapat: {self.KELLY_FRACTION}")
        if not (0 < self.TRADING_FEE_PCT < 1):
            raise ValueError(f"TRADING_FEE_PCT tidak valid: {self.TRADING_FEE_PCT}")
        if self.MAX_POSITION_USD <= 0:
            raise ValueError(f"MAX_POSITION_USD harus > 0, dapat: {self.MAX_POSITION_USD}")
        if self.MINIMUM_BANKROLL_HALT >= self.MINIMUM_BANKROLL_WARNING:
            raise ValueError(
                f"MINIMUM_BANKROLL_HALT ({self.MINIMUM_BANKROLL_HALT}) "
                f"harus < MINIMUM_BANKROLL_WARNING ({self.MINIMUM_BANKROLL_WARNING})"
            )
        if self.STOP_LOSS_PCT <= 0 or self.STOP_LOSS_PCT >= 1:
            raise ValueError(f"STOP_LOSS_PCT harus antara 0 dan 1: {self.STOP_LOSS_PCT}")
        if self.LOG_LEVEL not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError(f"LOG_LEVEL tidak valid: {self.LOG_LEVEL}")

    def get_min_edge(self, tier: int) -> float:
        """Return minimum edge berdasarkan city tier."""
        return {
            1: self.MIN_EDGE_TIER1,
            2: self.MIN_EDGE_TIER2,
            3: self.MIN_EDGE_TIER3,
        }.get(tier, self.MIN_EDGE_TIER2)

    def get_golden_hour_window(self, region: str) -> tuple:
        """Return golden hour window berdasarkan region."""
        return {
            "US":         self.GOLDEN_HOUR_US,
            "Canada":     self.GOLDEN_HOUR_CANADA,   # FIX BUG #11
            "Europe":     self.GOLDEN_HOUR_EUROPE,
            "Asia":       self.GOLDEN_HOUR_ASIA,
            "MiddleEast": self.GOLDEN_HOUR_ASIA,
            "Oceania":    self.GOLDEN_HOUR_OTHER,
            "Other":      self.GOLDEN_HOUR_OTHER,
        }.get(region, self.GOLDEN_HOUR_OTHER)


settings = AQLSettings()
