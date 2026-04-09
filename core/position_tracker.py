# ==============================================================================
# position_tracker.py — Open Position Tracking (FIXED)
# ==============================================================================
"""
AQL Position Tracker

Fixes:
  BUG #3 : Double-entry check di engine.py menggunakan position_id dengan
           format berbeda dari yang disimpan tracker.
           Fix: tambah method has_any_position_for(city_key, target_date)
           yang mengecek berdasarkan city_key + tanggal expires, bukan
           exact match position_id. Ini lebih robust karena tidak tergantung
           format string.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

from config.settings import settings

log = logging.getLogger("aql.positions")


# ── Position Data ─────────────────────────────────────────────────────────────

@dataclass
class OpenPosition:
    position_id: str
    market_id: str
    event_slug: str
    token_id: str
    city_key: str
    outcome_label: str
    market_type: str
    entry_price: float
    size_usd: float
    entry_time: str
    expires: str
    stop_loss_price: float
    take_profit_price: float
    current_price: float
    last_checked: str
    status: str               # OPEN / CLOSED_WIN / CLOSED_LOSS / EXPIRED

    @property
    def unrealized_pnl(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        contracts  = self.size_usd / self.entry_price
        sell_value = contracts * self.current_price
        fee        = sell_value * settings.TRADING_FEE_PCT
        return round(sell_value - self.size_usd - fee, 2)

    @property
    def pnl_pct(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return round(
            (self.current_price - self.entry_price) / self.entry_price * 100,
            1,
        )

    @property
    def hours_to_expiry(self) -> float:
        try:
            exp = datetime.fromisoformat(
                self.expires.replace("Z", "+00:00")
            )
            delta = exp - datetime.now(timezone.utc)
            return max(delta.total_seconds() / 3600, 0.0)
        except Exception:
            return 0.0

    @property
    def is_expired(self) -> bool:
        return self.hours_to_expiry <= 0

    @property
    def should_stop_loss(self) -> bool:
        return (
            settings.STOP_LOSS_ENABLED
            and self.current_price <= self.stop_loss_price
            and self.status == "OPEN"
        )

    @property
    def should_take_profit(self) -> bool:
        return (
            self.current_price >= self.take_profit_price
            and self.status == "OPEN"
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "OpenPosition":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


# ── Position Builder ──────────────────────────────────────────────────────────

def build_position(
    market_id: str,
    event_slug: str,
    token_id: str,
    city_key: str,
    outcome_label: str,
    market_type: str,
    entry_price: float,
    size_usd: float,
    expires: str,
) -> OpenPosition:
    now = datetime.now(timezone.utc).isoformat()

    stop_loss   = round(entry_price * (1 - settings.STOP_LOSS_PCT), 4)
    take_profit = round(entry_price * (1 + settings.TAKE_PROFIT_PCT), 4)
    take_profit = min(take_profit, 0.95)

    date_str    = expires[:10].replace("-", "")
    position_id = f"{city_key}-{outcome_label}-{date_str}".replace(" ", "_")

    return OpenPosition(
        position_id=position_id,
        market_id=market_id,
        event_slug=event_slug,
        token_id=token_id,
        city_key=city_key,
        outcome_label=outcome_label,
        market_type=market_type,
        entry_price=entry_price,
        size_usd=size_usd,
        entry_time=now,
        expires=expires,
        stop_loss_price=stop_loss,
        take_profit_price=take_profit,
        current_price=entry_price,
        last_checked=now,
        status="OPEN",
    )


# ── Position Store ────────────────────────────────────────────────────────────

class PositionTracker:

    def __init__(self) -> None:
        self._positions: dict[str, OpenPosition] = {}
        self._load()

    def _load(self) -> None:
        path = settings.STATE_FILE
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                data = json.load(f)
            raw = data.get("open_positions", {})
            for pid, pdata in raw.items():
                try:
                    self._positions[pid] = OpenPosition.from_dict(pdata)
                except Exception as e:
                    log.warning("Skip invalid position %s: %s", pid, e)
        except Exception as e:
            log.warning("Position load failed: %s", e)

    def _save(self) -> None:
        path = settings.STATE_FILE
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            existing = {}
            if os.path.exists(path):
                with open(path) as f:
                    existing = json.load(f)

            existing["open_positions"] = {
                pid: pos.to_dict()
                for pid, pos in self._positions.items()
            }
            existing["last_updated"] = datetime.now(timezone.utc).isoformat()

            with open(path, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception as e:
            log.error("Position save failed: %s", e)

    # ── Public API ────────────────────────────────────────────────────────

    def add(self, position: OpenPosition) -> None:
        self._positions[position.position_id] = position
        self._save()
        log.info(
            "[Positions] OPEN %s | %s @ %.4f | $%.2f",
            position.position_id,
            position.outcome_label,
            position.entry_price,
            position.size_usd,
        )

    def get(self, position_id: str) -> Optional[OpenPosition]:
        return self._positions.get(position_id)

    def has_position(self, position_id: str) -> bool:
        """Cek exact position_id."""
        pos = self._positions.get(position_id)
        return pos is not None and pos.status == "OPEN"

    def has_any_position_for(self, city_key: str, target_date: str) -> bool:
        """
        FIX BUG #3: Cek apakah sudah ada posisi OPEN untuk kota + tanggal ini,
        tanpa peduli outcome_label atau format position_id.

        target_date: string ISO date "YYYY-MM-DD" (dari market.end_date_iso[:10])

        Lebih robust daripada exact match position_id karena:
        - Tidak sensitif terhadap perubahan format position_id
        - Mencegah double-entry dari outcome berbeda pada event yang sama
        """
        date_compact = target_date.replace("-", "")
        for pos in self._positions.values():
            if pos.status != "OPEN":
                continue
            if pos.city_key != city_key:
                continue
            # Cek tanggal dari expires (format ISO) atau position_id (format compact)
            pos_date = pos.expires[:10].replace("-", "") if pos.expires else ""
            if pos_date == date_compact:
                log.debug(
                    "[Positions] Found existing position %s for %s @ %s",
                    pos.position_id, city_key, target_date,
                )
                return True
        return False

    def count_city(self, city_key: str) -> int:
        return sum(
            1 for p in self._positions.values()
            if p.city_key == city_key and p.status == "OPEN"
        )

    def get_open_positions(self) -> list[OpenPosition]:
        return [
            p for p in self._positions.values()
            if p.status == "OPEN"
        ]

    def get_expired_positions(self) -> list[OpenPosition]:
        return [
            p for p in self._positions.values()
            if p.status == "OPEN" and p.is_expired
        ]

    def get_exit_candidates(self) -> list[OpenPosition]:
        candidates = []
        for pos in self._positions.values():
            if pos.status != "OPEN":
                continue
            if pos.should_stop_loss or pos.should_take_profit:
                candidates.append(pos)
        return candidates

    def update_price(self, position_id: str, new_price: float) -> None:
        if position_id in self._positions:
            self._positions[position_id].current_price = new_price
            self._positions[position_id].last_checked = (
                datetime.now(timezone.utc).isoformat()
            )
            self._save()

    def close_position(
        self,
        position_id: str,
        status: str,
    ) -> Optional[OpenPosition]:
        if position_id not in self._positions:
            return None
        self._positions[position_id].status = status
        self._save()
        pos = self._positions[position_id]
        log.info(
            "[Positions] %s %s | PnL: %+.2f (%.1f%%)",
            status, position_id,
            pos.unrealized_pnl, pos.pnl_pct,
        )
        return pos

    def get_summary(self) -> dict:
        open_pos         = self.get_open_positions()
        total_invested   = sum(p.size_usd for p in open_pos)
        total_unrealized = sum(p.unrealized_pnl for p in open_pos)

        by_city: dict[str, int] = {}
        for p in open_pos:
            by_city[p.city_key] = by_city.get(p.city_key, 0) + 1

        return {
            "open_count":       len(open_pos),
            "total_invested":   round(total_invested, 2),
            "total_unrealized": round(total_unrealized, 2),
            "by_city":          by_city,
            "positions":        [
                {
                    "id":      p.position_id,
                    "outcome": p.outcome_label,
                    "city":    p.city_key,
                    "entry":   p.entry_price,
                    "current": p.current_price,
                    "pnl_pct": p.pnl_pct,
                    "expires": f"{p.hours_to_expiry:.1f}h",
                }
                for p in open_pos
            ],
        }
