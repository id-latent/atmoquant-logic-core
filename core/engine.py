# ══════════════════════════════════════════════════════════════════════════════
# engine.py = Kode Orchestrator
# ══════════════════════════════════════════════════════════════════════════════

"""
AQL Engine — Master Async Orchestrator
9-step pipeline per market:
  [1] Location resolution
  [2] Triple-Lock consensus fetch (ECMWF + GFS + NOAA, concurrent)
  [3] Discord consensus notification (always)
  [4] Triple-Lock gate
  [5] Probability signal + edge
  [6] Kelly position sizing
  [7] Circuit breaker guard
  [8] CLOB order submission
  [9] Discord trade notification
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
from core.risk import CircuitBreaker, kelly_position
from market.gamma_client import CLOBExecutor, GammaClient, PolyMarket
from notifications import notifier

log = logging.getLogger("aql.engine")

# ── Location Registry ─────────────────────────────────────────────────────────
# city_fragment_lowercase → (latitude, longitude)
# Add cities as new Polymarket temperature markets emerge.

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
    """Scan question text for a known city. Returns (name, lat, lon) or None."""
    q = question.lower()
    for city, (lat, lon) in LOCATION_REGISTRY.items():
        if city in q:
            return city.title(), lat, lon
    return None


# ── AQLEngine ─────────────────────────────────────────────────────────────────

class AQLEngine:
    VERSION = "1.0.0"

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
        """Full 9-step pipeline for one candidate market. Returns True on trade."""

        # [1] Location
        loc = _resolve_location(market.question)
        if loc is None:
            log.debug("Unknown location in: %s", market.question[:80])
            return False
        city, lat, lon = loc
        target_date = date.fromisoformat(market.end_date_iso[:10])

        # [2] Consensus
        consensus = await get_triple_lock_consensus(lat, lon, city, target_date)
        if consensus is None:
            log.warning("Consensus data unavailable for %s.", city)
            return False

        # [3] Discord update (unconditional)
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
                "[LOCK FAIL] %s Δ=%.2f°C — skipping %s",
                city, consensus.inter_model_variance, market.question[:60],
            )
            return False

        # [5] Probability + edge
        signal = compute_probability_signal(
            consensus=consensus,
            market_question=market.question,
            market_price=market.mid_price,
        )
        if signal is None:
            log.warning("Question unparseable: %s", market.question[:60])
            return False

        log.info(
            "[SIGNAL] %s P(YES)=%.3f market=%.3f edge=%.2f%% → %s",
            city, signal.prob_yes, signal.market_price,
            signal.net_edge * 100, signal.signal,
        )

        if signal.signal == "NO_TRADE":
            log.info("[NO TRADE] Edge %.2f%% below minimum.", signal.net_edge * 100)
            return False

        # [6] Kelly sizing
        position = kelly_position(signal, bankroll_usd)
        if position is None:
            log.info("[NO TRADE] Kelly returned None (non-positive EV).")
            return False

        log.info(
            "[SIZE] %s $%.2f | Kelly=%.5f | EV=$%.2f",
            position.side, position.size_usd, position.kelly_fraction, position.expected_value_usd,
        )

        # [7] Circuit breaker
        if self.breaker.is_open():
            await notifier.notify_error(
                title="Trade Blocked — Circuit Breaker Active",
                description="Reset via POST /admin/reset-breaker.",
                is_circuit_breaker=True,
            )
            return False

        # [8] Submit order
        receipt = await clob.submit_order(market, position.side, position.size_usd)
        if receipt is None:
            await notifier.notify_error(
                title="Order Submission Failed",
                description=(
                    f"Market: {market.question[:100]}\n"
                    f"Side: {position.side} | Size: ${position.size_usd:.2f}"
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

        log.info("[TRADE] %s | %s | $%.2f", market.question[:55], position.side, position.size_usd)
        return True

    async def run_scan_cycle(self, bankroll_usd: float = 200.0) -> None:
        """One full discovery → filter → trade cycle."""
        log.info("═══ AQL SCAN  %s ═══", datetime.now(timezone.utc).isoformat())

        if self.breaker.is_open():
            log.critical("Circuit breaker open — aborting scan.")
            await notifier.notify_error(
                title="Scan Aborted",
                description="Circuit breaker is open. Manual reset required.",
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
                log.info("No qualifying markets found this cycle.")
                return

            sem = asyncio.Semaphore(3)   # Max 3 concurrent market pipelines

            async def _safe(m: PolyMarket) -> bool:
                async with sem:
                    try:
                        return await self._process_market(http, m, clob, bankroll_usd)
                    except Exception as e:
                        log.error("Pipeline error [%s]: %s", m.market_id, e, exc_info=True)
                        await notifier.notify_error(
                            title="Unhandled Pipeline Error",
                            description=f"{m.question[:100]}\n{str(e)[:400]}",
                        )
                        return False

            results = await asyncio.gather(*[_safe(m) for m in markets])
            log.info(
                "Cycle done. Placed: %d / %d candidates.",
                sum(results), len(markets),
            )

    async def run_forever(self, bankroll_usd: float = 200.0) -> None:
        """
        Infinite monitoring loop with:
          • Daily PnL summary at UTC midnight
          • POLL_INTERVAL_SECONDS between cycles
          • Crash recovery with Discord error notification
        """
        await notifier.notify_startup(self.VERSION)
        last_summary: Optional[date] = None

        while True:
            try:
                now = datetime.now(timezone.utc)

                if last_summary != now.date():
                    await notifier.notify_daily_pnl_summary(
                        **self.breaker.get_daily_pnl_summary()
                    )
                    last_summary = now.date()

                await self.run_scan_cycle(bankroll_usd=bankroll_usd)

            except KeyboardInterrupt:
                log.info("Shutdown signal — exiting.")
                break
            except Exception as e:
                log.critical("Main loop error: %s", e, exc_info=True)
                await notifier.notify_error(
                    title="Critical Engine Error",
                    description=str(e)[:800],
                )

            await asyncio.sleep(settings.POLL_INTERVAL_SECONDS)
