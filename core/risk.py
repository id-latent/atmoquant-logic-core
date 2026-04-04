# ==============================================================================
# risk.py = Kelly Criterion + Circuit Breaker + Size Noise
# ==============================================================================
"""
AQL Risk Engine
Perbaikan dari versi sebelumnya:
- Circuit breaker membedakan TRADE_LOSS vs ORDER_REJECTED
- Kelly sizing dengan size noise agar tidak terdeteksi sebagai bot
- State persistence tetap via data/state.json
"""
from __future__ import annotations

import json
import logging
import os
import random
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

from config.settings import settings
from core.probability import ProbabilitySignal

log = logging.getLogger("aql.risk")


# ── Loss Type ─────────────────────────────────────────────────────────────────

class LossType:
    TRADE_LOSS     = "trade_loss"      # Market bergerak melawan posisi
    ORDER_REJECTED = "order_rejected"  # FOK tidak terisi — bukan salah model


# ── Persistent State ──────────────────────────────────────────────────────────

@dataclass
class TradingState:
    consecutive_losses: int       = 0
    total_trades: int             = 0
    total_wins: int               = 0
    total_pnl_usd: float          = 0.0
    circuit_breaker_active: bool  = False
    circuit_breaker_since: Optional[str] = None
    last_updated: str             = ""

    # Tracking terpisah untuk order rejections
    consecutive_rejections: int   = 0
    total_rejections: int         = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TradingState":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


def _load_state() -> TradingState:
    path = settings.STATE_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return TradingState.from_dict(json.load(f))
        except Exception as e:
            log.warning("State load failed (%s) — fresh state.", e)
    return TradingState()


def _save_state(state: TradingState) -> None:
    state.last_updated = datetime.now(timezone.utc).isoformat()
    with open(settings.STATE_FILE, "w") as f:
        json.dump(state.to_dict(), f, indent=2)


# ── Position Order ────────────────────────────────────────────────────────────

@dataclass
class PositionOrder:
    side: str
    size_usd: float
    kelly_fraction: float
    edge_used: float
    market_price: float
    max_profit_usd: float
    expected_value_usd: float


# ── Size Noise ────────────────────────────────────────────────────────────────

def _add_size_noise(size_usd: float) -> float:
    """
    Tambahkan noise kecil ke size agar tidak terdeteksi sebagai bot.
    Bot suspicious selalu order angka bulat: $10.00, $20.00, $50.00.
    Dengan noise: $12.00 → $11.73 atau $12.41 (terlihat lebih human).
    Range noise: ±4% dari size original.
    """
    noise_pct  = random.uniform(-0.04, 0.04)
    noisy_size = size_usd * (1 + noise_pct)
    return round(noisy_size, 2)


# ── Kelly Criterion ───────────────────────────────────────────────────────────

def kelly_position(
    signal: ProbabilitySignal,
    bankroll_usd: float,
    confidence_multiplier: float = 1.0,
) -> Optional[PositionOrder]:
    """
    Fractional Kelly sizing untuk binary prediction markets.

    Formula: f* = (p × (b + 1) − 1) / b
    where b = (1 / price) − 1

    Args:
        signal:                 ProbabilitySignal dari compute_probability_signal.
        bankroll_usd:           Total modal tersedia.
        confidence_multiplier:  0.5–1.0 dari ConfidenceScore (default 1.0).

    Returns PositionOrder atau None jika EV negatif.
    """
    if signal.signal == "BUY_YES":
        our_prob = signal.prob_yes
        price    = signal.market_price
    elif signal.signal == "BUY_NO":
        our_prob = 1.0 - signal.prob_yes
        price    = 1.0 - signal.market_price
    else:
        return None

    if not (0 < price < 1):
        log.warning("Degenerate price %.4f — skip Kelly.", price)
        return None

    b          = (1.0 / price) - 1.0
    full_kelly = (our_prob * (b + 1) - 1) / b

    if full_kelly <= 0:
        log.info("Full Kelly ≤ 0 (%.4f) — no positive EV.", full_kelly)
        return None

    # Fractional Kelly × confidence multiplier
    frac_kelly = full_kelly * settings.KELLY_FRACTION * confidence_multiplier
    raw_size   = bankroll_usd * frac_kelly

    # Apply caps
    size_usd = min(max(raw_size, 1.0), settings.MAX_POSITION_USD)

    # Tambahkan noise untuk anti-detection
    size_usd = _add_size_noise(size_usd)

    # Re-apply caps setelah noise
    size_usd = round(min(max(size_usd, 1.0), settings.MAX_POSITION_USD), 2)

    # Expected value
    contracts    = size_usd / price
    gross_profit = contracts * (1.0 - price)
    fee_cost     = size_usd * settings.TRADING_FEE_PCT
    ev_usd       = (our_prob * gross_profit) - ((1 - our_prob) * size_usd) - fee_cost

    return PositionOrder(
        side="YES" if signal.signal == "BUY_YES" else "NO",
        size_usd=size_usd,
        kelly_fraction=round(frac_kelly, 5),
        edge_used=signal.net_edge,
        market_price=price,
        max_profit_usd=round(gross_profit - fee_cost, 2),
        expected_value_usd=round(ev_usd, 2),
    )


# ── Circuit Breaker ───────────────────────────────────────────────────────────

class CircuitBreaker:
    """
    Stateful circuit breaker dengan pembedaan jenis loss.

    TRADE_LOSS     → increment consecutive_losses → bisa trip breaker
    ORDER_REJECTED → increment consecutive_rejections → tidak trip breaker
                     (FOK rejected = market tidak liquid, bukan model salah)

    State persist di data/state.json lintas Railway restart.
    Reset manual via POST /admin/reset-breaker.
    """

    def __init__(self) -> None:
        self._state = _load_state()

    def is_open(self) -> bool:
        return self._state.circuit_breaker_active

    def record_win(self, pnl_usd: float) -> None:
        self._state.consecutive_losses     = 0
        self._state.consecutive_rejections = 0
        self._state.total_wins            += 1
        self._state.total_trades          += 1
        self._state.total_pnl_usd          = round(
            self._state.total_pnl_usd + pnl_usd, 2
        )
        _save_state(self._state)
        log.info("WIN +$%.2f | Loss streak reset.", pnl_usd)

    def record_loss(
        self,
        pnl_usd: float,
        loss_type: str = LossType.TRADE_LOSS,
    ) -> bool:
        """
        Catat loss. Returns True jika circuit breaker baru saja trip.

        loss_type:
            LossType.TRADE_LOSS     → hitung ke streak, bisa trip breaker
            LossType.ORDER_REJECTED → catat tapi tidak trip breaker
        """
        if loss_type == LossType.ORDER_REJECTED:
            self._state.consecutive_rejections += 1
            self._state.total_rejections       += 1
            _save_state(self._state)
            log.warning(
                "ORDER REJECTED (bukan trade loss) | "
                "Consecutive rejections: %d",
                self._state.consecutive_rejections
            )
            return False

        # TRADE_LOSS — hitung ke consecutive streak
        self._state.consecutive_losses += 1
        self._state.total_trades       += 1
        self._state.total_pnl_usd       = round(
            self._state.total_pnl_usd - abs(pnl_usd), 2
        )

        tripped = False
        if self._state.consecutive_losses >= settings.CIRCUIT_BREAKER_LOSSES:
            self._state.circuit_breaker_active = True
            self._state.circuit_breaker_since  = datetime.now(
                timezone.utc
            ).isoformat()
            log.critical(
                "CIRCUIT BREAKER TRIPPED — %d consecutive losses!",
                self._state.consecutive_losses,
            )
            tripped = True

        _save_state(self._state)
        log.warning(
            "TRADE LOSS -$%.2f | Consecutive: %d",
            abs(pnl_usd), self._state.consecutive_losses,
        )
        return tripped

    def manual_reset(self) -> None:
        self._state.circuit_breaker_active = False
        self._state.consecutive_losses     = 0
        self._state.consecutive_rejections = 0
        self._state.circuit_breaker_since  = None
        _save_state(self._state)
        log.warning("Circuit breaker reset oleh operator.")

    @property
    def state(self) -> TradingState:
        return self._state

    def get_daily_pnl_summary(self) -> dict:
        s        = self._state
        win_rate = (
            s.total_wins / s.total_trades * 100
        ) if s.total_trades > 0 else 0.0
        return {
            "total_trades":         s.total_trades,
            "total_wins":           s.total_wins,
            "win_rate_pct":         round(win_rate, 1),
            "total_pnl_usd":        s.total_pnl_usd,
            "consecutive_losses":   s.consecutive_losses,
            "consecutive_rejections": s.consecutive_rejections,
            "circuit_breaker":      s.circuit_breaker_active,
            }
