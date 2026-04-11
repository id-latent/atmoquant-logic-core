"""
test_consensus_and_registry.py — Mock test untuk consensus.py dan location_registry.py

consensus.py: mock Open-Meteo API responses
location_registry.py: test logika golden hour, resolve_location, Canada fix
"""
import json
import math
import pytest
import httpx
import respx
from datetime import date, datetime, timezone, timedelta

from core.consensus import (
    get_triple_lock_consensus,
    is_decision_phase,
    _population_std,
)
from core.location_registry import (
    check_golden_hour,
    golden_hour_multiplier,
    resolve_location,
    GoldenHourStatus,
    LOCATION_REGISTRY,
    to_celsius,
)
from config.settings import settings


# ==============================================================================
# TEST _population_std — Bug #8 fix
# ==============================================================================

class TestPopulationStd:
    """
    Verifikasi bahwa std dev dihitung dengan benar.
    Ini adalah inti dari Bug #8 fix.
    """

    def test_identical_values_zero_std(self):
        """Semua model sama persis → std dev = 0."""
        assert _population_std([20.0, 20.0, 20.0]) == 0.0

    def test_known_values(self):
        """[10, 20, 30] → mean=20, deviations=[100, 0, 100], var=66.67, std=8.165."""
        result = _population_std([10.0, 20.0, 30.0])
        expected = math.sqrt(((10-20)**2 + (20-20)**2 + (30-20)**2) / 3)
        assert abs(result - round(expected, 3)) < 0.001

    def test_std_vs_range_outlier(self):
        """
        [20, 20, 20, 30] — ada 1 outlier.
        Range = 10 (sangat besar karena outlier).
        Std dev = 4.33 (lebih representatif — 3 model sepakat).
        Std dev HARUS lebih kecil dari range.
        """
        values = [20.0, 20.0, 20.0, 30.0]
        old_range = max(values) - min(values)  # = 10.0
        new_std   = _population_std(values)    # = 4.33

        assert new_std < old_range, \
            f"Std dev {new_std} harus < range {old_range} untuk distribusi dengan outlier"
        assert abs(new_std - 4.330) < 0.01

    def test_single_value_zero(self):
        """Satu nilai → std dev = 0 (tidak bisa menghitung dispersi)."""
        assert _population_std([25.0]) == 0.0

    def test_empty_zero(self):
        assert _population_std([]) == 0.0


# ==============================================================================
# TEST get_triple_lock_consensus — Mock Open-Meteo API
# ==============================================================================

def make_openmeteo_response(t_max: float, t_min: float, t_mean: float) -> dict:
    """Buat response tiruan Open-Meteo."""
    return {
        "daily": {
            "time": ["2026-04-15"],
            "temperature_2m_max":  [t_max],
            "temperature_2m_min":  [t_min],
            "temperature_2m_mean": [t_mean],
        }
    }


@pytest.mark.asyncio
class TestGetTripleLockConsensus:

    def _mock_all_models(self, t_max, t_min, t_mean, icon_t_max=None, icon_t_min=None, icon_t_mean=None):
        """Helper: mock semua 4 model endpoint."""
        for endpoint in ["/ecmwf", "/gfs", "/forecast"]:
            respx.get(
                url__startswith=f"{settings.OPENMETEO_BASE}{endpoint}"
            ).mock(return_value=httpx.Response(
                200, json=make_openmeteo_response(t_max, t_min, t_mean)
            ))
        icon_resp = make_openmeteo_response(
            icon_t_max or t_max,
            icon_t_min or t_min,
            icon_t_mean or t_mean,
        )
        respx.get(
            url__startswith=f"{settings.OPENMETEO_BASE}/dwd-icon"
        ).mock(return_value=httpx.Response(200, json=icon_resp))

    @respx.mock
    async def test_consensus_returns_result_when_all_models_ok(self):
        """Semua 4 model berhasil → return ConsensusResult."""
        self._mock_all_models(t_max=30.0, t_min=20.0, t_mean=25.0)

        result = await get_triple_lock_consensus(
            latitude=40.7128,
            longitude=-74.0060,
            location_name="new york",
            target_date=date(2026, 4, 15),
        )

        assert result is not None, "Harus return ConsensusResult"
        assert result.model_count == 4
        assert result.ecmwf is not None
        assert result.gfs   is not None
        assert result.noaa  is not None
        assert result.icon  is not None

    @respx.mock
    async def test_triple_lock_true_when_models_agree(self):
        """
        Semua model return nilai yang sama → std dev = 0 → triple_lock = True.
        """
        self._mock_all_models(t_max=30.0, t_min=20.0, t_mean=25.0)

        result = await get_triple_lock_consensus(
            latitude=40.7128, longitude=-74.0060,
            location_name="new york",
            target_date=date(2026, 4, 15),
        )

        assert result is not None
        assert result.triple_lock is True, \
            f"Semua model sama → harus triple_lock=True, σ={result.inter_model_variance}"
        assert result.inter_model_variance == 0.0

    @respx.mock
    async def test_triple_lock_false_when_models_disagree(self):
        """
        Model ECMWF/GFS/NOAA tidak sepakat (spread > 1°C std) → triple_lock = False.
        """
        # ECMWF = 30°C, GFS = 23°C, NOAA = 25°C mean → std > 1°C
        for endpoint, t_mean in [("/ecmwf", 30.0), ("/gfs", 23.0), ("/forecast", 25.0)]:
            respx.get(
                url__startswith=f"{settings.OPENMETEO_BASE}{endpoint}"
            ).mock(return_value=httpx.Response(
                200, json=make_openmeteo_response(t_mean+5, t_mean-5, t_mean)
            ))
        respx.get(
            url__startswith=f"{settings.OPENMETEO_BASE}/dwd-icon"
        ).mock(return_value=httpx.Response(
            200, json=make_openmeteo_response(27, 17, 24.0)
        ))

        result = await get_triple_lock_consensus(
            latitude=40.7128, longitude=-74.0060,
            location_name="new york",
            target_date=date(2026, 4, 15),
        )

        assert result is not None
        # Std dev dari [30, 23, 25, 24] >> 1.0
        assert result.triple_lock is False, \
            f"Model tidak sepakat → triple_lock harus False, σ={result.inter_model_variance}"
        assert result.inter_model_variance > 1.0

    @respx.mock
    async def test_icon_failure_degrades_gracefully(self):
        """
        ICON gagal → lanjut dengan 3 model, triple_lock masih bisa True.
        Bug asli: kalau ICON gagal, harusnya tidak abort.
        """
        for endpoint in ["/ecmwf", "/gfs", "/forecast"]:
            respx.get(
                url__startswith=f"{settings.OPENMETEO_BASE}{endpoint}"
            ).mock(return_value=httpx.Response(
                200, json=make_openmeteo_response(30.0, 20.0, 25.0)
            ))
        # ICON gagal dengan 500
        respx.get(
            url__startswith=f"{settings.OPENMETEO_BASE}/dwd-icon"
        ).mock(return_value=httpx.Response(500, text="Service unavailable"))

        result = await get_triple_lock_consensus(
            latitude=40.7128, longitude=-74.0060,
            location_name="new york",
            target_date=date(2026, 4, 15),
        )

        assert result is not None, "ICON gagal tidak boleh abort consensus"
        assert result.icon is None, "icon harus None jika gagal"
        assert result.model_count == 3

    @respx.mock
    async def test_required_model_failure_aborts(self):
        """
        ECMWF (required) gagal → return None (abort).
        """
        # ECMWF gagal
        respx.get(
            url__startswith=f"{settings.OPENMETEO_BASE}/ecmwf"
        ).mock(return_value=httpx.Response(500))
        # GFS dan NOAA OK
        for endpoint in ["/gfs", "/forecast"]:
            respx.get(
                url__startswith=f"{settings.OPENMETEO_BASE}{endpoint}"
            ).mock(return_value=httpx.Response(
                200, json=make_openmeteo_response(30.0, 20.0, 25.0)
            ))
        respx.get(
            url__startswith=f"{settings.OPENMETEO_BASE}/dwd-icon"
        ).mock(return_value=httpx.Response(
            200, json=make_openmeteo_response(30.0, 20.0, 25.0)
        ))

        result = await get_triple_lock_consensus(
            latitude=40.7128, longitude=-74.0060,
            location_name="new york",
            target_date=date(2026, 4, 15),
        )

        assert result is None, \
            "ECMWF gagal (required) → harus return None"

    @respx.mock
    async def test_consensus_values_are_averages(self):
        """
        consensus_t_mean harus rata-rata dari semua model.
        """
        # ECMWF=25, GFS=27, NOAA=29 → mean = 27.0
        for endpoint, t_mean in [("/ecmwf", 25.0), ("/gfs", 27.0), ("/forecast", 29.0)]:
            respx.get(
                url__startswith=f"{settings.OPENMETEO_BASE}{endpoint}"
            ).mock(return_value=httpx.Response(
                200, json=make_openmeteo_response(t_mean+3, t_mean-3, t_mean)
            ))
        respx.get(
            url__startswith=f"{settings.OPENMETEO_BASE}/dwd-icon"
        ).mock(return_value=httpx.Response(
            200, json=make_openmeteo_response(30, 20, 27.0)
        ))

        result = await get_triple_lock_consensus(
            latitude=40.7128, longitude=-74.0060,
            location_name="new york",
            target_date=date(2026, 4, 15),
        )

        assert result is not None
        # Mean dari [25, 27, 29, 27] = 27.0
        assert abs(result.consensus_t_mean - 27.0) < 0.1, \
            f"Expected mean 27.0, dapat {result.consensus_t_mean}"


# ==============================================================================
# TEST is_decision_phase — NWP model run windows
# ==============================================================================

class TestIsDecisionPhase:

    def test_at_00z_is_decision_phase(self):
        """Tepat di 00z → decision phase."""
        now = datetime(2026, 4, 15, 0, 0, tzinfo=timezone.utc)
        assert is_decision_phase(now) is True

    def test_at_12z_is_decision_phase(self):
        """Tepat di 12z → decision phase."""
        now = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
        assert is_decision_phase(now) is True

    def test_within_tolerance_is_decision_phase(self):
        """1 jam dari 00z → dalam tolerance ±2h → True."""
        now = datetime(2026, 4, 15, 1, 30, tzinfo=timezone.utc)
        assert is_decision_phase(now) is True

    def test_outside_tolerance_is_not_decision_phase(self):
        """6z (jauh dari 00z dan 12z) → False."""
        now = datetime(2026, 4, 15, 6, 0, tzinfo=timezone.utc)
        assert is_decision_phase(now) is False


# ==============================================================================
# TEST location_registry — Golden Hour dan Canada fix
# ==============================================================================

class TestLocationRegistry:

    def test_resolve_nyc_variations(self):
        """Semua variasi nama NYC harus resolve ke kota yang dikenal."""
        for query in [
            "Will the high temperature in NYC exceed 90°F?",
            "New York City high temperature",
            "temperature at JFK airport",
            "Central Park temperature",
        ]:
            city = resolve_location(query)
            assert city is not None, f"'{query}' harus resolve ke kota"

    def test_resolve_specific_before_general(self):
        """
        'st james park' harus match sebelum 'london'
        (lebih spesifik lebih panjang → prioritas lebih tinggi).
        """
        city = resolve_location("temperature at st james park")
        assert city is not None
        assert city.key == "st james park", \
            f"Harusnya 'st james park', dapat '{city.key}'"

    def test_resolve_unknown_city_returns_none(self):
        city = resolve_location("temperature in Atlantis tomorrow")
        assert city is None

    def test_canada_is_not_other_region(self):
        """
        Bug #11 fix: Toronto harus region 'Canada', bukan 'Other'.
        Golden hour yang salah menyebabkan bot skip market Canada.
        """
        toronto = LOCATION_REGISTRY.get("toronto")
        assert toronto is not None
        assert toronto.region == "Canada", \
            f"Toronto harus region 'Canada', dapat '{toronto.region}'"

    def test_canada_golden_hour_window(self):
        """Canada golden hour window harus sama dengan US (pola trading serupa)."""
        us_window  = settings.get_golden_hour_window("US")
        can_window = settings.get_golden_hour_window("Canada")
        assert can_window == us_window, \
            f"Canada {can_window} harus sama dengan US {us_window}"

    def test_golden_hour_open_status(self):
        """5 jam sebelum tutup + US region → status OPEN (window 2-10h)."""
        nyc = LOCATION_REGISTRY["new york"]
        status = check_golden_hour(nyc, hours_to_close=5.0)
        assert status == GoldenHourStatus.OPEN, \
            f"5h sebelum tutup US harus OPEN, dapat {status}"

    def test_golden_hour_near_status(self):
        """1.5 jam sebelum tutup → NEAR (sangat dekat close)."""
        nyc = LOCATION_REGISTRY["new york"]
        status = check_golden_hour(nyc, hours_to_close=1.5)
        assert status == GoldenHourStatus.NEAR, \
            f"1.5h sebelum tutup harus NEAR, dapat {status}"

    def test_golden_hour_warn_status(self):
        """15 jam sebelum tutup → WARN (jauh dari optimal window 2-10h)."""
        nyc = LOCATION_REGISTRY["new york"]
        status = check_golden_hour(nyc, hours_to_close=15.0)
        assert status == GoldenHourStatus.WARN, \
            f"15h sebelum tutup harus WARN, dapat {status}"

    def test_golden_hour_skip_too_far(self):
        """25 jam sebelum tutup → SKIP (di atas MAX_HOURS_TO_CLOSE=20h)."""
        nyc = LOCATION_REGISTRY["new york"]
        status = check_golden_hour(nyc, hours_to_close=25.0)
        assert status == GoldenHourStatus.SKIP

    def test_golden_hour_skip_too_close(self):
        """0.5 jam sebelum tutup → SKIP (di bawah MIN_HOURS_TO_CLOSE=1h)."""
        nyc = LOCATION_REGISTRY["new york"]
        status = check_golden_hour(nyc, hours_to_close=0.5)
        assert status == GoldenHourStatus.SKIP

    def test_golden_hour_multipliers(self):
        """Multiplier harus sesuai settings."""
        assert golden_hour_multiplier(GoldenHourStatus.OPEN) == settings.GOLDEN_HOUR_OPEN_MULT
        assert golden_hour_multiplier(GoldenHourStatus.WARN) == settings.GOLDEN_HOUR_WARN_MULT
        assert golden_hour_multiplier(GoldenHourStatus.NEAR) == settings.GOLDEN_HOUR_NEAR_MULT
        assert golden_hour_multiplier(GoldenHourStatus.SKIP) == 0.0

    def test_to_celsius_conversion(self):
        """Konversi Fahrenheit ke Celsius harus akurat."""
        assert abs(to_celsius(32.0, "F") - 0.0)   < 0.01   # freezing
        assert abs(to_celsius(212.0, "F") - 100.0) < 0.01  # boiling
        assert abs(to_celsius(98.6, "F") - 37.0)   < 0.01  # body temp
        assert abs(to_celsius(25.0, "C") - 25.0)   < 0.01  # passthrough

    def test_all_timezones_valid(self):
        """Semua timezone di registry harus valid (bisa di-load pytz)."""
        import pytz
        invalid = []
        for key, city in LOCATION_REGISTRY.items():
            try:
                pytz.timezone(city.tz)
            except Exception as e:
                invalid.append(f"{key}: {city.tz} — {e}")
        assert not invalid, f"Timezone tidak valid: {invalid}"

    def test_doha_timezone_valid(self):
        """Bug #17: Asia/Qatar harus valid di pytz."""
        import pytz
        doha = LOCATION_REGISTRY.get("doha")
        assert doha is not None
        tz = pytz.timezone(doha.tz)
        assert tz is not None
        assert "Qatar" in str(tz)
