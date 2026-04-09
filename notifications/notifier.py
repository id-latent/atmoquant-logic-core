# ==============================================================================
# notifications/notifier.py — Sistem Discord 4 Channel
# ==============================================================================
"""
AQL Notifier (FIXED)

Fixes:
  NOTIFIER-1: lock_line di notify_consensus_update hardcode "1.0°C"
              → sekarang pakai settings.TRIPLE_LOCK_VARIANCE_C
  NOTIFIER-2: Label "Selisih Antar Model Δ" diganti "σ Antar Model (std dev)"
              karena setelah fix consensus.py, nilai ini adalah std dev
              bukan range (max-min)

Routing channel:
  TERMINAL_WEBHOOK_URL → #📊-aql-terminal   (startup, heartbeat, PnL)
  WEATHER_WEBHOOK_URL  → #☁-weather-data    (consensus model cuaca)
  TRADE_WEBHOOK_URL    → #📈-aql-trades     (eksekusi order)
  ALERTS_WEBHOOK_URL   → #🚨-aql-alerts     (error, circuit breaker, unknown city)

Perbaikan dari versi sebelumnya:
  - HTTP client singleton (satu pool koneksi untuk semua notif, cegah memory leak)
  - Rate limit Discord 429 ditangani dengan backoff, tidak spam retry
  - Unknown city dedup: market yang sama hanya dikirim 1x per jam
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from config.settings import settings

log = logging.getLogger("aql.notifier")

# ── Warna Embed Discord ────────────────────────────────────────────────────────
COLOR_GREEN  = 0x2ECC71   # Trade berhasil dieksekusi
COLOR_BLUE   = 0x3498DB   # Consensus OK / Info
COLOR_RED    = 0xE74C3C   # Error / Circuit Breaker / Lock gagal
COLOR_GOLD   = 0xF1C40F   # PnL harian positif
COLOR_ORANGE = 0xE67E22   # Peringatan (warning)
COLOR_PURPLE = 0x9B59B6   # Laporan mingguan
COLOR_TEAL   = 0x1ABC9C   # Heartbeat
COLOR_YELLOW = 0xF39C12   # Alert big edge


# ── Singleton HTTP Client ──────────────────────────────────────────────────────
# PENTING: Jangan buat AsyncClient baru setiap notifikasi!
# Jika ada 50 unknown_city per scan, 50 connection pool akan terbuka sekaligus
# → RAM habis → container mati. Solusi: satu client shared untuk semua.
_http_client: Optional[httpx.AsyncClient] = None


def _get_http() -> httpx.AsyncClient:
    """Kembalikan satu AsyncClient yang dipakai bersama. Buat baru jika belum ada."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=5,           # maksimal 5 koneksi bersamaan ke Discord
                max_keepalive_connections=2, # simpan 2 koneksi idle agar cepat
                keepalive_expiry=30,         # tutup koneksi idle setelah 30 detik
            ),
            timeout=10.0,
        )
    return _http_client


async def close_http_client() -> None:
    """
    Tutup koneksi HTTP dengan bersih saat shutdown.
    Panggil di engine.py pada SIGTERM/finally block:
      from notifications.notifier import close_http_client
      await close_http_client()
    """
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


# ── Dedup Unknown City ─────────────────────────────────────────────────────────
# Menyimpan {market_id: waktu_terakhir_dikirim} agar alert yang sama
# tidak dikirim berkali-kali dalam satu jam. Dict dibersihkan otomatis.
_unknown_city_seen: dict[str, float] = {}
_UNKNOWN_CITY_TTL  = 3600.0  # 1 jam dalam detik


# ── Helper Internal ────────────────────────────────────────────────────────────

def _ts() -> str:
    """Timestamp sekarang dalam format 'YYYY-MM-DD HH:MM UTC'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _wrap(embeds: list[dict]) -> dict:
    """Bungkus embed dalam payload Discord webhook standar."""
    return {
        "username":   settings.DISCORD_BOT_NAME,
        "avatar_url": settings.DISCORD_AVATAR_URL,
        "embeds":     embeds,
    }


async def _post(payload: dict, target: str = "terminal") -> bool:
    """
    Kirim payload ke channel Discord yang ditentukan.

    target bisa: "terminal", "weather", "trades", "alerts"

    Penanganan khusus:
      - HTTP 204 = berhasil
      - HTTP 429 = Discord rate limit → tunggu Retry-After, jangan spam
      - Error lain → log warning/error, kembalikan False
    """
    url_map = {
        "terminal": settings.TERMINAL_WEBHOOK_URL,
        "weather":  settings.WEATHER_WEBHOOK_URL,
        "trades":   settings.TRADE_WEBHOOK_URL,
        "alerts":   settings.ALERTS_WEBHOOK_URL,
    }
    url    = url_map.get(target, settings.TERMINAL_WEBHOOK_URL)
    client = _get_http()  # pakai client singleton, bukan buat baru

    try:
        resp = await client.post(url, json=payload, timeout=10.0)

        if resp.status_code == 204:
            return True  # Discord mengembalikan 204 (No Content) untuk sukses

        if resp.status_code == 429:
            # Discord sedang membatasi request — tunggu sesuai instruksinya
            retry_after = float(resp.headers.get("Retry-After", "5"))
            log.warning(
                "Discord rate limit [%s] — tunggu %.1f detik", target, retry_after
            )
            await asyncio.sleep(min(retry_after, 30.0))  # maksimal tunggu 30 detik
            return False

        log.warning("Discord non-204 [%s]: status=%d", target, resp.status_code)
        return False

    except httpx.TimeoutException:
        log.warning("Discord timeout [%s]", target)
        return False
    except Exception as e:
        log.error("Discord kirim gagal [%s]: %s", target, str(e))
        return False


# ==============================================================================
# ── #📊-aql-terminal — STARTUP ────────────────────────────────────────────────
# ==============================================================================

async def notify_startup(
    bankroll_usd: float,
    registry_stats: dict,
) -> None:
    """
    Kirim notifikasi startup lengkap ke #📊-aql-terminal.
    Berisi konfigurasi engine, parameter risiko, coverage kota, dan status bankroll.
    """
    tier1 = ", ".join(
        c.title() for c in registry_stats.get("tier1_cities", [])[:6]
    )
    regions    = registry_stats.get("by_region", {})
    region_str = " · ".join(f"{k}: {v}" for k, v in regions.items())

    embed = {
        "color": COLOR_BLUE,
        "title": "🚀 AQL NODE ONLINE",
        "description": (
            "AtmoQuant Logic Engine v2.0.0 telah berjalan.\n"
            "Scanner market suhu terpadu — "
            "Multi-Outcome + Binary + Range."
        ),
        "fields": [
            {
                "name":   "⚙️ KONFIGURASI ENGINE",
                "value":  (
                    f"```\n"
                    f"Model          │ ECMWF · GFS · NOAA · ICON\n"
                    f"Interval Poll  │ {settings.POLL_INTERVAL_SECONDS}s "
                    f"(15 menit)\n"
                    f"Strategi       │ Terpadu (Multi + Binary + Range)\n"
                    f"Cache Market   │ Setiap {settings.CACHE_REANALYZE_CYCLES} "
                    f"siklus (30 menit)\n"
                    f"```"
                ),
                "inline": False,
            },
            {
                "name":   "💰 PARAMETER RISIKO",
                "value":  (
                    f"```\n"
                    f"Kelly Fraction │ {settings.KELLY_FRACTION}× "
                    f"(Quarter Kelly)\n"
                    f"Min Edge T1    │ {settings.MIN_EDGE_TIER1*100:.1f}%\n"
                    f"Min Edge T2    │ {settings.MIN_EDGE_TIER2*100:.1f}%\n"
                    f"Min Edge T3    │ {settings.MIN_EDGE_TIER3*100:.1f}%\n"
                    f"Max Posisi     │ ${settings.MAX_POSITION_USD:.0f} per trade\n"
                    f"Circuit Breaker│ {settings.CIRCUIT_BREAKER_LOSSES} "
                    f"kekalahan beruntun\n"
                    f"Stop Loss      │ -{settings.STOP_LOSS_PCT*100:.0f}% dari entry\n"
                    f"Take Profit    │ +{settings.TAKE_PROFIT_PCT*100:.0f}% dari entry\n"
                    f"```"
                ),
                "inline": False,
            },
            {
                "name":   "🌍 CAKUPAN KOTA",
                "value":  (
                    f"```\n"
                    f"Kota Dilacak   │ {registry_stats.get('total', 0)}+ kota\n"
                    f"Per Region     │ {region_str}\n"
                    f"Kota Tier 1    │ {tier1}\n"
                    f"```"
                ),
                "inline": False,
            },
            {
                "name":   "⏰ JENDELA GOLDEN HOUR",
                "value":  (
                    f"```\n"
                    f"US       │ {settings.GOLDEN_HOUR_US[0]}–"
                    f"{settings.GOLDEN_HOUR_US[1]}h sebelum tutup\n"
                    f"Eropa    │ {settings.GOLDEN_HOUR_EUROPE[0]}–"
                    f"{settings.GOLDEN_HOUR_EUROPE[1]}h sebelum tutup\n"
                    f"Asia     │ {settings.GOLDEN_HOUR_ASIA[0]}–"
                    f"{settings.GOLDEN_HOUR_ASIA[1]}h sebelum tutup\n"
                    f"```"
                ),
                "inline": False,
            },
            {
                "name":   "🔋 STATUS BANKROLL",
                "value":  (
                    f"```\n"
                    f"Tersedia       │ ${bankroll_usd:.2f}\n"
                    f"Status         │ "
                    f"{'🟢 Sehat' if bankroll_usd >= settings.MINIMUM_BANKROLL_WARNING else '🟡 Rendah'}\n"
                    f"```"
                ),
                "inline": False,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="terminal")


# ==============================================================================
# ── #📊-aql-terminal — HEARTBEAT ──────────────────────────────────────────────
# ==============================================================================

async def notify_heartbeat(
    bankroll_usd: float,
    scan_cycle: int,
    open_positions: int,
    today_trades: int,
    today_pnl: float,
    cache_entries: int,
) -> None:
    """
    Kirim heartbeat berkala ke #📊-aql-terminal (setiap ~1 jam).
    Berguna untuk memastikan bot masih hidup tanpa harus buka Railway.
    """
    sign  = "+" if today_pnl >= 0 else ""
    color = COLOR_TEAL

    embed = {
        "color": color,
        "title": "💓 AQL NODE — HEARTBEAT",
        "fields": [
            {
                "name":   "Status Engine",
                "value":  "```\n🟢 Berjalan normal\n```",
                "inline": False,
            },
            {
                "name":   "Siklus Scan",
                "value":  f"`{scan_cycle}`",
                "inline": True,
            },
            {
                "name":   "Posisi Terbuka",
                "value":  f"`{open_positions}`",
                "inline": True,
            },
            {
                "name":   "Entri Cache",
                "value":  f"`{cache_entries}`",
                "inline": True,
            },
            {
                "name":   "Trade Hari Ini",
                "value":  f"`{today_trades}`",
                "inline": True,
            },
            {
                "name":   "PnL Hari Ini",
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


# ==============================================================================
# ── #📊-aql-terminal — DAILY PnL SUMMARY ──────────────────────────────────────
# ==============================================================================

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
    """
    Kirim ringkasan PnL harian ke #📊-aql-terminal.
    Berisi breakdown per region, per jenis market, best/worst trade, dan all-time stats.
    """
    today_by_region = today_by_region or {}
    today_by_type   = today_by_type   or {}
    weekly          = weekly          or {}

    sign_total  = "+" if total_pnl_usd >= 0 else ""
    sign_today  = "+" if today_pnl_usd >= 0 else ""
    emoji       = "📈" if total_pnl_usd >= 0 else "📉"
    cb_str      = "🔴 AKTIF — Bot Dihentikan" if circuit_breaker else "🟢 Normal"
    color       = COLOR_GOLD if total_pnl_usd >= 0 else COLOR_ORANGE

    # Breakdown PnL per region
    region_lines = "\n".join(
        f"  {r:<12} │ {'+' if v >= 0 else ''}${v:.2f}"
        for r, v in today_by_region.items()
    ) or "  Tidak ada data"

    # Breakdown PnL per jenis market
    type_lines = "\n".join(
        f"  {t:<14} │ {'+' if v >= 0 else ''}${v:.2f}"
        for t, v in today_by_type.items()
    ) or "  Tidak ada data"

    embed = {
        "color": color,
        "title": f"🏆 RINGKASAN PnL HARIAN  {emoji}",
        "fields": [
            {
                "name":   "📅 HARI INI",
                "value":  (
                    f"```\n"
                    f"Trade      │ {today_trades} "
                    f"({today_wins}M / {today_trades - today_wins}K)\n"
                    f"Win Rate   │ {today_win_rate:.1f}%\n"
                    f"PnL        │ {sign_today}${today_pnl_usd:.2f}\n"
                    f"Rata Edge  │ {today_avg_edge*100:.1f}%\n"
                    f"Rata Posisi│ ${today_avg_position:.2f}\n"
                    f"```"
                ),
                "inline": False,
            },
            {
                "name":   "🌍 PER REGION HARI INI",
                "value":  f"```\n{region_lines}\n```",
                "inline": True,
            },
            {
                "name":   "📊 PER JENIS MARKET",
                "value":  f"```\n{type_lines}\n```",
                "inline": True,
            },
            {
                "name":   "🏅 TERBAIK / TERBURUK",
                "value":  (
                    f"```\n"
                    f"Terbaik │ {today_best_trade or 'N/A'} "
                    f"(+${today_best_pnl:.2f})\n"
                    f"Terburuk│ {today_worst_trade or 'N/A'} "
                    f"(${today_worst_pnl:.2f})\n"
                    f"```"
                ),
                "inline": False,
            },
            {
                "name":   "📈 ALL-TIME",
                "value":  (
                    f"```\n"
                    f"Total Trade   │ {total_trades}\n"
                    f"Win Rate      │ {win_rate_pct:.1f}%\n"
                    f"Total PnL     │ {sign_total}${total_pnl_usd:.2f}\n"
                    f"Kalah Beruntun│ {consecutive_losses}\n"
                    f"Ditolak       │ {consecutive_rejections}\n"
                    f"```"
                ),
                "inline": True,
            },
            {
                "name":   "📅 MINGGU INI",
                "value":  (
                    f"```\n"
                    f"Trade    │ {weekly.get('trades', 0)}\n"
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


# ==============================================================================
# ── #📊-aql-terminal — WEEKLY REPORT ──────────────────────────────────────────
# ==============================================================================

async def notify_weekly_report(weekly: dict) -> None:
    """Kirim laporan performa 7 hari terakhir ke #📊-aql-terminal."""
    sign  = "+" if weekly.get("pnl_usd", 0) >= 0 else ""
    emoji = "📈" if weekly.get("pnl_usd", 0) >= 0 else "📉"

    embed = {
        "color": COLOR_PURPLE,
        "title": f"📅 LAPORAN PERFORMA MINGGUAN  {emoji}",
        "description": "Ringkasan 7 hari terakhir",
        "fields": [
            {
                "name":   "Performa",
                "value":  (
                    f"```\n"
                    f"Total Trade │ {weekly.get('trades', 0)}\n"
                    f"Total Menang│ {weekly.get('wins', 0)}\n"
                    f"Win Rate    │ {weekly.get('win_rate', 0):.1f}%\n"
                    f"Total PnL   │ {sign}${weekly.get('pnl_usd', 0):.2f}\n"
                    f"```"
                ),
                "inline": False,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="terminal")


# ==============================================================================
# ── #☁-weather-data — CONSENSUS UPDATE ───────────────────────────────────────
# ==============================================================================

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
    """
    Kirim hasil consensus 4 model cuaca ke #☁-weather-data.

    Bar chart ■□ menunjukkan posisi relatif tiap model dalam rentang min-max.
    Jika variance > threshold, otomatis trigger notify_model_disagreement().
    """
    def _bar(val: float, lo: float, hi: float, w: int = 10) -> str:
        """Buat bar chart ■□ untuk visualisasi suhu model."""
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

    # Baris ICON: tampilkan nilainya jika tersedia, atau tandai degraded
    icon_line = (
        f"ICON   {_bar(icon_mean, lo, hi)} {icon_mean:.1f}°C\n"
        if icon_mean is not None
        else "ICON   ✗ (degraded — menggunakan 3 model)\n"
    )

    chart = (
        f"```\n"
        f"ECMWF  {_bar(ecmwf_mean, lo, hi)} {ecmwf_mean:.1f}°C\n"
        f"GFS    {_bar(gfs_mean,   lo, hi)} {gfs_mean:.1f}°C\n"
        f"NOAA   {_bar(noaa_mean,  lo, hi)} {noaa_mean:.1f}°C\n"
        f"{icon_line}"
        f"```"
    )

    # Quad-lock = semua model sepakat (variance ≤ 1.0°C)
    lock_line = (
        "✅ **QUAD-LOCK TERCAPAI**"
        if triple_lock
        else f"❌ **Lock Gagal** (σ={variance:.2f}°C > {settings.TRIPLE_LOCK_VARIANCE_C}°C)"
    )
    color = COLOR_BLUE if triple_lock else COLOR_RED

    # Status Golden Hour: apakah ini waktu optimal untuk entry
    gh_emoji = {
        "OPEN": "🟢 OPEN (optimal)",
        "WARN": "🟡 WARN (kurang optimal)",
        "NEAR": "🟠 NEAR (mendekati close)",
        "SKIP": "🔴 SKIP",
    }.get(golden_hour_status, golden_hour_status)

    # Jika model terlalu berbeda → kirim alert terpisah ke #☁-weather-data
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
                "name":   "Prakiraan Model",
                "value":  chart,
                "inline": False,
            },
            {
                "name":   "Consensus μ",
                "value":  f"`{consensus_mean:.2f}°C`",
                "inline": True,
            },
            {
                "name":   "σ Antar Model (std dev)",
                "value":  f"`±{variance:.3f}°C`",
                "inline": True,
            },
            {
                "name":   "Model Digunakan",
                "value":  f"`{model_count}/4`",
                "inline": True,
            },
            {
                "name":   "⏰ Golden Hour",
                "value":  f"`{gh_emoji}`",
                "inline": True,
            },
            {
                "name":   "Jam Hingga Tutup",
                "value":  f"`{hours_to_close:.1f}h`",
                "inline": True,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="weather")


# ==============================================================================
# ── #☁-weather-data — MODEL DISAGREEMENT ─────────────────────────────────────
# ==============================================================================

async def notify_model_disagreement(
    location_name: str,
    ecmwf_mean: float,
    gfs_mean: float,
    noaa_mean: float,
    icon_mean: Optional[float],
    variance: float,
) -> None:
    """
    Alert ke #☁-weather-data ketika model-model cuaca tidak sepakat.
    Biasanya terjadi saat ada front cuaca atau anomali atmosfer.
    Bot tetap bisa trade tapi Kelly akan dikurangi.
    """
    icon_str = f"\n  ICON   │ {icon_mean:.1f}°C" if icon_mean else ""

    embed = {
        "color": COLOR_ORANGE,
        "title": "⚡ MODEL TIDAK SEPAKAT",
        "description": (
            f"**{location_name.upper()}** — Variance sangat tinggi!\n"
            f"Kemungkinan ada front cuaca atau anomali atmosfer."
        ),
        "fields": [
            {
                "name":  "Bacaan Model",
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


# ==============================================================================
# ── #📈-aql-trades — TRADE EXECUTED ──────────────────────────────────────────
# ==============================================================================

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
    """
    Kirim detail eksekusi trade ke #📈-aql-trades.
    Berisi: detail order, analisis edge, breakdown sizing Kelly,
    prakiraan model, dan tabel semua outcome yang dievaluasi.
    """
    trade_emoji = "📈" if side == "YES" else "📉"

    # Tabel semua outcome yang dievaluasi (maksimal 8 untuk muat di Discord)
    # Diurutkan dari probabilitas model tertinggi ke terendah
    outcomes_lines = []
    for o in sorted(
        all_outcomes,
        key=lambda x: x.get("prob_model", 0),
        reverse=True,
    )[:8]:
        label    = o.get("label", "?")
        p_model  = o.get("prob_model", 0)   # probabilitas menurut model cuaca kita
        p_market = o.get("market_price", 0) # harga di Polymarket (= probabilitas pasar)
        net      = o.get("net_edge", 0)     # selisih = keunggulan kita
        selected = " ← DIBELI" if label == outcome_label else ""
        bar      = "■" * int(p_model * 10) + "□" * (10 - int(p_model * 10))
        outcomes_lines.append(
            f"  {label:<10} │ {bar} "
            f"{p_model*100:>4.1f}% vs {p_market*100:>4.1f}% "
            f"│ {'+' if net > 0 else ''}{net*100:.1f}%"
            f"{selected}"
        )
    outcomes_str = "\n".join(outcomes_lines) or "  Tidak ada data outcome"

    # Catatan WARN mode: jika Golden Hour bukan OPEN, Kelly dikurangi
    warn_note = ""
    if golden_hour_mult < 1.0:
        warn_note = (
            f"\n⚠️ **WARN MODE** — Golden Hour: {golden_hour_status} "
            f"(Kelly ×{golden_hour_mult:.1f})"
        )

    embed = {
        "color": COLOR_GREEN,
        "title": f"{trade_emoji} ORDER DIEKSEKUSI",
        "description": f"**{market_name[:100]}**{warn_note}",
        "fields": [
            {
                "name":   "📋 DETAIL TRADE",
                "value":  (
                    f"```\n"
                    f"Jenis Market   │ {market_type}\n"
                    f"Aksi           │ BUY {side}\n"
                    f"Outcome        │ {outcome_label}\n"
                    f"Harga Entry    │ {price:.4f} "
                    f"({price*100:.1f}% implied)\n"
                    f"Ukuran Posisi  │ ${size_usd:.2f}\n"
                    f"Maks Profit    │ ${ev_usd:.2f}\n"
                    f"```"
                ),
                "inline": False,
            },
            {
                "name":   "🧮 ANALISIS EDGE",
                "value":  (
                    f"```\n"
                    f"Strategy Edge  │ {edge_pct*100:.1f}% (net)\n"
                    f"Kekuatan Sinyal│ "
                    f"{'🟢 KUAT' if edge_pct >= 0.10 else '🟡 SEDANG'}\n"
                    f"```"
                ),
                "inline": True,
            },
            {
                "name":   "🎯 BREAKDOWN SIZING",
                "value":  (
                    f"```\n"
                    f"Kelly Frac     │ {kelly_fraction:.5f}\n"
                    f"Kepercayaan    │ ×{confidence_mult:.3f}\n"
                    f"Golden Hour    │ ×{golden_hour_mult:.2f}\n"
                    f"Volume         │ ×{volume_mult:.2f}\n"
                    f"Final Mult     │ ×{final_mult:.4f}\n"
                    f"```"
                ),
                "inline": True,
            },
            {
                "name":   "🌡️ PRAKIRAAN MODEL",
                "value":  (
                    f"```\n"
                    f"Rata-rata μ    │ {model_mean_c:.2f}°C\n"
                    f"Ketidakpastian │ ±{model_std_c:.2f}°C\n"
                    f"Outcome Terbaik│ {forecast_outcome}\n"
                    f"```"
                ),
                "inline": False,
            },
            {
                "name":   "📊 SEMUA OUTCOME DIEVALUASI",
                "value":  f"```\n{outcomes_str}\n```",
                "inline": False,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if order_id:
        embed["fields"].append({
            "name":   "ID Sistem",
            "value":  f"`{order_id}`",
            "inline": True,
        })
    if market_url:
        embed["fields"].append({
            "name":   "🌐 Market",
            "value":  f"[Verifikasi di Polymarket]({market_url})",
            "inline": True,
        })

    await _post(_wrap([embed]), target="trades")


# ==============================================================================
# ── #📈-aql-trades — BIG EDGE ALERT ──────────────────────────────────────────
# ==============================================================================

async def notify_big_edge(
    market_question: str,
    outcome_label: str,
    edge_pct: float,
    model_prob: float,
    market_price: float,
    city: str,
) -> None:
    """
    Alert ke #📈-aql-trades ketika edge yang ditemukan jauh di atas threshold.
    Ini bukan berarti trade langsung dieksekusi — hanya pemberitahuan.
    """
    embed = {
        "color": COLOR_YELLOW,
        "title": "🔥 EDGE BESAR TERDETEKSI",
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
                "name":   "Kota",
                "value":  f"`{city}`",
                "inline": True,
            },
            {
                "name":   "Outcome",
                "value":  f"`{outcome_label}`",
                "inline": True,
            },
            {
                "name":   "Prob Model",
                "value":  f"`{model_prob*100:.1f}%`",
                "inline": True,
            },
            {
                "name":   "Harga Pasar",
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


# ==============================================================================
# ── #📈-aql-trades — EXIT EXECUTED ───────────────────────────────────────────
# ==============================================================================

async def notify_exit_executed(
    position,
    reason: str,
    exit_price: float,
    pnl_usd: float,
) -> None:
    """
    Kirim notifikasi posisi ditutup ke #📈-aql-trades.
    reason: "TAKE_PROFIT" atau "STOP_LOSS"
    """
    is_win    = pnl_usd > 0
    color     = COLOR_GREEN if is_win else COLOR_RED
    emoji     = "✅ TAKE PROFIT" if reason == "TAKE_PROFIT" else "🛑 STOP LOSS"
    sign      = "+" if pnl_usd >= 0 else ""
    pnl_emoji = "📈" if is_win else "📉"

    embed = {
        "color": color,
        "title": f"{emoji} — POSISI DITUTUP {pnl_emoji}",
        "fields": [
            {
                "name":   "Posisi",
                "value":  f"`{position.position_id}`",
                "inline": False,
            },
            {
                "name":   "Outcome",
                "value":  f"`{position.outcome_label}`",
                "inline": True,
            },
            {
                "name":   "Harga Entry",
                "value":  f"`{position.entry_price:.4f}`",
                "inline": True,
            },
            {
                "name":   "Harga Exit",
                "value":  f"`{exit_price:.4f}`",
                "inline": True,
            },
            {
                "name":   "PnL",
                "value":  f"**`{sign}${pnl_usd:.2f}`**",
                "inline": True,
            },
            {
                "name":   "Ukuran",
                "value":  f"`${position.size_usd:.2f}`",
                "inline": True,
            },
            {
                "name":   "Ditahan Selama",
                "value":  f"`{position.hours_to_expiry:.1f}h tersisa`",
                "inline": True,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="trades")


# ==============================================================================
# ── #📊-aql-terminal — POSITION EXPIRED ──────────────────────────────────────
# ==============================================================================

async def notify_position_expired(position) -> None:
    """
    Kirim notifikasi posisi kadaluarsa ke #📊-aql-terminal.
    Artinya: market sudah tutup, posisi sedang menunggu resolusi dari Polymarket.
    """
    embed = {
        "color": COLOR_BLUE,
        "title": "⏳ POSISI KADALUARSA — Menunggu Resolusi",
        "fields": [
            {
                "name":   "Posisi",
                "value":  f"`{position.position_id}`",
                "inline": False,
            },
            {
                "name":   "Outcome",
                "value":  f"`{position.outcome_label}`",
                "inline": True,
            },
            {
                "name":   "Harga Entry",
                "value":  f"`{position.entry_price:.4f}`",
                "inline": True,
            },
            {
                "name":   "Harga Terakhir",
                "value":  f"`{position.current_price:.4f}`",
                "inline": True,
            },
            {
                "name":   "PnL Belum Terealisasi",
                "value":  (
                    f"`{'+' if position.unrealized_pnl >= 0 else ''}"
                    f"${position.unrealized_pnl:.2f}`"
                ),
                "inline": True,
            },
            {
                "name":   "Ukuran",
                "value":  f"`${position.size_usd:.2f}`",
                "inline": True,
            },
            {
                "name":   "Status",
                "value":  "`Menunggu resolusi Polymarket`",
                "inline": True,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="terminal")


# ==============================================================================
# ── #🚨-aql-alerts — ERROR / CIRCUIT BREAKER ─────────────────────────────────
# ==============================================================================

async def notify_error(
    title: str,
    description: str,
    is_circuit_breaker: bool = False,
) -> None:
    """
    Kirim alert error atau circuit breaker ke #🚨-aql-alerts.

    is_circuit_breaker=True: tampilkan header khusus circuit breaker.
    Judul mengandung "⚠️"/"Rendah"/"Kecil"/"Warning": tampilkan sebagai warning (orange).
    Selainnya: tampilkan sebagai system alert (merah).
    """
    if is_circuit_breaker:
        header = "⚡ CIRCUIT BREAKER AKTIF"
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


# ==============================================================================
# ── #🚨-aql-alerts — UNKNOWN CITY ────────────────────────────────────────────
# ==============================================================================

async def notify_unknown_city(market) -> None:
    """
    Alert ke #🚨-aql-alerts ketika market suhu ditemukan tapi kotanya
    tidak ada di LOCATION_REGISTRY.

    DEDUP AKTIF: market yang sama hanya dikirim maksimal 1x per jam.
    Ini mencegah ratusan alert spam per siklus scan yang menyebabkan
    memory leak dan OOM container.
    """
    # Gunakan market_id atau condition_id sebagai kunci unik
    market_key = getattr(market, "market_id", None) or getattr(market, "condition_id", "")
    now        = time.monotonic()

    # Cek: apakah market ini sudah pernah dikirim dalam 1 jam terakhir?
    last_sent = _unknown_city_seen.get(market_key, 0.0)
    if now - last_sent < _UNKNOWN_CITY_TTL:
        log.debug(
            "Unknown city dilewati dedup (%.0f detik lalu): %s",
            now - last_sent,
            getattr(market, "question", "")[:50],
        )
        return

    # Catat waktu pengiriman sekarang
    _unknown_city_seen[market_key] = now

    # Bersihkan entri lama (> 2 jam) agar dict tidak terus membesar
    expired = [k for k, t in _unknown_city_seen.items() if now - t > _UNKNOWN_CITY_TTL * 2]
    for k in expired:
        del _unknown_city_seen[k]

    embed = {
        "color": COLOR_ORANGE,
        "title": "🗺️ UNKNOWN CITY — Peluang Terlewat!",
        "description": "Temperature market ditemukan tapi kota tidak dikenal.",
        "fields": [
            {
                "name":   "Market",
                "value":  f"```\n{market.question[:200]}\n```",
                "inline": False,
            },
            {
                "name":   "Likuiditas",
                "value":  f"`${market.liquidity_usd:,.0f}`",
                "inline": True,
            },
            {
                "name":   "Jam Hingga Tutup",
                "value":  f"`{market.htc:.1f}h`",
                "inline": True,
            },
            {
                "name":   "Jenis",
                "value":  f"`{market.market_type}`",
                "inline": True,
            },
            {
                "name":   "Cara Fix",
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
                "value":  f"[Buka di Polymarket]({market.url})",
                "inline": False,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="alerts")


# ==============================================================================
# ── #🚨-aql-alerts — VOLUME WARNING ──────────────────────────────────────────
# ==============================================================================

async def notify_volume_warning(
    market_question: str,
    city: str,
    warning_message: str,
    spike_magnitude: float,
) -> None:
    """
    Alert ke #🚨-aql-alerts ketika ada lonjakan volume tidak wajar.
    Bot tidak berhenti trade, tapi Kelly dikurangi sebagai langkah hati-hati.
    """
    embed = {
        "color": COLOR_ORANGE,
        "title": "📊 PERINGATAN LONJAKAN VOLUME",
        "description": warning_message,
        "fields": [
            {
                "name":   "Kota",
                "value":  f"`{city}`",
                "inline": True,
            },
            {
                "name":   "Lonjakan",
                "value":  f"`{spike_magnitude:.1f}×` dari normal",
                "inline": True,
            },
            {
                "name":   "Market",
                "value":  f"`{market_question[:80]}`",
                "inline": False,
            },
            {
                "name":   "Tindakan",
                "value":  (
                    "Kelly dikurangi "
                    f"`×{settings.VOLUME_KELLY_REDUCTION:.1f}` "
                    "sebagai langkah kehati-hatian."
                ),
                "inline": False,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="alerts")


# ==============================================================================
# ── #🚨-aql-alerts — OPPORTUNITY MISSED ──────────────────────────────────────
# ==============================================================================

async def notify_opportunity_missed(
    market_question: str,
    outcome_label: str,
    edge_pct: float,
    reason: str,
) -> None:
    """
    Alert ke #🚨-aql-alerts ketika ada peluang trade tapi tidak bisa dieksekusi.
    Penyebab umum: circuit breaker aktif, posisi sudah penuh, atau di luar Golden Hour.
    """
    embed = {
        "color": COLOR_RED,
        "title": "❌ PELUANG TERLEWAT",
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
                "name":   "Alasan",
                "value":  f"`{reason}`",
                "inline": True,
            },
        ],
        "footer":    {"text": f"AQL NODE  •  {_ts()}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    await _post(_wrap([embed]), target="alerts")
