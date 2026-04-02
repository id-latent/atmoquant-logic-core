# ══════════════════════════════════════════════════════════════════════════════
# notifier.py = Kode Notifikasi Discord (Professional 4-Channel)
# ══════════════════════════════════════════════════════════════════════════════

from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Optional
import httpx
from config.settings import settings

log = logging.getLogger("aql.notifier")

COLOR_GREEN = 0x2ECC71  # Trades
COLOR_BLUE  = 0x3498DB  # Weather / Startup
COLOR_GOLD  = 0xF1C40F  # Daily PnL
COLOR_RED   = 0xE74C3C  # Alerts / Errors

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def _wrap(embeds: list[dict]) -> dict:
    return {
        "username":   settings.DISCORD_BOT_NAME,
        "avatar_url": settings.DISCORD_AVATAR_URL,
        "embeds":     embeds,
    }

async def _post(payload: dict, target: str = "terminal") -> bool:
    try:
        if target == "trades":
            url = settings.TRADE_WEBHOOK_URL
        elif target == "alerts":
            url = settings.ALERTS_WEBHOOK_URL
        elif target == "weather":
            url = settings.WEATHER_WEBHOOK_URL
        else:
            url = settings.TERMINAL_WEBHOOK_URL

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=10.0)
            return resp.status_code == 204
    except Exception as e:
        log.error(f"Discord delivery failed: {str(e)}")
        return False

# ── CHANNEL: TRADES (No Money Emojis) ────────────────────────────────────────

async def notify_trade_executed(market_name, side, price, size_usd, edge_pct, ev_usd, kelly_fraction, market_url, order_id=None):
    trade_symbol = "📈" if side.lower() == "buy" else "📉"
    fields = [
        {"name": "Action", "value": f"`{side.upper()}`", "inline": True},
        {"name": "Execution Price", "value": f"`{price:.4f}`", "inline": True},
        {"name": "Position Size", "value": f"`${size_usd:.2f}`", "inline": True},
        {"name": "Strategy Edge", "value": f"`{edge_pct*100:.1f}%`", "inline": True},
        {"name": "Expected Value", "value": f"`+${ev_usd:.2f}`", "inline": True},
        {"name": "Kelly Multiplier", "value": f"`{kelly_fraction:.4f}`", "inline": True},
    ]
    if order_id: fields.append({"name": "System ID", "value": f"`{order_id}`", "inline": False})
    fields.append({"name": "📊 Market Source", "value": f"[Verify on Polymarket]({market_url})", "inline": False})

    embed = {
        "color": COLOR_GREEN,
        "title": f"{trade_symbol} ORDER EXECUTED",
        "description": f"**{market_name[:100]}**",
        "fields": fields,
        "footer": {"text": f"AQL NODE • {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="trades")

# ── CHANNEL: WEATHER ─────────────────────────────────────────────────────────

async def notify_consensus_update(location_name, target_date, ecmwf_mean, gfs_mean, noaa_mean, consensus_mean, variance, triple_lock):
    def _bar(val, lo, hi, w=10):
        filled = round((val - lo) / (hi - lo) * w) if hi != lo else w // 2
        return "█" * max(0, min(filled, w)) + "░" * (w - max(0, min(filled, w)))

    lo, hi = min(ecmwf_mean, gfs_mean, noaa_mean), max(ecmwf_mean, gfs_mean, noaa_mean)
    chart = f"
http://googleusercontent.com/immersive_entry_chip/0

---

#### Railway Variables
1.  `TERMINAL_WEBHOOK_URL`
2.  `WEATHER_WEBHOOK_URL`
3.  `ALERTS_WEBHOOK_URL`
4.  `TRADE_WEBHOOK_URL`
