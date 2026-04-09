# ==============================================================================
# core/volume_analyzer.py — Volume Signal Analyzer (FIXED)
# ==============================================================================
"""
AQL Volume Analyzer

Fixes:
  VOLUME-1: magnitude dihitung dua kali — redundant ternary dihapus
  VOLUME-2: settings.VOLUME_WARNING_ENABLED tidak dicek — sekarang dicek
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from config.settings import settings

log = logging.getLogger("aql.volume")


@dataclass
class VolumeSignal:
    has_spike: bool
    spike_direction: str      # "WITH_FORECAST" / "AGAINST_FORECAST" / "NONE"
    spike_magnitude: float
    kelly_multiplier: float
    warning_message: str


def _neutral_signal(magnitude: float = 1.0) -> VolumeSignal:
    return VolumeSignal(
        has_spike=False,
        spike_direction="NONE",
        spike_magnitude=round(magnitude, 2),
        kelly_multiplier=1.0,
        warning_message="",
    )


def analyze_volume(
    outcome_label: str,
    volume_24h: float,
    avg_volume: float,
    forecast_outcome: str,
    market_leading_outcome: str,
) -> VolumeSignal:
    """
    Analisis volume untuk satu outcome.

    FIX VOLUME-1: Hapus redundant ternary magnitude calculation.
    FIX VOLUME-2: Cek settings.VOLUME_WARNING_ENABLED sebelum analisis.
    """
    # FIX VOLUME-2: Cek setting enabled
    if not settings.VOLUME_WARNING_ENABLED:
        return _neutral_signal()

    # Tidak ada data volume baseline
    if avg_volume <= 0:
        return _neutral_signal()

    # FIX VOLUME-1: avg_volume sudah pasti > 0, tidak perlu ternary
    magnitude = volume_24h / avg_volume

    if magnitude < settings.VOLUME_SPIKE_THRESHOLD:
        return _neutral_signal(magnitude)

    # Ada spike — cek arah
    is_against = (
        market_leading_outcome != forecast_outcome
        and market_leading_outcome != ""
    )

    if is_against:
        direction  = "AGAINST_FORECAST"
        kelly_mult = settings.VOLUME_KELLY_REDUCTION
        warning    = (
            f"Volume spike {magnitude:.1f}x terdeteksi pada "
            f"outcome **{market_leading_outcome}** — "
            f"berlawanan dengan forecast **{forecast_outcome}**. "
            f"Kelly dikurangi menjadi {kelly_mult:.0%}."
        )
        log.warning(
            "[Volume] AGAINST FORECAST — spike %.1fx on %s vs forecast %s",
            magnitude, market_leading_outcome, forecast_outcome,
        )
    else:
        direction  = "WITH_FORECAST"
        kelly_mult = 1.0
        warning    = ""
        log.info(
            "[Volume] WITH FORECAST — spike %.1fx confirms %s",
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
    """Rata-rata volume dari outcomes yang punya volume > 0."""
    if not outcomes_volumes:
        return 0.0
    valid = [v for v in outcomes_volumes if v > 0]
    if not valid:
        return 0.0
    return sum(valid) / len(valid)
