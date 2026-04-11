"""
test_risk_exit_position.py — Mock test untuk komponen non-network

risk.py, exit_strategy.py, position_tracker.py, volume_analyzer.py
tidak memanggil jaringan langsung — test ini menjalankan logika
sesungguhnya tanpa mock network.
"""
import json
import math
import os
import tempfile
import pytest

from core.risk import kelly_position, LossType, CircuitBreaker, TradingState
from core.volume_analyzer import analyze_volume, calculate_avg_volume, VolumeSignal
from core.position_tracker import PositionTracker, build_position, OpenPosition
from core.probability import ProbabilitySignal
from config.settings import settings


# ==============================================================================
# TEST kelly_position — Sizing
# ==============================================================================

def make_signal(
    signal: str = "BUY_YES",
    prob: float = 0.65,
    market_price: float = 0.50,
    yes_token: str = "yes_token_abc",
    no_token: str = "no_token_xyz",
) -> ProbabilitySignal:
    """Helper buat ProbabilitySignal untuk test."""
    return ProbabilitySignal(
        market_type="BINARY_ABOVE",
        direction="ABOVE",
        best_outcome_label="YES" if signal == "BUY_YES" else "NO",
        best_token_id=yes_token if signal == "BUY_YES" else no_token,
        best_market_price=market_price,
        best_prob_model=prob,
        best_edge=round(prob - market_price, 4),
        best_net_edge=round(prob - market_price - settings.TRADING_FEE_PCT, 4),
        signal=signal,
        all_outcomes=[],
        model_mean_c=25.0,
        model_std_c=1.5,
        threshold_c=25.0,
        threshold_low_c=25.0,
        threshold_high_c=25.0,
        forecast_outcome="YES",
    )


class TestKellyPosition:

    def test_returns_position_for_positive_ev(self):
        """Edge positif → kelly_position harus return PositionOrder."""
        signal   = make_signal("BUY_YES", prob=0.70, market_price=0.50)
        position = kelly_position(signal, bankroll_usd=200.0)
        assert position is not None
        assert position.size_usd > 0
        assert position.side == "YES"

    def test_returns_none_for_no_trade(self):
        """NO_TRADE signal → return None."""
        signal = make_signal("NO_TRADE")
        assert kelly_position(signal, bankroll_usd=200.0) is None

    def test_size_capped_at_max_position(self):
        """Size tidak boleh melebihi MAX_POSITION_USD."""
        signal   = make_signal("BUY_YES", prob=0.99, market_price=0.01)
        position = kelly_position(signal, bankroll_usd=10000.0)
        assert position is not None
        assert position.size_usd <= settings.MAX_POSITION_USD, \
            f"Size ${position.size_usd} melebihi max ${settings.MAX_POSITION_USD}"

    def test_size_minimum_one_dollar(self):
        """Size minimum $1 (tidak ada order < $1)."""
        signal   = make_signal("BUY_YES", prob=0.51, market_price=0.50)
        position = kelly_position(signal, bankroll_usd=5.0)  # bankroll kecil
        if position is not None:
            assert position.size_usd >= 1.0

    def test_buy_no_uses_correct_token(self):
        """
        Bug #4 regression: BUY_NO harus pakai no_token.
        Signal.best_token_id sudah di-set ke no_token oleh probability.py.
        Kelly hanya meneruskannya — tidak boleh menggantinya.
        """
        no_token = "no_token_xyz789"
        signal   = make_signal("BUY_NO", prob=0.30, market_price=0.50, no_token=no_token)
        position = kelly_position(signal, bankroll_usd=200.0)

        assert position is not None
        assert position.token_id == no_token, \
            f"BUY_NO harus pakai no_token={no_token}, dapat {position.token_id}"
        assert position.side == "NO"

    def test_confidence_multiplier_reduces_size(self):
        """Confidence 0.5 harus menghasilkan size lebih kecil dari confidence 1.0."""
        signal = make_signal("BUY_YES", prob=0.70, market_price=0.50)

        pos_full = kelly_position(signal, bankroll_usd=200.0, confidence_multiplier=1.0)
        pos_half = kelly_position(signal, bankroll_usd=200.0, confidence_multiplier=0.5)

        assert pos_full is not None and pos_half is not None
        # pos_half size harus lebih kecil atau sama (ada noise ±4%)
        # Gunakan margin yang cukup untuk noise
        assert pos_half.size_usd <= pos_full.size_usd * 0.80, \
            f"Confidence 0.5 harusnya reduce size: {pos_half.size_usd} vs {pos_full.size_usd}"

    def test_golden_hour_warn_reduces_size(self):
        """Golden Hour WARN (×0.7) harus menghasilkan size lebih kecil."""
        signal = make_signal("BUY_YES", prob=0.70, market_price=0.50)

        pos_open = kelly_position(signal, bankroll_usd=200.0, golden_hour_multiplier=1.0)
        pos_warn = kelly_position(signal, bankroll_usd=200.0, golden_hour_multiplier=0.7)

        assert pos_open is not None and pos_warn is not None
        assert pos_warn.size_usd <= pos_open.size_usd * 0.85

    def test_returns_none_for_negative_ev(self):
        """Jika prob sangat rendah dan harga tinggi → full_kelly negatif → None."""
        signal = make_signal("BUY_YES", prob=0.10, market_price=0.90)
        # b = (1/0.90) - 1 = 0.111
        # full_kelly = (0.10 * 1.111 - 1) / 0.111 = (0.111 - 1) / 0.111 = -8.0
        result = kelly_position(signal, bankroll_usd=200.0)
        assert result is None, "Negative EV harus return None"


# ==============================================================================
# TEST analyze_volume — Volume signal
# ==============================================================================

class TestAnalyzeVolume:

    def test_no_spike_below_threshold(self):
        """Volume 2× normal (< 3×) → tidak ada spike."""
        signal = analyze_volume(
            outcome_label="76°F",
            volume_24h=200.0,
            avg_volume=100.0,       # magnitude = 2× < 3× threshold
            forecast_outcome="76°F",
            market_leading_outcome="76°F",
        )
        assert not signal.has_spike
        assert signal.spike_direction == "NONE"
        assert signal.kelly_multiplier == 1.0

    def test_spike_with_forecast(self):
        """Volume 4× + leading == forecast → WITH_FORECAST, Kelly=1.0."""
        signal = analyze_volume(
            outcome_label="76°F",
            volume_24h=400.0,
            avg_volume=100.0,          # magnitude = 4× > 3×
            forecast_outcome="76°F",
            market_leading_outcome="76°F",  # sama dengan forecast
        )
        assert signal.has_spike
        assert signal.spike_direction == "WITH_FORECAST"
        assert signal.kelly_multiplier == 1.0

    def test_spike_against_forecast(self):
        """Volume 4× + leading ≠ forecast → AGAINST_FORECAST, Kelly dikurangi."""
        signal = analyze_volume(
            outcome_label="76°F",
            volume_24h=400.0,
            avg_volume=100.0,
            forecast_outcome="76°F",
            market_leading_outcome="80°F",  # berbeda dari forecast
        )
        assert signal.has_spike
        assert signal.spike_direction == "AGAINST_FORECAST"
        assert signal.kelly_multiplier == settings.VOLUME_KELLY_REDUCTION
        assert len(signal.warning_message) > 0

    def test_zero_avg_volume_neutral(self):
        """Tidak ada data volume baseline → neutral signal."""
        signal = analyze_volume("76°F", 500.0, 0.0, "76°F", "76°F")
        assert signal.spike_direction == "NONE"
        assert signal.kelly_multiplier == 1.0

    def test_warning_disabled_returns_neutral(self):
        """Jika VOLUME_WARNING_ENABLED=False → selalu neutral."""
        original = settings.VOLUME_WARNING_ENABLED
        try:
            settings.VOLUME_WARNING_ENABLED = False
            signal = analyze_volume("76°F", 400.0, 100.0, "76°F", "80°F")
            assert signal.spike_direction == "NONE"
            assert signal.kelly_multiplier == 1.0
        finally:
            settings.VOLUME_WARNING_ENABLED = original

    def test_calculate_avg_volume(self):
        """Rata-rata hanya dari volume > 0."""
        avg = calculate_avg_volume([100.0, 0.0, 200.0, 0.0, 300.0])
        assert abs(avg - 200.0) < 0.01  # (100+200+300)/3 = 200

    def test_calculate_avg_all_zero(self):
        assert calculate_avg_volume([0.0, 0.0, 0.0]) == 0.0

    def test_calculate_avg_empty(self):
        assert calculate_avg_volume([]) == 0.0


# ==============================================================================
# TEST PositionTracker + build_position — State management
# ==============================================================================

class TestPositionTracker:

    @pytest.fixture
    def tracker_with_tempfile(self, tmp_path):
        """PositionTracker yang pakai temp file untuk isolasi test."""
        state_file = str(tmp_path / "test_state.json")
        # Override settings untuk test
        original = settings.STATE_FILE
        settings.STATE_FILE = state_file
        tracker = PositionTracker()
        yield tracker
        settings.STATE_FILE = original

    def _make_position(self, city="new york", outcome="76F", date_str="2026-04-15"):
        return build_position(
            market_id="market_001",
            event_slug="nyc-temp-apr-15",
            token_id="yes_token_abc",
            city_key=city,
            outcome_label=outcome,
            market_type="BINARY_ABOVE",
            entry_price=0.35,
            size_usd=10.0,
            expires=f"{date_str}T23:00:00Z",
        )

    def test_add_and_retrieve_position(self, tracker_with_tempfile):
        tracker = tracker_with_tempfile
        pos = self._make_position()
        tracker.add(pos)

        retrieved = tracker.get(pos.position_id)
        assert retrieved is not None
        assert retrieved.position_id == pos.position_id
        assert retrieved.city_key == "new york"

    def test_has_any_position_for_detects_existing(self, tracker_with_tempfile):
        """
        Bug #3 fix: has_any_position_for harus detect posisi yang ada.
        """
        tracker = tracker_with_tempfile
        pos     = self._make_position(city="new york", date_str="2026-04-15")
        tracker.add(pos)

        assert tracker.has_any_position_for("new york", "2026-04-15") is True

    def test_has_any_position_for_different_date(self, tracker_with_tempfile):
        """Tanggal berbeda → tidak ada posisi."""
        tracker = tracker_with_tempfile
        pos     = self._make_position(city="new york", date_str="2026-04-15")
        tracker.add(pos)

        assert tracker.has_any_position_for("new york", "2026-04-16") is False

    def test_has_any_position_for_different_city(self, tracker_with_tempfile):
        """Kota berbeda → tidak ada posisi."""
        tracker = tracker_with_tempfile
        pos     = self._make_position(city="new york", date_str="2026-04-15")
        tracker.add(pos)

        assert tracker.has_any_position_for("london", "2026-04-15") is False

    def test_closed_position_not_detected(self, tracker_with_tempfile):
        """Posisi yang sudah ditutup tidak boleh dianggap ada."""
        tracker = tracker_with_tempfile
        pos     = self._make_position(city="new york", date_str="2026-04-15")
        tracker.add(pos)
        tracker.close_position(pos.position_id, "CLOSED_WIN")

        assert tracker.has_any_position_for("new york", "2026-04-15") is False

    def test_stop_loss_calculation(self):
        """
        Stop loss harus = entry * (1 - STOP_LOSS_PCT).
        Default: entry=0.35, SL=0.50 → stop=0.175.
        """
        pos = self._make_position()
        expected_sl = round(0.35 * (1 - settings.STOP_LOSS_PCT), 4)
        assert abs(pos.stop_loss_price - expected_sl) < 0.001, \
            f"Stop loss {pos.stop_loss_price} ≠ expected {expected_sl}"

    def test_take_profit_capped_at_095(self):
        """Take profit di-cap di 0.95 (fee masih ada)."""
        # Entry 0.01 × (1 + 1.50) = 0.025 → setelah cap = 0.025 (under 0.95, OK)
        pos = build_position(
            market_id="m", event_slug="e", token_id="t",
            city_key="new york", outcome_label="YES",
            market_type="BINARY_ABOVE",
            entry_price=0.60,  # entry tinggi → TP bisa > 0.95
            size_usd=10.0,
            expires="2026-04-15T23:00:00Z",
        )
        assert pos.take_profit_price <= 0.95, \
            f"Take profit {pos.take_profit_price} melebihi cap 0.95"

    def test_position_id_format(self):
        """Position ID harus format {city}-{outcome}-{date_compact}."""
        pos = self._make_position(city="new york", outcome="76F", date_str="2026-04-15")
        assert "new york" in pos.position_id or "new_york" in pos.position_id
        assert "76F" in pos.position_id
        assert "20260415" in pos.position_id

    def test_atomic_write_creates_no_partial_file(self, tracker_with_tempfile):
        """
        Atomic write: tidak ada file .tmp tersisa setelah save.
        """
        tracker = tracker_with_tempfile
        pos     = self._make_position()
        tracker.add(pos)

        tmp_path = settings.STATE_FILE + ".tmp"
        assert not os.path.exists(tmp_path), \
            ".tmp file tidak boleh tersisa setelah atomic write selesai"

    def test_state_persists_across_instances(self, tmp_path):
        """
        State harus persist ke disk dan dapat dibaca oleh instance baru.
        Ini verifikasi bahwa save/load bekerja end-to-end.
        """
        state_file = str(tmp_path / "persist_state.json")
        original   = settings.STATE_FILE
        settings.STATE_FILE = state_file

        try:
            # Instance 1: tambah posisi
            tracker1 = PositionTracker()
            pos      = self._make_position()
            tracker1.add(pos)

            # Instance 2: baca dari disk
            tracker2 = PositionTracker()
            assert tracker2.has_any_position_for("new york", "2026-04-15"), \
                "Posisi harus persist ke disk dan bisa dibaca instance baru"
        finally:
            settings.STATE_FILE = original


# ==============================================================================
# TEST CircuitBreaker — State machine trading
# ==============================================================================

class TestCircuitBreaker:

    @pytest.fixture
    def breaker(self, tmp_path):
        """CircuitBreaker dengan state file terisolasi."""
        state_file = str(tmp_path / "cb_state.json")
        original   = settings.STATE_FILE
        settings.STATE_FILE = state_file

        # Init state kosong
        with open(state_file, "w") as f:
            json.dump({}, f)

        cb = CircuitBreaker()
        yield cb
        settings.STATE_FILE = original

    def test_initial_state_not_open(self, breaker):
        assert breaker.is_open() is False

    def test_trips_after_consecutive_losses(self, breaker):
        """Setelah CIRCUIT_BREAKER_LOSSES losses → is_open() = True."""
        for i in range(settings.CIRCUIT_BREAKER_LOSSES):
            breaker.record_loss(5.0)
        assert breaker.is_open() is True

    def test_win_resets_consecutive_streak(self, breaker):
        """Win setelah 2 loss → consecutive_losses kembali ke 0."""
        breaker.record_loss(5.0)
        breaker.record_loss(5.0)
        breaker.record_win(10.0)
        assert breaker.state.consecutive_losses == 0

    def test_manual_reset_opens_breaker(self, breaker):
        """manual_reset() harus membuka kembali circuit breaker."""
        for i in range(settings.CIRCUIT_BREAKER_LOSSES):
            breaker.record_loss(5.0)
        assert breaker.is_open() is True

        breaker.manual_reset()
        assert breaker.is_open() is False
        assert breaker.state.consecutive_losses == 0

    def test_rejection_not_counted_in_loss_streak(self, breaker):
        """
        ORDER_REJECTED tidak menghitung ke consecutive_losses streak.
        Ini penting: rejection karena likuiditas tidak sama dengan trade loss.
        """
        breaker.record_loss(0.0, loss_type=LossType.ORDER_REJECTED)
        breaker.record_loss(0.0, loss_type=LossType.ORDER_REJECTED)
        breaker.record_loss(0.0, loss_type=LossType.ORDER_REJECTED)

        # Circuit breaker tidak boleh trip dari rejections saja
        assert breaker.is_open() is False
        assert breaker.state.consecutive_rejections == 3
        assert breaker.state.consecutive_losses == 0

    def test_daily_stats_tracked(self, breaker):
        """Win/loss harus dicatat di daily_stats."""
        breaker.record_win(15.0, region="US", market_type="BINARY_ABOVE")
        breaker.record_loss(5.0, region="Europe")

        summary = breaker.get_daily_pnl_summary()
        assert summary["today_trades"] >= 2
        assert summary["today_wins"] >= 1
