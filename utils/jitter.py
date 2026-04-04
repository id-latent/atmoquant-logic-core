# ==============================================================================
# jitter.py = Human-Like Request Delay
# ==============================================================================
"""
AQL Jitter Utility
Simulasi human-like latency sebelum setiap API request
untuk menghindari deteksi bot oleh Polymarket.
"""
from __future__ import annotations

import asyncio
import random
import logging

log = logging.getLogger("aql.jitter")


async def human_delay(
    min_ms: int = 300,
    max_ms: int = 1200,
    thinking_pause_chance: float = 0.05,
) -> None:
    """
    Tambahkan random delay sebelum request.

    Args:
        min_ms:                 Minimum delay dalam milidetik.
        max_ms:                 Maximum delay dalam milidetik.
        thinking_pause_chance:  Probabilitas 'thinking pause' ekstra.
                                Default 5% chance delay tambahan 2-5 detik.
    """
    delay_ms = random.randint(min_ms, max_ms)

    # Sesekali ada pause lebih lama seperti manusia yang berpikir
    if random.random() < thinking_pause_chance:
        extra_ms = random.randint(2000, 5000)
        delay_ms += extra_ms
        log.debug(
            "[Jitter] Thinking pause +%dms (total %dms)",
            extra_ms, delay_ms,
        )

    await asyncio.sleep(delay_ms / 1000)


async def order_delay() -> None:
    """
    Delay khusus sebelum submit order.
    Lebih panjang dari request biasa — 500ms hingga 1.5 detik.
    """
    await human_delay(min_ms=500, max_ms=1500)


async def pagination_delay() -> None:
    """
    Delay antar halaman pagination Gamma API.
    Lebih pendek — 200ms hingga 600ms.
    """
    await human_delay(min_ms=200, max_ms=600)
