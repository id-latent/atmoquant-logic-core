# ==============================================================================
# probability.py — Multi-Outcome Probability Evaluator (FIXED)
# ==============================================================================
"""
AQL Probability Engine

Fixes:
  BUG #5 : net_edge dihitung dari abs(edge) — sekarang dihitung per arah
           (BUY_YES: edge = prob - price, BUY_NO: edge = (1-prob) - (1-price))
  BUG #7 : evaluate_binary menggunakan consensus_t_max untuk semua market type.
           Sekarang:
             BINARY_ABOVE / MULTI_OUTCOME → t_max  (suhu tertinggi hari itu)
             BINARY_BELOW                 → t_min  (suhu terendah hari itu)
             BINARY_RANGE                 → t_mean (suhu rata-rata hari itu)
  BUG #12: Hapus dead code label_upper yang di-assign tapi tidak dipakai.
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
    label: str
    token_id: str
    market_price: float
    volume_24h: float


# ── Probability Signal ────────────────────────────────────────────────────────

@dataclass
class ProbabilitySignal:
    market_type: str
    direction: str

    best_outcome_label: str
    best_token_id: str
    best_market_price: float
    best_prob_model: float
    best_edge: float
    best_net_edge: float
    signal: str              # "BUY_YES" / "BUY_NO" / "NO_TRADE"

    all_outcomes: list[dict]

    model_mean_c: float
    model_std_c: float
    threshold_c: float
    threshold_low_c: float
    threshold_high_c: float

    forecast_outcome: str


# ── Outcome Parser untuk Multi-Outcome ───────────────────────────────────────

def parse_outcome_temperature(
    label: str,
    city: CityInfo,
) -> Optional[float]:
    """
    Parse angka suhu dari label outcome Polymarket.
    FIX BUG #12: Hapus label_upper yang tidak dipakai.
    """
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
    """True jika label adalah open-ended upper bound."""
    upper = label.upper()
    return any(kw in upper for kw in [
        "OR HIGHER", "OR MORE", "AND ABOVE",
        "AND HIGHER", "+", "≥", "ABOVE",
    ])


def is_open_ended_low(label: str) -> bool:
    """True jika label adalah open-ended lower bound."""
    upper = label.upper()
    return any(kw in upper for kw in [
        "OR BELOW", "OR LOWER", "AND BELOW",
        "OR LESS", "≤", "BELOW",
    ])


# ── Helper: net_edge per arah ─────────────────────────────────────────────────
# FIX BUG #5: Tidak lagi memakai abs(edge).
# BUY_YES: kita mau beli YES → edge = prob_model - market_price
#           net = edge - fee  (harus > 0 dan > min_edge)
# BUY_NO:  kita mau beli NO  → implied_prob_no = 1 - prob_model
#                               implied_price_no = 1 - market_price
#                               edge_no = implied_prob_no - implied_price_no
#           net = edge_no - fee

def _compute_edge_and_signal(
    prob_model: float,
    market_price: float,
    min_edge: float,
) -> tuple[float, float, str]:
    """
    Hitung raw edge, net edge, dan signal untuk satu outcome.

    Returns:
        (best_edge, best_net_edge, signal_str)
        signal_str: "BUY_YES" / "BUY_NO" / "NO_TRADE"
    """
    edge_yes = prob_model - market_price
    edge_no  = (1.0 - prob_model) - (1.0 - market_price)  # = -(edge_yes)

    net_yes  = round(edge_yes - settings.TRADING_FEE_PCT, 4)
    net_no   = round(edge_no  - settings.TRADING_FEE_PCT, 4)

    # Pilih sisi terbaik
    if net_yes >= net_no and net_yes >= min_edge:
        return round(edge_yes, 4), net_yes, "BUY_YES"
    elif net_no > net_yes and net_no >= min_edge:
        return round(edge_no, 4), net_no, "BUY_NO"
    else:
        # Ambil sisi dengan net_edge lebih besar untuk logging, tapi NO_TRADE
        if net_yes >= net_no:
            return round(edge_yes, 4), net_yes, "NO_TRADE"
        else:
            return round(edge_no, 4), net_no, "NO_TRADE"


# ── Multi-Outcome Evaluator ───────────────────────────────────────────────────

def evaluate_multi_outcome(
    outcomes: list[OutcomeCandidate],
    consensus: ConsensusResult,
    city: CityInfo,
    min_edge: float,
) -> Optional[ProbabilitySignal]:
    """
    Evaluasi semua outcomes dari multi-outcome event.
    FIX BUG #7: Pakai t_max (suhu tertinggi) untuk MULTI_OUTCOME.
    FIX BUG #5: net_edge dihitung per arah, tidak abs().
    """
    # MULTI_OUTCOME biasanya "Highest temperature in NYC?" → pakai t_max
    model_mean = consensus.consensus_t_max
    model_std  = (
        BASE_FORECAST_STD_C
        + VARIANCE_WEIGHT * consensus.inter_model_variance
    )

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

    parsed_outcomes.sort(key=lambda x: x["temp_c"])

    evaluated = []
    for i, item in enumerate(parsed_outcomes):
        temp_c   = item["temp_c"]
        outcome  = item["outcome"]

        if item["is_high"]:
            prob_model = float(norm.sf(temp_c, loc=model_mean, scale=model_std))
        elif item["is_low"]:
            prob_model = float(norm.cdf(temp_c, loc=model_mean, scale=model_std))
        else:
            if i == 0:
                lower = temp_c - 1.0
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

        # FIX BUG #5: gunakan _compute_edge_and_signal
        edge, net_edge, signal_str = _compute_edge_and_signal(
            prob_model, outcome.market_price, min_edge
        )

        evaluated.append({
            "label":        outcome.label,
            "token_id":     outcome.token_id,
            "temp_c":       temp_c,
            "prob_model":   prob_model,
            "market_price": outcome.market_price,
            "edge":         edge,
            "net_edge":     net_edge,
            "signal":       signal_str,
            "volume_24h":   outcome.volume_24h,
        })

    # Cari outcome dengan net_edge terbesar (termasuk NO_TRADE sebagai fallback)
    tradeable = [e for e in evaluated if e["signal"] != "NO_TRADE"]
    if not tradeable:
        best = max(evaluated, key=lambda x: x["net_edge"])
        signal_str = "NO_TRADE"
        log.info(
            "[Probability] MULTI | best net_edge %.2f%% < min %.2f%% — NO_TRADE",
            best["net_edge"] * 100, min_edge * 100,
        )
    else:
        best       = max(tradeable, key=lambda x: x["net_edge"])
        signal_str = best["signal"]

    forecast = max(evaluated, key=lambda x: x["prob_model"])

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

UNPARSEABLE_PATTERNS = [
    r"record\s+(high|low|temperature)",
    r"all[\s-]time",
    r"ever\s+recorded",
    r"historic(al)?",
    r"hottest\s+day",
    r"coldest\s+day",
]

# FIX BUG #10: Susun keyword lebih ketat — BELOW dicek dulu untuk menghindari
# false positive pada kata seperti "NOT REACH", "NOT EXCEED"
BELOW_KEYWORDS = [
    "NOT EXCEED", "NOT REACH", "STAY BELOW", "STAY UNDER",
    "BELOW", "UNDER ", "LESS THAN", "FAIL TO REACH",
    "DROP BELOW", "OR LOWER", "OR LESS",
]
ABOVE_KEYWORDS = [
    "EXCEED", "ABOVE", "OVER ", "MORE THAN",
    "AT LEAST", "REACH OR EXCEED", "SURPASS", "BREAK",
    "OR HIGHER", "OR MORE",
]


def _is_unparseable(question: str) -> bool:
    q_lower = question.lower()
    return any(re.search(p, q_lower) for p in UNPARSEABLE_PATTERNS)


def _extract_direction(question: str) -> Optional[str]:
    q_upper = question.upper()
    # BELOW dicek lebih dulu — keyword negatif lebih spesifik
    if any(kw in q_upper for kw in BELOW_KEYWORDS):
        return "BELOW"
    if any(kw in q_upper for kw in ABOVE_KEYWORDS):
        return "ABOVE"
    return None


def _extract_range(question: str) -> Optional[tuple[float, float]]:
    m = re.search(
        r"(\d+\.?\d*)\s*[-–]\s*(\d+\.?\d*)\s*°?\s*[Ff]\b",
        question,
    )
    if m:
        return to_celsius(float(m.group(1)), "F"), to_celsius(float(m.group(2)), "F")

    m = re.search(
        r"(\d+\.?\d*)\s*[-–]\s*(\d+\.?\d*)\s*°?\s*[Cc]\b",
        question,
    )
    if m:
        return float(m.group(1)), float(m.group(2))

    m = re.search(
        r"between\s+(\d+\.?\d*)\s+and\s+(\d+\.?\d*)\s*°?\s*[Ff]\b",
        question, re.IGNORECASE,
    )
    if m:
        return to_celsius(float(m.group(1)), "F"), to_celsius(float(m.group(2)), "F")

    m = re.search(
        r"between\s+(\d+\.?\d*)\s+and\s+(\d+\.?\d*)\s*°?\s*[Cc]\b",
        question, re.IGNORECASE,
    )
    if m:
        return float(m.group(1)), float(m.group(2))

    return None


def _extract_single(question: str, city: CityInfo) -> Optional[float]:
    for pattern in [
        r"(\d+\.?\d*)\s*°\s*[Ff]\b",
        r"(\d+\.?\d*)\s*[Ff]\b(?!\w)",
        r"(\d+\.?\d*)\s+degrees?\s+[Ff]\b",
        r"(\d+\.?\d*)\s+degrees?\s+fahrenheit",
    ]:
        m = re.search(pattern, question, re.IGNORECASE)
        if m:
            return to_celsius(float(m.group(1)), "F")

    for pattern in [
        r"(\d+\.?\d*)\s*°\s*[Cc]\b",
        r"(\d+\.?\d*)\s+degrees?\s+celsius",
        r"(\d+\.?\d*)\s+degrees?\s+[Cc]\b",
    ]:
        m = re.search(pattern, question, re.IGNORECASE)
        if m:
            return round(float(m.group(1)), 2)

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

    FIX BUG #5: net_edge dihitung per arah via _compute_edge_and_signal.
    FIX BUG #7: Pilih model_mean berdasarkan market type:
      ABOVE / tidak diketahui → t_max
      BELOW                   → t_min
      RANGE                   → t_mean
    """
    if _is_unparseable(question):
        log.info("[Probability] Skip unparseable: %s", question[:80])
        return None

    if not (0.01 <= market_price <= 0.99):
        return None

    model_std = (
        BASE_FORECAST_STD_C
        + VARIANCE_WEIGHT * consensus.inter_model_variance
    )

    # ── Cek range dulu ─────────────────────────────────────────────────────
    range_result = _extract_range(question)
    if range_result is not None:
        low_c, high_c = range_result
        midpoint      = round((low_c + high_c) / 2, 2)

        # FIX BUG #7: RANGE → pakai t_mean
        model_mean = consensus.consensus_t_mean

        prob_yes = float(
            norm.cdf(high_c, loc=model_mean, scale=model_std)
            - norm.cdf(low_c, loc=model_mean, scale=model_std)
        )
        prob_yes = round(min(max(prob_yes, 0.01), 0.99), 4)

        # FIX BUG #5: gunakan _compute_edge_and_signal
        edge, net_edge, signal_str = _compute_edge_and_signal(
            prob_yes, market_price, min_edge
        )

        # FIX BUG #4 (partial): untuk NO signal, gunakan no_token_id
        if signal_str == "BUY_YES":
            token_id = yes_token_id
        elif signal_str == "BUY_NO":
            token_id = no_token_id
        else:
            token_id = yes_token_id  # NO_TRADE — tidak dipakai

        return ProbabilitySignal(
            market_type=MarketType.BINARY_RANGE,
            direction="RANGE",
            best_outcome_label=f"{low_c:.1f}–{high_c:.1f}°C",
            best_token_id=token_id,
            best_market_price=market_price,
            best_prob_model=prob_yes,
            best_edge=edge,
            best_net_edge=net_edge,
            signal=signal_str,
            all_outcomes=[{
                "label":        "YES",
                "prob_model":   prob_yes,
                "market_price": market_price,
                "edge":         edge,
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
        # FIX BUG #7: ABOVE → t_max
        model_mean = consensus.consensus_t_max
        prob_yes   = float(norm.sf(threshold_c, loc=model_mean, scale=model_std))
        mtype      = MarketType.BINARY_ABOVE
    else:  # BELOW
        # FIX BUG #7: BELOW → t_min
        model_mean = consensus.consensus_t_min
        prob_yes   = float(norm.cdf(threshold_c, loc=model_mean, scale=model_std))
        mtype      = MarketType.BINARY_BELOW

    prob_yes = round(min(max(prob_yes, 0.01), 0.99), 4)

    # FIX BUG #5: gunakan _compute_edge_and_signal
    edge, net_edge, signal_str = _compute_edge_and_signal(
        prob_yes, market_price, min_edge
    )

    # FIX BUG #4 (partial): gunakan token yang benar per arah
    if signal_str == "BUY_YES":
        token_id = yes_token_id
    elif signal_str == "BUY_NO":
        token_id = no_token_id
    else:
        token_id = yes_token_id

    log.info(
        "[Probability] %s | threshold=%.2f°C | "
        "model_mean=%.2f°C | model=%.3f mkt=%.3f edge=%.2f%% | %s",
        mtype.value, threshold_c, model_mean,
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
        best_edge=edge,
        best_net_edge=net_edge,
        signal=signal_str,
        all_outcomes=[{
            "label":        "YES",
            "prob_model":   prob_yes,
            "market_price": market_price,
            "edge":         edge,
            "net_edge":     net_edge,
        }],
        model_mean_c=round(model_mean, 2),
        model_std_c=round(model_std, 3),
        threshold_c=threshold_c,
        threshold_low_c=threshold_c,
        threshold_high_c=threshold_c,
        forecast_outcome="YES" if prob_yes > 0.5 else "NO",
    )
