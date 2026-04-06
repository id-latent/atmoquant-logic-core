# ==============================================================================
# risk.py — Kelly Criterion + Circuit Breaker
# ==============================================================================
"""
AQL Risk Engine

Perubahan dari sebelumnya:
  - kelly_position menerima semua multiplier sekaligus
    (confidence + golden_hour + volume)
  - Circuit breaker tetap membedakan TRADE_LOSS vs ORDER_REJECTED
  - TradingState extended: daily_stats per tanggal + per region
  - Size noise anti-detection tetap ada
"""
from __future__ import annotations

import json
import logging
import os
import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, date, timezone
from typing import Optional

from config.settings import settings
from core.probability import ProbabilitySignal

log = logging.getLogger("aql.risk")


# ── Loss Type ─────────────────────────────────────────────────────────────────

class LossType:
    TRADE_LOSS     = "trade_loss"
    ORDER_REJECTED = "order_rejected"


# ── Daily Stats ───────────────────────────────────────────────────────────────

@dataclass
class DailyStats:
    date: str             = ""
    trades: int           = 0
    wins: int             = 0
    losses: int           = 0
    pnl_usd: float        = 0.0
    by_region: dict       = field(default_factory=dict)
    by_type: dict         = field(default_factory=dict)
    best_trade_pnl: float = 0.0
    worst_trade_pnl: float = 0.0
    best_trade_label: str = ""
    worst_trade_label: str = ""
    avg_edge: float       = 0.0
    total_edge: float     = 0.0
    avg_position: float   = 0.0
    total_invested: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DailyStats":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)

    @property
    def win_rate(self) -> float:
        if self.trades == 0:
            return 0.0
        return round(self.wins / self.trades * 100, 1)


# ── Trading State ─────────────────────────────────────────────────────────────

@dataclass
class TradingState:
    # ── Core counters ──────────────────────────────────────────────────────
    consecutive_losses: int       = 0
    consecutive_rejections: int   = 0
    total_trades: int             = 0
    total_wins: int               = 0
    total_rejections: int         = 0
    total_pnl_usd: float          = 0.0

    # ── Circuit breaker ────────────────────────────────────────────────────
    circuit_breaker_active: bool  = False
    circuit_breaker_since: Optional[str] = None

    # ── Daily stats (key = date string "2026-04-05") ───────────────────────
    daily_stats: dict             = field(default_factory=dict)

    # ── Weekly tracking ────────────────────────────────────────────────────
    week_start: str               = ""
    week_pnl: float               = 0.0
    week_trades: int              = 0
    week_wins: int                = 0

    last_updated: str             = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TradingState":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)

    def get_today_stats(self) -> DailyStats:
        today = str(date.today())
        if today not in self.daily_stats:
            self.daily_stats[today] = DailyStats(date=today).to_dict()
        return DailyStats.from_dict(self.daily_stats[today])

    def save_today_stats(self, stats: DailyStats) -> None:
        today = str(date.today())
        self.daily_stats[today] = stats.to_dict()

    def get_weekly_summary(self) -> dict:
        """Summary 7 hari terakhir."""
        from datetime import timedelta
        today = date.today()
        week_pnl  = 0.0
        week_trades = 0
        week_wins   = 0

        for i in range(7):
            day_str = str(today - timedelta(days=i))
            if day_str in self.daily_stats:
                ds = DailyStats.from_dict(self.daily_stats[day_str])
                week_pnl    += ds.pnl_usd
                week_trades += ds.trades
                week_wins   += ds.wins

        win_rate = (
            round(week_wins / week_trades * 100, 1)
            if week_trades > 0 else 0.0
        )
        return {
            "pnl_usd":   round(week_pnl, 2),
            "trades":    week_trades,
            "wins":      week_wins,
            "win_rate":  win_rate,
        }


# ── State Persistence ─────────────────────────────────────────────────────────

def _load_state() -> TradingState:
    path = settings.STATE_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
            return TradingState.from_dict(data)
        except Exception as e:
            log.warning("State load failed (%s) — fresh state.", e)
    return TradingState()


def _save_state(state: TradingState) -> None:
    state.last_updated = datetime.now(timezone.utc).isoformat()
    path = settings.STATE_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # Baca existing untuk preserve open_positions + market_cache
    existing = {}
    if os.path.exists(path):
        try:
            with open(path) as f:
                existing = json.load(f)
        except Exception:
            pass

    # Update hanya trading state fields
    state_dict = state.to_dict()
    existing.update(state_dict)

    with open(path, "w") as f:
        json.dump(existing, f, indent=2)


# ── Position Order ────────────────────────────────────────────────────────────

@dataclass
class PositionOrder:
    side: str                  # "YES" atau "NO"
    token_id: str              # Token yang akan dibeli
    size_usd: float
    kelly_fraction: float
    edge_used: float
    market_price: float
    max_profit_usd: float
    expected_value_usd: float
    confidence_mult: float
    golden_hour_mult: float
    volume_mult: float
    final_mult: float          # confidence × golden_hour × volume


# ── Size Noise ────────────────────────────────────────────────────────────────

def _add_size_noise(size_usd: float) -> float:
    """Tambah noise ±4% untuk anti-detection."""
    noise = random.uniform(-0.04, 0.04)
    return round(size_usd * (1 + noise), 2)


# ── Kelly Position Sizing ─────────────────────────────────────────────────────

def kelly_position(
    signal: ProbabilitySignal,
    bankroll_usd: float,
    confidence_multiplier: float = 1.0,
    golden_hour_multiplier: float = 1.0,
    volume_multiplier: float = 1.0,
) -> Optional[PositionOrder]:
    """
    Fractional Kelly dengan semua multiplier.

    Formula: f* = (p × (b+1) − 1) / b
    Applied: f_final = f* × KELLY_FRACTION
                     × confidence_mult
                     × golden_hour_mult
                     × volume_mult

    Args:
        signal:                 ProbabilitySignal dari probability engine
        bankroll_usd:           Total modal tersedia
        confidence_multiplier:  0.5–1.0 dari variance + horizon score
        golden_hour_multiplier: 1.0/0.7/0.5 dari golden hour status
        volume_multiplier:      0.6–1.0 dari volume signal analysis
    """
    # Tentukan side dan price
    if signal.signal == "BUY_YES":
        our_prob = signal.best_prob_model
        price    = signal.best_market_price
        token_id = signal.best_token_id
        side     = "YES"
    elif signal.signal == "BUY_NO":
        our_prob = 1.0 - signal.best_prob_model
        price    = 1.0 - signal.best_market_price
        token_id = signal.best_token_id  # Engine akan pilih NO token
        side     = "NO"
    else:
        return None  # NO_TRADE

    if not (0 < price < 1):
        log.warning("Degenerate price %.4f — skip.", price)
        return None

    # Kelly formula
    b          = (1.0 / price) - 1.0
    full_kelly = (our_prob * (b + 1) - 1) / b

    if full_kelly <= 0:
        log.info("Full Kelly ≤ 0 (%.4f) — no positive EV.", full_kelly)
        return None

    # Combined multiplier
    final_mult = (
        confidence_multiplier
        * golden_hour_multiplier
        * volume_multiplier
    )
    final_mult = round(min(max(final_mult, 0.1), 1.0), 4)

    frac_kelly = full_kelly * settings.KELLY_FRACTION * final_mult
    raw_size   = bankroll_usd * frac_kelly

    # Apply caps + noise
    size_usd = min(max(raw_size, 1.0), settings.MAX_POSITION_USD)
    size_usd = _add_size_noise(size_usd)
    size_usd = round(
        min(max(size_usd, 1.0), settings.MAX_POSITION_USD), 2
    )

    # EV calculation
    contracts    = size_usd / price
    gross_profit = contracts * (1.0 - price)
    fee_cost     = size_usd * settings.TRADING_FEE_PCT
    ev_usd       = (
        (our_prob * gross_profit)
        - ((1 - our_prob) * size_usd)
        - fee_cost
    )

    log.info(
        "[Kelly] side=%s price=%.4f size=$%.2f "
        "kelly=%.5f mult=%.4f EV=$%.2f",
        side, price, size_usd, frac_kelly, final_mult, ev_usd,
    )

    return PositionOrder(
        side=side,
        token_id=token_id,
        size_usd=size_usd,
        kelly_fraction=round(frac_kelly, 5),
        edge_used=signal.best_net_edge,
        market_price=price,
        max_profit_usd=round(gross_profit - fee_cost, 2),
        expected_value_usd=round(ev_usd, 2),
        confidence_mult=confidence_multiplier,
        golden_hour_mult=golden_hour_multiplier,
        volume_mult=volume_multiplier,
        final_mult=final_mult,
    )


# ── Circuit Breaker ───────────────────────────────────────────────────────────

class CircuitBreaker:
    """
    Stateful circuit breaker v2.0.0.
    Extended dengan daily stats tracking.
    """

    def __init__(self) -> None:
        self._state = _load_state()

    def is_open(self) -> bool:
        return self._state.circuit_breaker_active

    def record_win(
        self,
        pnl_usd: float,
        region: str = "Unknown",
        market_type: str = "Unknown",
        outcome_label: str = "",
        edge_pct: float = 0.0,
        size_usd: float = 0.0,
    ) -> None:
        """Record win dengan full stats tracking."""
        self._state.consecutive_losses = 0
        self._state.consecutive_rejections = 0
        self._state.total_wins   += 1
        self._state.total_trades += 1
        self._state.total_pnl_usd = round(
            self._state.total_pnl_usd + pnl_usd, 2
        )

        # Update daily stats
        today = self._state.get_today_stats()
        today.trades += 1
        today.wins   += 1
        today.pnl_usd = round(today.pnl_usd + pnl_usd, 2)

        # By region
        today.by_region[region] = round(
            today.by_region.get(region, 0.0) + pnl_usd, 2
        )
        # By market type
        today.by_type[market_type] = round(
            today.by_type.get(market_type, 0.0) + pnl_usd, 2
        )

        # Best trade tracking
        if pnl_usd > today.best_trade_pnl:
            today.best_trade_pnl   = pnl_usd
            today.best_trade_label = outcome_label

        # Edge tracking
        today.total_edge    += edge_pct
        today.total_invested += size_usd
        if today.trades > 0:
            today.avg_edge     = round(today.total_edge / today.trades, 4)
            today.avg_position = round(today.total_invested / today.trades, 2)

        self._state.save_today_stats(today)
        _save_state(self._state)
        log.info("WIN +$%.2f | streak reset.", pnl_usd)

    def record_loss(
        self,
        pnl_usd: float,
        loss_type: str = LossType.TRADE_LOSS,
        region: str = "Unknown",
        market_type: str = "Unknown",
        outcome_label: str = "",
        edge_pct: float = 0.0,
        size_usd: float = 0.0,
    ) -> bool:
        """
        Record loss. Returns True jika circuit breaker trip.
        ORDER_REJECTED tidak menghitung ke streak.
        """
        if loss_type == LossType.ORDER_REJECTED:
            self._state.consecutive_rejections += 1
            self._state.total_rejections       += 1
            _save_state(self._state)
            log.warning(
                "ORDER REJECTED | consecutive: %d",
                self._state.consecutive_rejections,
            )
            return False

        # TRADE_LOSS
        self._state.consecutive_losses += 1
        self._state.total_trades       += 1
        self._state.total_pnl_usd       = round(
            self._state.total_pnl_usd - abs(pnl_usd), 2
        )

        # Daily stats
        today = self._state.get_today_stats()
        today.trades  += 1
        today.losses  += 1
        today.pnl_usd  = round(today.pnl_usd - abs(pnl_usd), 2)

        today.by_region[region] = round(
            today.by_region.get(region, 0.0) - abs(pnl_usd), 2
        )
        today.by_type[market_type] = round(
            today.by_type.get(market_type, 0.0) - abs(pnl_usd), 2
        )

        if -abs(pnl_usd) < today.worst_trade_pnl:
            today.worst_trade_pnl   = -abs(pnl_usd)
            today.worst_trade_label = outcome_label

        today.total_edge    += edge_pct
        today.total_invested += size_usd
        if today.trades > 0:
            today.avg_edge     = round(today.total_edge / today.trades, 4)
            today.avg_position = round(today.total_invested / today.trades, 2)

        self._state.save_today_stats(today)

        # Circuit breaker check
        tripped = False
        if self._state.consecutive_losses >= settings.CIRCUIT_BREAKER_LOSSES:
            self._state.circuit_breaker_active = True
            self._state.circuit_breaker_since  = (
                datetime.now(timezone.utc).isoformat()
            )
            log.critical(
                "CIRCUIT BREAKER TRIPPED — %d consecutive losses!",
                self._state.consecutive_losses,
            )
            tripped = True

        _save_state(self._state)
        log.warning(
            "LOSS -$%.2f | consecutive: %d",
            abs(pnl_usd), self._state.consecutive_losses,
        )
        return tripped

    def record_rejection(self) -> None:
        """Shorthand untuk order rejection."""
        self.record_loss(0.0, loss_type=LossType.ORDER_REJECTED)

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
        """Summary untuk Discord #📊-aql-terminal."""
        s     = self._state
        today = s.get_today_stats()

        win_rate = (
            round(s.total_wins / s.total_trades * 100, 1)
            if s.total_trades > 0 else 0.0
        )

        return {
            # Alltime
            "total_trades":          s.total_trades,
            "total_wins":            s.total_wins,
            "win_rate_pct":          win_rate,
            "total_pnl_usd":         s.total_pnl_usd,
            "consecutive_losses":    s.consecutive_losses,
            "consecutive_rejections": s.consecutive_rejections,
            "circuit_breaker":       s.circuit_breaker_active,

            # Today
            "today_trades":          today.trades,
            "today_wins":            today.wins,
            "today_pnl_usd":         today.pnl_usd,
            "today_win_rate":        today.win_rate,
            "today_by_region":       today.by_region,
            "today_by_type":         today.by_type,
            "today_best_trade":      today.best_trade_label,
            "today_best_pnl":        today.best_trade_pnl,
            "today_worst_trade":     today.worst_trade_label,
            "today_worst_pnl":       today.worst_trade_pnl,
            "today_avg_edge":        today.avg_edge,
            "today_avg_position":    today.avg_position,

            # Weekly
            "weekly": s.get_weekly_summary(),
    }
