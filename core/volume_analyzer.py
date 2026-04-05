# ==============================================================================
# core/volume_analyzer.py — Volume Signal Analyzer 
# ==============================================================================
"""
AQL Volume Analyzer
Menganalisis volume trading sebagai signal tambahan.

Strategy: WARNING jika volume spike berlawanan dengan forecast
  → Kurangi Kelly ×0.6 sebagai tanda kehati-hatian

Logic:
  1. Ambil volume 24h per outcome dari Gamma API
  2. Deteksi spike: volume naik > VOLUME_SPIKE_THRESHOLD (300%)
  3. Cek arah spike vs forecast direction
  4. Jika berlawanan → kirim warning + kurangi Kelly
  5. Jika searah → tidak ada aksi (volume confirmation)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from config.settings import settings

log = logging.getLogger("aql.volume")


# ── Volume Signal ─────────────────────────────────────────────────────────────

@dataclass
class VolumeSignal:
    has_spike: bool
    spike_direction: str      # "WITH_FORECAST" / "AGAINST_FORECAST" / "NONE"
    spike_magnitude: float    # Berapa kali lipat dari normal
    kelly_multiplier: float   # Multiplier yang harus diterapkan
    warning_message: str      # Pesan untuk Discord jika ada warning


def analyze_volume(
    outcome_label: str,
    volume_24h: float,
    avg_volume: float,
    forecast_outcome: str,
    market_leading_outcome: str,
) -> VolumeSignal:
    """
    Analisis volume untuk satu outcome.

    Args:
        outcome_label:         Label outcome yang kita mau beli ("76°F")
        volume_24h:            Volume 24 jam terakhir untuk outcome ini
        avg_volume:            Volume rata-rata normal untuk market ini
        forecast_outcome:      Outcome yang diprediksi model ("76°F")
        market_leading_outcome: Outcome dengan volume tertinggi di market

    Returns:
        VolumeSignal dengan kelly_multiplier yang sudah disesuaikan
    """
    # Tidak ada data volume → tidak ada signal
    if avg_volume <= 0:
        return VolumeSignal(
            has_spike=False,
            spike_direction="NONE",
            spike_magnitude=1.0,
            kelly_multiplier=1.0,
            warning_message="",
        )

    magnitude = volume_24h / avg_volume if avg_volume > 0 else 1.0

    # Tidak ada spike yang signifikan
    if magnitude < settings.VOLUME_SPIKE_THRESHOLD:
        return VolumeSignal(
            has_spike=False,
            spike_direction="NONE",
            spike_magnitude=round(magnitude, 2),
            kelly_multiplier=1.0,
            warning_message="",
        )

    # Ada spike — cek arahnya
    # Jika outcome dengan volume spike = outcome yang kita forecast
    # → Smart money setuju dengan kita (WITH_FORECAST)
    # Jika outcome dengan volume spike ≠ outcome forecast
    # → Smart money berlawanan (AGAINST_FORECAST) → warning!
    is_against = (
        market_leading_outcome != forecast_outcome
        and market_leading_outcome != ""
    )

    if is_against:
        direction   = "AGAINST_FORECAST"
        kelly_mult  = settings.VOLUME_KELLY_REDUCTION
        warning     = (
            f"⚠️ Volume spike {magnitude:.1f}× terdeteksi pada "
            f"outcome **{market_leading_outcome}** — "
            f"berlawanan dengan forecast **{forecast_outcome}**. "
            f"Kelly dikurangi menjadi {kelly_mult:.0%}."
        )
        log.warning(
            "[Volume] AGAINST FORECAST — spike %.1f× on %s vs forecast %s",
            magnitude, market_leading_outcome, forecast_outcome,
        )
    else:
        direction  = "WITH_FORECAST"
        kelly_mult = 1.0
        warning    = ""
        log.info(
            "[Volume] WITH FORECAST — spike %.1f× confirms %s",
            magnitude, forecast_outcome,
        )

    return VolumeSignal(
        has_spike=True,
        spike_direction=direction,
        spike_magnitude=round(magnitude, 2),
        kelly_multiplier=kelly_mult,
        warning_message=warning,
    )


def calculate_avg_volume(outcomes_volumes: list[float]) -> float:
    """
    Hitung rata-rata volume dari semua outcomes dalam satu event.
    Dipakai sebagai baseline untuk deteksi spike.
    """
    if not outcomes_volumes:
        return 0.0
    valid = [v for v in outcomes_volumes if v > 0]
    if not valid:
        return 0.0
    return sum(valid) / len(valid)
