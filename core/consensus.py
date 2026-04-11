# ==============================================================================
# consensus.py — Quad-Lock Consensus Engine (FIXED)
# ==============================================================================
"""
AQL Consensus Engine — Quad-Lock Logic

Fixes:
  BUG #8 : inter_model_variance sebelumnya dihitung sebagai range (max-min),
           bukan variance statistik sesungguhnya. Sekarang dihitung sebagai
           population standard deviation (σ) dari t_mean semua model aktif.

           Mengapa std dev, bukan variance (σ²)?
           - σ dalam satuan yang sama dengan °C — lebih intuitif
           - Dipakai di probability.py sebagai:
               model_std = BASE_FORECAST_STD_C + VARIANCE_WEIGHT * inter_model_variance
             sehingga satuan harus °C, bukan °C²
           - Triple-lock threshold TRIPLE_LOCK_VARIANCE_C (1.0) bermakna:
             "std dev antar model ≤ 1°C" — lebih masuk akal daripada range ≤ 1°C
             karena range sangat sensitif terhadap outlier 1 model.

           Backward compat: nama field tetap "inter_model_variance" tapi
           nilai yang tersimpan sekarang adalah std dev (σ), bukan range.
           Update settings.TRIPLE_LOCK_VARIANCE_C jika diperlukan.
"""
from __future__ import annotations

import asyncio
import logging
import math
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

REQUIRED_MODELS = {"ECMWF", "GFS", "NOAA"}
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

    triple_lock=True jika inter_model_variance (std dev) ≤ TRIPLE_LOCK_VARIANCE_C.

    CATATAN BUG #8: inter_model_variance sekarang adalah population std dev (σ)
    dari t_mean semua model aktif, bukan range (max-min) seperti sebelumnya.
    """
    target_date: date
    location_name: str
    latitude: float
    longitude: float

    ecmwf: ModelForecast
    gfs: ModelForecast
    noaa: ModelForecast
    icon: Optional[ModelForecast]

    model_count: int
    consensus_t_max: float
    consensus_t_min: float
    consensus_t_mean: float
    inter_model_variance: float   # Sekarang = std dev (σ), bukan range

    triple_lock: bool
    timestamp: datetime

    @property
    def active_models(self) -> list[ModelForecast]:
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


# ── Helper: Population Std Dev ────────────────────────────────────────────────

def _population_std(values: list[float]) -> float:
    """
    FIX BUG #8: Hitung population standard deviation (σ) dari list nilai.

    Population std dev dipakai (bukan sample) karena kita mengukur
    dispersi semua model yang kita miliki, bukan estimasi populasi lebih besar.

    σ = sqrt( Σ(xᵢ - μ)² / n )
    """
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / n
    return round(math.sqrt(variance), 3)


# ── Single Attempt Fetcher ────────────────────────────────────────────────────

async def _fetch_once(
    client: httpx.AsyncClient,
    model_name: str,
    latitude: float,
    longitude: float,
    target_date: date,
) -> Optional[ModelForecast]:
    endpoint     = MODEL_ENDPOINTS[model_name]
    extra_params = MODEL_PARAMS[model_name]

    # ── Hitung berapa hari ke depan target_date dari hari ini ──────────────
    # Open-Meteo tidak suka start_date=end_date=today bersamaan dengan
    # forecast_days — menyebabkan HTTP 400. Solusi: gunakan forecast_days
    # saja (tanpa start_date/end_date), lalu ambil data dari index yang tepat.
    from datetime import date as _date
    today        = _date.today()
    horizon_days = max((target_date - today).days, 0)
    # Minta minimal 1 hari ke depan, maksimal 7
    n_days       = max(horizon_days + 1, 1)

    params = {
        "latitude":      latitude,
        "longitude":     longitude,
        "daily":         "temperature_2m_max,temperature_2m_min,temperature_2m_mean",
        "timezone":      "UTC",
        "forecast_days": min(n_days, 7),  # Open-Meteo max 7 hari gratis
        **extra_params,
    }

    resp = await client.get(endpoint, params=params, timeout=15.0)
    resp.raise_for_status()
    data = resp.json()

    daily = data.get("daily", {})
    if not daily.get("time"):
        log.warning("[%s] Empty daily block untuk %s", model_name, target_date)
        return None

    # Cari index yang sesuai dengan target_date
    target_str = target_date.isoformat()
    times      = daily.get("time", [])
    try:
        idx = times.index(target_str)
    except ValueError:
        # target_date tidak ada di response — ambil index terakhir
        idx = len(times) - 1
        log.debug("[%s] target_date %s tidak ada, pakai idx=%d", model_name, target_str, idx)

    t_max  = daily["temperature_2m_max"][idx]
    t_min  = daily["temperature_2m_min"][idx]
    t_mean = daily["temperature_2m_mean"][idx]

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
      - ICON: OPSIONAL (degradasi ke 3 model jika gagal)

    FIX BUG #8: inter_model_variance dihitung sebagai population std dev (σ)
    dari t_mean semua model aktif, bukan range (max-min).
    """
    async with httpx.AsyncClient() as client:
        ecmwf, gfs, noaa, icon = await asyncio.gather(
            _fetch_with_retry(client, "ECMWF", latitude, longitude, target_date),
            _fetch_with_retry(client, "GFS",   latitude, longitude, target_date),
            _fetch_with_retry(client, "NOAA",  latitude, longitude, target_date),
            _fetch_with_retry(client, "ICON",  latitude, longitude, target_date),
        )

    if any(m is None for m in [ecmwf, gfs, noaa]):
        log.error(
            "Consensus aborted — model wajib tidak lengkap. "
            "ECMWF=%s | GFS=%s | NOAA=%s",
            ecmwf is not None,
            gfs   is not None,
            noaa  is not None,
        )
        return None

    if icon is None:
        log.warning(
            "[Consensus] ICON gagal — lanjut dengan 3 model."
        )

    active = [m for m in [ecmwf, gfs, noaa, icon] if m is not None]

    t_means = [m.t_mean_c for m in active]
    t_maxes = [m.t_max_c  for m in active]
    t_mins  = [m.t_min_c  for m in active]

    # FIX BUG #8: gunakan population std dev, bukan range
    std_dev     = _population_std(t_means)
    triple_lock = std_dev <= settings.TRIPLE_LOCK_VARIANCE_C
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
        inter_model_variance=std_dev,   # sekarang = σ, bukan range
        triple_lock=triple_lock,
        timestamp=datetime.now(timezone.utc),
    )

    log.info(
        "[Consensus] %s | %d models | %s | σ=%.3f°C | Lock=%s",
        location_name, n,
        result.models_summary,
        std_dev, triple_lock,
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
