"""
conftest.py — Shared fixtures untuk semua mock test AQL.

Pytest otomatis memuat file ini sebelum test apapun jalan.
Semua fixture di sini tersedia di setiap test tanpa perlu import.
"""
import os
import sys
import pytest

# ── Inject env vars sebelum settings.py di-import ────────────────────────────
# settings.py memanggil _require_env() saat class dibuat.
# Tanpa ini, semua test akan crash dengan ValueError.
os.environ.setdefault("POLY_PRIVATE_KEY",       "0x" + "a" * 64)
os.environ.setdefault("TERMINAL_WEBHOOK_URL",   "https://discord.com/api/webhooks/0/test")
os.environ.setdefault("WEATHER_WEBHOOK_URL",    "https://discord.com/api/webhooks/1/test")
os.environ.setdefault("TRADE_WEBHOOK_URL",      "https://discord.com/api/webhooks/2/test")
os.environ.setdefault("ALERTS_WEBHOOK_URL",     "https://discord.com/api/webhooks/3/test")

# ── Path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

# ── Import setelah env vars di-set ────────────────────────────────────────────
from datetime import date, datetime, timezone
from dataclasses import dataclass
from typing import Optional

from core.consensus import ConsensusResult, ModelForecast
from core.location_registry import CityInfo, LOCATION_REGISTRY


# ── Shared Fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def nyc() -> CityInfo:
    """CityInfo untuk New York City — kota Tier 1, unit F."""
    return LOCATION_REGISTRY["new york"]


@pytest.fixture
def london() -> CityInfo:
    """CityInfo untuk London — kota Tier 1 Europe, unit C."""
    return LOCATION_REGISTRY["london"]


@pytest.fixture
def toronto() -> CityInfo:
    """CityInfo untuk Toronto — Canada region (Bug #11 fix)."""
    return LOCATION_REGISTRY["toronto"]


def make_model_forecast(model: str, t_max: float, t_min: float, t_mean: float) -> ModelForecast:
    """Helper buat ModelForecast dummy."""
    return ModelForecast(
        model=model,
        target_date=date(2026, 4, 15),
        t_max_c=t_max,
        t_min_c=t_min,
        t_mean_c=t_mean,
        fetched_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def consensus_tight(nyc) -> ConsensusResult:
    """
    Consensus dengan variance rendah — seharusnya triple_lock=True.
    Semua model sepakat: NYC sekitar 30°C.
    std dev = 0.47°C < 1.0°C threshold.
    """
    ecmwf = make_model_forecast("ECMWF", t_max=31.0, t_min=22.0, t_mean=30.0)
    gfs   = make_model_forecast("GFS",   t_max=30.5, t_min=21.5, t_mean=29.5)
    noaa  = make_model_forecast("NOAA",  t_max=31.5, t_min=22.5, t_mean=30.5)
    icon  = make_model_forecast("ICON",  t_max=31.0, t_min=22.0, t_mean=30.0)

    means = [30.0, 29.5, 30.5, 30.0]
    import math
    n     = len(means)
    mu    = sum(means) / n
    sigma = round(math.sqrt(sum((x - mu) ** 2 for x in means) / n), 3)

    return ConsensusResult(
        target_date=date(2026, 4, 15),
        location_name="new york",
        latitude=40.7128, longitude=-74.0060,
        ecmwf=ecmwf, gfs=gfs, noaa=noaa, icon=icon,
        model_count=4,
        consensus_t_max=round(sum([31.0,30.5,31.5,31.0])/4, 2),
        consensus_t_min=round(sum([22.0,21.5,22.5,22.0])/4, 2),
        consensus_t_mean=round(mu, 2),
        inter_model_variance=sigma,
        triple_lock=(sigma <= 1.0),
        timestamp=datetime.now(timezone.utc),
    )


@pytest.fixture
def consensus_loose(nyc) -> ConsensusResult:
    """
    Consensus dengan variance tinggi — seharusnya triple_lock=False.
    Model tidak sepakat: range 20–30°C, std dev > 1.0.
    """
    ecmwf = make_model_forecast("ECMWF", t_max=32.0, t_min=24.0, t_mean=30.0)
    gfs   = make_model_forecast("GFS",   t_max=25.0, t_min=17.0, t_mean=23.0)
    noaa  = make_model_forecast("NOAA",  t_max=27.0, t_min=19.0, t_mean=25.0)

    means = [30.0, 23.0, 25.0]
    import math
    n     = len(means)
    mu    = sum(means) / n
    sigma = round(math.sqrt(sum((x - mu) ** 2 for x in means) / n), 3)

    return ConsensusResult(
        target_date=date(2026, 4, 15),
        location_name="new york",
        latitude=40.7128, longitude=-74.0060,
        ecmwf=ecmwf, gfs=gfs, noaa=noaa, icon=None,
        model_count=3,
        consensus_t_max=round(sum([32.0,25.0,27.0])/3, 2),
        consensus_t_min=round(sum([24.0,17.0,19.0])/3, 2),
        consensus_t_mean=round(mu, 2),
        inter_model_variance=sigma,
        triple_lock=(sigma <= 1.0),
        timestamp=datetime.now(timezone.utc),
    )


@pytest.fixture
def yes_token_id() -> str:
    return "yes_token_abc123def456"


@pytest.fixture
def no_token_id() -> str:
    return "no_token_xyz789uvw012"
