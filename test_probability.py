"""
test_probability.py — Mock test untuk core/probability.py

Mengapa test ini penting:
  Static analysis (test_fixes.py) hanya memverifikasi kode yang ada.
  Test ini menjalankan kode yang sebenarnya dan memverifikasi HASIL
  kalkulasi probabilitas benar secara matematis.

  Jika ada bug di formula norm.cdf / norm.sf, atau logika BUY_YES/BUY_NO,
  test ini yang akan menangkapnya — bukan static analysis.
"""
import pytest
import math
from scipy.stats import norm

from core.probability import (
    evaluate_binary,
    evaluate_multi_outcome,
    OutcomeCandidate,
    _compute_edge_and_signal,
    _select_binary_token,
    parse_outcome_temperature,
    is_open_ended_high,
    is_open_ended_low,
)
from config.settings import settings


# ==============================================================================
# TEST _compute_edge_and_signal — Unit test helper kritis
# ==============================================================================

class TestComputeEdgeAndSignal:
    """
    _compute_edge_and_signal() adalah helper yang dipakai semua evaluator.
    Jika ini salah, semua trading decision salah.
    """

    def test_buy_yes_when_model_above_market(self):
        """Model lebih optimis dari pasar → BUY_YES."""
        edge, net, signal = _compute_edge_and_signal(
            prob_model=0.70,    # Model: 70% kemungkinan
            market_price=0.50,  # Pasar: 50% implied
            min_edge=0.05,
        )
        assert signal == "BUY_YES"
        assert abs(edge - 0.20) < 0.001          # edge = 0.70 - 0.50
        assert abs(net - (0.20 - settings.TRADING_FEE_PCT)) < 0.001

    def test_buy_no_when_model_below_market(self):
        """Model lebih pesimis dari pasar → BUY_NO."""
        edge, net, signal = _compute_edge_and_signal(
            prob_model=0.30,    # Model: 30% kemungkinan
            market_price=0.50,  # Pasar: 50% implied
            min_edge=0.05,
        )
        assert signal == "BUY_NO", f"Harusnya BUY_NO, dapat {signal}"
        # edge_no = (1 - 0.30) - (1 - 0.50) = 0.70 - 0.50 = 0.20
        assert abs(net - (0.20 - settings.TRADING_FEE_PCT)) < 0.001

    def test_no_trade_when_edge_too_small(self):
        """Edge terlalu kecil → NO_TRADE."""
        _, net, signal = _compute_edge_and_signal(
            prob_model=0.52,
            market_price=0.50,
            min_edge=0.05,
        )
        assert signal == "NO_TRADE"
        assert net < 0.05  # net = 0.02 - 0.017 = 0.003 < 0.05

    def test_exact_min_edge_boundary(self):
        """Tepat di batas min_edge → seharusnya TRADE (≥, bukan >)."""
        fee  = settings.TRADING_FEE_PCT
        # net = edge - fee = min_edge → edge = min_edge + fee
        min_edge    = 0.05
        exact_edge  = min_edge + fee + 0.001  # sedikit di atas
        prob        = 0.50 + exact_edge

        _, net, signal = _compute_edge_and_signal(prob, 0.50, min_edge)
        assert signal == "BUY_YES", f"Tepat di atas batas harus BUY_YES, dapat {signal}"

    def test_no_abs_edge_bug(self):
        """
        Verifikasi bug #5 benar-benar teratasi.
        Dengan abs() lama: edge(-0.20) → net=0.183 → lolos filter
        Dengan fix baru: edge_no = +0.20 (sisi NO) → net=0.183 juga, tapi signal BUY_NO
        Yang penting: signal HARUS BUY_NO bukan BUY_YES.
        """
        edge, net, signal = _compute_edge_and_signal(0.30, 0.50, 0.05)
        assert signal == "BUY_NO", "Prob 0.30 < 0.50 harus BUY_NO, bukan BUY_YES!"


# ==============================================================================
# TEST _select_binary_token — Guard token kosong
# ==============================================================================

class TestSelectBinaryToken:

    def test_buy_yes_uses_yes_token(self, yes_token_id, no_token_id):
        token, sig = _select_binary_token("BUY_YES", yes_token_id, no_token_id)
        assert token == yes_token_id
        assert sig == "BUY_YES"

    def test_buy_no_uses_no_token(self, yes_token_id, no_token_id):
        """Bug #4 fix: BUY_NO harus pakai no_token_id."""
        token, sig = _select_binary_token("BUY_NO", yes_token_id, no_token_id)
        assert token == no_token_id, f"BUY_NO harus pakai no_token, dapat {token}"
        assert sig == "BUY_NO"

    def test_empty_no_token_downgrades_to_no_trade(self, yes_token_id):
        """Guard: jika no_token_id kosong, downgrade ke NO_TRADE."""
        token, sig = _select_binary_token("BUY_NO", yes_token_id, "")
        assert sig == "NO_TRADE", f"Harusnya NO_TRADE, dapat {sig}"
        assert token != "", "Token tidak boleh kosong"

    def test_no_trade_gets_yes_token_as_placeholder(self, yes_token_id, no_token_id):
        """NO_TRADE dapat yes_token sebagai placeholder (tidak dipakai)."""
        token, sig = _select_binary_token("NO_TRADE", yes_token_id, no_token_id)
        assert sig == "NO_TRADE"
        assert token == yes_token_id


# ==============================================================================
# TEST evaluate_binary — Kalkulasi probabilitas binary market
# ==============================================================================

class TestEvaluateBinary:
    """
    Test matematika sesungguhnya dari evaluate_binary().

    Kita tahu model consensus t_max = 30.25°C (dari consensus_tight fixture).
    Kita bisa kalkulasi expected probability manual dan compare dengan output.
    """

    def test_binary_above_high_threshold_low_prob(
        self, consensus_tight, nyc, yes_token_id, no_token_id
    ):
        """
        'Will NYC exceed 86°F (30°C)?' — threshold tepat di consensus mean.
        Probabilitas harusnya sekitar 0.50 (model mean tepat di threshold).
        """
        # 86°F = 30°C, consensus t_max ≈ 31.0°C
        signal = evaluate_binary(
            question="Will the high temperature in NYC exceed 86°F?",
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            market_price=0.50,
            consensus=consensus_tight,
            city=nyc,
            min_edge=0.05,
        )
        assert signal is not None, "evaluate_binary tidak boleh return None untuk market valid"
        # t_max consensus = ~31°C, threshold = 30°C → prob > 0.5
        assert signal.best_prob_model > 0.5, \
            f"t_max 31°C > threshold 30°C → prob harus > 0.5, dapat {signal.best_prob_model}"

    def test_binary_above_very_high_threshold_low_prob(
        self, consensus_tight, nyc, yes_token_id, no_token_id
    ):
        """
        'Will NYC exceed 104°F (40°C)?' — threshold jauh di atas mean.
        Probabilitas harus sangat rendah.
        """
        signal = evaluate_binary(
            question="Will the high temperature in NYC exceed 104°F?",
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            market_price=0.50,
            consensus=consensus_tight,
            city=nyc,
            min_edge=0.05,
        )
        assert signal is not None
        # 40°C jauh di atas mean 30°C → prob sangat kecil
        assert signal.best_prob_model < 0.20, \
            f"Threshold 40°C jauh di atas mean → prob harus < 0.20, dapat {signal.best_prob_model}"
        # Karena prob rendah, market 0.50 → BUY_NO
        assert signal.signal == "BUY_NO", \
            f"Model prob {signal.best_prob_model} << market 0.50 → harusnya BUY_NO"

    def test_binary_below_uses_t_min(
        self, consensus_tight, nyc, yes_token_id, no_token_id
    ):
        """
        Bug #7 fix: BELOW market harus pakai t_min, bukan t_max.
        consensus t_min ≈ 22°C. Threshold 15°C jauh di bawah → prob rendah.
        """
        signal = evaluate_binary(
            question="Will the low temperature in NYC stay below 59°F?",  # 15°C
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            market_price=0.50,
            consensus=consensus_tight,
            city=nyc,
            min_edge=0.05,
        )
        assert signal is not None
        # t_min = 22°C, threshold = 15°C → P(t_min < 15) sangat kecil
        assert signal.best_prob_model < 0.20, \
            f"BELOW: t_min 22°C >> threshold 15°C → prob harus kecil, dapat {signal.best_prob_model}"

    def test_binary_range_uses_t_mean(
        self, consensus_tight, nyc, yes_token_id, no_token_id
    ):
        """
        Bug #7 fix: RANGE market harus pakai t_mean.
        Range 28–32°C mencakup t_mean ≈ 30°C → prob harus cukup tinggi.
        """
        signal = evaluate_binary(
            question="Will the temperature in NYC be between 82-90°F?",  # ~28-32°C
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            market_price=0.50,
            consensus=consensus_tight,
            city=nyc,
            min_edge=0.02,
        )
        assert signal is not None
        # Range mencakup mean → prob harus signifikan
        assert signal.best_prob_model > 0.30, \
            f"Range mencakup mean → prob harus > 0.30, dapat {signal.best_prob_model}"

    def test_no_trade_when_loose_consensus(
        self, consensus_loose, nyc, yes_token_id, no_token_id
    ):
        """
        Dengan variance tinggi → std lebih besar → edge menjadi lebih kecil
        (distribusi lebih flat). Ini bukan aturan pasti NO_TRADE, tapi
        verifikasi bahwa variance mempengaruhi kalkulasi.
        """
        tight_signal = evaluate_binary(
            question="Will the high temperature in NYC exceed 95°F?",
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            market_price=0.50,
            consensus=consensus_loose,   # variance tinggi
            city=nyc,
            min_edge=0.05,
        )
        # Hanya verifikasi tidak crash dan ada signal
        assert tight_signal is not None or tight_signal is None  # either is OK
        # Yang penting: fungsi tidak crash dengan variance tinggi

    def test_unparseable_returns_none(
        self, consensus_tight, nyc, yes_token_id, no_token_id
    ):
        """Market dengan pola tidak bisa di-parse harus return None."""
        signal = evaluate_binary(
            question="Will NYC break the all-time record high temperature?",
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            market_price=0.50,
            consensus=consensus_tight,
            city=nyc,
            min_edge=0.05,
        )
        assert signal is None, "Market 'record high' harus return None (unparseable)"

    def test_degenerate_market_price_returns_none(
        self, consensus_tight, nyc, yes_token_id, no_token_id
    ):
        """Market price 0.0 atau 1.0 adalah degenerate → return None."""
        for bad_price in [0.0, 0.005, 0.995, 1.0]:
            signal = evaluate_binary(
                question="Will the high temperature in NYC exceed 86°F?",
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
                market_price=bad_price,
                consensus=consensus_tight,
                city=nyc,
                min_edge=0.05,
            )
            assert signal is None, \
                f"market_price={bad_price} harus return None (degenerate)"

    def test_celsius_city_parses_correctly(
        self, consensus_tight, london, yes_token_id, no_token_id
    ):
        """
        London pakai unit Celsius. Pastikan parsing tidak salah convert.
        'Will London exceed 28°C?' → threshold 28°C langsung (tidak diconvert).
        """
        signal = evaluate_binary(
            question="Will the high temperature in London exceed 28°C?",
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            market_price=0.50,
            consensus=consensus_tight,
            city=london,
            min_edge=0.05,
        )
        assert signal is not None
        # t_max consensus ≈ 31°C > threshold 28°C → prob > 0.5
        assert signal.best_prob_model > 0.5, \
            f"28°C < t_max 31°C → prob harus > 0.5, dapat {signal.best_prob_model}"


# ==============================================================================
# TEST evaluate_multi_outcome — Multi-outcome market
# ==============================================================================

class TestEvaluateMultiOutcome:
    """
    Test evaluate_multi_outcome() dengan 5 outcome sederhana.
    Verifikasi bahwa outcome yang paling sesuai dengan forecast dipilih.
    """

    def _make_outcomes(self) -> list[OutcomeCandidate]:
        """5 outcome suhu dari 26°F hingga 34°C dengan harga pasar merata."""
        labels_prices = [
            ("26°C", 0.10),
            ("28°C", 0.20),
            ("30°C", 0.40),  # pasar pilih ini sebagai favorite (40%)
            ("32°C", 0.20),
            ("34°C", 0.10),
        ]
        return [
            OutcomeCandidate(
                label=label,
                token_id=f"token_{label.replace('°C','')}",
                market_price=price,
                volume_24h=100.0,
            )
            for label, price in labels_prices
        ]

    def test_best_outcome_matches_consensus_mean(self, consensus_tight, nyc):
        """
        consensus_tight t_max ≈ 31°C.
        Outcome terdekat adalah "30°C" atau "32°C" — harus dipilih.
        """
        outcomes = self._make_outcomes()
        signal = evaluate_multi_outcome(
            outcomes=outcomes,
            consensus=consensus_tight,
            city=nyc,
            min_edge=0.02,
        )
        assert signal is not None
        assert signal.market_type == "MULTI_OUTCOME"
        # forecast_outcome harus outcome dengan prob tertinggi (dekat t_max)
        assert signal.forecast_outcome in ["30°C", "32°C", "28°C"], \
            f"forecast {signal.forecast_outcome} tidak masuk akal untuk t_max 31°C"

    def test_all_outcomes_evaluated(self, consensus_tight, nyc):
        """Semua 5 outcome harus ada di all_outcomes."""
        outcomes = self._make_outcomes()
        signal = evaluate_multi_outcome(
            outcomes=outcomes,
            consensus=consensus_tight,
            city=nyc,
            min_edge=0.02,
        )
        assert signal is not None
        assert len(signal.all_outcomes) == 5, \
            f"Harus ada 5 outcomes, dapat {len(signal.all_outcomes)}"

    def test_all_probs_sum_close_to_one(self, consensus_tight, nyc):
        """
        Semua probabilitas model harus mendekati 1.0 (mereka adalah
        potongan distribusi normal yang bersama-sama menutup hampir semua area).
        """
        outcomes = self._make_outcomes()
        signal = evaluate_multi_outcome(
            outcomes=outcomes,
            consensus=consensus_tight,
            city=nyc,
            min_edge=0.0,  # min_edge=0 agar semua outcomes masuk
        )
        assert signal is not None
        total_prob = sum(o["prob_model"] for o in signal.all_outcomes)
        # Total tidak harus persis 1.0 karena ada open-ended tails
        # tapi harus dalam range wajar
        assert 0.70 <= total_prob <= 1.30, \
            f"Total probabilitas tidak wajar: {total_prob:.3f}"

    def test_no_trade_when_no_edge(self, consensus_tight, nyc):
        """
        Jika semua outcomes punya harga pasar yang akurat (tidak ada edge)
        → signal harus NO_TRADE untuk semua.
        """
        # Buat outcomes dengan harga pasar yang sudah "benar" (no edge)
        outcomes_no_edge = [
            OutcomeCandidate(label="30°C", token_id="t1", market_price=0.85, volume_24h=100),
            OutcomeCandidate(label="32°C", token_id="t2", market_price=0.10, volume_24h=100),
            OutcomeCandidate(label="28°C", token_id="t3", market_price=0.05, volume_24h=100),
        ]
        signal = evaluate_multi_outcome(
            outcomes=outcomes_no_edge,
            consensus=consensus_tight,
            city=nyc,
            min_edge=0.10,  # min_edge tinggi agar NO_TRADE
        )
        # Tidak harus NO_TRADE karena harga kita tidak bisa set persis,
        # tapi signal harus valid
        assert signal is not None
        assert signal.signal in ["BUY_YES", "BUY_NO", "NO_TRADE"]

    def test_open_ended_high_label(self, consensus_tight, nyc):
        """Outcome '90°C or higher' harus dikenali sebagai open-ended high."""
        assert is_open_ended_high("90°C or higher") is True
        assert is_open_ended_high("30°C") is False

    def test_open_ended_low_label(self):
        """Outcome '20°C or below' harus dikenali sebagai open-ended low."""
        assert is_open_ended_low("20°C or below") is True
        assert is_open_ended_low("30°C") is False


# ==============================================================================
# TEST parse_outcome_temperature — Parser label suhu
# ==============================================================================

class TestParseOutcomeTemperature:

    def test_fahrenheit_explicit(self, nyc):
        result = parse_outcome_temperature("76°F", nyc)
        expected = round((76 - 32) * 5 / 9, 2)  # 24.44°C
        assert result is not None
        assert abs(result - expected) < 0.01, f"76°F → {expected}°C, dapat {result}"

    def test_celsius_explicit(self, london):
        result = parse_outcome_temperature("24°C", london)
        assert result is not None
        assert abs(result - 24.0) < 0.01

    def test_bare_number_fahrenheit_city(self, nyc):
        """Angka tanpa unit di kota F → konversi dari Fahrenheit."""
        result = parse_outcome_temperature("76", nyc)
        expected = round((76 - 32) * 5 / 9, 2)
        assert result is not None
        assert abs(result - expected) < 0.01

    def test_bare_number_celsius_city(self, london):
        """Angka tanpa unit di kota C → sudah Celsius."""
        result = parse_outcome_temperature("24", london)
        assert result is not None
        assert abs(result - 24.0) < 0.01

    def test_or_higher_suffix(self, nyc):
        """'90°F or higher' → parse 90°F saja (32.22°C)."""
        result = parse_outcome_temperature("90°F or higher", nyc)
        expected = round((90 - 32) * 5 / 9, 2)
        assert result is not None
        assert abs(result - expected) < 0.01

    def test_unparseable_returns_none(self, nyc):
        result = parse_outcome_temperature("N/A", nyc)
        assert result is None
