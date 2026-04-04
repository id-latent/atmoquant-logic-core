# ==============================================================================
# engine.py = Master Orchestrator Pipeline
# ==============================================================================
"""
AQL Engine — 9-Step Pipeline per Market
Perbaikan dari versi sebelumnya:
- Unknown city detector + Discord alert
- Bankroll health check (warning + halt)
- Confidence multiplier terintegrasi ke Kelly
- Circuit breaker membedakan trade loss vs order rejected
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Optional

import httpx

from config.settings import settings
from core.consensus import get_triple_lock_consensus, is_decision_phase
from core.probability import compute_probability_signal
from core.risk import CircuitBreaker, LossType, kelly_position
from market.gamma_client import CLOBExecutor, GammaClient, PolyMarket
from notifications import notifier

log = logging.getLogger("aql.engine")

# ── Bankroll Safety Thresholds ────────────────────────────────────────────────
MINIMUM_BANKROLL_HALT    = 15.0   # Stop trading jika di bawah ini
MINIMUM_BANKROLL_WARNING = 50.0   # Kirim warning jika di bawah ini

# ── Location Registry ─────────────────────────────────────────────────────────
# Format: "nama_kota_lowercase": (latitude, longitude)
# Tambah kota baru di sini jika ada market Polymarket yang tidak terdeteksi

LOCATION_REGISTRY: dict[str, tuple[float, float]] = {
    # United States
    "new york":    (40.7128, -74.0060),
    "nyc":         (40.7128, -74.0060),
    "chicago":     (41.8781, -87.6298),
    "los angeles": (34.0522, -118.2437),
    "miami":       (25.7617, -80.1918),
    "houston":     (29.7604, -95.3698),
    "dallas":      (32.7767, -96.7970),
    "phoenix":     (33.4484, -112.0740),
    "seattle":     (47.6062, -122.3321),
    "denver":      (39.7392, -104.9903),
    "atlanta":     (33.7490, -84.3880),
    "las vegas":   (36.1699, -115.1398),
    "boston":      (42.3601, -71.0589),
    "minneapolis": (44.9778, -93.2650),
    # Europe
    "london":      (51.5074,  -0.1278),
    "paris":       (48.8566,   2.3522),
    "berlin":      (52.5200,  13.4050),
    "madrid":      (40.4168,  -3.7038),
    "rome":        (41.9028,  12.4964),
    "amsterdam":   (52.3676,   4.9041),
    "zurich":      (47.3769,   8.5417),
}


def _resolve_location(question: str) -> Optional[tuple[str, float, float]]:
    """Scan question text untuk kota yang dikenal."""
    q = question.lower()
    for city, (lat, lon) in LOCATION_REGISTRY.items():
        if city in q:
            return city.title(), lat, lon
    return None


# ── Bankroll Health Check ─────────────────────────────────────────────────────

async def _check_bankroll_health(bankroll_usd: float) -> bool:
    """
    Cek apakah bankroll cukup untuk trading aman.
    Returns False jika bankroll terlalu kecil dan trading harus dihentikan.
    """
    if bankroll_usd < MINIMUM_BANKROLL_HALT:
        log.critical(
            "Bankroll $%.2f di bawah minimum $%.2f — trading dihentikan.",
            bankroll_usd, MINIMUM_BANKROLL_HALT,
        )
        await notifier.notify_error(
            title="🚨 Bankroll Terlalu Kecil — Trading Dihentikan",
            description=(
                f"Bankroll saat ini: **${bankroll_usd:.2f}**\n"
                f"Minimum untuk trading: **${MINIMUM_BANKROLL_HALT:.2f}**\n\n"
                f"Bot dihentikan otomatis untuk melindungi modal.\n"
                f"Top up wallet lalu update `BANKROLL_USD` "
                f"di Railway → Variables."
            ),
        )
        return False

    if bankroll_usd < MINIMUM_BANKROLL_WARNING:
        log.warning(
            "Bankroll $%.2f mendekati batas minimum.",
            bankroll_usd,
        )
        await notifier.notify_error(
            title="⚠️ Bankroll Rendah",
            description=(
                f"Bankroll saat ini: **${bankroll_usd:.2f}**\n"
                f"Disarankan minimum: **${MINIMUM_BANKROLL_WARNING:.2f}**\n\n"
                f"Bot tetap berjalan tapi pertimbangkan top up segera.\n"
                f"Kelly sizing sudah berkurang proporsional."
            ),
        )

    return True


# ── Unknown City Alert ────────────────────────────────────────────────────────

async def _alert_unknown_location(market: PolyMarket) -> None:
    """
    Kirim alert ke Discord ketika temperature market ditemukan
    tapi lokasinya tidak ada di LOCATION_REGISTRY.
    Berguna agar kamu bisa tambahkan kota baru secara manual.
    """
    await notifier.notify_error(
        title="🗺️ Kota Tidak Dikenal — Market Dilewati",
        description=(
            f"**Market temperature ditemukan tapi kota tidak dikenal:**\n"
            f"```\n{market.question[:200]}\n```\n"
            f"**Liquidity:** ${market.liquidity_usd:,.0f}\n"
            f"**Hours to close:** {market.hours_to_close:.1f}h\n"
            f"**Link:** {market.url}\n\n"
            f"Tambahkan kota ke `LOCATION_REGISTRY` di `core/engine.py`\n"
            f"Format: `\"nama kota\": (latitude, longitude)`"
        ),
    )
    log.warning(
        "[UNKNOWN CITY] %s | $%.0f liq | %.1fh to close",
        market.question[:70], market.liquidity_usd, market.hours_to_close,
    )


# ── AQL Engine ────────────────────────────────────────────────────────────────

class AQLEngine:
    VERSION = "1.1.0"

    def __init__(self) -> None:
        self.breaker = CircuitBreaker()
        log.info("AQL Engine v%s initialised.", self.VERSION)

    async def _process_market(
        self,
        http_client: httpx.AsyncClient,
        market: PolyMarket,
        clob: CLOBExecutor,
        bankroll_usd: float,
    ) -> bool:
        """Full 9-step pipeline untuk satu market kandidat."""

        # [1] Resolve location
        loc = _resolve_location(market.question)
        if loc is None:
            await _alert_unknown_location(market)
            return False

        city, lat, lon = loc
        target_date = date.fromisoformat(market.end_date_iso[:10])

        # Hitung horizon untuk confidence scoring
        horizon_days = max((target_date - date.today()).days, 1)

        # [2] Triple-Lock consensus dengan retry
        consensus = await get_triple_lock_consensus(
            lat, lon, city, target_date
        )
        if consensus is None:
            log.warning("Consensus tidak tersedia untuk %s.", city)
            return False

        # [3] Discord consensus update (selalu dikirim)
        await notifier.notify_consensus_update(
            location_name=city,
            target_date=str(target_date),
            ecmwf_mean=consensus.ecmwf.t_mean_c,
            gfs_mean=consensus.gfs.t_mean_c,
            noaa_mean=consensus.noaa.t_mean_c,
            consensus_mean=consensus.consensus_t_mean,
            variance=consensus.inter_model_variance,
            triple_lock=consensus.triple_lock,
        )

        # [4] Triple-Lock gate
        if not consensus.triple_lock:
            log.info(
                "[LOCK FAIL] %s Δ=%.2f°C — skip.",
                city, consensus.inter_model_variance,
            )
            return False

        # [5] Probability signal + edge
        signal = compute_probability_signal(
            consensus=consensus,
            market_question=market.question,
            market_price=market.mid_price,
        )
        if signal is None:
            log.warning("Question unparseable: %s", market.question[:60])
            return False

        log.info(
            "[SIGNAL] %s P(YES)=%.3f mkt=%.3f edge=%.2f%% → %s",
            city, signal.prob_yes, signal.market_price,
            signal.net_edge * 100, signal.signal,
        )

        if signal.signal == "NO_TRADE":
            log.info(
                "[NO TRADE] Edge %.2f%% di bawah minimum.",
                signal.net_edge * 100,
            )
            return False

        # [5b] Confidence multiplier berdasarkan variance + horizon
        # Semakin kecil variance dan semakin dekat horizon → multiplier tinggi
        variance_score  = max(0.0, 1.0 - consensus.inter_model_variance)
        horizon_score   = max(0.3, 1.0 - (horizon_days - 1) * 0.10)
        confidence_mult = round(0.5 + ((variance_score + horizon_score) / 2 * 0.5), 4)
        confidence_mult = min(max(confidence_mult, 0.5), 1.0)

        log.info(
            "[CONFIDENCE] variance_score=%.3f horizon_score=%.3f mult=%.4f",
            variance_score, horizon_score, confidence_mult,
        )

        # [6] Kelly sizing dengan confidence multiplier
        position = kelly_position(
            signal,
            bankroll_usd,
            confidence_multiplier=confidence_mult,
        )
        if position is None:
            log.info("[NO TRADE] Kelly returned None (non-positive EV).")
            return False

        log.info(
            "[SIZE] %s $%.2f | Kelly=%.5f | EV=$%.2f | conf=%.4f",
            position.side, position.size_usd,
            position.kelly_fraction, position.expected_value_usd,
            confidence_mult,
        )

        # [7] Circuit breaker check
        if self.breaker.is_open():
            await notifier.notify_error(
                title="Trade Blocked — Circuit Breaker Active",
                description="Reset via POST /admin/reset-breaker.",
                is_circuit_breaker=True,
            )
            return False

        # [8] Submit order
        receipt = await clob.submit_order(
            market, position.side, position.size_usd
        )

        if receipt is None:
            # Order rejected (FOK tidak terisi) — BUKAN trade loss
            self.breaker.record_loss(
                pnl_usd=0,
                loss_type=LossType.ORDER_REJECTED,
            )
            await notifier.notify_error(
                title="Order Rejected — FOK Tidak Terisi",
                description=(
                    f"Market: {market.question[:100]}\n"
                    f"Side: {position.side} | "
                    f"Size: ${position.size_usd:.2f}\n"
                    f"Kemungkinan: liquiditas tidak cukup saat eksekusi."
                ),
            )
            return False

        # [9] Trade notification
        await notifier.notify_trade_executed(
            market_name=market.question,
            side=position.side,
            price=position.market_price,
            size_usd=position.size_usd,
            edge_pct=signal.net_edge,
            ev_usd=position.expected_value_usd,
            kelly_fraction=position.kelly_fraction,
            market_url=market.url,
            order_id=receipt.get("orderID"),
        )

        log.info(
            "[TRADE] %s | %s | $%.2f",
            market.question[:55], position.side, position.size_usd,
        )
        return True

    # ── Scan Cycle ────────────────────────────────────────────────────────────

    async def run_scan_cycle(self, bankroll_usd: float = 200.0) -> None:
        """Satu full discovery → filter → trade cycle."""
        log.info(
            "═══ AQL SCAN %s ═══",
            datetime.now(timezone.utc).isoformat(),
        )

        # Bankroll health check
        if not await _check_bankroll_health(bankroll_usd):
            return

        # Circuit breaker check
        if self.breaker.is_open():
            log.critical("Circuit breaker open — scan dibatalkan.")
            await notifier.notify_error(
                title="Scan Dibatalkan — Circuit Breaker Active",
                description="Reset via POST /admin/reset-breaker.",
                is_circuit_breaker=True,
            )
            return

        async with httpx.AsyncClient() as http:
            gamma  = GammaClient(http)
            clob   = CLOBExecutor(http)
            window = settings.ENTRY_WINDOW_HOURS_BEFORE

            markets = await gamma.discover_temperature_markets(
                min_liquidity_usd=500.0,
                hours_before_close_min=window - 1,
                hours_before_close_max=window + 1,
            )

            if not markets:
                log.info("Tidak ada market yang memenuhi syarat cycle ini.")
                return

            # Max 3 market diproses bersamaan
            sem = asyncio.Semaphore(3)

            async def _safe(m: PolyMarket) -> bool:
                async with sem:
                    try:
                        return await self._process_market(
                            http, m, clob, bankroll_usd
                        )
                    except Exception as e:
                        log.error(
                            "Pipeline error [%s]: %s",
                            m.market_id, e, exc_info=True,
                        )
                        await notifier.notify_error(
                            title="Unhandled Pipeline Error",
                            description=(
                                f"{m.question[:100]}\n{str(e)[:400]}"
                            ),
                        )
                        return False

            results = await asyncio.gather(*[_safe(m) for m in markets])
            log.info(
                "Cycle selesai. Trade: %d / %d kandidat.",
                sum(results), len(markets),
            )

    # ── Forever Loop ──────────────────────────────────────────────────────────

    async def run_forever(self, bankroll_usd: float = 200.0) -> None:
        """
        Infinite monitoring loop:
        - Daily PnL summary setiap tengah malam UTC
        - Scan cycle setiap POLL_INTERVAL_SECONDS
        - Auto-recovery dari crash dengan Discord notification
        """
        await notifier.notify_startup(self.VERSION)
        last_summary: Optional[date] = None

        while True:
            try:
                now = datetime.now(timezone.utc)

                # Daily PnL summary di tengah malam UTC
                if last_summary != now.date():
                    await notifier.notify_daily_pnl_summary(
                        **self.breaker.get_daily_pnl_summary()
                    )
                    last_summary = now.date()

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
