# ══════════════════════════════════════════════════════════════════════════════
# consensus.py = Kode Triple-Lock
# ══════════════════════════════════════════════════════════════════════════════

"""
AQL Consensus Engine — Triple-Lock Logic
Fetches ECMWF, GFS, and NOAA forecasts from Open-Meteo concurrently.
Returns a ConsensusResult only when all three models agree within
TRIPLE_LOCK_VARIANCE_C (default 1.0°C). Strict None propagation on
any data failure — never silently degrades to a 2-model consensus.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

import httpx

from config.settings import settings

log = logging.getLogger("aql.consensus")

# ── Model → Open-Meteo endpoint + parameter mapping ──────────────────────────

MODEL_ENDPOINTS: dict[str, str] = {
    "ECMWF": f"{settings.OPENMETEO_BASE}/ecmwf",
    "GFS":   f"{settings.OPENMETEO_BASE}/gfs",
    "NOAA":  f"{settings.OPENMETEO_BASE}/forecast",
}

MODEL_PARAMS: dict[str, dict] = {
    "ECMWF": {"models": "ecmwf_ifs04"},
    "GFS":   {"models": "gfs_seamless"},
    "NOAA":  {"models": "best_match"},
}


# ── Data Structures ───────────────────────────────────────────────────────────

@dataclass
class ModelForecast:
    model: str
    target_date: date
    t_max_c: float
    t_min_c: float
    t_mean_c: float
    fetched_at: datetime


@dataclass
class ConsensusResult:
    """Emitted regardless of triple_lock status — caller checks triple_lock field."""
    target_date: date
    location_name: str
    latitude: float
    longitude: float

    ecmwf: ModelForecast
    gfs: ModelForecast
    noaa: ModelForecast

    consensus_t_max: float       # Arithmetic mean across all 3 models
    consensus_t_min: float
    consensus_t_mean: float
    inter_model_variance: float  # max(t_means) - min(t_means)

    triple_lock: bool            # True iff variance ≤ TRIPLE_LOCK_VARIANCE_C
    timestamp: datetime

    @property
    def models_summary(self) -> str:
        return (
            f"ECMWF {self.ecmwf.t_mean_c:.1f}°C | "
            f"GFS {self.gfs.t_mean_c:.1f}°C | "
            f"NOAA {self.noaa.t_mean_c:.1f}°C"
        )


# ── Single-Model Fetcher ──────────────────────────────────────────────────────

async def _fetch_model_forecast(
    client: httpx.AsyncClient,
    model_name: str,
    latitude: float,
    longitude: float,
    target_date: date,
) -> Optional[ModelForecast]:
    """
    Async fetch of daily temperature forecast for one NWP model.
    Returns None on any HTTP or data error — never raises.
    """
    endpoint    = MODEL_ENDPOINTS[model_name]
    extra_params = MODEL_PARAMS[model_name]

    params = {
        "latitude":    latitude,
        "longitude":   longitude,
        "daily":       "temperature_2m_max,temperature_2m_min,temperature_2m_mean",
        "timezone":    "UTC",
        "start_date":  target_date.isoformat(),
        "end_date":    target_date.isoformat(),
        "forecast_days": 7,
        **extra_params,
    }

    try:
        resp = await client.get(endpoint, params=params, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()

        daily = data.get("daily", {})
        if not daily.get("time"):
            log.warning("[%s] Empty daily block for %s", model_name, target_date)
            return None

        t_max  = daily["temperature_2m_max"][0]
        t_min  = daily["temperature_2m_min"][0]
        t_mean = daily["temperature_2m_mean"][0]

        if any(v is None for v in [t_max, t_min, t_mean]):
            log.warning("[%s] Null temperature values for %s", model_name, target_date)
            return None

        return ModelForecast(
            model=model_name,
            target_date=target_date,
            t_max_c=float(t_max),
            t_min_c=float(t_min),
            t_mean_c=float(t_mean),
            fetched_at=datetime.now(timezone.utc),
        )

    except httpx.HTTPStatusError as e:
        log.error(
            "[%s] HTTP %d from Open-Meteo: %s",
            model_name, e.response.status_code, e.response.text[:200],
        )
    except httpx.TimeoutException:
        log.error("[%s] Request timed out for %s", model_name, target_date)
    except Exception as e:
        log.error("[%s] Unexpected error: %s", model_name, str(e))

    return None


# ── Triple-Lock Evaluator ─────────────────────────────────────────────────────

async def get_triple_lock_consensus(
    latitude: float,
    longitude: float,
    location_name: str,
    target_date: date,
) -> Optional[ConsensusResult]:
    """
    Concurrently fetch ECMWF, GFS, NOAA and evaluate Triple-Lock condition.
    Returns None if ANY model fails — system requires all three for signal integrity.
    """
    async with httpx.AsyncClient() as client:
        ecmwf, gfs, noaa = await asyncio.gather(
            _fetch_model_forecast(client, "ECMWF", latitude, longitude, target_date),
            _fetch_model_forecast(client, "GFS",   latitude, longitude, target_date),
            _fetch_model_forecast(client, "NOAA",  latitude, longitude, target_date),
        )

    if any(m is None for m in [ecmwf, gfs, noaa]):
        log.error(
            "Consensus aborted — incomplete model data. "
            "ECMWF=%s | GFS=%s | NOAA=%s",
            ecmwf is not None, gfs is not None, noaa is not None,
        )
        return None

    t_means = [ecmwf.t_mean_c, gfs.t_mean_c, noaa.t_mean_c]
    t_maxes = [ecmwf.t_max_c,  gfs.t_max_c,  noaa.t_max_c]
    t_mins  = [ecmwf.t_min_c,  gfs.t_min_c,  noaa.t_min_c]

    variance    = round(max(t_means) - min(t_means), 3)
    triple_lock = variance <= settings.TRIPLE_LOCK_VARIANCE_C

    result = ConsensusResult(
        target_date=target_date,
        location_name=location_name,
        latitude=latitude,
        longitude=longitude,
        ecmwf=ecmwf,
        gfs=gfs,
        noaa=noaa,
        consensus_t_max=round(sum(t_maxes) / 3, 2),
        consensus_t_min=round(sum(t_mins)  / 3, 2),
        consensus_t_mean=round(sum(t_means) / 3, 2),
        inter_model_variance=variance,
        triple_lock=triple_lock,
        timestamp=datetime.now(timezone.utc),
    )

    log.info(
        "[Consensus] %s | %s | Δ=%.3f°C | TripleLock=%s",
        location_name, result.models_summary, variance, triple_lock,
    )
    return result


# ── 00z / 12z Decision Phase Guard ───────────────────────────────────────────

def is_decision_phase(now: Optional[datetime] = None) -> bool:
    """
    Returns True if current UTC hour falls within ±2h of a 00z or 12z model run.
    Fresh model data is typically available ~2–4h after nominal run time.
    """
    now       = now or datetime.now(timezone.utc)
    tolerance = 2

    for run_hour in settings.DECISION_PHASE_HOURS:
        diff = abs(now.hour - run_hour)
        if diff <= tolerance or diff >= (24 - tolerance):
            return True
    return False

