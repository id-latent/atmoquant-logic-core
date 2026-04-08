# ==============================================================================
# core/location_registry.py — Global City Registry
# ==============================================================================
"""
AQL Global Location Registry
90+ kota dengan metadata lengkap:
  lat, lon   : koordinat Open-Meteo
  tz         : IANA timezone untuk Golden Hour
  region     : US / Europe / Asia / Oceania / MiddleEast / Other
  unit       : F atau C (standar Polymarket per region)
  tier       : 1=high liquidity, 2=medium, 3=emerging

Unit Rules (Polymarket standard):
  US          → F (National Weather Service standard)
  Canada      → C
  Europe      → C (termasuk UK/London — Met Office standard)
  Asia        → C
  Middle East → C
  Oceania     → C
  Latin Am    → C
  Africa      → C
"""
from __future__ import annotations

import re
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from functools import lru_cache
from typing import Optional

import pytz

from config.settings import settings

log = logging.getLogger("aql.registry")


# ── Enums ─────────────────────────────────────────────────────────────────────

class GoldenHourStatus(str, Enum):
    OPEN = "OPEN"    # Kondisi optimal → Kelly ×1.0
    WARN = "WARN"    # Kurang optimal jauh → Kelly ×0.7
    NEAR = "NEAR"    # Dekat close, likuiditas tipis → Kelly ×0.5
    SKIP = "SKIP"    # Di luar semua window → tidak trade


class MarketUnit(str, Enum):
    FAHRENHEIT = "F"
    CELSIUS    = "C"


# ── City Data ─────────────────────────────────────────────────────────────────

@dataclass
class CityInfo:
    key: str
    lat: float
    lon: float
    tz: str
    region: str
    unit: str
    tier: int

    @property
    def timezone(self):
        return pytz.timezone(self.tz)

    def local_now(self) -> datetime:
        return datetime.now(self.timezone)

    def __repr__(self) -> str:
        return (
            f"CityInfo({self.key!r}, "
            f"tier={self.tier}, "
            f"region={self.region!r}, "
            f"unit={self.unit!r})"
        )


# ── Raw Registry Data ─────────────────────────────────────────────────────────
# Format: key → (lat, lon, tz, region, unit, tier)

_RAW: dict[str, tuple] = {

    # ==========================================================================
    # UNITED STATES
    # ==========================================================================

    # New York & North East — Tier 1
    "new york":      (40.7128, -74.0060, "America/New_York",    "US", "F", 1),
    "nyc":           (40.7128, -74.0060, "America/New_York",    "US", "F", 1),
    "jfk":           (40.6413, -73.7781, "America/New_York",    "US", "F", 1),
    "lga":           (40.7769, -73.8740, "America/New_York",    "US", "F", 1),
    "central park":  (40.7829, -73.9654, "America/New_York",    "US", "F", 1),
    "newark":        (40.6895, -74.1745, "America/New_York",    "US", "F", 2),
    "ewr":           (40.6895, -74.1745, "America/New_York",    "US", "F", 2),
    "coney island":  (40.5749, -73.9859, "America/New_York",    "US", "F", 2),
    "the battery":   (40.7033, -74.0170, "America/New_York",    "US", "F", 2),
    "boston":        (42.3601, -71.0589, "America/New_York",    "US", "F", 2),
    "philadelphia":  (39.9526, -75.1652, "America/New_York",    "US", "F", 2),

    # South & Central
    "dallas":        (32.7767, -96.7970, "America/Chicago",     "US", "F", 1),
    "dfw":           (32.8998, -97.0403, "America/Chicago",     "US", "F", 1),
    "miami":         (25.7617, -80.1918, "America/New_York",    "US", "F", 1),
    "mia":           (25.7959, -80.2870, "America/New_York",    "US", "F", 1),
    "houston":       (29.7604, -95.3698, "America/Chicago",     "US", "F", 2),
    "chicago":       (41.8781, -87.6298, "America/Chicago",     "US", "F", 1),
    "atlanta":       (33.7490, -84.3880, "America/New_York",    "US", "F", 2),
    "phoenix":       (33.4484,-112.0740, "America/Phoenix",     "US", "F", 2),
    "minneapolis":   (44.9778, -93.2650, "America/Chicago",     "US", "F", 2),
    "new orleans":   (29.9511, -90.0715, "America/Chicago",     "US", "F", 2),
    "nashville":     (36.1627, -86.7816, "America/Chicago",     "US", "F", 2),

    # West Coast
    "los angeles":   (34.0522,-118.2437, "America/Los_Angeles", "US", "F", 1),
    "lax":           (33.9416,-118.4085, "America/Los_Angeles", "US", "F", 1),
    "las vegas":     (36.1716,-115.1391, "America/Los_Angeles", "US", "F", 2),
    "seattle":       (47.6062,-122.3321, "America/Los_Angeles", "US", "F", 2),
    "denver":        (39.7392,-104.9903, "America/Denver",      "US", "F", 2),
    "san francisco": (37.7749,-122.4194, "America/Los_Angeles", "US", "F", 2),
    "portland":      (45.5051,-122.6750, "America/Los_Angeles", "US", "F", 2),
    "san diego":     (32.7157,-117.1611, "America/Los_Angeles", "US", "F", 2),

    # Other US
    "aspen":         (39.1911,-106.8175, "America/Denver",      "US", "F", 3),
    "honolulu":      (21.3069,-157.8583, "Pacific/Honolulu",    "US", "F", 2),

    # ==========================================================================
    # CANADA
    # ==========================================================================
    "toronto":       (43.6532, -79.3832, "America/Toronto",     "Other", "C", 2),
    "yyz":           (43.6777, -79.6248, "America/Toronto",     "Other", "C", 2),
    "vancouver":     (49.2827,-123.1207, "America/Vancouver",   "Other", "C", 2),
    "montreal":      (45.5017, -73.5673, "America/Toronto",     "Other", "C", 2),
    "calgary":       (51.0447,-114.0719, "America/Edmonton",    "Other", "C", 3),

    # ==========================================================================
    # EUROPE
    # ==========================================================================

    # UK
    "london":        (51.5074,  -0.1278, "Europe/London",      "Europe", "C", 1),
    "heathrow":      (51.4700,  -0.4543, "Europe/London",      "Europe", "C", 1),
    "lhr":           (51.4700,  -0.4543, "Europe/London",      "Europe", "C", 1),
    "st james park": (51.5031,  -0.1312, "Europe/London",      "Europe", "C", 1),
    "gatwick":       (51.1537,  -0.1821, "Europe/London",      "Europe", "C", 2),
    "manchester":    (53.4808,  -2.2426, "Europe/London",      "Europe", "C", 2),

    # Western Europe
    "paris":         (48.8566,   2.3522, "Europe/Paris",       "Europe", "C", 2),
    "cdg":           (49.0097,   2.5479, "Europe/Paris",       "Europe", "C", 2),
    "berlin":        (52.5200,  13.4050, "Europe/Berlin",      "Europe", "C", 2),
    "amsterdam":     (52.3676,   4.9041, "Europe/Amsterdam",   "Europe", "C", 2),
    "rome":          (41.9028,  12.4964, "Europe/Rome",        "Europe", "C", 2),
    "madrid":        (40.4168,  -3.7038, "Europe/Madrid",      "Europe", "C", 2),
    "barcelona":     (41.3851,   2.1734, "Europe/Madrid",      "Europe", "C", 2),
    "ibiza":         (38.9067,   1.4206, "Europe/Madrid",      "Europe", "C", 3),
    "zurich":        (47.3769,   8.5417, "Europe/Zurich",      "Europe", "C", 2),
    "vienna":        (48.2082,  16.3738, "Europe/Vienna",      "Europe", "C", 2),
    "munich":        (48.1351,  11.5820, "Europe/Berlin",      "Europe", "C", 2),
    "brussels":      (50.8503,   4.3517, "Europe/Brussels",    "Europe", "C", 2),
    "lisbon":        (38.7223,  -9.1393, "Europe/Lisbon",      "Europe", "C", 2),
    "athens":        (37.9838,  23.7275, "Europe/Athens",      "Europe", "C", 2),
    "istanbul":      (41.0082,  28.9784, "Europe/Istanbul",    "Europe", "C", 2),
    "ankara":        (39.9334,  32.8597, "Europe/Istanbul",    "Europe", "C", 2),

    # Eastern Europe
    "warsaw":        (52.2297,  21.0122, "Europe/Warsaw",      "Europe", "C", 3),
    "prague":        (50.0755,  14.4378, "Europe/Prague",      "Europe", "C", 3),
    "budapest":      (47.4979,  19.0402, "Europe/Budapest",    "Europe", "C", 3),

    # ==========================================================================
    # ASIA
    # ==========================================================================

    # East Asia
    "tokyo":         (35.6762, 139.6503, "Asia/Tokyo",         "Asia", "C", 2),
    "hnd":           (35.5494, 139.7798, "Asia/Tokyo",         "Asia", "C", 2),
    "seoul":         (37.5665, 126.9780, "Asia/Seoul",         "Asia", "C", 2),
    "beijing":       (39.9042, 116.4074, "Asia/Shanghai",      "Asia", "C", 2),
    "shanghai":      (31.2304, 121.4737, "Asia/Shanghai",      "Asia", "C", 2),
    "hong kong":     (22.3193, 114.1694, "Asia/Hong_Kong",     "Asia", "C", 2),
    "taipei":        (25.0330, 121.5654, "Asia/Taipei",        "Asia", "C", 3),

    # Southeast Asia
    "singapore":     ( 1.3521, 103.8198, "Asia/Singapore",     "Asia", "C", 2),
    "jakarta":       (-6.2088, 106.8456, "Asia/Jakarta",       "Asia", "C", 2),
    "cgk":           (-6.1256, 106.6559, "Asia/Jakarta",       "Asia", "C", 2),
    "bangkok":       (13.7563, 100.5018, "Asia/Bangkok",       "Asia", "C", 3),
    "kuala lumpur":  ( 3.1390, 101.6869, "Asia/Kuala_Lumpur",  "Asia", "C", 3),
    "manila":        (14.5995, 120.9842, "Asia/Manila",        "Asia", "C", 3),
    "ho chi minh":   (10.8231, 106.6297, "Asia/Ho_Chi_Minh",   "Asia", "C", 3),

    # South Asia
    "mumbai":        (19.0760,  72.8777, "Asia/Kolkata",       "Asia", "C", 2),
    "delhi":         (28.7041,  77.1025, "Asia/Kolkata",       "Asia", "C", 2),
    "karachi":       (24.8607,  67.0011, "Asia/Karachi",       "Asia", "C", 3),

    # ==========================================================================
    # MIDDLE EAST
    # ==========================================================================
    "dubai":         (25.2048,  55.2708, "Asia/Dubai",         "MiddleEast", "C", 2),
    "dxb":           (25.2532,  55.3657, "Asia/Dubai",         "MiddleEast", "C", 2),
    "riyadh":        (24.7136,  46.6753, "Asia/Riyadh",        "MiddleEast", "C", 2),
    "kuwait city":   (29.3759,  47.9774, "Asia/Kuwait",        "MiddleEast", "C", 3),
    "abu dhabi":     (24.4539,  54.3773, "Asia/Dubai",         "MiddleEast", "C", 2),
    "doha":          (25.2854,  51.5310, "Asia/Qatar",         "MiddleEast", "C", 2),
    "tel aviv":      (32.0853,  34.7818, "Asia/Jerusalem",     "MiddleEast", "C", 3),

    # ==========================================================================
    # OCEANIA
    # ==========================================================================
    "sydney":        (-33.8688, 151.2093, "Australia/Sydney",    "Oceania", "C", 2),
    "bondi":         (-33.8908, 151.2743, "Australia/Sydney",    "Oceania", "C", 2),
    "melbourne":     (-37.8136, 144.9631, "Australia/Melbourne", "Oceania", "C", 2),
    "perth":         (-31.9505, 115.8605, "Australia/Perth",     "Oceania", "C", 2),
    "brisbane":      (-27.4698, 153.0251, "Australia/Brisbane",  "Oceania", "C", 2),
    "auckland":      (-36.8509, 174.7645, "Pacific/Auckland",    "Oceania", "C", 2),
    "wellington":    (-41.2866, 174.7756, "Pacific/Auckland",    "Oceania", "C", 2),

    # ==========================================================================
    # LATIN AMERICA
    # ==========================================================================
    "sao paulo":      (-23.5505, -46.6333, "America/Sao_Paulo",                 "Other", "C", 2),
    "sbgr":           (-23.4356, -46.4731, "America/Sao_Paulo",                 "Other", "C", 2),
    "buenos aires":   (-34.6037, -58.3816, "America/Argentina/Buenos_Aires",     "Other", "C", 2),
    "rio de janeiro": (-22.9068, -43.1729, "America/Sao_Paulo",                 "Other", "C", 3),
    "bogota":         (  4.7110, -74.0721, "America/Bogota",                    "Other", "C", 3),
    "lima":           (-12.0464, -77.0428, "America/Lima",                      "Other", "C", 3),
    "mexico city":    ( 19.4326, -99.1332, "America/Mexico_City",               "Other", "C", 3),

    # ==========================================================================
    # AFRICA
    # ==========================================================================
    "cairo":          ( 30.0444,  31.2357, "Africa/Cairo",          "Other", "C", 3),
    "johannesburg":   (-26.2041,  28.0473, "Africa/Johannesburg",   "Other", "C", 3),
    "cape town":      (-33.9249,  18.4241, "Africa/Johannesburg",   "Other", "C", 3),
    "lagos":          (  6.5244,   3.3792, "Africa/Lagos",          "Other", "C", 3),
    "nairobi":        ( -1.2921,  36.8219, "Africa/Nairobi",        "Other", "C", 3),
    "casablanca":     ( 33.5731,  -7.5898, "Africa/Casablanca",     "Other", "C", 3),
}


# ── Build Registry ────────────────────────────────────────────────────────────

LOCATION_REGISTRY: dict[str, CityInfo] = {
    key: CityInfo(
        key=key,
        lat=v[0], lon=v[1], tz=v[2],
        region=v[3], unit=v[4], tier=v[5],
    )
    for key, v in _RAW.items()
}

# Pre-sorted keys (panjang → pendek) untuk priority match "st james park" > "london"
_SORTED_KEYS: tuple[str, ...] = tuple(
    sorted(LOCATION_REGISTRY.keys(), key=len, reverse=True)
)


# ── Lookup Functions ──────────────────────────────────────────────────────────

@lru_cache(maxsize=512)
def resolve_location(question: str) -> Optional[CityInfo]:
    """
    Scan teks pertanyaan untuk kota yang dikenal.
    Prioritaskan match lebih spesifik (lebih panjang) dulu.
    Contoh: "st james park" match sebelum "london".

    LRU cache (maxsize=512): pertanyaan yang sama tidak perlu loop 90+ kota ulang.
    Cache otomatis di-evict saat sudah penuh (LRU policy).
    """
    q = question.lower()
    for key in _SORTED_KEYS:
        if key in q:
            city = LOCATION_REGISTRY[key]
            log.debug(
                "[Registry] Match '%s' → %s (tier %d)",
                key, city.key, city.tier,
            )
            return city
    return None


def get_city(name: str) -> Optional[CityInfo]:
    """Direct lookup by exact key."""
    return LOCATION_REGISTRY.get(name.lower())


# ── Unit Detection ────────────────────────────────────────────────────────────

def detect_unit(question: str, city: CityInfo) -> str:
    """
    Auto-detect unit dari teks pertanyaan.
    Override default city.unit jika ada tanda eksplisit.

    Priority:
      1. Explicit °F atau FAHRENHEIT di teks → F
      2. Explicit °C atau CELSIUS di teks → C
      3. Default dari city registry
    """
    q_upper = question.upper()

    if re.search(r'°\s*F\b|\bFAHRENHEIT\b', q_upper):
        return "F"
    if re.search(r'°\s*C\b|\bCELSIUS\b', q_upper):
        return "C"

    return city.unit


def to_celsius(value: float, unit: str) -> float:
    """Convert nilai ke Celsius."""
    if unit == "F":
        return round((value - 32) * 5 / 9, 2)
    return round(value, 2)


def to_display(value_c: float, unit: str) -> str:
    """Format suhu untuk display Discord."""
    if unit == "F":
        f = round(value_c * 9 / 5 + 32, 1)
        return f"{f}°F ({value_c:.1f}°C)"
    return f"{value_c:.1f}°C"


# ── Golden Hour Guard ─────────────────────────────────────────────────────────

def check_golden_hour(
    city: CityInfo,
    hours_to_close: float,
) -> GoldenHourStatus:
    """
    Cek status Golden Hour untuk city + hours_to_close.

    Returns:
      OPEN → kondisi optimal, Kelly ×1.0
      WARN → kurang optimal (terlalu jauh), Kelly ×0.7
      NEAR → mendekati close, Kelly ×0.5
      SKIP → di luar semua window, tidak trade
    """
    if hours_to_close > settings.MAX_HOURS_TO_CLOSE:
        return GoldenHourStatus.SKIP

    if hours_to_close < settings.MIN_HOURS_TO_CLOSE:
        return GoldenHourStatus.SKIP

    open_min, open_max = settings.get_golden_hour_window(city.region)

    # NEAR: antara MIN dan open_min (sangat dekat close)
    near_min = settings.MIN_HOURS_TO_CLOSE
    near_max = open_min

    # WARN upper: antara open_max dan MAX
    warn_upper_min = open_max
    warn_upper_max = settings.MAX_HOURS_TO_CLOSE

    if open_min <= hours_to_close <= open_max:
        return GoldenHourStatus.OPEN

    if near_min <= hours_to_close < near_max:
        return GoldenHourStatus.NEAR

    if warn_upper_min < hours_to_close <= warn_upper_max:
        return GoldenHourStatus.WARN

    return GoldenHourStatus.SKIP


def golden_hour_multiplier(status: GoldenHourStatus) -> float:
    """Kelly multiplier berdasarkan Golden Hour status."""
    return {
        GoldenHourStatus.OPEN: settings.GOLDEN_HOUR_OPEN_MULT,
        GoldenHourStatus.WARN: settings.GOLDEN_HOUR_WARN_MULT,
        GoldenHourStatus.NEAR: settings.GOLDEN_HOUR_NEAR_MULT,
        GoldenHourStatus.SKIP: 0.0,
    }.get(status, 0.0)


# ── Adaptive Liquidity ────────────────────────────────────────────────────────

def calculate_min_liquidity(
    market_type: str,
    hours_to_close: float,
    city_tier: int,
) -> float:
    """
    Hitung minimum liquidity secara dinamis.

    Faktor:
      1. Market type (multi > binary > range)
      2. Proximity ke close (makin dekat → threshold turun)
      3. City tier (tier 1 lebih ketat, tier 3 lebih longgar)
    """
    base = {
        "MULTI_OUTCOME":  settings.LIQUIDITY_BASE_MULTI,
        "BINARY_ABOVE":   settings.LIQUIDITY_BASE_BINARY,
        "BINARY_BELOW":   settings.LIQUIDITY_BASE_BINARY,
        "BINARY_RANGE":   settings.LIQUIDITY_BASE_RANGE,
    }.get(market_type, settings.LIQUIDITY_BASE_BINARY)

    # Proximity factor
    if hours_to_close <= 4:
        proximity = 0.5
    elif hours_to_close <= 8:
        proximity = 0.7
    elif hours_to_close <= 14:
        proximity = 1.0
    else:
        proximity = 1.3

    # Tier factor
    tier_factor = {1: 1.2, 2: 1.0, 3: 0.7}.get(city_tier, 1.0)

    return max(
        base * proximity * tier_factor,
        settings.LIQUIDITY_HARD_FLOOR,
    )


# ── Registry Stats ────────────────────────────────────────────────────────────

def registry_summary() -> dict:
    """Statistik registry untuk startup notification."""
    total   = len(LOCATION_REGISTRY)
    regions: dict[str, int] = {}
    tiers:   dict[int, int] = {1: 0, 2: 0, 3: 0}
    units:   dict[str, int] = {"F": 0, "C": 0}

    for city in LOCATION_REGISTRY.values():
        regions[city.region] = regions.get(city.region, 0) + 1
        tiers[city.tier]     = tiers.get(city.tier, 0) + 1
        units[city.unit]     = units.get(city.unit, 0) + 1

    tier1_cities = [
        c.key.title()
        for c in LOCATION_REGISTRY.values()
        if c.tier == 1
    ]

    return {
        "total":        total,
        "by_region":    regions,
        "by_tier":      tiers,
        "by_unit":      units,
        "tier1_cities": tier1_cities,
    }
