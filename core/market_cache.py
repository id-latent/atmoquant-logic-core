# ==============================================================================
# core/market_cache.py — Market Analysis Cache
# ==============================================================================
"""
AQL Market Cache
Menyimpan hasil analisis market untuk menghindari re-analysis berulang.

Logic:
  - Re-analyze setiap 2 scan cycles (30 menit) — CACHE_REANALYZE_CYCLES
  - Re-analyze jika harga berubah > 3% dari cache terakhir
  - Cache dibersihkan otomatis untuk market yang sudah expired

Menghemat:
  - API calls ke Open-Meteo (consensus fetch)
  - Komputasi Normal CDF
  - Rate limit risk
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

from config.settings import settings

log = logging.getLogger("aql.cache")


# ── Cache Entry ───────────────────────────────────────────────────────────────

@dataclass
class MarketCacheEntry:
    cache_key: str           # "{city_key}-{date}" atau condition_id
    condition_id: str        # Polymarket condition ID
    city_key: str
    target_date: str         # ISO date string
    last_analyzed: str       # ISO datetime UTC
    scan_cycle: int          # Cycle number saat dianalisis
    last_price: float        # Harga saat dianalisis
    consensus_mean_c: float  # Forecast mean dari consensus
    consensus_variance: float
    triple_lock: bool
    analysis_count: int      # Berapa kali sudah dianalisis
    expires: str             # endDate market — cache dihapus setelah ini

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MarketCacheEntry":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)

    @property
    def is_expired(self) -> bool:
        """Cache entry expired jika market sudah tutup."""
        try:
            exp = datetime.fromisoformat(
                self.expires.replace("Z", "+00:00")
            )
            return datetime.now(timezone.utc) > exp
        except Exception:
            return False


# ── Market Cache ──────────────────────────────────────────────────────────────

class MarketCache:
    """
    Cache untuk hasil analisis market.
    Persist di data/state.json.
    """

    def __init__(self) -> None:
        self._cache: dict[str, MarketCacheEntry] = {}
        self._current_cycle: int = 0
        self._load()

    def _load(self) -> None:
        path = settings.STATE_FILE
        if not os.path.exists(path):
            return
        try:
            with open(path) as f:
                data = json.load(f)
            raw = data.get("market_cache", {})
            self._current_cycle = data.get("scan_cycle", 0)

            for key, entry_data in raw.items():
                try:
                    entry = MarketCacheEntry.from_dict(entry_data)
                    # Jangan load entry yang sudah expired
                    if not entry.is_expired:
                        self._cache[key] = entry
                except Exception as e:
                    log.debug("Skip cache entry %s: %s", key, e)

            log.info(
                "[Cache] Loaded %d entries (cycle %d)",
                len(self._cache), self._current_cycle,
            )
        except Exception as e:
            log.warning("Cache load failed: %s", e)

    def _save(self) -> None:
        path = settings.STATE_FILE
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            existing = {}
            if os.path.exists(path):
                with open(path) as f:
                    existing = json.load(f)

            # Bersihkan expired entries sebelum save
            self._cleanup()

            existing["market_cache"] = {
                key: entry.to_dict()
                for key, entry in self._cache.items()
            }
            existing["scan_cycle"] = self._current_cycle

            with open(path, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception as e:
            log.error("Cache save failed: %s", e)

    def _cleanup(self) -> None:
        """Hapus cache entries yang sudah expired."""
        expired_keys = [
            k for k, v in self._cache.items()
            if v.is_expired
        ]
        for key in expired_keys:
            del self._cache[key]
        if expired_keys:
            log.debug("[Cache] Cleaned %d expired entries", len(expired_keys))

    def increment_cycle(self) -> int:
        """Increment scan cycle counter. Dipanggil di awal setiap scan."""
        self._current_cycle += 1
        self._save()
        return self._current_cycle

    @property
    def current_cycle(self) -> int:
        return self._current_cycle

    # ── Cache Logic ───────────────────────────────────────────────────────

    def should_analyze(
        self,
        cache_key: str,
        current_price: float,
    ) -> bool:
        """
        Apakah market ini perlu dianalisis ulang?

        Returns True (perlu analisis) jika:
          1. Belum ada di cache (pertama kali)
          2. Sudah melewati CACHE_REANALYZE_CYCLES (default: 2 cycle = 30 menit)
          3. Harga berubah lebih dari CACHE_PRICE_CHANGE_PCT (default: 3%)
        """
        entry = self._cache.get(cache_key)

        # Belum pernah dianalisis
        if entry is None:
            log.debug("[Cache] MISS %s — first analysis", cache_key)
            return True

        # Sudah melewati reanalyze cycles
        cycles_elapsed = self._current_cycle - entry.scan_cycle
        if cycles_elapsed >= settings.CACHE_REANALYZE_CYCLES:
            log.debug(
                "[Cache] STALE %s — %d cycles elapsed",
                cache_key, cycles_elapsed,
            )
            return True

        # Harga berubah signifikan
        if entry.last_price > 0:
            price_change = abs(current_price - entry.last_price) / entry.last_price
            if price_change >= settings.CACHE_PRICE_CHANGE_PCT:
                log.debug(
                    "[Cache] PRICE CHANGE %s — %.1f%% change",
                    cache_key, price_change * 100,
                )
                return True

        log.debug("[Cache] HIT %s — using cached analysis", cache_key)
        return False

    def get(self, cache_key: str) -> Optional[MarketCacheEntry]:
        """Return cached entry jika ada dan belum expired."""
        entry = self._cache.get(cache_key)
        if entry and not entry.is_expired:
            return entry
        return None

    def set(
        self,
        cache_key: str,
        condition_id: str,
        city_key: str,
        target_date: str,
        current_price: float,
        consensus_mean_c: float,
        consensus_variance: float,
        triple_lock: bool,
        expires: str,
    ) -> None:
        """Simpan atau update cache entry setelah analisis."""
        existing = self._cache.get(cache_key)
        count    = (existing.analysis_count + 1) if existing else 1

        self._cache[cache_key] = MarketCacheEntry(
            cache_key=cache_key,
            condition_id=condition_id,
            city_key=city_key,
            target_date=target_date,
            last_analyzed=datetime.now(timezone.utc).isoformat(),
            scan_cycle=self._current_cycle,
            last_price=current_price,
            consensus_mean_c=consensus_mean_c,
            consensus_variance=consensus_variance,
            triple_lock=triple_lock,
            analysis_count=count,
            expires=expires,
        )
        self._save()
        log.debug(
            "[Cache] SET %s (analysis #%d)",
            cache_key, count,
        )

    def get_stats(self) -> dict:
        """Stats cache untuk logging."""
        self._cleanup()
        return {
            "total_entries":  len(self._cache),
            "current_cycle":  self._current_cycle,
            "locked_markets": sum(
                1 for e in self._cache.values()
                if e.triple_lock
            ),
            "analysis_total": sum(
                e.analysis_count for e in self._cache.values()
            ),
      }
