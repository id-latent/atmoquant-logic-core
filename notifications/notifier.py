# ==============================================================================
# notifications/notifier.py — Discord 4-Channel System
# ==============================================================================
"""
AQL Notifier

Channel routing:
  TERMINAL_WEBHOOK_URL → #📊-aql-terminal
  WEATHER_WEBHOOK_URL  → #☁-weather-data
  TRADE_WEBHOOK_URL    → #📈-aql-trades
  ALERTS_WEBHOOK_URL   → #🚨-aql-alerts

Notifikasi baru dari sebelumnya:
  - Startup super detail (tanpa version line)
  - Consensus dengan 4 model + ICON status + Golden Hour
  - Trade dengan semua outcomes evaluated + multiplier breakdown
  - Daily PnL dengan regional + market type breakdown
  - Hourly heartbeat
  - Weekly performance report
  - Big edge alert
  - Volume warning
  - Unknown city alert
  - Position expired
  - Exit executed (SL/TP)
  - Opportunity missed (CB active)
  - Model disagreement alert
  - WARN mode entry
  - Bankroll alerts
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from config.settings import settings

log = logging.getLogger("aql.notifier")

# ── Colors ────────────────────────────────────────────────────────────────────
COLOR_GREEN  = 0x2ECC71   # Trade executed
COLOR_BLUE   = 0x3498DB   # Consensus OK / Info
COLOR_RED    = 0xE74C3C   # Error / CB / Lock failed
COLOR_GOLD   = 0xF1C40F   # Daily PnL positive
COLOR_ORANGE = 0xE67E22   # Warning
COLOR_PURPLE = 0x9B59B6   # Weekly report
COLOR_TEAL   = 0x1ABC9C   # Heartbeat
COLOR_YELLOW = 0xF39C12   # Big edge alert


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _wrap(embeds: list[dict]) -> dict:
    return {
        "username":   settings.DISCORD_BOT_NAME,
        "avatar_url": settings.DISCORD_AVATAR_URL,
        "embeds":     embeds,
    }


async def _post(payload: dict, target: str = "terminal") -> bool:
    url_map = {
        "terminal": settings.TERMINAL_WEBHOOK_URL,
        "weather":  settings.WEATHER_WEBHOOK_URL,
        "trades":   settings.TRADE_WEBHOOK_URL,
        "alerts":   settings.ALERTS_WEBHOOK_URL,
    }
    url = url_map.get(target, settings.TERMINAL_WEBHOOK_URL)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=10.0)
            if resp.status_code == 204:
                return True
            log.warning(
                "Discord non-204 [%s]: %d", target, resp.status_code
            )
            return False
    except Exception as e:
        log.error("Discord send failed [%s]: %s", target, str(e))
        return False


# ── #📊-aql-terminal — Startup ────────────────────────────────────────────────

async def notify_startup(
    bankroll_usd: float,
    registry_stats: dict,
) -> None:
    tier1 = ", ".join(
        c.title() for c in registry_stats.get("tier1_cities", [])[:6]
    )
    regions = registry_stats.get("by_region", {})
    region_str = " · ".join(
        f"{k}: {v}" for k, v in regions.items()
    )

    embed = {
        "color": COLOR_BLUE,
        "title": "🚀 AQL NODE ONLINE",
        "description": (
            "AtmoQuant Logic Engine v2.0.0 has started.\n"
            "Unified temperature market scanner — "
            "Multi-Outcome + Binary + Range."
        ),
        "fields": [
            {
                "name":   "⚙️ ENGINE CONFIGURATION",
                "value":  (
                    f"```\n"
                    f"Models         │ ECMWF · GFS · NOAA · ICON\n"
                    f"Poll Interval  │ {settings.POLL_INTERVAL_SECONDS}s "
                    f"(15 menit)\n"
                    f"Strategy       │ Unified (Multi + Binary + Range)\n"
                    f"Market Cache   │ Every {settings.CACHE_REANALYZE_CYCLES} "
                    f"cycles (30 min)\n"
                    f"```"
                ),
                "inline": False,
            },
            {
                "name":   "💰 RISK PARAMETERS",
                "value":  (
                    f"```\n"
                    f"Kelly Fraction │ {settings.KELLY_FRACTION}× "
                    f"(Quarter Kelly)\n"
                    f"Min Edge T1    │ {settings.MIN_EDGE_TIER1*100:.1f}%\n"
                    f"Min Edge T2    │ {settings.MIN_EDGE_TIER2*100:.1f}%\n"
                    f"Min Edge T3    │ {settings.MIN_EDGE_TIER3*100:.1f}%\n"
                    f"Max Position   │ ${settings.MAX_POSITION_USD:.0f} per trade\n"
                    f"Circuit Breaker│ {settings.CIRCUIT_BREAKER_LOSSES} "
                    f"consecutive losses\n"
                    f"Stop Loss      │ -{settings.STOP_LOSS_PCT*100:.0f}% from entry\n"
                    f"Take Profit    │ +{settings.TAKE_PROFIT_PCT*100:.0f}% from entry\n"
                    f"```"
                ),
                "inline": False,
            },
            {
                "name":   "🌍 COVERAGE",
                "value":  (
                    f"```\n"
                    f"Cities Tracked │ {registry_stats.get('total', 0)}+ cities\n"
                    f"By Region      │ {region_str}\n"
                    f"Tier 1 Cities  │ {tier1}\n"
                    f"```"
                ),
                "inline": False,
            },
            {
                "name":   "⏰ GOLDEN HOUR WINDOWS",
                "value":  (
                    f"```\n"
                    f"US       │ {settings.GOLDEN_HOUR_US[0]}–"
                    f"{settings.GOLDEN_HOUR_US[1]}h before close\n"
                    f"Europe   │ {settings.GOLDEN_HOUR_EUROPE[0]}–"
                    f"{settings.GOLDEN_HOUR_EUROPE[1]}h before close\n"
                    f"Asia     │ {settings.GOLDEN_HOUR_ASIA[0]}–"
                    f"{settings.GOLDEN_HOUR_ASIA[1]}h before close\n"
                    f"```"
                ),
                "inline": False,
            },
            {
                "name":   "🔋 BANKROLL STATUS",
                "value":  (
                    f"```\n"
                    f"Available      │ ${bankroll_usd:.2f}\n"
                    f"Status         │ "
                    f"{'🟢 Healthy' if bankroll_usd >= settings.MINIMUM_BANKROLL_WARNING else '🟡 Low'}\n"
                    f"```"
                ),
                "inline": False,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="terminal")


# ── #📊-aql-terminal — Heartbeat ─────────────────────────────────────────────

async def notify_heartbeat(
    bankroll_usd: float,
    scan_cycle: int,
    open_positions: int,
    today_trades: int,
    today_pnl: float,
    cache_entries: int,
) -> None:
    sign  = "+" if today_pnl >= 0 else ""
    color = COLOR_TEAL

    embed = {
        "color": color,
        "title": "💓 AQL NODE — HEARTBEAT",
        "fields": [
            {
                "name":   "Engine Status",
                "value":  "```\n🟢 Running normally\n```",
                "inline": False,
            },
            {
                "name":   "Scan Cycles",
                "value":  f"`{scan_cycle}`",
                "inline": True,
            },
            {
                "name":   "Open Positions",
                "value":  f"`{open_positions}`",
                "inline": True,
            },
            {
                "name":   "Cache Entries",
                "value":  f"`{cache_entries}`",
                "inline": True,
            },
            {
                "name":   "Today Trades",
                "value":  f"`{today_trades}`",
                "inline": True,
            },
            {
                "name":   "Today PnL",
                "value":  f"`{sign}${today_pnl:.2f}`",
                "inline": True,
            },
            {
                "name":   "Bankroll",
                "value":  f"`${bankroll_usd:.2f}`",
                "inline": True,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="terminal")


# ── #📊-aql-terminal — Daily PnL ─────────────────────────────────────────────

async def notify_daily_pnl_summary(
    total_trades: int,
    total_wins: int,
    win_rate_pct: float,
    total_pnl_usd: float,
    consecutive_losses: int,
    consecutive_rejections: int = 0,
    circuit_breaker: bool = False,
    today_trades: int = 0,
    today_wins: int = 0,
    today_pnl_usd: float = 0.0,
    today_win_rate: float = 0.0,
    today_by_region: dict = None,
    today_by_type: dict = None,
    today_best_trade: str = "",
    today_best_pnl: float = 0.0,
    today_worst_trade: str = "",
    today_worst_pnl: float = 0.0,
    today_avg_edge: float = 0.0,
    today_avg_position: float = 0.0,
    weekly: dict = None,
    **kwargs,
) -> None:
    today_by_region = today_by_region or {}
    today_by_type   = today_by_type   or {}
    weekly          = weekly          or {}

    sign_total  = "+" if total_pnl_usd >= 0 else ""
    sign_today  = "+" if today_pnl_usd >= 0 else ""
    emoji       = "📈" if total_pnl_usd >= 0 else "📉"
    cb_str      = "🔴 ACTIVE — Halted" if circuit_breaker else "🟢 Nominal"
    color       = COLOR_GOLD if total_pnl_usd >= 0 else COLOR_ORANGE

    # Region breakdown
    region_lines = "\n".join(
        f"  {r:<12} │ {'+' if v >= 0 else ''}${v:.2f}"
        for r, v in today_by_region.items()
    ) or "  No data"

    # Market type breakdown
    type_lines = "\n".join(
        f"  {t:<14} │ {'+' if v >= 0 else ''}${v:.2f}"
        for t, v in today_by_type.items()
    ) or "  No data"

    embed = {
        "color": color,
        "title": f"🏆 DAILY PnL SUMMARY  {emoji}",
        "fields": [
            {
                "name":   "📅 TODAY",
                "value":  (
                    f"```\n"
                    f"Trades     │ {today_trades} "
                    f"({today_wins}W / {today_trades - today_wins}L)\n"
                    f"Win Rate   │ {today_win_rate:.1f}%\n"
                    f"PnL        │ {sign_today}${today_pnl_usd:.2f}\n"
                    f"Avg Edge   │ {today_avg_edge*100:.1f}%\n"
                    f"Avg Size   │ ${today_avg_position:.2f}\n"
                    f"```"
                ),
                "inline": False,
            },
            {
                "name":   "🌍 BY REGION TODAY",
                "value":  f"```\n{region_lines}\n```",
                "inline": True,
            },
            {
                "name":   "📊 BY MARKET TYPE",
                "value":  f"```\n{type_lines}\n```",
                "inline": True,
            },
            {
                "name":   "🏅 BEST / WORST",
                "value":  (
                    f"```\n"
                    f"Best  │ {today_best_trade or 'N/A'} "
                    f"(+${today_best_pnl:.2f})\n"
                    f"Worst │ {today_worst_trade or 'N/A'} "
                    f"(${today_worst_pnl:.2f})\n"
                    f"```"
                ),
                "inline": False,
            },
            {
                "name":   "📈 ALL-TIME",
                "value":  (
                    f"```\n"
                    f"Total Trades │ {total_trades}\n"
                    f"Win Rate     │ {win_rate_pct:.1f}%\n"
                    f"Total PnL    │ {sign_total}${total_pnl_usd:.2f}\n"
                    f"Loss Streak  │ {consecutive_losses}\n"
                    f"Rejected     │ {consecutive_rejections}\n"
                    f"```"
                ),
                "inline": True,
            },
            {
                "name":   "📅 THIS WEEK",
                "value":  (
                    f"```\n"
                    f"Trades   │ {weekly.get('trades', 0)}\n"
                    f"Win Rate │ {weekly.get('win_rate', 0):.1f}%\n"
                    f"PnL      │ "
                    f"{'+' if weekly.get('pnl_usd', 0) >= 0 else ''}"
                    f"${weekly.get('pnl_usd', 0):.2f}\n"
                    f"```"
                ),
                "inline": True,
            },
            {
                "name":   "⚡ CIRCUIT BREAKER",
                "value":  f"`{cb_str}`",
                "inline": False,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="terminal")


# ── #📊-aql-terminal — Weekly Report ─────────────────────────────────────────

async def notify_weekly_report(weekly: dict) -> None:
    sign = "+" if weekly.get("pnl_usd", 0) >= 0 else ""
    emoji = "📈" if weekly.get("pnl_usd", 0) >= 0 else "📉"

    embed = {
        "color": COLOR_PURPLE,
        "title": f"📅 WEEKLY PERFORMANCE REPORT  {emoji}",
        "description": "Summary 7 hari terakhir",
        "fields": [
            {
                "name":   "Performance",
                "value":  (
                    f"```\n"
                    f"Total Trades │ {weekly.get('trades', 0)}\n"
                    f"Total Wins   │ {weekly.get('wins', 0)}\n"
                    f"Win Rate     │ {weekly.get('win_rate', 0):.1f}%\n"
                    f"Total PnL    │ {sign}${weekly.get('pnl_usd', 0):.2f}\n"
                    f"```"
                ),
                "inline": False,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="terminal")


# ── #☁-weather-data — Consensus Update ───────────────────────────────────────

async def notify_consensus_update(
    location_name: str,
    target_date: str,
    ecmwf_mean: float,
    gfs_mean: float,
    noaa_mean: float,
    consensus_mean: float,
    variance: float,
    triple_lock: bool,
    icon_mean: Optional[float] = None,
    model_count: int = 3,
    golden_hour_status: str = "OPEN",
    hours_to_close: float = 0.0,
) -> None:
    def _bar(val: float, lo: float, hi: float, w: int = 10) -> str:
        filled = (
            round((val - lo) / (hi - lo) * w)
            if hi != lo else w // 2
        )
        filled = max(0, min(filled, w))
        return "■" * filled + "□" * (w - filled)

    all_vals = [ecmwf_mean, gfs_mean, noaa_mean]
    if icon_mean is not None:
        all_vals.append(icon_mean)
    lo, hi = min(all_vals), max(all_vals)

    icon_line = (
        f"ICON   {_bar(icon_mean, lo, hi)} {icon_mean:.1f}°C\n"
        if icon_mean is not None
        else "ICON   ✗ (degraded — using 3 models)\n"
    )

    chart = (
        f"```\n"
        f"ECMWF  {_bar(ecmwf_mean, lo, hi)} {ecmwf_mean:.1f}°C\n"
        f"GFS    {_bar(gfs_mean,   lo, hi)} {gfs_mean:.1f}°C\n"
        f"NOAA   {_bar(noaa_mean,  lo, hi)} {noaa_mean:.1f}°C\n"
        f"{icon_line}"
        f"```"
    )

    lock_line = (
        "✅ **QUAD-LOCK ACHIEVED**"
        if triple_lock
        else f"❌ **Lock Failed** (Δ={variance:.2f}°C > 1.0°C)"
    )
    color = COLOR_BLUE if triple_lock else COLOR_RED

    # Golden Hour indicator
    gh_emoji = {
        "OPEN": "🟢 OPEN (optimal)",
        "WARN": "🟡 WARN (kurang optimal)",
        "NEAR": "🟠 NEAR (mendekati close)",
        "SKIP": "🔴 SKIP",
    }.get(golden_hour_status, golden_hour_status)

    # Model disagreement alert
    if variance > settings.MODEL_DISAGREE_THRESHOLD:
        await notify_model_disagreement(
            location_name=location_name,
            ecmwf_mean=ecmwf_mean,
            gfs_mean=gfs_mean,
            noaa_mean=noaa_mean,
            icon_mean=icon_mean,
            variance=variance,
        )

    embed = {
        "color": color,
        "title": "🔵 AQL CONSENSUS UPDATE",
        "description": (
            f"**{location_name.upper()}** — {target_date}\n"
            f"{lock_line}"
        ),
        "fields": [
            {
                "name":   "Model Forecasts",
                "value":  chart,
                "inline": False,
            },
            {
                "name":   "Consensus μ",
                "value":  f"`{consensus_mean:.2f}°C`",
                "inline": True,
            },
            {
                "name":   "Inter-Model Δ",
                "value":  f"`±{variance:.3f}°C`",
                "inline": True,
            },
            {
                "name":   "Models Used",
                "value":  f"`{model_count}/4`",
                "inline": True,
            },
            {
                "name":   "⏰ Golden Hour",
                "value":  f"`{gh_emoji}`",
                "inline": True,
            },
            {
                "name":   "Hours to Close",
                "value":  f"`{hours_to_close:.1f}h`",
                "inline": True,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="weather")


# ── #☁-weather-data — Model Disagreement ─────────────────────────────────────

async def notify_model_disagreement(
    location_name: str,
    ecmwf_mean: float,
    gfs_mean: float,
    noaa_mean: float,
    icon_mean: Optional[float],
    variance: float,
) -> None:
    icon_str = f"\n  ICON   │ {icon_mean:.1f}°C" if icon_mean else ""

    embed = {
        "color": COLOR_ORANGE,
        "title": "⚡ MODEL DISAGREEMENT DETECTED",
        "description": (
            f"**{location_name.upper()}** — Variance sangat tinggi!\n"
            f"Kemungkinan ada front cuaca atau anomali atmosfer."
        ),
        "fields": [
            {
                "name":  "Model Readings",
                "value": (
                    f"```\n"
                    f"  ECMWF  │ {ecmwf_mean:.1f}°C\n"
                    f"  GFS    │ {gfs_mean:.1f}°C\n"
                    f"  NOAA   │ {noaa_mean:.1f}°C"
                    f"{icon_str}\n"
                    f"  Δ      │ {variance:.2f}°C  ⚠️\n"
                    f"```"
                ),
                "inline": False,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="weather")


# ── #📈-aql-trades — Trade Executed ──────────────────────────────────────────

async def notify_trade_executed(
    market_name: str,
    side: str,
    outcome_label: str,
    price: float,
    size_usd: float,
    edge_pct: float,
    ev_usd: float,
    kelly_fraction: float,
    confidence_mult: float,
    golden_hour_mult: float,
    volume_mult: float,
    final_mult: float,
    market_url: str,
    order_id: Optional[str],
    all_outcomes: list,
    forecast_outcome: str,
    model_mean_c: float,
    model_std_c: float,
    golden_hour_status: str,
    market_type: str,
) -> None:
    trade_emoji = "📈" if side == "YES" else "📉"

    # Build outcomes table (max 8 outcomes untuk Discord)
    outcomes_lines = []
    for o in sorted(
        all_outcomes,
        key=lambda x: x.get("prob_model", 0),
        reverse=True,
    )[:8]:
        label    = o.get("label", "?")
        p_model  = o.get("prob_model", 0)
        p_market = o.get("market_price", 0)
        net      = o.get("net_edge", 0)
        selected = " ← BOUGHT" if label == outcome_label else ""
        bar      = "■" * int(p_model * 10) + "□" * (10 - int(p_model * 10))
        outcomes_lines.append(
            f"  {label:<10} │ {bar} "
            f"{p_model*100:>4.1f}% vs {p_market*100:>4.1f}% "
            f"│ {'+' if net > 0 else ''}{net*100:.1f}%"
            f"{selected}"
        )
    outcomes_str = "\n".join(outcomes_lines) or "  No outcomes data"

    # WARN mode indicator
    warn_note = ""
    if golden_hour_mult < 1.0:
        warn_note = (
            f"\n⚠️ **WARN MODE** — Golden Hour: {golden_hour_status} "
            f"(Kelly ×{golden_hour_mult:.1f})"
        )

    embed = {
        "color": COLOR_GREEN,
        "title": f"{trade_emoji} ORDER EXECUTED",
        "description": f"**{market_name[:100]}**{warn_note}",
        "fields": [
            {
                "name":   "📋 TRADE DETAILS",
                "value":  (
                    f"```\n"
                    f"Market Type    │ {market_type}\n"
                    f"Action         │ BUY {side}\n"
                    f"Outcome        │ {outcome_label}\n"
                    f"Entry Price    │ {price:.4f} "
                    f"({price*100:.1f}% implied)\n"
                    f"Position Size  │ ${size_usd:.2f}\n"
                    f"Max Profit     │ ${ev_usd:.2f}\n"
                    f"```"
                ),
                "inline": False,
            },
            {
                "name":   "🧮 EDGE ANALYSIS",
                "value":  (
                    f"```\n"
                    f"Strategy Edge  │ {edge_pct*100:.1f}% (net)\n"
                    f"Signal Strength│ "
                    f"{'🟢 STRONG' if edge_pct >= 0.10 else '🟡 MODERATE'}\n"
                    f"```"
                ),
                "inline": True,
            },
            {
                "name":   "🎯 SIZING BREAKDOWN",
                "value":  (
                    f"```\n"
                    f"Kelly Frac     │ {kelly_fraction:.5f}\n"
                    f"Confidence     │ ×{confidence_mult:.3f}\n"
                    f"Golden Hour    │ ×{golden_hour_mult:.2f}\n"
                    f"Volume         │ ×{volume_mult:.2f}\n"
                    f"Final Mult     │ ×{final_mult:.4f}\n"
                    f"```"
                ),
                "inline": True,
            },
            {
                "name":   "🌡️ FORECAST",
                "value":  (
                    f"```\n"
                    f"Model Mean     │ {model_mean_c:.2f}°C\n"
                    f"Uncertainty σ  │ ±{model_std_c:.2f}°C\n"
                    f"Best Forecast  │ {forecast_outcome}\n"
                    f"```"
                ),
                "inline": False,
            },
            {
                "name":   "📊 ALL OUTCOMES EVALUATED",
                "value":  f"```\n{outcomes_str}\n```",
                "inline": False,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if order_id:
        embed["fields"].append({
            "name":   "System ID",
            "value":  f"`{order_id}`",
            "inline": True,
        })
    if market_url:
        embed["fields"].append({
            "name":   "🌐 Market",
            "value":  f"[Verify on Polymarket]({market_url})",
            "inline": True,
        })

    await _post(_wrap([embed]), target="trades")


# ── #📈-aql-trades — Big Edge Alert ──────────────────────────────────────────

async def notify_big_edge(
    market_question: str,
    outcome_label: str,
    edge_pct: float,
    model_prob: float,
    market_price: float,
    city: str,
) -> None:
    embed = {
        "color": COLOR_YELLOW,
        "title": "🔥 MASSIVE EDGE DETECTED",
        "description": (
            f"Edge **{edge_pct*100:.1f}%** — "
            f"jauh di atas minimum threshold!"
        ),
        "fields": [
            {
                "name":   "Market",
                "value":  f"`{market_question[:100]}`",
                "inline": False,
            },
            {
                "name":   "City",
                "value":  f"`{city}`",
                "inline": True,
            },
            {
                "name":   "Outcome",
                "value":  f"`{outcome_label}`",
                "inline": True,
            },
            {
                "name":   "Model Prob",
                "value":  f"`{model_prob*100:.1f}%`",
                "inline": True,
            },
            {
                "name":   "Market Price",
                "value":  f"`{market_price*100:.1f}%`",
                "inline": True,
            },
            {
                "name":   "Net Edge",
                "value":  f"**`{edge_pct*100:.1f}%`**",
                "inline": True,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="trades")


# ── #📈-aql-trades — Exit Executed ───────────────────────────────────────────

async def notify_exit_executed(
    position,
    reason: str,
    exit_price: float,
    pnl_usd: float,
) -> None:
    is_win   = pnl_usd > 0
    color    = COLOR_GREEN if is_win else COLOR_RED
    emoji    = "✅ TAKE PROFIT" if reason == "TAKE_PROFIT" else "🛑 STOP LOSS"
    sign     = "+" if pnl_usd >= 0 else ""
    pnl_emoji = "📈" if is_win else "📉"

    embed = {
        "color": color,
        "title": f"{emoji} — POSITION CLOSED {pnl_emoji}",
        "fields": [
            {
                "name":   "Position",
                "value":  f"`{position.position_id}`",
                "inline": False,
            },
            {
                "name":   "Outcome",
                "value":  f"`{position.outcome_label}`",
                "inline": True,
            },
            {
                "name":   "Entry Price",
                "value":  f"`{position.entry_price:.4f}`",
                "inline": True,
            },
            {
                "name":   "Exit Price",
                "value":  f"`{exit_price:.4f}`",
                "inline": True,
            },
            {
                "name":   "PnL",
                "value":  f"**`{sign}${pnl_usd:.2f}`**",
                "inline": True,
            },
            {
                "name":   "Size",
                "value":  f"`${position.size_usd:.2f}`",
                "inline": True,
            },
            {
                "name":   "Held For",
                "value":  (
                    f"`{position.hours_to_expiry:.1f}h remaining`"
                ),
                "inline": True,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="trades")


# ── #📊-aql-terminal — Position Expired ──────────────────────────────────────

async def notify_position_expired(position) -> None:
    embed = {
        "color": COLOR_BLUE,
        "title": "⏳ POSITION EXPIRED — Awaiting Resolution",
        "fields": [
            {
                "name":   "Position",
                "value":  f"`{position.position_id}`",
                "inline": False,
            },
            {
                "name":   "Outcome",
                "value":  f"`{position.outcome_label}`",
                "inline": True,
            },
            {
                "name":   "Entry Price",
                "value":  f"`{position.entry_price:.4f}`",
                "inline": True,
            },
            {
                "name":   "Last Price",
                "value":  f"`{position.current_price:.4f}`",
                "inline": True,
            },
            {
                "name":   "Unrealized PnL",
                "value":  (
                    f"`{'+' if position.unrealized_pnl >= 0 else ''}"
                    f"${position.unrealized_pnl:.2f}`"
                ),
                "inline": True,
            },
            {
                "name":   "Size",
                "value":  f"`${position.size_usd:.2f}`",
                "inline": True,
            },
            {
                "name":   "Status",
                "value":  "`Awaiting Polymarket resolution`",
                "inline": True,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="terminal")


# ── #🚨-aql-alerts — Error / Circuit Breaker ─────────────────────────────────

async def notify_error(
    title: str,
    description: str,
    is_circuit_breaker: bool = False,
) -> None:
    if is_circuit_breaker:
        header = "⚡ CIRCUIT BREAKER TRIPPED"
        color  = COLOR_RED
    elif any(w in title for w in ["⚠️", "Rendah", "Kecil", "Warning"]):
        header = title
        color  = COLOR_ORANGE
    else:
        header = "🔴 SYSTEM ALERT"
        color  = COLOR_RED

    embed = {
        "color": color,
        "title": header,
        "description": f"**{title}**\n\n{description[:1800]}",
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="alerts")


# ── #🚨-aql-alerts — Unknown City ────────────────────────────────────────────

async def notify_unknown_city(market) -> None:
    embed = {
        "color": COLOR_ORANGE,
        "title": "🗺️ UNKNOWN CITY — Peluang Terlewat!",
        "description": (
            "Temperature market ditemukan tapi kota tidak dikenal."
        ),
        "fields": [
            {
                "name":   "Market",
                "value":  f"```\n{market.question[:200]}\n```",
                "inline": False,
            },
            {
                "name":   "Liquidity",
                "value":  f"`${market.liquidity_usd:,.0f}`",
                "inline": True,
            },
            {
                "name":   "Hours to Close",
                "value":  f"`{market.htc:.1f}h`",
                "inline": True,
            },
            {
                "name":   "Type",
                "value":  f"`{market.market_type}`",
                "inline": True,
            },
            {
                "name":   "Fix",
                "value":  (
                    "Tambahkan ke `LOCATION_REGISTRY` "
                    "di `core/location_registry.py`:\n"
                    '```python\n"nama kota": '
                    "(lat, lon, tz, region, unit, tier)\n```"
                ),
                "inline": False,
            },
            {
                "name":   "🔗 Market",
                "value":  f"[Open on Polymarket]({market.url})",
                "inline": False,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="alerts")


# ── #🚨-aql-alerts — Volume Warning ──────────────────────────────────────────

async def notify_volume_warning(
    market_question: str,
    city: str,
    warning_message: str,
    spike_magnitude: float,
) -> None:
    embed = {
        "color": COLOR_ORANGE,
        "title": "📊 VOLUME SPIKE WARNING",
        "description": warning_message,
        "fields": [
            {
                "name":   "City",
                "value":  f"`{city}`",
                "inline": True,
            },
            {
                "name":   "Spike",
                "value":  f"`{spike_magnitude:.1f}×` dari normal",
                "inline": True,
            },
            {
                "name":   "Market",
                "value":  f"`{market_question[:80]}`",
                "inline": False,
            },
            {
                "name":   "Action",
                "value":  (
                    "Kelly dikurangi "
                    f"`×{settings.VOLUME_KELLY_REDUCTION:.1f}` "
                    "sebagai tindakan kehati-hatian."
                ),
                "inline": False,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="alerts")


# ── #🚨-aql-alerts — Opportunity Missed ──────────────────────────────────────

async def notify_opportunity_missed(
    market_question: str,
    outcome_label: str,
    edge_pct: float,
    reason: str,
) -> None:
    embed = {
        "color": COLOR_RED,
        "title": "❌ MISSED OPPORTUNITY",
        "description": f"Trade tidak bisa dieksekusi karena: **{reason}**",
        "fields": [
            {
                "name":   "Market",
                "value":  f"`{market_question[:100]}`",
                "inline": False,
            },
            {
                "name":   "Outcome",
                "value":  f"`{outcome_label}`",
                "inline": True,
            },
            {
                "name":   "Edge",
                "value":  f"`{edge_pct*100:.1f}%`",
                "inline": True,
            },
            {
                "name":   "Reason",
                "value":  f"`{reason}`",
                "inline": True,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="alerts")
