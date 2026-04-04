<div align="center">

```
 █████╗  ██████╗ ██╗         ███╗   ██╗ ██████╗ ██████╗ ███████╗
██╔══██╗██╔═══██╗██║         ████╗  ██║██╔═══██╗██╔══██╗██╔════╝
███████║██║   ██║██║         ██╔██╗ ██║██║   ██║██║  ██║█████╗
██╔══██║██║▄▄ ██║██║         ██║╚██╗██║██║   ██║██║  ██║██╔══╝
██║  ██║╚██████╔╝███████╗    ██║ ╚████║╚██████╔╝██████╔╝███████╗
╚═╝  ╚═╝ ╚══▀▀═╝ ╚══════╝    ╚═╝  ╚═══╝ ╚═════╝ ╚═════╝ ╚══════╝
```

# AtmoQuant Logic — AQL Node
### *Quantitative Weather Prediction & Autonomous Execution Engine*

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111.0-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Railway](https://img.shields.io/badge/Deploy-Railway-0B0D0E?style=for-the-badge&logo=railway&logoColor=white)](https://railway.app)
[![Polymarket](https://img.shields.io/badge/Market-Polymarket-6C47FF?style=for-the-badge)](https://polymarket.com)
[![Discord](https://img.shields.io/badge/Monitor-Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.com)
[![License](https://img.shields.io/badge/License-MIT-22C55E?style=for-the-badge)](LICENSE)

> **"The market prices weather. AQL knows the weather."**
>
> Mesin arbitrase meteorologis yang memvalidasi silang tiga superkomputer NWP global —
> **ECMWF, GFS, dan NOAA** — terhadap harga implied probability Polymarket
> pada kontrak Daily Temperature.
> Jika atmosfer setuju, AQL eksekusi. Jika tidak, AQL menunggu.

---

[**Arsitektur**](#-arsitektur-sistem) · [**Triple-Lock**](#-core-engine--triple-lock-consensus) · [**Risk Engine**](#-risk-management-engine) · [**Discord**](#-sistem-monitoring-discord-4-channel) · [**Deploy**](#-deployment-guide--railway) · [**Infrastructure**](#-deployment--infrastructure-guide) · [**Anti-Detection**](#-anti-detection-strategy) · [**Troubleshooting**](#-troubleshooting--debug-guide) · [**Changelog**](#-changelog) · [**Roadmap**](#-future-roadmap)

</div>

---

## 🧠 Filosofi & Tesis

**AQL Node** bukan sekadar trading bot. Ini adalah **mesin arbitrase meteorologis** — sistem yang memantau ketelitian tiga superkomputer cuaca global independen dan mengeksploitasi celah antara konsensus forecast mereka dengan probabilitas yang ditawarkan pasar prediksi.

### Formula Inti

```
HARGA PASAR  = Probabilitas Implied Crowd      P(event)
FORECAST AQL = Probabilitas Saintifik NWP      P(event)

EDGE     = |AQL_FORECAST − HARGA_PASAR|
NET_EDGE = EDGE − TRADING_FEE (1.7%)

TRADE    = Jika NET_EDGE ≥ 5% AND Triple-Lock ✅
```

### Mengapa Weather Markets?

| Faktor | Pasar Cuaca | Pasar Politik/Olahraga |
|--------|------------|------------------------|
| **Kualitas Data** | Objektif, superkomputer-backed | Subjektif, sentiment-driven |
| **Ketersediaan Model** | Gratis via Open-Meteo API | Tidak ada equivalent |
| **Resolusi** | Same-day hingga 7 hari | Hari hingga bulan |
| **Sumber Edge** | Terukur via NWP skill score | Sulit disistematisasi |
| **Asimetri Informasi** | Tinggi — trader pakai intuisi | Rendah — dianalisis luas |

---

## 📁 Arsitektur Sistem

```
+------------------------------------------------------------------------+
|                        AQL NODE v1.1.0                                 |
|                    Railway.app (Docker Container)                      |
+--------------------+---------------------------------------------------+
|   FastAPI Server   |              AQL Engine Loop                      |
|                    |         (Setiap 900 detik / 15 menit)             |
|  GET  /health      |                                                   |
|  POST /admin/      |  [1] Bankroll Health Check                        |
|    reset-breaker   |  [2] Market Discovery  ── Gamma API               |
|  GET  /admin/      |  [3] Triple-Lock Consensus ── 3 NWP + Retry       |
|    scan-now        |  [4] Discord Consensus Notification               |
|                    |  [5] Triple-Lock Gate (Δ ≤ 1.0°C)                |
|                    |  [5b] Confidence Scoring                          |
|                    |  [6] Kelly Position Sizing + Noise                |
|                    |  [7] Circuit Breaker Check                        |
|                    |  [8] CLOB Order Submission (Polygon)              |
|                    |  [9] Discord Trade Notification                   |
+----------+-----------+-----------+-------------+----------------------+
|Open-Meteo| Polymarket | Discord   | data/       | utils/               |
|ECMWF/GFS | Gamma+CLOB | 4 Webhooks| state.json  | jitter + headers     |
|NOAA      | Polygon    | 4 Channels| Persistent  | Anti-detection       |
+----------+-----------+-----------+-------------+----------------------+
```

### Struktur Folder

```
atmoquant-logic-core/
│
├── 📄 main.py                     # FastAPI entry point + background engine
├── 📄 requirements.txt            # Pinned dependencies (~80MB RAM)
├── 📄 Dockerfile                  # Multi-stage build: builder + runtime
├── 📄 railway.toml                # Railway deployment config
├── 📄 .env.example                # Environment variable template
│
├── 📁 config/
│   ├── __init__.py
│   └── settings.py                # Semua konstanta, endpoints, risk params
│
├── 📁 core/
│   ├── __init__.py
│   ├── consensus.py               # Triple-Lock NWP engine + retry logic
│   ├── probability.py             # Robust parser + Normal CDF → P(YES/NO)
│   ├── risk.py                    # Kelly + Circuit Breaker + Size Noise
│   └── engine.py                  # Master 9-step orchestrator
│
├── 📁 market/
│   ├── __init__.py
│   └── gamma_client.py            # Gamma API + CLOB + anti-detection
│
├── 📁 notifications/
│   ├── __init__.py
│   └── notifier.py                # Discord 4-channel rich embed system
│
├── 📁 utils/
│   ├── __init__.py
│   ├── jitter.py                  # Human-like request delay
│   └── headers.py                 # Random user-agent rotation
│
└── 📁 data/
    └── state.json                 # Auto-created: PnL, losses, breaker state
```

---

## ⚡ Core Engine — Triple-Lock Consensus

Triple-Lock adalah inti non-negotiable dari edge AQL. **Tidak ada trade tanpa konsensus tiga model.**

### Step 1 — Concurrent Fetch dengan Retry

```python
# Tiga model di-fetch BERSAMAAN menggunakan asyncio.gather
# Setiap model memiliki exponential backoff retry:
#   Attempt 1: langsung
#   Attempt 2: tunggu 1.5s
#   Attempt 3: tunggu 3.0s
#   Rate limit (429): tunggu lebih lama

ecmwf, gfs, noaa = await asyncio.gather(
    _fetch_with_retry(client, "ECMWF", lat, lon, date),
    _fetch_with_retry(client, "GFS",   lat, lon, date),
    _fetch_with_retry(client, "NOAA",  lat, lon, date),
)
```

Data yang diambil per model:

| Variable | Keterangan |
|----------|-----------|
| `temperature_2m_max` | Suhu harian tertinggi (°C) |
| `temperature_2m_min` | Suhu harian terendah (°C) |
| `temperature_2m_mean` | Suhu harian rata-rata (°C) |

> ⚠️ **Integrity Rule:** Jika satu model gagal setelah semua retry habis — konsensus **dibatalkan**. AQL tidak pernah degradasi ke 2-model consensus.

### Step 2 — Variance Calculation

```python
t_means        = [ecmwf.t_mean_c, gfs.t_mean_c, noaa.t_mean_c]
consensus_mean = sum(t_means) / 3
variance_delta = max(t_means) - min(t_means)
```

### Step 3 — Triple-Lock Gate

```
Δ ≤ 1.0°C  -->  TRIPLE LOCK ✅  -->  Lanjut ke Probability Engine
Δ >  1.0°C  -->  LOCK FAILED  ❌  -->  Abort + Discord alert
```

### Step 4 — Confidence Scoring (v1.1.0)

```python
# Semakin kecil variance + semakin dekat horizon = skor lebih tinggi
variance_score  = max(0.0, 1.0 - consensus.inter_model_variance)
horizon_score   = max(0.3, 1.0 - (horizon_days - 1) * 0.10)
confidence_mult = 0.5 + ((variance_score + horizon_score) / 2 * 0.5)
# Range: 0.5× hingga 1.0× → mempengaruhi Kelly fraction
```

### Step 5 — Probability Mapping (Normal CDF)

```python
sigma   = 1.5 + (0.5 × variance_delta)   # Lebih besar variance = lebih lebar σ

# Arah ABOVE (misal: "exceed 90°F")
P_YES = norm.sf(threshold_c, loc=mu, scale=sigma)

# Arah BELOW (misal: "stay below 32°F")
P_YES = norm.cdf(threshold_c, loc=mu, scale=sigma)

NET_EDGE = |P_YES_model - P_YES_market| - 0.017   # kurangi fee 1.7%
```

### Supported Market Locations

| Region | Kota |
|--------|------|
| 🇺🇸 US East | New York/NYC, Miami, Boston, Atlanta |
| 🇺🇸 US Central | Chicago, Houston, Dallas, Minneapolis |
| 🇺🇸 US West | Los Angeles, Phoenix, Seattle, Denver, Las Vegas |
| 🇪🇺 Europe | London, Paris, Berlin, Madrid, Rome, Amsterdam, Zurich |

**Menambah kota baru** di `core/engine.py`:
```python
LOCATION_REGISTRY: dict[str, tuple[float, float]] = {
    "jakarta":  (-6.2088, 106.8456),
    "tokyo":    (35.6762, 139.6503),
    "singapore":(1.3521,  103.8198),
}
```

---

## 💰 Risk Management Engine

### 1. Fractional Kelly Criterion

```python
b          = (1 / price) - 1              # Net decimal odds
f_full     = (p × (b + 1) - 1) / b       # Full Kelly fraction
f_applied  = f_full × KELLY_FRACTION × confidence_multiplier

size_USD   = bankroll × f_applied
# Hard caps: min = $1.00, max = $50.00
```

| Kelly Variant | Volatilitas | Growth Rate | Risiko Ruin |
|---------------|------------|-------------|-------------|
| Full Kelly (1.0×) | Sangat Tinggi | Maksimum | Signifikan |
| Half Kelly (0.5×) | Tinggi | ~75% max | Rendah |
| **Quarter Kelly (0.25×)** | **Moderat** | **~56% max** | **Minimal** |

### 2. Kelly Size Noise — Anti-Detection

```python
# Bot suspicious selalu order angka bulat: $10.00, $20.00, $50.00
# Dengan noise ±4%: $12.00 → $11.73 atau $12.41
noise_pct  = random.uniform(-0.04, 0.04)
noisy_size = size_usd * (1 + noise_pct)
```

### 3. Circuit Breaker

```
State: consecutive_losses  →  disimpan di data/state.json

Setiap TRADE_LOSS     →  consecutive_losses += 1
Setiap ORDER_REJECTED →  consecutive_rejections += 1 (TIDAK trip breaker)
Setiap WIN            →  consecutive_losses = 0

Jika consecutive_losses >= 3:
    circuit_breaker_active = TRUE
    SEMUA TRADING DIHENTIKAN
    Discord #🚨-aql-alerts alert merah dikirim
    Reset manual: POST /admin/reset-breaker
```

> **Perbedaan penting:** `TRADE_LOSS` adalah posisi yang kalah karena model salah prediksi. `ORDER_REJECTED` adalah FOK tidak terisi karena likuiditas — **bukan** kesalahan model dan **tidak** menghitung ke streak.

### 4. Bankroll Guard

| Bankroll | Status | Aksi Bot |
|----------|--------|---------|
| `> $50` | ✅ Ideal | Normal |
| `$15 – $50` | ⚠️ Warning | Jalan + kirim alert Discord |
| `< $15` | 🚨 Halt | Trading dihentikan otomatis |

### Parameter Risk (`config/settings.py`)

| Parameter | Default | Fungsi |
|-----------|---------|--------|
| `KELLY_FRACTION` | `0.25` | Konservatisme sizing |
| `MIN_EDGE_PCT` | `0.05` | Minimum 5% net edge |
| `TRADING_FEE_PCT` | `0.017` | Fee Polymarket 1.7% |
| `MAX_POSITION_USD` | `50.0` | Hard cap per trade |
| `CIRCUIT_BREAKER_LOSSES` | `3` | Losses sebelum halt |
| `TRIPLE_LOCK_VARIANCE_C` | `1.0` | Max inter-model Δ (°C) |
| `POLL_INTERVAL_SECONDS` | `900` | Interval scan 15 menit |
| `ENTRY_WINDOW_HOURS_BEFORE` | `13` | Entry 12–14h sebelum resolusi |

---

## 📣 Sistem Monitoring Discord 4-Channel

### Arsitektur Channel

```
Discord Server
│
├── 1. #📊-aql-terminal   ← Startup heartbeat + Daily PnL Summary
├── 2. #☁-weather-data    ← Triple-Lock consensus + model forecast
├── 3. #📈-aql-trades     ← Detail eksekusi trade lengkap
└── 4. #🚨-aql-alerts     ← Error + Circuit Breaker + Warning
```

### Color Reference

| Warna | Hex | Channel | Trigger |
|-------|-----|---------|---------|
| 🟢 GREEN | `#2ECC71` | `#📈-aql-trades` | Trade berhasil dieksekusi |
| 🔵 BLUE | `#3498DB` | `#☁-weather-data` | Triple-Lock tercapai |
| 🔴 RED | `#E74C3C` | `#☁-weather-data` / `#🚨-aql-alerts` | Lock gagal / Error / CB |
| 🏆 GOLD | `#F1C40F` | `#📊-aql-terminal` | Daily PnL positif |
| 🟠 ORANGE | `#E67E22` | `#🚨-aql-alerts` | Warning bankroll rendah |

### Contoh Embed Nyata

**`#📊-aql-terminal` — Startup**
```
🚀 AQL NODE ONLINE
─────────────────────────────────────
Version        | 1.1.0
Models         | ECMWF | GFS | NOAA
Poll Interval  | 900s
Min Edge       | 5.0%
Kelly Fraction | 0.25×
Circuit Breaker| 3 losses
─────────────────────────────────────
AQL NODE  •  2025-07-04 00:00 UTC
```

**`#☁-weather-data` — Triple-Lock**
```
🔵 AQL CONSENSUS UPDATE
Chicago — 2025-07-04
✅ TRIPLE LOCK ACHIEVED
─────────────────────────────────────
ECMWF ■■■■■■■□□□  36.2°C
GFS   ■■■■■■■■□□  36.7°C
NOAA  ■■■■■■■□□□  36.1°C

Consensus μ    | 36.33°C
Inter-Model Δ  | ±0.600°C
─────────────────────────────────────
AQL NODE  •  2025-07-04 10:15 UTC
```

**`#📈-aql-trades` — Trade Execution**
```
📈 ORDER EXECUTED
Will the high in Chicago exceed 90°F on July 4?
─────────────────────────────────────
Action          | YES
Execution Price | 0.4200
Position Size   | $12.43
Strategy Edge   | 8.3%
Expected Value  | +$2.18
Kelly Multiplier| 0.0625
─────────────────────────────────────
System ID       | 0x7fa3...
🌐 Market Source| [Verify on Polymarket]
─────────────────────────────────────
AQL NODE  •  2025-07-04 10:16 UTC
```

**`#🚨-aql-alerts` — Circuit Breaker**
```
⚡ CIRCUIT BREAKER TRIPPED
─────────────────────────────────────
3 consecutive trade losses recorded.
Semua trading dihentikan otomatis.

Reset via:
POST /admin/reset-breaker
─────────────────────────────────────
AQL NODE  •  2025-07-04 14:22 UTC
```

---

## 🔐 Environment Variables

Semua kredensial dibaca **exclusively** dari environment variables.

### Variabel Wajib

| Variable | Tipe | Deskripsi |
|----------|------|-----------|
| `POLY_PRIVATE_KEY` | 🔴 Secret | Private key wallet Polygon |
| `TERMINAL_WEBHOOK_URL` | 🔴 Secret | Discord webhook `#📊-aql-terminal` |
| `WEATHER_WEBHOOK_URL` | 🔴 Secret | Discord webhook `#☁-weather-data` |
| `TRADE_WEBHOOK_URL` | 🔴 Secret | Discord webhook `#📈-aql-trades` |
| `ALERTS_WEBHOOK_URL` | 🔴 Secret | Discord webhook `#🚨-aql-alerts` |
| `BANKROLL_USD` | Number | Modal awal (default: `200.0`) |
| `LOG_LEVEL` | String | `DEBUG`/`INFO`/`WARNING` (default: `INFO`) |

### Cara Buat Discord Webhook

```
Discord Server → Server Settings → Integrations
→ Webhooks → New Webhook → pilih channel → Copy URL
```

Buat 4 webhook, satu untuk setiap channel.

### Set di Railway

```
Railway Dashboard
  └── Project → Service AQL
        └── Tab "Variables"
              POLY_PRIVATE_KEY      = 0x...
              TERMINAL_WEBHOOK_URL  = https://discord.com/api/webhooks/...
              WEATHER_WEBHOOK_URL   = https://discord.com/api/webhooks/...
              TRADE_WEBHOOK_URL     = https://discord.com/api/webhooks/...
              ALERTS_WEBHOOK_URL    = https://discord.com/api/webhooks/...
              BANKROLL_USD          = 200.0
              LOG_LEVEL             = INFO
```

> ⚠️ Jangan set variabel `PORT` — Railway inject otomatis.

---

## 🚀 Deployment Guide — Railway

### Prerequisites

- [ ] Repo ini sudah ada di GitHub
- [ ] Akun Railway ([railway.app](https://railway.app))
- [ ] Wallet Polygon dengan USDC
- [ ] Discord server dengan 4 channel + 4 webhook

### Step-by-Step

**Step 1 — Push ke GitHub**
```bash
git add .
git commit -m "deploy: AQL Node v1.1.0"
git push origin main
```

**Step 2 — Deploy di Railway**
```
1. railway.app → New Project
2. Deploy from GitHub repo
3. Pilih: id-latent/atmoquant-logic-core
4. Railway auto-detect Dockerfile → build mulai
```

**Step 3 — Set environment variables**

Isi semua variabel dari tabel di atas di Railway → Variables.

**Step 4 — Verifikasi health check**
```bash
curl https://your-service.railway.app/health
```

Response yang diharapkan:
```json
{
  "status": "ok",
  "circuit_breaker": false,
  "consecutive_losses": 0,
  "total_trades": 0,
  "total_pnl_usd": 0.0,
  "version": "1.1.0"
}
```

**Step 5 — Konfirmasi Discord**

Dalam 30 detik setelah deploy, `#📊-aql-terminal` menerima embed **🚀 AQL NODE ONLINE**.

### Admin Endpoints

| Endpoint | Method | Fungsi |
|----------|--------|--------|
| `/health` | `GET` | Status engine + state PnL |
| `/admin/reset-breaker` | `POST` | Reset circuit breaker |
| `/admin/scan-now` | `GET` | Force scan cycle sekarang |

---

## 🏗️ Deployment & Infrastructure Guide

### A. Railway (Current)

Setup Railway sudah tercakup di section deployment di atas. Railway adalah pilihan terbaik untuk memulai karena zero-config dan auto-deploy dari GitHub.

### B. VPS Migration Guide (Future)

**Kapan saatnya pindah dari Railway ke VPS?**
```
- Bot sudah profitable dan stabil minimal 30 hari
- Butuh kontrol penuh atas networking dan latency
- Monthly cost Railway > cost VPS dengan spec sama
- Perlu custom firewall rules
```

**Spesifikasi VPS Minimum yang Direkomendasikan:**
```
RAM  : 1GB (AQL target ~80MB, sisanya untuk OS)
CPU  : 1 vCPU
Disk : 20GB SSD
OS   : Ubuntu 22.04 LTS
Lokasi: Pilih server yang dekat dengan Polymarket infrastructure
        (US East — New York / Virginia direkomendasikan)
```

**Setup VPS:**
```bash
# 1. Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# 2. Clone repo
git clone https://github.com/id-latent/atmoquant-logic-core.git
cd atmoquant-logic-core

# 3. Buat .env dari template
cp .env.example .env
nano .env  # isi kredensial

# 4. Build dan run
docker build -t aql-node .
docker run -d \
  --name aql \
  --restart unless-stopped \
  --env-file .env \
  -p 8080:8080 \
  aql-node

# 5. Cek status
docker logs -f aql
```

**Auto-restart dengan systemd:**
```ini
# /etc/systemd/system/aql.service
[Unit]
Description=AQL Node
After=docker.service
Requires=docker.service

[Service]
Restart=always
ExecStart=/usr/bin/docker start -a aql
ExecStop=/usr/bin/docker stop aql

[Install]
WantedBy=multi-user.target
```

### C. AI Agent Integration (Future)

**Claude API sebagai Market Analyst:**
```python
# Contoh integrasi yang direncanakan:
# - Auto-analisis pertanyaan pasar yang kompleks
# - Deteksi anomali cuaca dari berita terkini
# - Evaluasi apakah suatu market worth trading

import anthropic

async def analyze_market_with_ai(market_question: str) -> dict:
    client = anthropic.Anthropic()
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": f"Analyze this weather market: {market_question}"
        }]
    )
    return {"analysis": message.content[0].text}
```

**Additional Weather Data Sources:**
```
- AccuWeather API    → Hyperlocal forecasts
- NOAA Alerts API   → Extreme weather warnings
- Weather.com API   → Crowd-sourced observations
- Windy API         → Visual wind/pressure maps
```

### D. Scaling Architecture (Future)

**Multi-Instance Deployment:**

```
Instance 1: US Markets
  Kota: New York, Chicago, Los Angeles, Miami,
        Houston, Dallas, Phoenix, Seattle, Denver, Atlanta

Instance 2: Europe Markets
  Kota: London, Paris, Berlin, Madrid, Rome,
        Amsterdam, Zurich

Instance 3: Asia Markets
  Kota: Tokyo, Singapore, Bangkok, Jakarta,
        Kuala Lumpur, Seoul, Hong Kong, Mumbai
```

**Shared Infrastructure:**
```
Redis       → Shared state antar instance
             (mencegah double-entry pada market yang sama)
PostgreSQL  → Trade history + analytics
             (ganti data/state.json untuk production)
Grafana     → Real-time monitoring dashboard
             (PnL curve, win rate, drawdown chart)
```

### E. Security Hardening (Future)

```
Secrets Manager  → HashiCorp Vault / AWS Secrets Manager
                   (ganti environment variables biasa)
IP Whitelisting  → Batasi akses /admin/* hanya dari IP tertentu
2FA Endpoint     → Tambahkan token auth untuk /admin/reset-breaker
Rate Limiting    → Nginx rate limit untuk semua admin endpoints
Audit Log        → Log semua aksi admin ke file terpisah
```

---

## 🛡️ Anti-Detection Strategy

AQL menggunakan tiga lapisan perlindungan untuk menghindari deteksi sebagai automated bot oleh Polymarket.

### Layer 1 — Request Jittering

```python
# utils/jitter.py
# Setiap request diberi delay acak sebelum dikirim
# 5% chance ada "thinking pause" 2-5 detik tambahan

async def human_delay(min_ms=300, max_ms=1200):
    delay_ms = random.randint(min_ms, max_ms)
    if random.random() < 0.05:          # thinking pause
        delay_ms += random.randint(2000, 5000)
    await asyncio.sleep(delay_ms / 1000)
```

### Layer 2 — User-Agent Rotation

```python
# utils/headers.py
# Setiap request menggunakan browser identity yang berbeda
# Pool: Chrome Windows, Chrome Mac, Chrome Linux,
#       Firefox Windows, Safari Mac, Chrome Android

def random_headers() -> dict:
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "application/json",
        ...
    }
```

### Layer 3 — Kelly Size Noise

```python
# core/risk.py
# Hindari order angka bulat yang mencurigakan
# $10.00 → $9.63 atau $10.38 (±4% random)

def _add_size_noise(size_usd: float) -> float:
    noise_pct = random.uniform(-0.04, 0.04)
    return round(size_usd * (1 + noise_pct), 2)
```

---

## 🔧 Troubleshooting & Debug Guide

### A. Startup Errors

**`KeyError: 'TERMINAL_WEBHOOK_URL'`**
```
Penyebab: Environment variable tidak diset di Railway.
Fix:
  Railway → Service → Variables → tambahkan:
  TERMINAL_WEBHOOK_URL = https://discord.com/api/webhooks/...
  WEATHER_WEBHOOK_URL  = https://discord.com/api/webhooks/...
  TRADE_WEBHOOK_URL    = https://discord.com/api/webhooks/...
  ALERTS_WEBHOOK_URL   = https://discord.com/api/webhooks/...
```

**`ModuleNotFoundError: No module named 'config'`**
```
Penyebab: File __init__.py tidak ada, atau dijalankan
          dari folder yang salah.
Fix:
  # Pastikan semua __init__.py ada
  touch config/__init__.py
  touch core/__init__.py
  touch market/__init__.py
  touch notifications/__init__.py
  touch utils/__init__.py

  # Jalankan SELALU dari root folder
  cd atmoquant-logic-core
  python main.py
```

**`Address already in use: port 8080`**
```
Penyebab: Port 8080 sudah dipakai proses lain.
Fix:
  # Cari dan kill proses yang pakai port 8080
  lsof -i :8080
  kill -9 <PID>

  # Atau ganti port via env var
  PORT=8081 python main.py
```

### B. Consensus Errors

**`[ECMWF] Request timed out`**
```
Penyebab: Open-Meteo API lambat atau down.
Behavior: Bot otomatis retry 3x dengan exponential backoff.
          Jika semua retry gagal → consensus dibatalkan.
Monitor:  Cek status Open-Meteo: https://open-meteo.com/
Fix manual: Naikkan timeout di consensus.py jika koneksi lambat.
```

**`Triple-Lock selalu gagal (Δ selalu > 1.0°C)`**
```
Penyebab A: Cuaca sedang tidak stabil / extreme weather event.
            → Normal, bot menunggu kondisi lebih stabil.

Penyebab B: Threshold terlalu ketat.
Fix B: Naikkan TRIPLE_LOCK_VARIANCE_C di settings.py
       Nilai default: 1.0°C
       Coba: 1.2°C atau 1.5°C (lebih longgar, lebih banyak trade)
       Perhatian: Lebih longgar = edge lebih kecil per trade.
```

**`[Parser] Direction tidak ditemukan`**
```
Penyebab: Pertanyaan pasar tidak mengandung keyword arah
          yang dikenali (exceed, above, below, dll).
Fix: Pertanyaan ini tidak bisa di-parse otomatis.
     Bot akan skip market ini — normal behavior.
     Cek log probability.py untuk detail.
```

### C. Trading Errors

**`CLOB submission failed [400]`**
```
Penyebab: Order format tidak valid atau signature salah.
Fix:
  1. Cek POLY_PRIVATE_KEY sudah benar di Railway Variables
  2. Pastikan wallet punya cukup USDC
  3. Cek POLY_CHAIN_ID = 137 (Polygon Mainnet)
```

**`ORDER_REJECTED — FOK tidak terisi`**
```
Penyebab: Likuiditas pasar tidak cukup saat eksekusi.
Behavior: TIDAK menghitung ke circuit breaker streak.
          Bot catat sebagai consecutive_rejections.
Fix: Bot akan coba lagi di scan cycle berikutnya.
     Jika sering terjadi, naikkan min_liquidity_usd di engine.py
     (default: $500)
```

**`Bankroll $X.XX di bawah minimum`**
```
Penyebab: Saldo wallet kurang dari MINIMUM_BANKROLL_HALT ($15).
Behavior: Trading otomatis dihentikan.
Fix:
  1. Top up wallet Polygon dengan USDC
  2. Update BANKROLL_USD di Railway Variables
  3. Bot akan otomatis resume di scan berikutnya
```

### D. Discord Errors

**`Discord non-204: 404`**
```
Penyebab: Webhook URL tidak valid atau sudah dihapus.
Fix:
  1. Buat webhook baru di Discord
  2. Update variabel webhook di Railway Variables
  3. Restart service Railway
```

**`Discord non-204: 429`**
```
Penyebab: Discord rate limit — terlalu banyak embed dikirim.
Behavior: Bot log warning tapi tetap berjalan.
Fix: Kurangi frekuensi notifikasi jika terjadi terus-menerus.
     Normal terjadi jika banyak market ditemukan sekaligus.
```

### E. Railway Errors

**`Build failed — Dockerfile error`**
```
Penyebab: Dependencies gagal install saat build.
Fix:
  1. Cek requirements.txt tidak ada typo versi
  2. Pastikan Dockerfile tidak dimodifikasi
  3. Cek Railway build logs untuk error spesifik
```

**`Health check failed — service restart loop`**
```
Penyebab: Aplikasi crash saat startup.
Fix:
  1. Cek Railway logs untuk traceback error
  2. Pastikan semua env vars sudah diset
  3. Cek apakah data/state.json valid:
     Isi harus: {"consecutive_losses": 0, ...}
     Jika corrupt: hapus file, Railway akan recreate
```

### F. Circuit Breaker

**Langkah saat Circuit Breaker Trip:**
```
LANGKAH 1 — Jangan langsung reset
  Buka Discord #🚨-aql-alerts
  Lihat 3 trade terakhir di #📈-aql-trades
  Analisis: apakah ada pola kesalahan atau bad luck biasa?

LANGKAH 2 — Investigasi penyebab
  Model meleset semua?   → Cuaca anomali, tunggu kondisi normal
  Parser salah baca?     → Cek log probability.py
  Likuiditas buruk?      → Naikkan min_liquidity_usd
  Bad luck biasa?        → Aman untuk reset

LANGKAH 3 — Reset breaker
  curl -X POST https://your-service.railway.app/admin/reset-breaker

LANGKAH 4 — Monitor trade berikutnya
  Pantau #☁-weather-data untuk Triple-Lock status
  Pantau #📈-aql-trades untuk konfirmasi eksekusi
```

---

## 📦 Dependencies

```
fastapi==0.111.0           # REST API + health/admin endpoints
uvicorn[standard]==0.29.0  # ASGI server
httpx==0.27.0              # Async HTTP (Open-Meteo, Gamma, Discord)
eth-account==0.11.0        # EIP-712 order signing (Polygon)
eth-keys==0.5.1            # Ethereum key primitives
scipy==1.13.0              # Normal CDF probability mapping
numpy==1.26.4              # Numerical foundation
python-dotenv==1.0.1       # .env support (local dev)
```

**Kenapa RAM ~80MB?**

| Excluded | Alasan | RAM Saved |
|----------|--------|-----------|
| `prophet` | Diganti Normal CDF | ~320MB |
| `web3.py` | Cukup `eth-account` | ~150MB |
| `pandas` | Tidak dibutuhkan | ~40MB |
| `torch/sklearn` | Belum diperlukan | ~200MB+ |

---

## 🔒 Security Model

| Ancaman | Mitigasi |
|---------|---------|
| Private key exposure | Railway env vars — tidak ada di source code |
| Webhook URL bocor | Sama — tidak di-commit ke Git |
| `.gitignore` | `.env`, `data/state.json`, `*.pem`, `*.key` |
| Container runtime | Non-root user `aql` (uid=1000) |
| Order replay | Nonce = millisecond timestamp |
| Admin endpoint | Read-only `/health`, POST untuk reset |

---

## 📋 Changelog

### v1.1.0 (Current)
```
ADDED
+ Exponential backoff retry di consensus.py (3 attempts)
+ Robust temperature threshold parser di probability.py
+ Circuit breaker membedakan TRADE_LOSS vs ORDER_REJECTED
+ Kelly size noise ±4% untuk anti-detection
+ Unknown city detector + Discord alert
+ Bankroll health guard ($15 halt, $50 warning)
+ Confidence scoring berdasarkan variance + horizon
+ Request jittering (utils/jitter.py)
+ User-agent rotation (utils/headers.py)
+ Orange color untuk warning Discord embeds
+ Rejected orders tracking di daily PnL summary
+ Version bump ke 1.1.0 di engine.py dan notifier.py

FIXED
+ .env.example diupdate ke 4 webhook terpisah
+ Parser tidak lagi salah baca tahun sebagai threshold
+ Parser skip market dengan "record high/low" tanpa angka
+ ORDER_REJECTED tidak lagi menghitung ke circuit breaker
```

### v1.0.0 (Initial)
```
+ Triple-Lock consensus engine (ECMWF + GFS + NOAA)
+ Normal CDF probability mapping
+ Fractional Kelly Criterion (0.25x)
+ Circuit breaker (3 consecutive losses)
+ Discord 4-channel notification system
+ Polymarket Gamma API market discovery
+ CLOB EIP-712 order signing
+ FastAPI health + admin endpoints
+ Railway Dockerfile deployment
+ Persistent state via data/state.json
```

---

## 🗺️ Future Roadmap

### Phase 2 — Enhanced Intelligence
- [ ] Multi-city parallel processing (10+ kota simultan)
- [ ] Pasar presipitasi & wind speed
- [ ] NWP skill score weighting (ECMWF > GFS > NOAA berdasarkan akurasi historis)
- [ ] 7-day forward calendar pre-analysis

### Phase 3 — Infrastructure
- [ ] Web dashboard (FastAPI + React): consensus map, PnL curve
- [ ] PostgreSQL: ganti state.json dengan relational DB
- [ ] Telegram mirror: duplikasi Discord alert
- [ ] Multi-wallet support per region

### Phase 4 — Scaling
- [ ] Multi-instance: US + Europe + **Asia** (Tokyo, Singapore, Jakarta, dll)
- [ ] Redis shared state antar instance
- [ ] Grafana monitoring dashboard

### Phase 5 — Advanced Risk
- [ ] AI dynamic risk adjustment (Claude API integration)
- [ ] Drawdown-based position scaling otomatis
- [ ] Correlation filter antar market
- [ ] Full backtesting framework (2020–2024 Open-Meteo archive)

---

<div align="center">

---

**AQL Node v1.1.0** — *Dibangun dengan presisi. Ditenagai superkomputer.*

```
ECMWF  ×  GFS  ×  NOAA  →  Triple-Lock  →  Kelly  →  Alpha
```

> *"Atmosfer tidak peduli sentiment pasar. AQL mendengarkan atmosfer."*

---

[![GitHub](https://img.shields.io/badge/Source-id--latent%2Fatmoquant--logic--core-181717?style=flat-square&logo=github)](https://github.com/id-latent/atmoquant-logic-core)
[![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=flat-square&logo=python)](https://python.org)
[![Railway](https://img.shields.io/badge/Railway-Deployed-0B0D0E?style=flat-square&logo=railway)](https://railway.app)
[![Polymarket](https://img.shields.io/badge/Polymarket-Weather_Markets-6C47FF?style=flat-square)](https://polymarket.com)

</div>
