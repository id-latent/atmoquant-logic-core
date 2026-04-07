# ==============================================================================
# core/exit_strategy.py — Exit Strategy
# ==============================================================================
"""
AQL Exit Strategy
Monitor open positions dan eksekusi exit jika:
  1. Stop Loss: harga turun X% dari entry → sell untuk batasi kerugian
  2. Take Profit: harga naik X% dari entry → sell untuk lock profit
  3. Expired: market sudah tutup → log untuk resolusi

Catatan: Threshold SL/TP akan di-tune berdasarkan data live.
Default: SL=-50%, TP=+150%
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from config.settings import settings
from core.position_tracker import OpenPosition, PositionTracker

log = logging.getLogger("aql.exit")


# ── Exit Result ───────────────────────────────────────────────────────────────

class ExitReason:
    STOP_LOSS   = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    EXPIRED     = "EXPIRED"
    MANUAL      = "MANUAL"


class ExitResult:
    def __init__(
        self,
        position: OpenPosition,
        reason: str,
        exit_price: float,
        pnl_usd: float,
        success: bool,
        message: str = "",
    ) -> None:
        self.position   = position
        self.reason     = reason
        self.exit_price = exit_price
        self.pnl_usd    = pnl_usd
        self.success    = success
        self.message    = message

    @property
    def is_win(self) -> bool:
        return self.pnl_usd > 0


# ── Exit Strategy Engine ──────────────────────────────────────────────────────

class ExitStrategy:
    """
    Monitor dan eksekusi exit untuk open positions.
    Dipanggil setiap scan cycle oleh engine.
    """

    def __init__(self, tracker: PositionTracker) -> None:
        self._tracker = tracker

    async def check_and_exit(
        self,
        http_client: httpx.AsyncClient,
    ) -> list[ExitResult]:
        """
        Check semua open positions untuk exit conditions.
        Returns list of ExitResult untuk positions yang di-exit.
        """
        results: list[ExitResult] = []

        # 1. Cek expired positions dulu
        expired = self._tracker.get_expired_positions()
        for pos in expired:
            result = self._handle_expired(pos)
            results.append(result)

        # 2. Cek exit candidates (SL/TP)
        if settings.STOP_LOSS_ENABLED:
            candidates = self._tracker.get_exit_candidates()
            for pos in candidates:
                result = await self._execute_exit(http_client, pos)
                if result:
                    results.append(result)

        return results

    def _handle_expired(self, pos: OpenPosition) -> ExitResult:
        """
        Handle posisi yang sudah expired (market tutup).
        Tidak perlu sell — tunggu resolusi dari Polymarket.
        """
        self._tracker.close_position(pos.position_id, "EXPIRED")

        log.info(
            "[Exit] EXPIRED %s | entry=%.4f current=%.4f | PnL: %+.2f",
            pos.position_id, pos.entry_price,
            pos.current_price, pos.unrealized_pnl,
        )

        return ExitResult(
            position=pos,
            reason=ExitReason.EXPIRED,
            exit_price=pos.current_price,
            pnl_usd=0.0,  # PnL nyata ditentukan saat resolusi
            success=True,
            message="Awaiting Polymarket resolution",
        )

    async def _execute_exit(
        self,
        http_client: httpx.AsyncClient,
        pos: OpenPosition,
    ) -> Optional[ExitResult]:
        """
        Eksekusi sell order untuk exit position.
        Returns ExitResult atau None jika gagal.
        """
        from market.gamma_client import CLOBExecutor

        reason = (
            ExitReason.STOP_LOSS
            if pos.should_stop_loss
            else ExitReason.TAKE_PROFIT
        )

        exit_price = pos.current_price

        log.info(
            "[Exit] %s %s | entry=%.4f current=%.4f | target: SL=%.4f TP=%.4f",
            reason, pos.position_id,
            pos.entry_price, pos.current_price,
            pos.stop_loss_price, pos.take_profit_price,
        )

        try:
            clob = CLOBExecutor(http_client)
            receipt = await clob.sell_position(
                token_id=pos.token_id,
                size_usd=pos.size_usd,
                entry_price=pos.entry_price,
                current_price=exit_price,
            )

            if receipt is None:
                log.error(
                    "[Exit] Sell order failed for %s", pos.position_id
                )
                return None

            # Hitung PnL nyata
            contracts   = pos.size_usd / pos.entry_price
            sell_value  = contracts * exit_price
            fee         = sell_value * settings.TRADING_FEE_PCT
            pnl_usd     = round(sell_value - pos.size_usd - fee, 2)

            status = "CLOSED_WIN" if pnl_usd > 0 else "CLOSED_LOSS"
            self._tracker.close_position(pos.position_id, status)

            return ExitResult(
                position=pos,
                reason=reason,
                exit_price=exit_price,
                pnl_usd=pnl_usd,
                success=True,
                message=receipt.get("orderID", ""),
            )

        except Exception as e:
            log.error(
                "[Exit] Exception for %s: %s",
                pos.position_id, str(e),
            )
            return None

    async def update_prices(
        self,
        http_client: httpx.AsyncClient,
    ) -> None:
        """
        Update harga terkini untuk semua open positions.
        Dipanggil setiap scan cycle.
        """
        from market.gamma_client import GammaClient

        open_positions = self._tracker.get_open_positions()
        if not open_positions:
            return

        gamma = GammaClient(http_client)
        log.debug(
            "[Exit] Updating prices for %d positions",
            len(open_positions),
        )

        for pos in open_positions:
            try:
                new_price = await gamma.refresh_market_price(
                    pos.market_id
                )
                if new_price is not None:
                    self._tracker.update_price(
                        pos.position_id, new_price
                    )
            except Exception as e:
                log.debug(
                    "[Exit] Price update failed %s: %s",
                    pos.position_id, str(e),
                )
