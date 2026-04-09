# ==============================================================================
# engine.py — Master Orchestrator v2.0.1 (FIXED)
# ==============================================================================
"""
AQL Engine Unified Pipeline

Fixes:
  BUG #2 : Race condition pada trades_per_city — dict biasa di-share antar
           coroutine concurrent. Sekarang menggunakan asyncio.Lock per kota
           sehingga hanya satu coroutine yang bisa increment counter per kota
           dalam satu waktu.
  BUG #3 : position_id tidak konsisten antara double-entry check di engine
           dan format yang disimpan di PositionTracker.
           Fix: gunakan format yang sama persis dengan build_position():
             "{city_key}-{outcome_label}-{date_str}"
           tapi karena di step [2] kita belum tahu outcome_label,
           kita cek semua posisi kota+tanggal, bukan exact match.
           Pendekatan: tracker.has_any_position_for(city_key, target_date)
  BUG #6 : CircuitBreaker / PositionTracker / MarketCache diinstansiasi baru
           di setiap HTTP request. Fix ada di main.py — engine menyediakan
           accessor property agar main.py bisa pakai instance yang sama.
  BUG #9  semaphore: nama dan semantik diselaraskan. Semaphore mengontrol
           total concurrent market processing (bukan per-kota). Nama
           variabel diubah agar tidak menyesatkan.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Optional

import httpx

from config.settings import settings
from core.consensus import get_triple_lock_consensus
from core.exit_strategy import ExitStrategy
from core.location_registry import (
    CityInfo,
    GoldenHourStatus,
    check_golden_hour,
    golden_hour_multiplier,
)
from core.market_cache import MarketCache
from core.position_tracker import PositionTracker, build_position
from core.probability import (
    OutcomeCandidate,
    ProbabilitySignal,
    evaluate_binary,
    evaluate_multi_outcome,
)
from core.risk import CircuitBreaker, LossType, kelly_position
from core.volume_analyzer import analyze_volume, calculate_avg_volume
from market.gamma_client import CLOBExecutor, GammaClient, TemperatureMarket
from notifications import notifier

log = logging.getLogger("aql.engine")


class AQLEngine:
    VERSION = "2.0.1"

    def __init__(self) -> None:
        self.breaker  = CircuitBreaker()
        self.tracker  = PositionTracker()
        self.cache    = MarketCache()
        # FIX BUG #2: Lock per kota untuk mencegah race condition
        self._city_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        log.info("AQL Engine v%s initialised.", self.VERSION)

    # ── Bankroll Health ───────────────────────────────────────────────────────

    async def _check_bankroll(self, bankroll_usd: float) -> bool:
        if bankroll_usd < settings.MINIMUM_BANKROLL_HALT:
            log.critical(
                "Bankroll $%.2f di bawah minimum $%.2f",
                bankroll_usd, settings.MINIMUM_BANKROLL_HALT,
            )
            await notifier.notify_error(
                title="Bankroll Terlalu Kecil — Trading Dihentikan",
                description=(
                    f"Bankroll saat ini: **${bankroll_usd:.2f}**\n"
                    f"Minimum: **${settings.MINIMUM_BANKROLL_HALT:.2f}**\n\n"
                    f"Top up wallet lalu update `BANKROLL_USD` di Railway → Variables."
                ),
            )
            return False

        if bankroll_usd < settings.MINIMUM_BANKROLL_WARNING:
            log.warning("Bankroll $%.2f rendah.", bankroll_usd)
            await notifier.notify_error(
                title="Bankroll Rendah",
                description=(
                    f"Bankroll saat ini: **${bankroll_usd:.2f}**\n"
                    f"Disarankan minimum: **${settings.MINIMUM_BANKROLL_WARNING:.2f}**\n"
                    f"Kelly sizing otomatis dikurangi.\n"
                    f"Pertimbangkan top up segera."
                ),
            )
        return True

    # ── Confidence Scoring ────────────────────────────────────────────────────

    def _compute_confidence(
        self,
        consensus,
        horizon_days: int,
    ) -> float:
        variance_score = max(0.0, 1.0 - consensus.inter_model_variance)
        horizon_score  = max(0.3, 1.0 - (horizon_days - 1) * 0.10)
        raw            = 0.5 + ((variance_score + horizon_score) / 2 * 0.5)
        return round(min(max(raw, 0.5), 1.0), 4)

    # ── Process Single Market ─────────────────────────────────────────────────

    async def _process_market(
        self,
        http_client: httpx.AsyncClient,
        market: TemperatureMarket,
        clob: CLOBExecutor,
        bankroll_usd: float,
        trades_per_city: dict[str, int],
    ) -> bool:
        try:
            async with asyncio.timeout(30):
                return await self._run_pipeline(
                    http_client, market, clob, bankroll_usd, trades_per_city
                )
        except asyncio.TimeoutError:
            log.warning(
                "[Engine] TIMEOUT 30s — market %s (%s) dilewati",
                market.condition_id[:12],
                market.question[:50],
            )
            return False

    async def _run_pipeline(
        self,
        http_client: httpx.AsyncClient,
        market: TemperatureMarket,
        clob: CLOBExecutor,
        bankroll_usd: float,
        trades_per_city: dict[str, int],
    ) -> bool:
        """Pipeline inti dengan fix race condition dan position ID."""

        city      = market.city
        gh_mult   = market.golden_hour_mult
        gh_status = GoldenHourStatus(market.golden_hour_status)

        # [1] Max trades per city — FIX BUG #2: gunakan asyncio.Lock per kota
        async with self._city_locks[city.key]:
            city_trades = trades_per_city.get(city.key, 0)
            if city_trades >= settings.MAX_TRADES_PER_CITY:
                log.debug(
                    "[Engine] Max %d trades untuk %s — skip",
                    settings.MAX_TRADES_PER_CITY, city.key,
                )
                return False
            # Reserve slot SEKARANG sebelum await apapun
            # (akan di-confirm atau di-rollback di akhir pipeline)
            trades_per_city[city.key] = city_trades + 1
            reserved = True

        try:
            result = await self._execute_pipeline(
                http_client, market, clob, bankroll_usd,
                city, gh_mult, gh_status,
            )
            if not result:
                # Rollback reservation jika pipeline tidak menghasilkan trade
                async with self._city_locks[city.key]:
                    trades_per_city[city.key] = max(
                        trades_per_city.get(city.key, 1) - 1, 0
                    )
            return result

        except Exception:
            # Rollback jika exception
            async with self._city_locks[city.key]:
                trades_per_city[city.key] = max(
                    trades_per_city.get(city.key, 1) - 1, 0
                )
            raise

    async def _execute_pipeline(
        self,
        http_client: httpx.AsyncClient,
        market: TemperatureMarket,
        clob: CLOBExecutor,
        bankroll_usd: float,
        city: CityInfo,
        gh_mult: float,
        gh_status: GoldenHourStatus,
    ) -> bool:
        """Pipeline inti setelah slot sudah direservasi."""

        target_date = market.end_date_iso[:10]

        # [2] Double entry check — FIX BUG #3:
        # Gunakan has_any_position_for() untuk cek semua posisi kota+tanggal
        # (karena outcome_label belum diketahui di tahap ini)
        if self.tracker.has_any_position_for(city.key, target_date):
            log.debug(
                "[Engine] Already has position for %s @ %s — skip",
                city.key, target_date,
            )
            return False

        # [3] Hitung horizon
        try:
            target_dt    = date.fromisoformat(target_date)
            horizon_days = max((target_dt - date.today()).days, 1)
        except Exception:
            horizon_days = 1

        # [4] Cache check
        current_price = (
            market.outcomes[0].price
            if market.outcomes
            else market.mid_price
        )
        if not self.cache.should_analyze(market.cache_key, current_price):
            log.debug("[Engine] Cache HIT %s — skip reanalysis", market.cache_key)
            return False

        # [5] Quad-Lock consensus + volume pre-calc paralel
        leading_volume = 0.0
        avg_volume     = 0.0

        if market.market_type == "MULTI_OUTCOME" and market.outcomes:
            volumes    = [o.volume_24h for o in market.outcomes]
            avg_volume = calculate_avg_volume(volumes)
            leading    = max(market.outcomes, key=lambda o: o.price)
            leading_volume = leading.volume_24h

        consensus, _ = await asyncio.gather(
            get_triple_lock_consensus(city.lat, city.lon, city.key, target_dt),
            asyncio.sleep(0),
        )

        if consensus is None:
            log.warning("[Engine] Consensus gagal untuk %s", city.key)
            return False

        # [6] Update cache
        self.cache.set(
            cache_key=market.cache_key,
            condition_id=market.condition_id,
            city_key=city.key,
            target_date=target_date,
            current_price=current_price,
            consensus_mean_c=consensus.consensus_t_mean,
            consensus_variance=consensus.inter_model_variance,
            triple_lock=consensus.triple_lock,
            expires=market.end_date_iso,
        )

        # [7] Discord consensus notification
        await notifier.notify_consensus_update(
            location_name=city.key.title(),
            target_date=target_date,
            ecmwf_mean=consensus.ecmwf.t_mean_c,
            gfs_mean=consensus.gfs.t_mean_c,
            noaa_mean=consensus.noaa.t_mean_c,
            consensus_mean=consensus.consensus_t_mean,
            variance=consensus.inter_model_variance,
            triple_lock=consensus.triple_lock,
            icon_mean=consensus.icon.t_mean_c if consensus.icon else None,
            model_count=consensus.model_count,
            golden_hour_status=gh_status.value,
            hours_to_close=market.htc,
        )

        # [8] Triple-Lock gate
        if not consensus.triple_lock:
            log.info(
                "[Engine] Lock failed %s σ=%.3f°C",
                city.key, consensus.inter_model_variance,
            )
            return False

        # [9] Min edge berdasarkan tier
        min_edge = settings.get_min_edge(city.tier)

        # [10] Probability evaluation
        signal: Optional[ProbabilitySignal] = None

        if market.market_type == "MULTI_OUTCOME":
            candidates = [
                OutcomeCandidate(
                    label=o.label,
                    token_id=o.token_id,
                    market_price=o.price,
                    volume_24h=o.volume_24h,
                )
                for o in market.outcomes
            ]
            signal = evaluate_multi_outcome(
                outcomes=candidates,
                consensus=consensus,
                city=city,
                min_edge=min_edge,
            )
        else:
            signal = evaluate_binary(
                question=market.question,
                yes_token_id=market.yes_token_id,
                no_token_id=market.no_token_id,
                market_price=market.mid_price,
                consensus=consensus,
                city=city,
                min_edge=min_edge,
                volume_24h=market.volume_usd,
            )

        if signal is None:
            log.warning("[Engine] Signal gagal: %s", market.question[:60])
            return False

        if signal.signal == "NO_TRADE":
            log.info(
                "[Engine] NO_TRADE — edge %.2f%% < min %.2f%%",
                signal.best_net_edge * 100, min_edge * 100,
            )
            return False

        # [11] Big edge alert
        if signal.best_net_edge >= settings.BIG_EDGE_THRESHOLD:
            await notifier.notify_big_edge(
                market_question=market.question,
                outcome_label=signal.best_outcome_label,
                edge_pct=signal.best_net_edge,
                model_prob=signal.best_prob_model,
                market_price=signal.best_market_price,
                city=city.key.title(),
            )

        # [12] Volume analysis
        if market.market_type == "MULTI_OUTCOME" and market.outcomes:
            leading_outcome = max(market.outcomes, key=lambda o: o.price)
            vol_signal = analyze_volume(
                outcome_label=signal.best_outcome_label,
                volume_24h=leading_volume,
                avg_volume=avg_volume,
                forecast_outcome=signal.forecast_outcome,
                market_leading_outcome=leading_outcome.label,
            )
        else:
            from core.volume_analyzer import VolumeSignal
            vol_signal = VolumeSignal(
                has_spike=False,
                spike_direction="NONE",
                spike_magnitude=1.0,
                kelly_multiplier=1.0,
                warning_message="",
            )

        if vol_signal.spike_direction == "AGAINST_FORECAST":
            await notifier.notify_volume_warning(
                market_question=market.question,
                city=city.key.title(),
                warning_message=vol_signal.warning_message,
                spike_magnitude=vol_signal.spike_magnitude,
            )

        # [13] Confidence scoring
        confidence_mult = self._compute_confidence(consensus, horizon_days)

        # [14] Kelly sizing
        position = kelly_position(
            signal=signal,
            bankroll_usd=bankroll_usd,
            confidence_multiplier=confidence_mult,
            golden_hour_multiplier=gh_mult,
            volume_multiplier=vol_signal.kelly_multiplier,
        )

        if position is None:
            log.info("[Engine] Kelly returned None — no positive EV")
            return False

        log.info(
            "[Engine] SIZE %s $%.2f | kelly=%.5f | "
            "conf=%.3f gh=%.2f vol=%.2f final=%.4f",
            position.side, position.size_usd,
            position.kelly_fraction,
            position.confidence_mult,
            position.golden_hour_mult,
            position.volume_mult,
            position.final_mult,
        )

        # [15] Circuit breaker
        if self.breaker.is_open():
            await notifier.notify_error(
                title="Trade Blocked — Circuit Breaker Active",
                description="Reset via POST /admin/reset-breaker.",
                is_circuit_breaker=True,
            )
            await notifier.notify_opportunity_missed(
                market_question=market.question,
                outcome_label=signal.best_outcome_label,
                edge_pct=signal.best_net_edge,
                reason="Circuit Breaker Active",
            )
            return False

        # [16] Submit order
        ask_price = (
            signal.best_market_price
            if signal.signal == "BUY_YES"
            else (1.0 - signal.best_market_price)
        )
        receipt = await clob.submit_order(
            token_id=signal.best_token_id,
            size_usd=position.size_usd,
            ask_price=ask_price,
        )

        if receipt is None:
            self.breaker.record_rejection()
            await notifier.notify_error(
                title="Order Rejected — FOK Tidak Terisi",
                description=(
                    f"Market: {market.question[:100]}\n"
                    f"Outcome: {signal.best_outcome_label}\n"
                    f"Size: ${position.size_usd:.2f}"
                ),
            )
            return False

        # [17] Add to position tracker
        open_pos = build_position(
            market_id=market.condition_id,
            event_slug=market.event_slug,
            token_id=signal.best_token_id,
            city_key=city.key,
            outcome_label=signal.best_outcome_label,
            market_type=market.market_type,
            entry_price=signal.best_market_price,
            size_usd=position.size_usd,
            expires=market.end_date_iso,
        )
        self.tracker.add(open_pos)

        # [18] Discord trade notification
        await notifier.notify_trade_executed(
            market_name=market.question,
            side=position.side,
            outcome_label=signal.best_outcome_label,
            price=signal.best_market_price,
            size_usd=position.size_usd,
            edge_pct=signal.best_net_edge,
            ev_usd=position.expected_value_usd,
            kelly_fraction=position.kelly_fraction,
            confidence_mult=position.confidence_mult,
            golden_hour_mult=position.golden_hour_mult,
            volume_mult=position.volume_mult,
            final_mult=position.final_mult,
            market_url=market.url,
            order_id=receipt.get("orderID"),
            all_outcomes=signal.all_outcomes,
            forecast_outcome=signal.forecast_outcome,
            model_mean_c=signal.model_mean_c,
            model_std_c=signal.model_std_c,
            golden_hour_status=gh_status.value,
            market_type=market.market_type,
        )

        log.info(
            "[TRADE] %s | %s | %s | $%.2f",
            city.key.upper(),
            signal.best_outcome_label,
            position.side,
            position.size_usd,
        )
        return True

    # ── Scan Cycle ────────────────────────────────────────────────────────────

    async def run_scan_cycle(self, bankroll_usd: float = 200.0) -> None:
        log.info(
            "═══ AQL SCAN v%s  %s ═══",
            self.VERSION,
            datetime.now(timezone.utc).isoformat(),
        )

        try:
            async with asyncio.timeout(300):
                await self._run_scan(bankroll_usd)
        except asyncio.TimeoutError:
            log.error(
                "[Engine] TIMEOUT 300s — scan cycle dihentikan paksa."
            )
            await notifier.notify_error(
                title="Scan Cycle Timeout",
                description=(
                    "Scan cycle melebihi 300 detik dan dihentikan paksa.\n"
                    "Kemungkinan: koneksi Open-Meteo atau Polymarket lambat."
                ),
            )

    async def _run_scan(self, bankroll_usd: float) -> None:
        self.cache.increment_cycle()

        if not await self._check_bankroll(bankroll_usd):
            return

        if self.breaker.is_open():
            log.critical("Circuit breaker open — scan aborted.")
            await notifier.notify_error(
                title="Scan Aborted — Circuit Breaker Active",
                description="Reset via POST /admin/reset-breaker.",
                is_circuit_breaker=True,
            )
            return

        async with httpx.AsyncClient() as http:
            gamma  = GammaClient(http)
            clob   = CLOBExecutor(http)
            exit_s = ExitStrategy(self.tracker)

            await exit_s.update_prices(http)
            exit_results = await exit_s.check_and_exit(http)

            for result in exit_results:
                if result.reason == "EXPIRED":
                    await notifier.notify_position_expired(
                        position=result.position,
                    )
                else:
                    await notifier.notify_exit_executed(
                        position=result.position,
                        reason=result.reason,
                        exit_price=result.exit_price,
                        pnl_usd=result.pnl_usd,
                    )

                    _pos    = self.tracker.get(result.position.position_id)
                    _region = _pos.city_key.title() if _pos else "Unknown"

                    if result.is_win:
                        self.breaker.record_win(
                            pnl_usd=result.pnl_usd,
                            region=_region,
                            market_type=result.position.market_type,
                            outcome_label=result.position.outcome_label,
                        )
                    else:
                        self.breaker.record_loss(
                            pnl_usd=result.pnl_usd,
                            region=_region,
                            market_type=result.position.market_type,
                            outcome_label=result.position.outcome_label,
                        )

            markets = await gamma.discover_temperature_markets()

            for unk in gamma.unknown_markets:
                await notifier.notify_unknown_city(market=unk)

            if not markets:
                log.info("[Engine] Tidak ada market yang qualify.")
                return

            def sort_key(m: TemperatureMarket) -> tuple:
                type_priority = 0 if m.market_type == "MULTI_OUTCOME" else 1
                return (type_priority, -m.liquidity_usd)

            markets_sorted = sorted(markets, key=sort_key)

            trades_per_city: dict[str, int] = {}

            # FIX BUG #9 semantik: semaphore mengontrol total concurrent
            # market processing — rename agar tidak menyesatkan
            sem = asyncio.Semaphore(settings.MAX_CONCURRENT_CITIES)

            async def _safe(m: TemperatureMarket) -> bool:
                async with sem:
                    try:
                        return await self._process_market(
                            http, m, clob, bankroll_usd, trades_per_city
                        )
                    except Exception as e:
                        log.error(
                            "[Engine] Pipeline error %s: %s",
                            m.condition_id[:12], e, exc_info=True,
                        )
                        await notifier.notify_error(
                            title="Unhandled Pipeline Error",
                            description=f"{m.question[:100]}\n{str(e)[:400]}",
                        )
                        return False

            results = await asyncio.gather(
                *[_safe(m) for m in markets_sorted]
            )
            placed = sum(results)

            log.info(
                "[Engine] Cycle done. Trades: %d / %d candidates.",
                placed, len(markets),
            )

    # ── Forever Loop ──────────────────────────────────────────────────────────

    async def run_forever(self, bankroll_usd: float = 200.0) -> None:
        from core.location_registry import registry_summary
        await notifier.notify_startup(
            bankroll_usd=bankroll_usd,
            registry_stats=registry_summary(),
        )

        last_summary_date:   Optional[date] = None
        last_heartbeat_hour: Optional[int]  = None
        last_weekly_report:  Optional[date] = None

        while True:
            try:
                now = datetime.now(timezone.utc)

                if last_summary_date != now.date():
                    await notifier.notify_daily_pnl_summary(
                        **self.breaker.get_daily_pnl_summary()
                    )
                    last_summary_date = now.date()

                if (
                    settings.HOURLY_HEARTBEAT
                    and last_heartbeat_hour != now.hour
                ):
                    cache_stats = self.cache.get_stats()
                    pos_summary = self.tracker.get_summary()
                    await notifier.notify_heartbeat(
                        bankroll_usd=bankroll_usd,
                        scan_cycle=cache_stats["current_cycle"],
                        open_positions=pos_summary["open_count"],
                        today_trades=self.breaker.state.get_today_stats().trades,
                        today_pnl=self.breaker.state.get_today_stats().pnl_usd,
                        cache_entries=cache_stats["total_entries"],
                    )
                    last_heartbeat_hour = now.hour

                if (
                    now.weekday() == settings.WEEKLY_REPORT_DAY
                    and last_weekly_report != now.date()
                ):
                    weekly = self.breaker.state.get_weekly_summary()
                    await notifier.notify_weekly_report(weekly=weekly)
                    last_weekly_report = now.date()

                await self.run_scan_cycle(bankroll_usd=bankroll_usd)

            except KeyboardInterrupt:
                log.info("Shutdown signal — keluar.")
                break
            except Exception as e:
                log.critical("Main loop error: %s", e, exc_info=True)
                await notifier.notify_error(
                    title="Critical Engine Error",
                    description=str(e)[:800],
                )

            await asyncio.sleep(settings.POLL_INTERVAL_SECONDS)
