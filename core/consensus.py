# ==============================================================================
# consensus.py — Quad-Lock Consensus Engine ( ECMWF, GFS, NOAA + ICON )
# ==============================================================================
"""
AQL Consensus Engine — Quad-Lock Logic
4 model NWP: ECMWF + GFS + NOAA + ICON

Perubahan dan Tambahan dari sebelumnya:
  - Tambah ICON (DWD Germany) sebagai model ke-4
  - ICON optional: jika gagal, degradasi ke 3 model (tidak abort)
  - ECMWF/GFS/NOAA tetap wajib semua 3
  - model_count field baru di ConsensusResult
  - Exponential backoff retry tetap ada di semua model
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

# ── Retry Config ──────────────────────────────────────────────────────────────
MAX_RETRIES    = 3
BASE_DELAY_SEC = 1.5

# ── Model Endpoints ───────────────────────────────────────────────────────────
MODEL_ENDPOINTS: dict[str, str] = {
    "ECMWF": f"{settings.OPENMETEO_BASE}/ecmwf",
    "GFS":   f"{settings.OPENMETEO_BASE}/gfs",
    "NOAA":  f"{settings.OPENMETEO_BASE}/forecast",
    "ICON":  f"{settings.OPENMETEO_BASE}/dwd-icon",
}

MODEL_PARAMS: dict[str, dict] = {
    "ECMWF": {"models": "ecmwf_ifs04"},
    "GFS":   {"models": "gfs_seamless"},
    "NOAA":  {"models": "best_match"},
    "ICON":  {"models": "icon_global"},
}

# Model yang WAJIB ada — jika salah satu gagal, abort
REQUIRED_MODELS = {"ECMWF", "GFS", "NOAA"}

# Model opsional — jika gagal, degradasi (tidak abort)
OPTIONAL_MODELS = {"ICON"}


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
    """
    Hasil konsensus dari semua model yang berhasil.
    triple_lock=True jika variance ≤ TRIPLE_LOCK_VARIANCE_C.
    """
    target_date: date
    location_name: str
    latitude: float
    longitude: float

    # Model results (ICON bisa None jika gagal)
    ecmwf: ModelForecast
    gfs: ModelForecast
    noaa: ModelForecast
    icon: Optional[ModelForecast]

    # Statistik — dihitung dari semua model yang berhasil
    model_count: int
    consensus_t_max: float
    consensus_t_min: float
    consensus_t_mean: float
    inter_model_variance: float

    triple_lock: bool
    timestamp: datetime

    @property
    def active_models(self) -> list[ModelForecast]:
        """List model yang berhasil di-fetch."""
        models = [self.ecmwf, self.gfs, self.noaa]
        if self.icon is not None:
            models.append(self.icon)
        return models

    @property
    def models_summary(self) -> str:
        parts = [
            f"ECMWF {self.ecmwf.t_mean_c:.1f}°C",
            f"GFS {self.gfs.t_mean_c:.1f}°C",
            f"NOAA {self.noaa.t_mean_c:.1f}°C",
        ]
        if self.icon:
            parts.append(f"ICON {self.icon.t_mean_c:.1f}°C")
        return " | ".join(parts)

    @property
    def icon_status(self) -> str:
        return "✅" if self.icon else "❌ (degraded)"


# ── Single Attempt Fetcher ────────────────────────────────────────────────────

async def _fetch_once(
    client: httpx.AsyncClient,
    model_name: str,
    latitude: float,
    longitude: float,
    target_date: date,
) -> Optional[ModelForecast]:
    """Satu attempt fetch tanpa retry."""
    endpoint    = MODEL_ENDPOINTS[model_name]
    extra_params = MODEL_PARAMS[model_name]

    params = {
        "latitude":      latitude,
        "longitude":     longitude,
        "daily":         "temperature_2m_max,temperature_2m_min,temperature_2m_mean",
        "timezone":      "UTC",
        "start_date":    target_date.isoformat(),
        "end_date":      target_date.isoformat(),
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
    Attempt 1: langsung
    Attempt 2: tunggu 1.5s
    Attempt 3: tunggu 3.0s
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
                        model_name, attempt,
                    )
                return result

        except httpx.TimeoutException:
            log.warning(
                "[%s] Timeout attempt %d/%d",
                model_name, attempt, MAX_RETRIES,
            )

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status == 429:
                wait = BASE_DELAY_SEC * (2 ** attempt) + 5.0
                log.warning(
                    "[%s] Rate limited (429). Tunggu %.1fs",
                    model_name, wait,
                )
                await asyncio.sleep(wait)
                continue
            log.warning(
                "[%s] HTTP %d attempt %d/%d",
                model_name, status, attempt, MAX_RETRIES,
            )

        except Exception as e:
            log.error(
                "[%s] Error attempt %d/%d: %s",
                model_name, attempt, MAX_RETRIES, str(e),
            )

        if attempt < MAX_RETRIES:
            delay = BASE_DELAY_SEC * (2 ** (attempt - 1))
            await asyncio.sleep(delay)

    log.error(
        "[%s] Semua %d attempt gagal untuk %s",
        model_name, MAX_RETRIES, target_date,
    )
    return None


# ── Quad-Lock Evaluator ───────────────────────────────────────────────────────

async def get_triple_lock_consensus(
    latitude: float,
    longitude: float,
    location_name: str,
    target_date: date,
) -> Optional[ConsensusResult]:
    """
    Concurrent fetch 4 model dengan retry.

    Rules:
      - ECMWF + GFS + NOAA: WAJIB semua 3
        → Jika salah satu gagal: return None (abort)
      - ICON: OPSIONAL
        → Jika gagal: lanjut dengan 3 model (degradasi)

    Returns None jika 3 model wajib tidak lengkap.
    """
    async with httpx.AsyncClient() as client:
        ecmwf, gfs, noaa, icon = await asyncio.gather(
            _fetch_with_retry(client, "ECMWF", latitude, longitude, target_date),
            _fetch_with_retry(client, "GFS",   latitude, longitude, target_date),
            _fetch_with_retry(client, "NOAA",  latitude, longitude, target_date),
            _fetch_with_retry(client, "ICON",  latitude, longitude, target_date),
        )

    # Cek 3 model wajib
    if any(m is None for m in [ecmwf, gfs, noaa]):
        log.error(
            "Consensus aborted — model wajib tidak lengkap. "
            "ECMWF=%s | GFS=%s | NOAA=%s",
            ecmwf is not None,
            gfs   is not None,
            noaa  is not None,
        )
        return None

    # ICON opsional — log jika gagal tapi tidak abort
    if icon is None:
        log.warning(
            "[Consensus] ICON gagal — lanjut dengan 3 model. "
            "Akurasi sedikit berkurang untuk kota Eropa."
        )

    # Kumpulkan semua model yang berhasil
    active = [m for m in [ecmwf, gfs, noaa, icon] if m is not None]

    t_means = [m.t_mean_c for m in active]
    t_maxes = [m.t_max_c  for m in active]
    t_mins  = [m.t_min_c  for m in active]

    variance    = round(max(t_means) - min(t_means), 3)
    triple_lock = variance <= settings.TRIPLE_LOCK_VARIANCE_C
    n           = len(active)

    result = ConsensusResult(
        target_date=target_date,
        location_name=location_name,
        latitude=latitude,
        longitude=longitude,
        ecmwf=ecmwf,
        gfs=gfs,
        noaa=noaa,
        icon=icon,
        model_count=n,
        consensus_t_max=round(sum(t_maxes) / n, 2),
        consensus_t_min=round(sum(t_mins)  / n, 2),
        consensus_t_mean=round(sum(t_means) / n, 2),
        inter_model_variance=variance,
        triple_lock=triple_lock,
        timestamp=datetime.now(timezone.utc),
    )

    log.info(
        "[Consensus] %s | %d models | %s | Δ=%.3f°C | Lock=%s",
        location_name, n,
        result.models_summary,
        variance, triple_lock,
    )

    return result


# ── Decision Phase Guard ──────────────────────────────────────────────────────

def is_decision_phase(now: Optional[datetime] = None) -> bool:
    """
    Returns True jika UTC hour dalam window ±2h
    dari 00z atau 12z model run.
    """
    now       = now or datetime.now(timezone.utc)
    tolerance = 2

    for run_hour in settings.DECISION_PHASE_HOURS:
        diff = abs(now.hour - run_hour)
        if diff <= tolerance or diff >= (24 - tolerance):
            return True
    return False
