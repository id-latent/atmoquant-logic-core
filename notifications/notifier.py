# ══════════════════════════════════════════════════════════════════════════════
# notifier.py = Kode Notifikasi Discord
# ══════════════════════════════════════════════════════════════════════════════

"""
AQL Notifier — Discord Webhook Integration
Bot identity: "AQL NODE"

Color legend:
  GREEN  0x2ECC71 — Trade Executed
  BLUE   0x3498DB — Consensus Update  (RED if Triple-Lock failed)
  GOLD   0xF1C40F — Daily PnL Summary
  RED    0xE74C3C — System Error / Circuit Breaker
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from config.settings import settings

log = logging.getLogger("aql.notifier")

COLOR_GREEN = 0x2ECC71
COLOR_BLUE  = 0x3498DB
COLOR_GOLD  = 0xF1C40F
COLOR_RED   = 0xE74C3C


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _wrap(embeds: list[dict]) -> dict:
    return {
        "username":   settings.DISCORD_BOT_NAME,
        "avatar_url": settings.DISCORD_AVATAR_URL,
        "embeds":     embeds,
    }


async def _post(payload: dict) -> bool:
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                settings.DISCORD_WEBHOOK_URL,
                json=payload,
                timeout=10.0,
            )
            if resp.status_code == 204:
                return True
            log.warning("Discord non-204: %d %s", resp.status_code, resp.text[:100])
            return False
    except Exception as e:
        log.error("Discord delivery failed: %s", str(e))
        return False


# ── GREEN — Trade Executed ────────────────────────────────────────────────────

async def notify_trade_executed(
    market_name: str,
    side: str,
    price: float,
    size_usd: float,
    edge_pct: float,
    ev_usd: float,
    kelly_fraction: float,
    market_url: str,
    order_id: Optional[str] = None,
) -> None:
    fields = [
        {"name": "Side",           "value": f"`{side}`",               "inline": True},
        {"name": "Entry Price",    "value": f"`{price:.4f}`",          "inline": True},
        {"name": "Size",           "value": f"`${size_usd:.2f}`",      "inline": True},
        {"name": "Net Edge",       "value": f"`{edge_pct*100:.1f}%`",  "inline": True},
        {"name": "Expected Value", "value": f"`+${ev_usd:.2f}`",       "inline": True},
        {"name": "Kelly Fraction", "value": f"`{kelly_fraction:.4f}`", "inline": True},
    ]
    if order_id:
        fields.append({"name": "Order ID",     "value": f"`{order_id}`",                        "inline": False})
    if market_url:
        fields.append({"name": "🔗 Market",    "value": f"[Open on Polymarket]({market_url})",  "inline": False})

    embed = {
        "color":       COLOR_GREEN,
        "title":       "🟢 TRADE EXECUTED",
        "description": f"**{market_name[:100]}**",
        "fields":      fields,
        "footer":      {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]))
    log.info("[Discord] Trade executed.")


# ── BLUE — Consensus Update ───────────────────────────────────────────────────

async def notify_consensus_update(
    location_name: str,
    target_date: str,
    ecmwf_mean: float,
    gfs_mean: float,
    noaa_mean: float,
    consensus_mean: float,
    variance: float,
    triple_lock: bool,
) -> None:
    def _bar(val: float, lo: float, hi: float, w: int = 10) -> str:
        filled = round((val - lo) / (hi - lo) * w) if hi != lo else w // 2
        filled = max(0, min(filled, w))
        return "█" * filled + "░" * (w - filled)

    lo, hi = min(ecmwf_mean, gfs_mean, noaa_mean), max(ecmwf_mean, gfs_mean, noaa_mean)
    chart = (
        f"```\n"
        f"ECMWF  {_bar(ecmwf_mean, lo, hi)} {ecmwf_mean:.1f}°C\n"
        f"GFS    {_bar(gfs_mean,   lo, hi)} {gfs_mean:.1f}°C\n"
        f"NOAA   {_bar(noaa_mean,  lo, hi)} {noaa_mean:.1f}°C\n"
        f"```"
    )

    lock_line  = "✅ TRIPLE LOCK ACHIEVED" if triple_lock else f"❌ Lock Failed (Δ={variance:.2f}°C)"
    lock_color = COLOR_BLUE if triple_lock else COLOR_RED

    embed = {
        "color":       lock_color,
        "title":       "🔵 AQL CONSENSUS UPDATE",
        "description": f"**{location_name}** — {target_date}\n{lock_line}",
        "fields": [
            {"name": "Model Forecasts", "value": chart,                         "inline": False},
            {"name": "Consensus μ",     "value": f"`{consensus_mean:.2f}°C`",   "inline": True},
            {"name": "Inter-Model Δ",   "value": f"`{variance:.3f}°C`",         "inline": True},
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]))
    log.info("[Discord] Consensus update sent — lock=%s", triple_lock)


# ── GOLD — Daily PnL Summary ──────────────────────────────────────────────────

async def notify_daily_pnl_summary(
    total_trades: int,
    total_wins: int,
    win_rate_pct: float,
    total_pnl_usd: float,
    consecutive_losses: int,
    circuit_breaker: bool,
) -> None:
    sign     = "+" if total_pnl_usd >= 0 else ""
    emoji    = "📈" if total_pnl_usd >= 0 else "📉"
    cb_str   = "🔴 ACTIVE — Halted" if circuit_breaker else "🟢 Nominal"

    embed = {
        "color":  COLOR_GOLD,
        "title":  f"🏆 DAILY PnL SUMMARY  {emoji}",
        "fields": [
            {"name": "Trades",    "value": f"`{total_trades}`",              "inline": True},
            {"name": "Win Rate",  "value": f"`{win_rate_pct:.1f}%`",         "inline": True},
            {"name": "Total PnL", "value": f"`{sign}${total_pnl_usd:.2f}`",  "inline": True},
            {"name": "Loss Streak","value": f"`{consecutive_losses}`",       "inline": True},
            {"name": "Breaker",   "value": cb_str,                           "inline": True},
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]))
    log.info("[Discord] Daily PnL summary sent.")


# ── RED — Error / Circuit Breaker ─────────────────────────────────────────────

async def notify_error(
    title: str,
    description: str,
    is_circuit_breaker: bool = False,
) -> None:
    header = "⚡ CIRCUIT BREAKER TRIPPED" if is_circuit_breaker else "🔴 SYSTEM ERROR"
    embed  = {
        "color":       COLOR_RED,
        "title":       header,
        "description": f"**{title}**\n\n{description[:1800]}",
        "footer":      {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]))
    log.error("[Discord] Error embed sent: %s", title)


# ── Startup Heartbeat ─────────────────────────────────────────────────────────

async def notify_startup(version: str = "1.0.0") -> None:
    embed = {
        "color":       COLOR_BLUE,
        "title":       "🚀 AQL NODE ONLINE",
        "description": (
            "AtmoQuant Logic Engine has started.\n"
            "Scanning Polymarket temperature contracts via Triple-Lock consensus."
        ),
        "fields": [
            {"name": "Version",       "value": f"`{version}`",                               "inline": True},
            {"name": "Models",        "value": "`ECMWF | GFS | NOAA`",                      "inline": True},
            {"name": "Poll",          "value": f"`{settings.POLL_INTERVAL_SECONDS}s`",       "inline": True},
            {"name": "Min Edge",      "value": f"`{settings.MIN_EDGE_PCT*100:.1f}%`",         "inline": True},
            {"name": "Kelly Frac",    "value": f"`{settings.KELLY_FRACTION}×`",               "inline": True},
            {"name": "Breaker Limit", "value": f"`{settings.CIRCUIT_BREAKER_LOSSES} losses`","inline": True},
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]))

