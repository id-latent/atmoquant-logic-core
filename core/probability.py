# ==============================================================================
# probability.py — Multi-Outcome Probability Evaluator
# ==============================================================================
"""
AQL Probability Engine

Perubahan dari sebelumnya :
  - Tambah MULTI_OUTCOME evaluator (11 outcomes per event)
  - Tetap support BINARY_ABOVE, BINARY_BELOW, BINARY_RANGE
  - Return BEST outcome (edge terbesar) dari semua candidates
  - Unit-aware: otomatis konversi F/C berdasarkan city registry
  - Robust parser tetap ada untuk binary markets

Market Types:
  MULTI_OUTCOME : "Highest temperature in NYC?" → 11 outcomes
  BINARY_ABOVE  : "Will NYC exceed 90°F?"
  BINARY_BELOW  : "Will NYC stay below 32°F?"
  BINARY_RANGE  : "Will NYC be 56-57°F?"
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from scipy.stats import norm

from config.settings import settings
from core.consensus import ConsensusResult
from core.location_registry import CityInfo, to_celsius

log = logging.getLogger("aql.probability")

# ── Uncertainty Constants ─────────────────────────────────────────────────────
BASE_FORECAST_STD_C = 1.5
VARIANCE_WEIGHT     = 0.5


# ── Market Type ───────────────────────────────────────────────────────────────

class MarketType(str, Enum):
    MULTI_OUTCOME = "MULTI_OUTCOME"
    BINARY_ABOVE  = "BINARY_ABOVE"
    BINARY_BELOW  = "BINARY_BELOW"
    BINARY_RANGE  = "BINARY_RANGE"


# ── Outcome Candidate ─────────────────────────────────────────────────────────

@dataclass
class OutcomeCandidate:
    """Satu outcome dari multi-outcome event atau binary market."""
    label: str           # "76°F" atau "13°C" atau "YES"
    token_id: str        # YES token ID untuk order
    market_price: float  # Harga pasar saat ini (implied probability)
    volume_24h: float    # Volume 24 jam (untuk volume analysis)


# ── Probability Signal ────────────────────────────────────────────────────────

@dataclass
class ProbabilitySignal:
    market_type: str
    direction: str           # "ABOVE" / "BELOW" / "RANGE" / "MULTI"

    # Best outcome yang dipilih
    best_outcome_label: str
    best_token_id: str
    best_market_price: float
    best_prob_model: float   # P dari model
    best_edge: float         # Raw edge
    best_net_edge: float     # Edge setelah fee
    signal: str              # "BUY_YES" / "BUY_NO" / "NO_TRADE"

    # Data lengkap semua outcomes (untuk Discord embed)
    all_outcomes: list[dict]

    # Model data
    model_mean_c: float
    model_std_c: float
    threshold_c: float       # Threshold utama (midpoint untuk range)
    threshold_low_c: float
    threshold_high_c: float

    # Forecast outcome yang paling mungkin
    forecast_outcome: str    # Label outcome yang paling sesuai forecast


# ── Outcome Parser untuk Multi-Outcome ───────────────────────────────────────

def parse_outcome_temperature(
    label: str,
    city: CityInfo,
) -> Optional[float]:
    """
    Parse angka suhu dari label outcome Polymarket.

    Supported formats:
      "76°F"        → 24.44°C
      "13°C"        → 13.0°C
      "76"          → pakai city.unit default
      "76°F or higher" → 76°F (batas bawah)
      "60°F or below"  → 60°F (batas atas)
      "26°C or below"  → 26°C
    """
    label_upper = label.upper()

    # Explicit °F
    m = re.search(r"(\d+\.?\d*)\s*°?\s*F\b", label, re.IGNORECASE)
    if m:
        return to_celsius(float(m.group(1)), "F")

    # Explicit °C
    m = re.search(r"(\d+\.?\d*)\s*°?\s*C\b", label, re.IGNORECASE)
    if m:
        return float(m.group(1))

    # Bare number — pakai city unit
    m = re.search(r"(\d+\.?\d*)", label)
    if m:
        val = float(m.group(1))
        return to_celsius(val, city.unit)

    return None


def is_open_ended_high(label: str) -> bool:
    """
    True jika label adalah open-ended upper bound.
    Contoh: "80°F or higher", "18°C or higher", "90+"
    """
    label_upper = label.upper()
    return any(kw in label_upper for kw in [
        "OR HIGHER", "OR MORE", "AND ABOVE",
        "AND HIGHER", "+", "≥", "ABOVE",
    ])


def is_open_ended_low(label: str) -> bool:
    """
    True jika label adalah open-ended lower bound.
    Contoh: "40°F or below", "5°C or lower"
    """
    label_upper = label.upper()
    return any(kw in label_upper for kw in [
        "OR BELOW", "OR LOWER", "AND BELOW",
        "OR LESS", "≤", "BELOW",
    ])


# ── Multi-Outcome Evaluator ───────────────────────────────────────────────────

def evaluate_multi_outcome(
    outcomes: list[OutcomeCandidate],
    consensus: ConsensusResult,
    city: CityInfo,
    min_edge: float,
) -> Optional[ProbabilitySignal]:
    """
    Evaluasi semua outcomes dari multi-outcome event.
    Return signal untuk outcome dengan edge terbesar.

    Untuk setiap outcome (misal "76°F"):
      P(outcome) = P(temp dalam range outcome tersebut)
                 = norm.cdf(upper_bound) - norm.cdf(lower_bound)

    Untuk open-ended high (misal "90°F or higher"):
      P = norm.sf(lower_bound)  ← survival function

    Untuk open-ended low (misal "40°F or below"):
      P = norm.cdf(upper_bound)
    """
    model_mean = consensus.consensus_t_max  # Pakai t_max untuk "highest temp"
    model_std  = (
        BASE_FORECAST_STD_C
        + VARIANCE_WEIGHT * consensus.inter_model_variance
    )

    # Sort outcomes by temperature untuk menentukan range boundaries
    parsed_outcomes = []
    for outcome in outcomes:
        temp_c = parse_outcome_temperature(outcome.label, city)
        if temp_c is None:
            continue
        parsed_outcomes.append({
            "outcome":    outcome,
            "temp_c":     temp_c,
            "is_high":    is_open_ended_high(outcome.label),
            "is_low":     is_open_ended_low(outcome.label),
        })

    if not parsed_outcomes:
        log.warning("[Probability] Tidak ada outcome yang bisa diparsing")
        return None

    # Sort by temperature ascending
    parsed_outcomes.sort(key=lambda x: x["temp_c"])

    # Hitung P(outcome) untuk setiap outcome
    evaluated = []
    for i, item in enumerate(parsed_outcomes):
        temp_c   = item["temp_c"]
        outcome  = item["outcome"]

        if item["is_high"]:
            # "90°F or higher" → P(temp ≥ 90°F)
            prob_model = float(norm.sf(temp_c, loc=model_mean, scale=model_std))

        elif item["is_low"]:
            # "40°F or below" → P(temp ≤ 40°F)
            prob_model = float(norm.cdf(temp_c, loc=model_mean, scale=model_std))

        else:
            # Discrete value "76°F" → P(75.5°F ≤ temp < 76.5°F)
            # Boundary = midpoint antara nilai ini dan nilai tetangga
            if i == 0:
                lower = temp_c - 1.0  # Asumsi step 1 unit
            else:
                lower = (temp_c + parsed_outcomes[i-1]["temp_c"]) / 2

            if i == len(parsed_outcomes) - 1:
                upper = temp_c + 1.0
            else:
                upper = (temp_c + parsed_outcomes[i+1]["temp_c"]) / 2

            prob_model = float(
                norm.cdf(upper, loc=model_mean, scale=model_std)
                - norm.cdf(lower, loc=model_mean, scale=model_std)
            )

        prob_model = round(min(max(prob_model, 0.001), 0.999), 4)
        edge       = prob_model - outcome.market_price
        net_edge   = round(abs(edge) - settings.TRADING_FEE_PCT, 4)

        evaluated.append({
            "label":        outcome.label,
            "token_id":     outcome.token_id,
            "temp_c":       temp_c,
            "prob_model":   prob_model,
            "market_price": outcome.market_price,
            "edge":         round(edge, 4),
            "net_edge":     net_edge,
            "signal":       "BUY_YES" if edge > 0 else "BUY_NO",
            "volume_24h":   outcome.volume_24h,
        })

    # Cari outcome dengan net_edge terbesar
    best = max(evaluated, key=lambda x: x["net_edge"])

    # Forecast outcome = label dengan prob_model tertinggi
    forecast = max(evaluated, key=lambda x: x["prob_model"])

    if best["net_edge"] < min_edge:
        log.info(
            "[Probability] Best net_edge %.2f%% < min %.2f%% — NO_TRADE",
            best["net_edge"] * 100, min_edge * 100,
        )
        signal_str = "NO_TRADE"
    else:
        signal_str = best["signal"]

    log.info(
        "[Probability] MULTI | best=%s | model=%.3f mkt=%.3f "
        "edge=%.2f%% net=%.2f%% | %s",
        best["label"],
        best["prob_model"],
        best["market_price"],
        best["edge"] * 100,
        best["net_edge"] * 100,
        signal_str,
    )

    return ProbabilitySignal(
        market_type=MarketType.MULTI_OUTCOME,
        direction="MULTI",
        best_outcome_label=best["label"],
        best_token_id=best["token_id"],
        best_market_price=best["market_price"],
        best_prob_model=best["prob_model"],
        best_edge=best["edge"],
        best_net_edge=best["net_edge"],
        signal=signal_str,
        all_outcomes=evaluated,
        model_mean_c=round(model_mean, 2),
        model_std_c=round(model_std, 3),
        threshold_c=round(forecast["temp_c"], 2),
        threshold_low_c=round(forecast["temp_c"], 2),
        threshold_high_c=round(forecast["temp_c"], 2),
        forecast_outcome=forecast["label"],
    )


# ── Binary Market Parser & Evaluator ─────────────────────────────────────────

# Unparseable patterns
UNPARSEABLE_PATTERNS = [
    r"record\s+(high|low|temperature)",
    r"all[\s-]time",
    r"ever\s+recorded",
    r"historic(al)?",
    r"hottest\s+day",
    r"coldest\s+day",
]

ABOVE_KEYWORDS = [
    "EXCEED", "ABOVE", "OVER", "MORE THAN",
    "AT LEAST", "REACH OR EXCEED", "SURPASS", "BREAK",
]
BELOW_KEYWORDS = [
    "BELOW", "UNDER", "LESS THAN", "NOT REACH",
    "STAY UNDER", "FAIL TO REACH", "DROP BELOW",
    "NOT EXCEED",
]


def _is_unparseable(question: str) -> bool:
    q_lower = question.lower()
    for pattern in UNPARSEABLE_PATTERNS:
        if re.search(pattern, q_lower):
            return True
    return False


def _extract_direction(question: str) -> Optional[str]:
    q_upper = question.upper()
    if any(kw in q_upper for kw in BELOW_KEYWORDS):
        return "BELOW"
    if any(kw in q_upper for kw in ABOVE_KEYWORDS):
        return "ABOVE"
    return None


def _extract_range(question: str) -> Optional[tuple[float, float]]:
    """Extract range (low_c, high_c) dari pertanyaan."""
    # "56-57°F" atau "56 - 57°F"
    m = re.search(
        r"(\d+\.?\d*)\s*[-–]\s*(\d+\.?\d*)\s*°?\s*[Ff]\b",
        question,
    )
    if m:
        low  = to_celsius(float(m.group(1)), "F")
        high = to_celsius(float(m.group(2)), "F")
        return low, high

    m = re.search(
        r"(\d+\.?\d*)\s*[-–]\s*(\d+\.?\d*)\s*°?\s*[Cc]\b",
        question,
    )
    if m:
        return float(m.group(1)), float(m.group(2))

    # "between X and Y°F"
    m = re.search(
        r"between\s+(\d+\.?\d*)\s+and\s+(\d+\.?\d*)\s*°?\s*[Ff]\b",
        question, re.IGNORECASE,
    )
    if m:
        low  = to_celsius(float(m.group(1)), "F")
        high = to_celsius(float(m.group(2)), "F")
        return low, high

    m = re.search(
        r"between\s+(\d+\.?\d*)\s+and\s+(\d+\.?\d*)\s*°?\s*[Cc]\b",
        question, re.IGNORECASE,
    )
    if m:
        return float(m.group(1)), float(m.group(2))

    return None


def _extract_single(question: str, city: CityInfo) -> Optional[float]:
    """Extract single threshold dalam Celsius."""
    # Explicit °F
    for pattern in [
        r"(\d+\.?\d*)\s*°\s*[Ff]\b",
        r"(\d+\.?\d*)\s*[Ff]\b(?!\w)",
        r"(\d+\.?\d*)\s+degrees?\s+[Ff]\b",
        r"(\d+\.?\d*)\s+degrees?\s+fahrenheit",
    ]:
        m = re.search(pattern, question, re.IGNORECASE)
        if m:
            return to_celsius(float(m.group(1)), "F")

    # Explicit °C
    for pattern in [
        r"(\d+\.?\d*)\s*°\s*[Cc]\b",
        r"(\d+\.?\d*)\s+degrees?\s+celsius",
        r"(\d+\.?\d*)\s+degrees?\s+[Cc]\b",
    ]:
        m = re.search(pattern, question, re.IGNORECASE)
        if m:
            return round(float(m.group(1)), 2)

    # Bare integer heuristic
    bare = re.findall(r"\b(\d{2,3})\b", question)
    candidates = [
        float(v) for v in bare
        if not re.match(r"^(19|20)\d{2}$", v)
    ]
    if len(candidates) == 1:
        val = candidates[0]
        if city.unit == "F" and 32 <= val <= 130:
            return to_celsius(val, "F")
        if city.unit == "C" and -20 <= val <= 50:
            return val

    return None


def evaluate_binary(
    question: str,
    yes_token_id: str,
    no_token_id: str,
    market_price: float,
    consensus: ConsensusResult,
    city: CityInfo,
    min_edge: float,
    volume_24h: float = 0.0,
) -> Optional[ProbabilitySignal]:
    """
    Evaluasi binary market (YES/NO).
    Handles: ABOVE, BELOW, RANGE.
    """
    if _is_unparseable(question):
        log.info("[Probability] Skip unparseable: %s", question[:80])
        return None

    if not (0.01 <= market_price <= 0.99):
        return None

    model_mean = consensus.consensus_t_max
    model_std  = (
        BASE_FORECAST_STD_C
        + VARIANCE_WEIGHT * consensus.inter_model_variance
    )

    # ── Cek range dulu ─────────────────────────────────────────────────────
    range_result = _extract_range(question)
    if range_result is not None:
        low_c, high_c   = range_result
        midpoint        = round((low_c + high_c) / 2, 2)

        prob_yes = float(
            norm.cdf(high_c, loc=model_mean, scale=model_std)
            - norm.cdf(low_c, loc=model_mean, scale=model_std)
        )
        prob_yes = round(min(max(prob_yes, 0.01), 0.99), 4)

        edge     = prob_yes - market_price
        net_edge = round(abs(edge) - settings.TRADING_FEE_PCT, 4)

        if net_edge < min_edge:
            signal_str = "NO_TRADE"
        elif edge > 0:
            signal_str = "BUY_YES"
        else:
            signal_str = "BUY_NO"

        token_id = yes_token_id if signal_str == "BUY_YES" else no_token_id

        return ProbabilitySignal(
            market_type=MarketType.BINARY_RANGE,
            direction="RANGE",
            best_outcome_label=f"{low_c:.1f}–{high_c:.1f}°C",
            best_token_id=token_id,
            best_market_price=market_price,
            best_prob_model=prob_yes,
            best_edge=round(edge, 4),
            best_net_edge=net_edge,
            signal=signal_str,
            all_outcomes=[{
                "label":        "YES",
                "prob_model":   prob_yes,
                "market_price": market_price,
                "edge":         round(edge, 4),
                "net_edge":     net_edge,
            }],
            model_mean_c=round(model_mean, 2),
            model_std_c=round(model_std, 3),
            threshold_c=midpoint,
            threshold_low_c=low_c,
            threshold_high_c=high_c,
            forecast_outcome="YES" if prob_yes > 0.5 else "NO",
        )

    # ── Single threshold ────────────────────────────────────────────────────
    direction   = _extract_direction(question)
    threshold_c = _extract_single(question, city)

    if direction is None or threshold_c is None:
        log.warning("[Probability] Cannot parse binary: %s", question[:80])
        return None

    if direction == "ABOVE":
        prob_yes = float(
            norm.sf(threshold_c, loc=model_mean, scale=model_std)
        )
        mtype = MarketType.BINARY_ABOVE
    else:
        prob_yes = float(
            norm.cdf(threshold_c, loc=model_mean, scale=model_std)
        )
        mtype = MarketType.BINARY_BELOW

    prob_yes = round(min(max(prob_yes, 0.01), 0.99), 4)
    edge     = prob_yes - market_price
    net_edge = round(abs(edge) - settings.TRADING_FEE_PCT, 4)

    if net_edge < min_edge:
        signal_str = "NO_TRADE"
    elif edge > 0:
        signal_str = "BUY_YES"
    else:
        signal_str = "BUY_NO"

    token_id = yes_token_id if signal_str == "BUY_YES" else no_token_id

    log.info(
        "[Probability] %s | threshold=%.2f°C | "
        "model=%.3f mkt=%.3f edge=%.2f%% | %s",
        mtype.value, threshold_c,
        prob_yes, market_price,
        net_edge * 100, signal_str,
    )

    return ProbabilitySignal(
        market_type=mtype,
        direction=direction,
        best_outcome_label="YES" if signal_str == "BUY_YES" else "NO",
        best_token_id=token_id,
        best_market_price=market_price,
        best_prob_model=prob_yes,
        best_edge=round(edge, 4),
        best_net_edge=net_edge,
        signal=signal_str,
        all_outcomes=[{
            "label":        "YES",
            "prob_model":   prob_yes,
            "market_price": market_price,
            "edge":         round(edge, 4),
            "net_edge":     net_edge,
        }],
        model_mean_c=round(model_mean, 2),
        model_std_c=round(model_std, 3),
        threshold_c=threshold_c,
        threshold_low_c=threshold_c,
        threshold_high_c=threshold_c,
        forecast_outcome="YES" if prob_yes > 0.5 else "NO",
    )
