# ==============================================================================
# probability.py = Normal CDF Probability Mapper + Robust Question Parser
# ==============================================================================
"""
AQL Probability Mapper
Converts Triple-Lock ConsensusResult into binary P(YES) estimate
via Normal CDF, then computes net edge vs Polymarket implied price.

Perbaikan dari versi sebelumnya:
- Parser lebih robust: prioritas °F explicit > °C explicit > konteks kalimat
- Guard terhadap ambiguous bare numbers
- Deteksi pattern "record high/low" yang tidak punya threshold angka
- Log lebih informatif untuk debugging
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from scipy.stats import norm

from core.consensus import ConsensusResult

log = logging.getLogger("aql.probability")

# ── Uncertainty Model ─────────────────────────────────────────────────────────
BASE_FORECAST_STD_C = 1.5   # Baseline NWP uncertainty (°C)
VARIANCE_WEIGHT     = 0.5   # Variance-to-σ inflation factor


# ── Signal Dataclass ──────────────────────────────────────────────────────────

@dataclass
class ProbabilitySignal:
    market_question: str
    direction: str       # "ABOVE" | "BELOW"
    threshold_c: float

    model_mean_c: float
    model_std_c: float

    prob_yes: float      # Model-derived P(YES)
    market_price: float  # Polymarket implied P(YES)
    edge: float          # prob_yes − market_price (pre-friction)
    net_edge: float      # |edge| − trading_fee (actionable edge)

    signal: str          # "BUY_YES" | "BUY_NO" | "NO_TRADE"


# ── Question Parser ───────────────────────────────────────────────────────────

# Pattern yang TIDAK bisa di-parse dengan aman — skip market ini
UNPARSEABLE_PATTERNS = [
    r"record\s+(high|low|temperature)",   # "record high" — tidak ada threshold angka
    r"all[\s-]time",                      # "all-time high"
    r"ever\s+recorded",
    r"historic(al)?",
    r"hottest\s+day",
    r"coldest\s+day",
]

# Direction keywords
ABOVE_KEYWORDS = [
    "EXCEED", "ABOVE", "OVER", "MORE THAN",
    "AT LEAST", "REACH OR EXCEED", "HIT OR EXCEED",
    "TOP", "SURPASS", "BREAK",
]
BELOW_KEYWORDS = [
    "BELOW", "UNDER", "LESS THAN", "NOT REACH",
    "STAY UNDER", "FAIL TO REACH", "DROP BELOW",
    "NOT EXCEED", "NOT TOP",
]


def _is_unparseable(question: str) -> bool:
    """
    Deteksi pertanyaan yang tidak punya threshold angka yang jelas.
    Contoh: "Will Chicago set a record high temperature?"
    → Tidak ada angka → tidak bisa di-parse → skip.
    """
    q_lower = question.lower()
    for pattern in UNPARSEABLE_PATTERNS:
        if re.search(pattern, q_lower):
            log.info(
                "[Parser] Skip — unparseable pattern '%s': %s",
                pattern, question[:80]
            )
            return True
    return False


def _extract_direction(question: str) -> Optional[str]:
    """Extract arah dari teks pertanyaan. Returns 'ABOVE', 'BELOW', atau None."""
    q_upper = question.upper()

    # Cek BELOW dulu (lebih spesifik, hindari false positive)
    if any(kw in q_upper for kw in BELOW_KEYWORDS):
        return "BELOW"

    if any(kw in q_upper for kw in ABOVE_KEYWORDS):
        return "ABOVE"

    return None


def _extract_threshold(question: str) -> Optional[float]:
    """
    Extract threshold suhu dalam Celsius dari teks pertanyaan.

    Prioritas:
    1. Angka dengan °F explicit  → konversi ke °C
    2. Angka dengan °C explicit  → langsung pakai
    3. Bare integer di range wajar (hanya jika tidak ambigu)

    Returns threshold dalam °C atau None jika tidak bisa diparsing.
    """

    # ── Prioritas 1: Fahrenheit explicit ─────────────────────────────────────
    # Match: "90°F", "90 °F", "90F", "90 F", "90 degrees F"
    f_patterns = [
        r"(\d+\.?\d*)\s*°\s*[Ff]\b",
        r"(\d+\.?\d*)\s*[Ff]\b(?!\w)",          # "90F" tapi bukan "Fahrenheit"
        r"(\d+\.?\d*)\s+degrees?\s+[Ff]\b",
        r"(\d+\.?\d*)\s+degrees?\s+fahrenheit",
    ]
    for pattern in f_patterns:
        m = re.search(pattern, question, re.IGNORECASE)
        if m:
            tf = float(m.group(1))
            tc = round((tf - 32) * 5 / 9, 2)
            log.debug("[Parser] °F match: %.1f°F → %.2f°C", tf, tc)
            return tc

    # ── Prioritas 2: Celsius explicit ────────────────────────────────────────
    c_patterns = [
        r"(\d+\.?\d*)\s*°\s*[Cc]\b",
        r"(\d+\.?\d*)\s+degrees?\s+celsius",
        r"(\d+\.?\d*)\s+degrees?\s+[Cc]\b",
    ]
    for pattern in c_patterns:
        m = re.search(pattern, question, re.IGNORECASE)
        if m:
            tc = round(float(m.group(1)), 2)
            log.debug("[Parser] °C match: %.2f°C", tc)
            return tc

    # ── Prioritas 3: Bare integer — hanya jika sangat yakin ──────────────────
    # Cari semua angka 2-3 digit di pertanyaan
    bare_matches = re.findall(r"\b(\d{2,3})\b", question)

    # Filter angka yang bukan tahun (1900-2099)
    candidates = [
        float(v) for v in bare_matches
        if not re.match(r"^(19|20)\d{2}$", v)
    ]

    if len(candidates) == 1:
        # Hanya ada satu angka non-tahun — lebih aman untuk dipakai
        val = candidates[0]

        # Range suhu °F yang wajar untuk market Polymarket (US-centric)
        if 32 <= val <= 130:
            tc = round((val - 32) * 5 / 9, 2)
            log.debug(
                "[Parser] Bare number (°F assumed): %.0f → %.2f°C", val, tc
            )
            return tc

        # Range suhu °C yang wajar
        if -20 <= val <= 50:
            log.debug("[Parser] Bare number (°C assumed): %.0f°C", val)
            return round(val, 2)

    elif len(candidates) > 1:
        # Lebih dari satu angka non-tahun → ambigu → tidak bisa dipercaya
        log.warning(
            "[Parser] Ambiguous bare numbers %s — skip: %s",
            candidates, question[:80]
        )
        return None

    log.warning("[Parser] No threshold found: %s", question[:80])
    return None


def parse_temperature_threshold(question: str) -> Optional[tuple[str, float]]:
    """
    Main parser: extract (direction, threshold_celsius).
    Returns None jika tidak bisa diparsing dengan aman.
    """
    # Cek dulu apakah pertanyaan ini unparseable
    if _is_unparseable(question):
        return None

    direction = _extract_direction(question)
    if direction is None:
        log.warning("[Parser] Direction tidak ditemukan: %s", question[:100])
        return None

    threshold_c = _extract_threshold(question)
    if threshold_c is None:
        return None

    log.info(
        "[Parser] OK → %s %.2f°C | %s",
        direction, threshold_c, question[:70]
    )
    return direction, threshold_c


# ── Signal Calculator ─────────────────────────────────────────────────────────

def compute_probability_signal(
    consensus: ConsensusResult,
    market_question: str,
    market_price: float,
    target_temp_type: str = "MAX",
) -> Optional[ProbabilitySignal]:
    """
    Derive trade signal dari Triple-Lock consensus dan market question.

    Args:
        consensus:        Validated ConsensusResult.
        market_question:  Full Polymarket question text.
        market_price:     Current mid-price sebagai P(YES) [0, 1].
        target_temp_type: "MAX" | "MIN" | "MEAN"

    Returns:
        ProbabilitySignal atau None jika question tidak bisa diparsing.
    """
    from config.settings import settings

    # Validasi market_price
    if not (0.01 <= market_price <= 0.99):
        log.warning(
            "[Signal] market_price %.4f di luar range valid — skip.",
            market_price
        )
        return None

    parsed = parse_temperature_threshold(market_question)
    if parsed is None:
        return None

    direction, threshold_c = parsed

    temp_map = {
        "MAX":  consensus.consensus_t_max,
        "MIN":  consensus.consensus_t_min,
        "MEAN": consensus.consensus_t_mean,
    }
    model_mean = temp_map.get(target_temp_type.upper(), consensus.consensus_t_mean)
    model_std  = BASE_FORECAST_STD_C + VARIANCE_WEIGHT * consensus.inter_model_variance

    # P(YES) via scipy Normal CDF
    if direction == "ABOVE":
        prob_yes = float(norm.sf(threshold_c, loc=model_mean, scale=model_std))
    else:
        prob_yes = float(norm.cdf(threshold_c, loc=model_mean, scale=model_std))

    prob_yes = round(min(max(prob_yes, 0.01), 0.99), 4)

    edge     = prob_yes - market_price
    net_edge = round(abs(edge) - settings.TRADING_FEE_PCT, 4)

    if net_edge < settings.MIN_EDGE_PCT:
        signal = "NO_TRADE"
    elif edge > 0:
        signal = "BUY_YES"
    else:
        signal = "BUY_NO"

    log.info(
        "[Signal] %s %.2f°C | model=%.3f mkt=%.3f | edge=%.3f net=%.3f | %s",
        direction, threshold_c, prob_yes, market_price, edge, net_edge, signal
    )

    return ProbabilitySignal(
        market_question=market_question,
        direction=direction,
        threshold_c=threshold_c,
        model_mean_c=round(model_mean, 2),
        model_std_c=round(model_std, 3),
        prob_yes=prob_yes,
        market_price=market_price,
        edge=round(edge, 4),
        net_edge=net_edge,
        signal=signal,
  )
