# ══════════════════════════════════════════════════════════════════════════════
# probability.py = Kode Normal CDF
# ══════════════════════════════════════════════════════════════════════════════

"""
AQL Probability Mapper
Converts a Triple-Lock ConsensusResult into a binary P(YES) estimate
via a normal distribution CDF, then computes the net edge versus the
current Polymarket implied price.

Uncertainty model:
  σ = BASE_FORECAST_STD_C + (VARIANCE_WEIGHT × inter_model_variance)
  Higher inter-model disagreement → wider σ → smaller probability extremes.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from scipy.stats import norm

from core.consensus import ConsensusResult

log = logging.getLogger("aql.probability")

BASE_FORECAST_STD_C = 1.5   # Baseline NWP uncertainty (°C)
VARIANCE_WEIGHT     = 0.5   # Variance-to-σ inflation factor


@dataclass
class ProbabilitySignal:
    market_question: str
    direction: str       # "ABOVE" | "BELOW"
    threshold_c: float

    model_mean_c: float
    model_std_c: float

    prob_yes: float      # Model-derived P(YES)
    market_price: float  # Polymarket implied P(YES)
    edge: float          # prob_yes − market_price  (pre-friction)
    net_edge: float      # |edge| − trading_fee  (actionable edge)

    signal: str          # "BUY_YES" | "BUY_NO" | "NO_TRADE"


# ── Question Parser ───────────────────────────────────────────────────────────

def parse_temperature_threshold(question: str) -> Optional[tuple[str, float]]:
    """
    Extract (direction, threshold_celsius) from a Polymarket question string.

    Supported patterns:
      "Will the high in Chicago exceed 90°F …"   → ("ABOVE", 32.22)
      "Will NYC max temp be above 35°C …"         → ("ABOVE", 35.0)
      "Will temperature stay below 32°F …"        → ("BELOW", 0.0)
      "Will Dallas reach 105 on Friday …"         → ("ABOVE", 40.56)

    Returns None when direction or threshold cannot be reliably parsed.
    """
    q_up = question.upper()

    # Determine direction
    if any(kw in q_up for kw in ["EXCEED", "ABOVE", "OVER", "MORE THAN", "AT LEAST", "REACH"]):
        direction = "ABOVE"
    elif any(kw in q_up for kw in ["BELOW", "UNDER", "LESS THAN", "NOT REACH", "STAY UNDER"]):
        direction = "BELOW"
    else:
        log.warning("Direction unresolvable: %s", question[:100])
        return None

    # ── Fahrenheit (explicit) ─────────────────────────────────────────────
    f_match = re.search(r"(\d+\.?\d*)\s*°?\s*F\b", question, re.IGNORECASE)
    if f_match:
        tf = float(f_match.group(1))
        tc = round((tf - 32) * 5 / 9, 2)
        log.debug("Parsed °F: %.1f → %.2f°C (%s)", tf, tc, direction)
        return direction, tc

    # ── Celsius (explicit) ────────────────────────────────────────────────
    c_match = re.search(r"(\d+\.?\d*)\s*°?\s*C\b", question, re.IGNORECASE)
    if c_match:
        tc = round(float(c_match.group(1)), 2)
        log.debug("Parsed °C: %.2f (%s)", tc, direction)
        return direction, tc

    # ── Bare integer heuristic (assume °F if > 60 for US markets) ─────────
    bare_match = re.search(r"\b(\d{2,3})\b", question)
    if bare_match:
        val = float(bare_match.group(1))
        if val > 60:
            tc = round((val - 32) * 5 / 9, 2)
            log.debug("Bare number → °F heuristic: %.0f → %.2f°C (%s)", val, tc, direction)
            return direction, tc

    log.warning("Threshold unresolvable: %s", question[:100])
    return None


# ── Signal Calculator ─────────────────────────────────────────────────────────

def compute_probability_signal(
    consensus: ConsensusResult,
    market_question: str,
    market_price: float,
    target_temp_type: str = "MAX",  # "MAX" | "MIN" | "MEAN"
) -> Optional[ProbabilitySignal]:
    """
    Given a Triple-Lock consensus and a market question, derive the trade signal.

    Args:
        consensus:        A validated ConsensusResult (triple_lock=True expected
                          by caller, but function is defensive).
        market_question:  Full Polymarket question text.
        market_price:     Current Polymarket mid-price as P(YES) in [0, 1].
        target_temp_type: Which consensus statistic to compare against threshold.

    Returns:
        ProbabilitySignal with .signal in {"BUY_YES", "BUY_NO", "NO_TRADE"},
        or None if the question cannot be parsed.
    """
    from config.settings import settings  # local import avoids circular at module load

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

    # P(YES) via scipy normal distribution
    prob_yes = float(
        norm.sf(threshold_c, loc=model_mean, scale=model_std)
        if direction == "ABOVE"
        else norm.cdf(threshold_c, loc=model_mean, scale=model_std)
    )
    prob_yes = round(min(max(prob_yes, 0.01), 0.99), 4)

    edge     = prob_yes - market_price
    net_edge = round(abs(edge) - settings.TRADING_FEE_PCT, 4)

    if net_edge < settings.MIN_EDGE_PCT:
        signal = "NO_TRADE"
    elif edge > 0:
        signal = "BUY_YES"
    else:
        signal = "BUY_NO"

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

