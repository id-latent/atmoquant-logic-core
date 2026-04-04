# ==============================================================================
# consensus.py = Triple-Lock Engine + Retry Logic
# ==============================================================================
"""
AQL Consensus Engine — Triple-Lock Logic
Fetches ECMWF, GFS, and NOAA forecasts from Open-Meteo concurrently.
Includes exponential backoff retry untuk setiap model.
Returns ConsensusResult only when all three models agree within
TRIPLE_LOCK_VARIANCE_C (default 1.0°C).
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

# ── Retry Configuration ───────────────────────────────────────────────────────
MAX_RETRIES    = 3
BASE_DELAY_SEC = 1.5

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
    """Emitted regardless of triple_lock status — caller checks triple_lock."""
    target_date: date
    location_name: str
    latitude: float
    longitude: float

    ecmwf: ModelForecast
    gfs: ModelForecast
    noaa: ModelForecast

    consensus_t_max: float
    consensus_t_min: float
    consensus_t_mean: float
    inter_model_variance: float

    triple_lock: bool
    timestamp: datetime

    @property
    def models_summary(self) -> str:
        return (
            f"ECMWF {self.ecmwf.t_mean_c:.1f}°C | "
            f"GFS {self.gfs.t_mean_c:.1f}°C | "
            f"NOAA {self.noaa.t_mean_c:.1f}°C"
        )


# ── Single Attempt Fetcher ────────────────────────────────────────────────────

async def _fetch_once(
    client: httpx.AsyncClient,
    model_name: str,
    latitude: float,
    longitude: float,
    target_date: date,
) -> Optional[ModelForecast]:
    """Satu attempt fetch tanpa retry. Dipanggil oleh _fetch_with_retry."""
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

    resp = await client.get(endpoint, params=params, timeout=15.0)
    resp.raise_for_status()
    data = resp.json()

    daily = data.get("daily", {})
    if not daily.get("time"):
        log.warning("[%s] Empty daily block untuk %s", model_name, target_date)
        return None

    t_max  = daily["temperature_2m_max"][0]
    t_min  = daily["temperature_2m_min"][0]
    t_mean = daily["temperature_2m_mean"][0]

    if any(v is None for v in [t_max, t_min, t_mean]):
        log.warning("[%s] Null values untuk %s", model_name, target_date)
        return None

    return ModelForecast(
        model=model_name,
        target_date=target_date,
        t_max_c=float(t_max),
        t_min_c=float(t_min),
        t_mean_c=float(t_mean),
        fetched_at=datetime.now(timezone.utc),
    )


# ── Retry Wrapper ─────────────────────────────────────────────────────────────

async def _fetch_with_retry(
    client: httpx.AsyncClient,
    model_name: str,
    latitude: float,
    longitude: float,
    target_date: date,
) -> Optional[ModelForecast]:
    """
    Fetch dengan exponential backoff retry.
    Attempt 1 : langsung
    Attempt 2 : tunggu 1.5s
    Attempt 3 : tunggu 3.0s
    Gagal semua → return None
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = await _fetch_once(
                client, model_name, latitude, longitude, target_date
            )
            if result is not None:
                if attempt > 1:
                    log.info(
                        "[%s] Berhasil di attempt ke-%d",
                        model_name, attempt
                    )
                return result

        except httpx.TimeoutException:
            log.warning(
                "[%s] Timeout attempt %d/%d",
                model_name, attempt, MAX_RETRIES
            )

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            # Rate limit → tunggu lebih lama
            if status == 429:
                wait = BASE_DELAY_SEC * (2 ** attempt) + 5.0
                log.warning(
                    "[%s] Rate limited (429). Tunggu %.1fs sebelum retry.",
                    model_name, wait
                )
                await asyncio.sleep(wait)
                continue
            # Server error (500, 503) → retry normal
            log.warning(
                "[%s] HTTP %d attempt %d/%d",
                model_name, status, attempt, MAX_RETRIES
            )

        except Exception as e:
            log.error(
                "[%s] Unexpected error attempt %d/%d: %s",
                model_name, attempt, MAX_RETRIES, str(e)
            )

        # Tunggu sebelum retry berikutnya (kecuali attempt terakhir)
        if attempt < MAX_RETRIES:
            delay = BASE_DELAY_SEC * (2 ** (attempt - 1))
            log.debug(
                "[%s] Retry dalam %.1fs... (attempt %d/%d)",
                model_name, delay, attempt + 1, MAX_RETRIES
            )
            await asyncio.sleep(delay)

    log.error(
        "[%s] Semua %d attempt gagal untuk %s",
        model_name, MAX_RETRIES, target_date
    )
    return None


# ── Triple-Lock Evaluator ─────────────────────────────────────────────────────

async def get_triple_lock_consensus(
    latitude: float,
    longitude: float,
    location_name: str,
    target_date: date,
) -> Optional[ConsensusResult]:
    """
    Concurrent fetch ECMWF + GFS + NOAA dengan retry.
    Returns None jika ANY model gagal setelah semua retry habis.
    Tidak pernah degradasi ke 2-model consensus.
    """
    async with httpx.AsyncClient() as client:
        ecmwf, gfs, noaa = await asyncio.gather(
            _fetch_with_retry(client, "ECMWF", latitude, longitude, target_date),
            _fetch_with_retry(client, "GFS",   latitude, longitude, target_date),
            _fetch_with_retry(client, "NOAA",  latitude, longitude, target_date),
        )

    if any(m is None for m in [ecmwf, gfs, noaa]):
        log.error(
            "Consensus aborted — data tidak lengkap. "
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
    Returns True jika UTC hour saat ini dalam window ±2h
    dari 00z atau 12z model run.
    """
    now       = now or datetime.now(timezone.utc)
    tolerance = 2

    for run_hour in settings.DECISION_PHASE_HOURS:
        diff = abs(now.hour - run_hour)
        if diff <= tolerance or diff >= (24 - tolerance):
            return True
    return False
