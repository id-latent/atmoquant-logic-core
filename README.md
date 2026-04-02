<div align="center">

```
 █████╗  ██████╗ ██╗         ███╗   ██╗ ██████╗ ██████╗ ███████╗
██╔══██╗██╔═══██╗██║         ████╗  ██║██╔═══██╗██╔══██╗██╔════╝
███████║██║   ██║██║         ██╔██╗ ██║██║   ██║██║  ██║█████╗  
██╔══██║██║▄▄ ██║██║         ██║╚██╗██║██║   ██║██║  ██║██╔══╝  
██║  ██║╚██████╔╝███████╗    ██║ ╚████║╚██████╔╝██████╔╝███████╗
╚═╝  ╚═╝ ╚══▀▀═╝ ╚══════╝    ╚═╝  ╚═══╝ ╚═════╝ ╚═════╝ ╚══════╝
```

# AtmoQuant Logic (AQL) Node
### *Quantitative Weather Prediction & Autonomous Execution Engine*

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Railway](https://img.shields.io/badge/Deploy-Railway-0B0D0E?style=for-the-badge&logo=railway&logoColor=white)](https://railway.app)
[![Polymarket](https://img.shields.io/badge/Market-Polymarket-6C47FF?style=for-the-badge)](https://polymarket.com)
[![Discord](https://img.shields.io/badge/Monitor-Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discord.com)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)

> **"The market prices weather. AQL knows the weather."**
>
> A fully autonomous arbitrage engine that cross-validates three global NWP supercomputers —
> ECMWF, GFS, and NOAA — against Polymarket's implied probabilities on Daily Temperature contracts.
> If the atmosphere agrees, AQL executes. If it doesn't, AQL waits.

---

[**Architecture**](#-architecture-overview) · [**Triple-Lock**](#-core-engine--triple-lock-consensus) · [**Risk Engine**](#-risk-management-engine) · [**Discord Setup**](#-4-channel-discord-monitoring-system) · [**Deploy**](#-deployment-guide--railway) · [**Mobile Dev**](#-mobile-development-guide-samsung-a54--termux) · [**Roadmap**](#-future-roadmap)

</div>

---

## 🧠 Project Identity & Philosophy

**AQL Node** is not a trading bot in the conventional sense. It is a **meteorological arbitrage engine** — a system that monitors the precision of three independent global weather supercomputers and exploits the gap between their consensus forecast and Polymarket's crowd-implied probability.

The core thesis is simple but powerful:

```
MARKET PRICE = Crowd's Implied P(event)
AQL FORECAST = Scientific P(event) from 3 NWP supercomputers

EDGE = AQL_FORECAST − MARKET_PRICE − FRICTION
```

When the models agree (Triple-Lock achieved) and the edge exceeds 5% net of fees, AQL deploys capital — sized precisely by the Fractional Kelly Criterion — and notifies you in real-time via a 4-channel Discord monitoring system.

### Why Weather Markets?

| Factor | Weather Markets | Political/Sports Markets |
|--------|----------------|--------------------------|
| **Data Quality** | Objective, supercomputer-backed | Subjective, sentiment-driven |
| **Model Availability** | Free via Open-Meteo API | No equivalent free feed |
| **Resolution Speed** | Same-day to 7-day | Days to months |
| **Edge Source** | Quantifiable NWP skill | Hard to systematize |
| **Information Asymmetry** | High — most traders use gut feel | Low — widely analyzed |

---

## 📐 Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                        AQL NODE v1.0                                │
│                   Railway.app (Docker Container)                    │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────────┐    ┌─────────────────────────────────────────┐   │
│  │  FastAPI App  │    │           AQL Engine Loop               │   │
│  │  /health      │    │  Every 900s (15 min):                   │   │
│  │  /admin/reset │    │  [1] Market Discovery (Gamma API)       │   │
│  │  /admin/scan  │    │  [2] Triple-Lock Consensus              │   │
│  └──────────────┘    │  [3] Probability Signal (Normal CDF)    │   │
│                       │  [4] Kelly Position Sizing              │   │
│                       │  [5] Circuit Breaker Check              │   │
│                       │  [6] CLOB Order Execution               │   │
│                       │  [7] Discord Notification               │   │
│                       └─────────────────────────────────────────┘   │
│                                                                     │
├───────────┬──────────────┬──────────────┬────────────────────────────┤
│  Open-Meteo│  Polymarket  │  Discord     │  data/state.json           │
│  ECMWF/GFS │  Gamma API   │  Webhooks    │  (Persistent PnL State)    │
│  NOAA feeds│  CLOB API    │  4 Channels  │                            │
└───────────┴──────────────┴──────────────┴────────────────────────────┘
```

### Project Directory Structure

```
atmo_quant_logic/
│
├── 📄 main.py                          # FastAPI entry point + background engine task
├── 📄 requirements.txt                 # Pinned dependencies (~80MB RAM footprint)
├── 📄 Dockerfile                       # Multi-stage build (builder + runtime)
├── 📄 railway.toml                     # Railway deployment configuration
├── 📄 .env.example                     # Environment variable template
│
├── 📁 config/
│   ├── __init__.py
│   └── settings.py                     # All constants, API endpoints, risk params
│
├── 📁 core/
│   ├── __init__.py
│   ├── consensus.py                    # Triple-Lock NWP consensus engine
│   ├── probability.py                  # Normal CDF → P(YES/NO) + edge calculator
│   ├── risk.py                         # Fractional Kelly + Circuit Breaker
│   └── engine.py                       # Master 9-step pipeline orchestrator
│
├── 📁 market/
│   ├── __init__.py
│   └── gamma_client.py                 # Gamma API discovery + CLOB order signing
│
├── 📁 notifications/
│   ├── __init__.py
│   └── notifier.py                     # Discord 4-channel rich embed system
│
└── 📁 data/
    └── state.json                      # Auto-created: PnL, losses, breaker state
```

---

## ⚡ Core Engine — Triple-Lock Consensus

The Triple-Lock is the non-negotiable centerpiece of AQL's edge. No trade is placed unless all three independent NWP (Numerical Weather Prediction) models agree.

### Step 1 — Concurrent Multi-Model Fetch

AQL queries three separate Open-Meteo API endpoints **simultaneously** using `asyncio.gather`:

```
ECMWF (European Centre)  →  api.open-meteo.com/v1/ecmwf   [ecmwf_ifs04]
GFS   (NOAA Global)      →  api.open-meteo.com/v1/gfs      [gfs_seamless]
NOAA  (Best Match)       →  api.open-meteo.com/v1/forecast  [best_match]
```

For each model, AQL retrieves three values for the target date:
- `temperature_2m_max` → Daily high (°C)
- `temperature_2m_min` → Daily low (°C)
- `temperature_2m_mean` → Daily mean (°C)

> ⚠️ **Integrity Rule:** If ANY single model returns null or HTTP error, the entire consensus is aborted. AQL never degrades to a 2-model decision.

### Step 2 — Variance Calculation (Δ)

```python
t_means  = [ecmwf.t_mean_c, gfs.t_mean_c, noaa.t_mean_c]

μ (consensus_mean) = sum(t_means) / 3
Δ (variance)       = max(t_means) − min(t_means)
```

### Step 3 — Triple-Lock Gate

```
IF Δ ≤ 1.0°C  →  TRIPLE LOCK ✅  →  Proceed to Probability Engine
IF Δ >  1.0°C  →  LOCK FAILED  ❌  →  Abort. Send Discord notification. Wait.
```

The `1.0°C` threshold is configurable via `TRIPLE_LOCK_VARIANCE_C` in `settings.py`. Tighter variance means the three supercomputers are in strong agreement — the atmospheric signal is clean. Looser agreement means uncertainty. AQL only bets on certainty.

### Step 4 — Probability Mapping (Normal CDF)

Once Triple-Lock is achieved, the consensus forecast is treated as a **normal distribution**:

```
μ  = consensus_t_mean (or t_max / t_min depending on question type)
σ  = BASE_STD (1.5°C) + VARIANCE_WEIGHT (0.5) × Δ
```

*Higher inter-model variance inflates σ → more conservative probability estimate.*

The market question is parsed to extract a **direction** and **threshold**:

| Question Pattern | Direction | Example |
|-----------------|-----------|---------|
| "exceed", "above", "over" | `ABOVE` | "Will Chicago exceed 90°F?" |
| "below", "under", "less than" | `BELOW` | "Will NYC stay below 32°F?" |

```python
# P(YES) for ABOVE direction:
P(YES) = norm.sf(threshold_c, loc=μ, scale=σ)   # Survival Function

# P(YES) for BELOW direction:
P(YES) = norm.cdf(threshold_c, loc=μ, scale=σ)   # Cumulative Distribution Function
```

### Step 5 — Edge Validation

```
EDGE     = P(YES)_model − P(YES)_market
NET_EDGE = |EDGE| − 0.017  (1.7% Polymarket fee)

IF net_edge ≥ 0.05 (5%)  →  Signal: BUY_YES or BUY_NO
IF net_edge <  0.05       →  NO_TRADE
```

### Full Consensus Flow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                    TRIPLE-LOCK PIPELINE                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  [ECMWF] ─┐                                                     │
│  [GFS  ] ─┼──► asyncio.gather() ──► Variance Δ = max−min      │
│  [NOAA ] ─┘                              │                      │
│                                          ▼                      │
│                              Δ ≤ 1.0°C? ──YES──► Normal CDF   │
│                                   │                    │        │
│                                   NO                   ▼        │
│                                   │           P(YES) estimate   │
│                                   ▼                    │        │
│                           Discord BLUE/RED    Edge > 5%?       │
│                           + Skip market          │              │
│                                              YES / NO           │
│                                              │      │           │
│                                              ▼      ▼           │
│                                         BUY_YES  NO_TRADE       │
│                                         BUY_NO                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 💰 Risk Management Engine

AQL uses two interlocking risk controls that make the system safe to run autonomously.

### 1. Fractional Kelly Criterion — Position Sizing

The Kelly Criterion is the mathematically optimal bet-sizing formula. AQL uses a **fractional** (0.25×) variant for conservative drawdown management:

```
b          = (1 / price) − 1      ← net decimal odds
f*         = (p × (b + 1) − 1) / b  ← full Kelly fraction
f_applied  = f* × 0.25            ← 25% fractional Kelly
size_USD   = bankroll × f_applied

CAPS: min = $1.00,  max = $50.00 (MAX_POSITION_USD)
```

**Why Fractional Kelly?**

| Kelly Variant | Volatility | Growth Rate | Ruin Risk |
|--------------|-----------|-------------|-----------|
| Full Kelly (1.0×) | Very High | Maximum | Significant |
| Half Kelly (0.5×) | High | ~75% of max | Low |
| **Quarter Kelly (0.25×)** | **Moderate** | **~56% of max** | **Minimal** |
| Fixed size | Low | Variable | Variable |

Quarter Kelly is the industry standard for algo trading systems where model uncertainty exists. It sacrifices some growth rate in exchange for dramatically reduced drawdown.

### 2. Circuit Breaker — Consecutive Loss Protection

```
State:  consecutive_losses counter  (persisted in data/state.json)

On LOSS  →  consecutive_losses += 1
On WIN   →  consecutive_losses = 0  (streak reset)

IF consecutive_losses ≥ 3:
    circuit_breaker_active = TRUE
    ALL TRADING HALTED
    Discord RED alert dispatched
    Manual reset required: POST /admin/reset-breaker
```

The Circuit Breaker fires after **3 consecutive losses** (configurable via `CIRCUIT_BREAKER_LOSSES`). This protects against edge degradation events such as:
- API data feed corruption
- Systematic market mispricing
- Unusual weather event regimes outside model skill

> 🔑 **State Persistence:** Both the loss counter and circuit breaker status are saved to `data/state.json` after every trade. A Railway container restart will **not** reset the breaker — the state survives.

---

## 📣 4-Channel Discord Monitoring System

AQL dispatches rich color-coded embeds to four dedicated Discord channels. Each channel has a single, clear purpose.

### Channel Architecture

```
Your Discord Server
│
├── 📊 #aql-terminal      ← Engine lifecycle + Daily PnL Summary
├── ☁️  #weather-data      ← Triple-Lock status + model forecasts
├── 🚨 #alerts            ← Errors + Circuit Breaker activations
└── 📈 #aql-trades        ← Trade executions with full details
```

### Embed Color Reference

| Color | Hex | Channel | Trigger |
|-------|-----|---------|---------|
| 🟢 **GREEN** | `#2ECC71` | `#aql-trades` | Trade successfully executed |
| 🔵 **BLUE** | `#3498DB` | `#weather-data` | Triple-Lock achieved |
| 🔴 **RED** (consensus) | `#E74C3C` | `#weather-data` | Triple-Lock failed (models disagree) |
| 🏆 **GOLD** | `#F1C40F` | `#aql-terminal` | Daily PnL Summary at UTC midnight |
| 🚨 **RED** (error) | `#E74C3C` | `#alerts` | System error or Circuit Breaker |

### Channel-by-Channel Guide

#### 📊 `#aql-terminal` — Engine Heartbeat

Receives startup confirmation and midnight PnL summary.

```
🚀 AQL NODE ONLINE
─────────────────────────────────
Version      │ 1.0.0
Models       │ ECMWF | GFS | NOAA
Poll         │ 900s
Min Edge     │ 5.0%
Kelly Frac   │ 0.25×
Breaker Limit│ 3 losses
─────────────────────────────────
AQL NODE  •  2025-07-04 00:00 UTC

🏆 DAILY PnL SUMMARY  📈
─────────────────────────────────
Trades   │ 12
Win Rate │ 75.0%
Total PnL│ +$18.40
Streak   │ 0
Breaker  │ 🟢 Nominal
```

#### ☁️ `#weather-data` — Consensus Updates

Every market that passes discovery triggers a consensus embed, regardless of Triple-Lock result.

```
🔵 AQL CONSENSUS UPDATE
Chicago — 2025-07-04
✅ TRIPLE LOCK ACHIEVED
─────────────────────────────────
Model Forecasts
  ECMWF  ███████░░░ 36.2°C
  GFS    ████████░░ 36.7°C
  NOAA   ███████░░░ 36.1°C

Consensus μ │ 36.33°C
Inter-Model Δ │ 0.600°C
─────────────────────────────────
AQL NODE  •  2025-07-04 10:15 UTC
```

#### 🚨 `#alerts` — Critical Notifications

```
⚡ CIRCUIT BREAKER TRIPPED
Trade Blocked — Circuit Breaker Active
─────────────────────────────────
Reset via POST /admin/reset-breaker.
3 consecutive losses recorded.
─────────────────────────────────
AQL NODE  •  2025-07-04 14:22 UTC
```

#### 📈 `#aql-trades` — Trade Execution Log

```
🟢 TRADE EXECUTED
Will the high in Chicago exceed 90°F on July 4?
─────────────────────────────────
Side         │ YES
Entry Price  │ 0.4200
Size         │ $12.50
Net Edge     │ 8.3%
Expected Val │ +$2.18
Kelly Frac   │ 0.0625
─────────────────────────────────
Order ID     │ 0x7fa3...
🔗 Market    │ [Open on Polymarket]
─────────────────────────────────
AQL NODE  •  2025-07-04 10:16 UTC
```

---

## 🌍 Supported Market Locations

AQL includes a pre-configured location registry for automatic city detection from market question text:

| Region | Cities |
|--------|--------|
| 🇺🇸 **US East** | New York / NYC, Miami, Boston, Atlanta |
| 🇺🇸 **US Central** | Chicago, Houston, Dallas, Minneapolis |
| 🇺🇸 **US West** | Los Angeles, Phoenix, Seattle, Denver, Las Vegas |
| 🇪🇺 **Europe** | London, Paris, Berlin, Madrid, Rome, Amsterdam, Zurich |

> To add a city, append it to `LOCATION_REGISTRY` in `core/engine.py`:
> ```python
> "tokyo": (35.6762, 139.6503),
> "sydney": (-33.8688, 151.2093),
> ```

---

## 🛠️ Environment Variables Guide

All sensitive credentials are loaded **exclusively** from environment variables. Nothing is hardcoded.

### Required Variables

| Variable | Type | Description | Example |
|----------|------|-------------|---------|
| `POLY_PRIVATE_KEY` | Secret | Polygon wallet private key for CLOB order signing | `0xabc123...` |
| `DISCORD_WEBHOOK_URL` | Secret | Single webhook URL (or use channel routing below) | `https://discord.com/api/webhooks/...` |
| `BANKROLL_USD` | Number | Total capital available to AQL (USD) | `200.0` |
| `LOG_LEVEL` | String | Python logging verbosity | `INFO` |

### Discord Multi-Channel Routing

For the full 4-channel setup, create four separate webhooks in Discord and update `notifier.py`:

| Channel | Env Variable | Discord Path |
|---------|-------------|--------------|
| `#aql-terminal` | `DISCORD_TERMINAL_URL` | Server Settings → Integrations → Webhooks → New |
| `#weather-data` | `DISCORD_WEATHER_URL` | Same process → different channel |
| `#alerts` | `DISCORD_ALERTS_URL` | Same process → different channel |
| `#aql-trades` | `DISCORD_TRADES_URL` | Same process → different channel |

### Setting Variables on Railway

```
Railway Dashboard
  └── Your Project
        └── AQL Service
              └── Variables (tab)
                    ├── POLY_PRIVATE_KEY  = 0x...
                    ├── DISCORD_WEBHOOK_URL = https://discord.com/api/webhooks/...
                    ├── BANKROLL_USD      = 200.0
                    └── LOG_LEVEL         = INFO
```

> ⚠️ **Never** add a `PORT` variable — Railway injects it automatically.

---

## 🚀 Deployment Guide — Railway

### Prerequisites

- GitHub account with the AQL repository
- Railway account ([railway.app](https://railway.app)) — start with $5 trial credit
- A funded Polygon wallet (USDC for trading)
- A Discord server with 4 channels and 4 webhooks created

### Step-by-Step Deployment

**Step 1 — Prepare your GitHub repository**

```bash
# Initialize repo (from Termux or any terminal)
cd atmo_quant_logic
git init
git add .
git commit -m "feat: AQL Node v1.0 — initial deployment"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/aql-node.git
git push -u origin main
```

**Step 2 — Create Railway project**

```
1. Go to railway.app → Log in with GitHub
2. Click "New Project"
3. Select "Deploy from GitHub repo"
4. Authorize Railway → Select your aql-node repo
5. Railway auto-detects Dockerfile → Build starts
```

**Step 3 — Set environment variables**

```
Service → Variables tab → Add all required variables (see table above)
```

**Step 4 — Verify health check**

Once deployed, Railway exposes a public URL. Test it:

```bash
curl https://your-aql-service.railway.app/health
```

Expected response:
```json
{
  "status": "ok",
  "circuit_breaker": false,
  "consecutive_losses": 0,
  "total_trades": 0,
  "total_pnl_usd": 0.0,
  "version": "1.0.0"
}
```

**Step 5 — Confirm Discord startup notification**

Within 30 seconds of deployment, `#aql-terminal` should receive the **🚀 AQL NODE ONLINE** embed.

### Admin Endpoints

| Endpoint | Method | Action |
|----------|--------|--------|
| `/health` | `GET` | Check engine status + PnL state |
| `/admin/reset-breaker` | `POST` | Reset circuit breaker after halt |
| `/admin/scan-now` | `GET` | Force immediate market scan |

---

## 📱 Mobile Development Guide (Samsung A54 + Termux)

This entire system was built, tested, and deployed from a mobile phone. Here is the exact workflow.

### Initial Termux Setup

```bash
# Install core packages
pkg update && pkg upgrade -y
pkg install python git curl -y

# Install pip dependencies
pip install -r requirements.txt --break-system-packages
```

### Common Issue: ModuleNotFoundError

If you see `ModuleNotFoundError: No module named 'config'` or similar:

**Root Cause:** Python cannot find the package because `__init__.py` files are missing.

**Fix:**

```bash
# From the project root directory — create all missing __init__.py files
touch config/__init__.py
touch core/__init__.py
touch market/__init__.py
touch notifications/__init__.py

# Verify they exist
find . -name "__init__.py"
```

**Also verify your run command uses the correct working directory:**

```bash
# CORRECT — run from project root
cd /data/data/com.termux/files/home/atmo_quant_logic
python main.py

# WRONG — never run from a subdirectory
cd core/
python engine.py  # Will fail: can't find config module
```

### Local Testing Without Railway

```bash
# Create a local .env file from the example
cp .env.example .env
nano .env          # Fill in your actual keys

# Load env vars and run
export $(cat .env | xargs) && python main.py
```

### Monitoring Railway Logs from Mobile Browser

1. Open [railway.app](https://railway.app) in Chrome/Firefox on your phone
2. Navigate to your project → AQL Service
3. Click **"Deployments"** tab → Select latest deployment
4. Scroll to **"Logs"** section — live streaming logs appear here
5. Bookmark this page for instant monitoring

### Useful Mobile Terminal Shortcuts (Termux)

```bash
# Check if bot is running locally
curl localhost:8080/health | python -m json.tool

# Watch logs in real-time (if running locally)
python main.py 2>&1 | tee aql.log

# Check state file (PnL persistence)
cat data/state.json | python -m json.tool

# Force reset circuit breaker (Railway deployed)
curl -X POST https://your-aql.railway.app/admin/reset-breaker

# Trigger immediate scan
curl https://your-aql.railway.app/admin/scan-now
```

### RAM Management Tips (Mobile)

AQL is architected for low memory. Intentional exclusions:

| Excluded | Why | RAM Saved |
|----------|-----|-----------|
| `prophet` | Replaced by GradientBoosting-style | ~320MB |
| `web3.py` | Replaced by `eth-account` only | ~150MB |
| `pandas` | Not needed for this pipeline | ~40MB |
| `torch/sklearn` | No ML inference yet | ~200MB+ |

**Target RAM:** ~80MB peak. Fits comfortably within Railway's free/trial tier.

---

## 🔧 Configuration Reference

All tunable parameters live in `config/settings.py`:

```python
# Risk Controls
KELLY_FRACTION          = 0.25    # Position sizing conservatism (0.25 = 25% of full Kelly)
MIN_EDGE_PCT            = 0.05    # 5% minimum net edge required for trade entry
TRADING_FEE_PCT         = 0.017   # 1.7% Polymarket fee
MAX_POSITION_USD        = 50.0    # Hard cap per trade (USD)
CIRCUIT_BREAKER_LOSSES  = 3       # Consecutive losses before halt

# Consensus Engine
TRIPLE_LOCK_VARIANCE_C  = 1.0     # Max inter-model spread (°C) for trade entry
DECISION_PHASE_HOURS    = [0, 12] # 00z and 12z model run windows
ENTRY_WINDOW_HOURS_BEFORE = 13    # Target entry 12–14h before resolution

# Runtime
POLL_INTERVAL_SECONDS   = 900     # 15-minute monitoring cycle
```

### Parameter Tuning Guide

| Parameter | Increase Effect | Decrease Effect |
|-----------|----------------|-----------------|
| `TRIPLE_LOCK_VARIANCE_C` | More trades, less certainty | Fewer trades, higher conviction |
| `MIN_EDGE_PCT` | Fewer but higher-quality trades | More trades, riskier |
| `KELLY_FRACTION` | Larger positions, more volatility | Smaller positions, smoother equity |
| `MAX_POSITION_USD` | More capital at risk per trade | Capital protected |
| `POLL_INTERVAL_SECONDS` | Less frequent checks | More responsive (higher API cost) |

---

## 📦 Dependencies

```
fastapi==0.111.0           # REST API framework
uvicorn[standard]==0.29.0  # ASGI server
httpx==0.27.0              # Async HTTP client
eth-account==0.11.0        # EIP-712 order signing (Polygon)
eth-keys==0.5.1            # Ethereum key primitives
scipy==1.13.0              # Normal CDF probability mapping
numpy==1.26.4              # Numerical foundation
python-dotenv==1.0.1       # .env file support (local dev)
```

---

## 🗺️ Future Roadmap

### Phase 2 — Enhanced Intelligence

- [ ] **Multi-City Parallel Processing** — Run consensus pipelines for 10+ cities simultaneously using `asyncio.gather` at the engine level
- [ ] **Precipitation & Wind Markets** — Extend beyond temperature to rain probability and wind speed contracts
- [ ] **NWP Skill Score Weighting** — Weight ECMWF/GFS/NOAA by their historical accuracy for each city/season (not equal-weight consensus)
- [ ] **7-Day Forward Calendar** — Pre-analyze upcoming markets 7 days ahead, build position schedule

### Phase 3 — Infrastructure

- [ ] **Web Dashboard** — FastAPI + React real-time dashboard showing consensus maps, open positions, PnL equity curve
- [ ] **PostgreSQL Integration** — Replace `state.json` with proper relational storage (trade history, per-market analytics)
- [ ] **Telegram Bot Mirror** — Duplicate Discord alerts to Telegram for redundant mobile monitoring
- [ ] **Multi-Wallet Support** — Route different market categories to separate Polygon wallets

### Phase 4 — Advanced Risk

- [ ] **AI-Driven Dynamic Risk Adjustment** — Use rolling win rate and Sharpe ratio to auto-tune `KELLY_FRACTION` and `MIN_EDGE_PCT`
- [ ] **Drawdown-Based Position Scaling** — Reduce position sizes automatically as bankroll drawdown increases
- [ ] **Correlation Filtering** — Avoid simultaneous positions in highly correlated markets (e.g., Chicago + Minneapolis in same weather system)
- [ ] **Backtesting Framework** — 2020–2024 historical validation against Open-Meteo archive data

---

## 🔐 Security Model

| Threat | Mitigation |
|--------|-----------|
| Private key exposure | Never in source code — Railway env vars only |
| Webhook URL leakage | Same as above — `.env.example` contains no real values |
| `.gitignore` coverage | `.env`, `data/state.json`, `*.pem`, `*.key` all ignored |
| Container runtime | Non-root `aql` user in Docker image |
| Order over-signing | Nonce = millisecond timestamp, prevents replay attacks |

---

## 📊 Performance Expectations

These are **illustrative estimates** based on the system's architecture, not guaranteed returns. Real performance depends on market availability, liquidity, and actual model skill.

| Metric | Conservative | Moderate | Aggressive |
|--------|-------------|----------|------------|
| Trades / day | 1–3 | 3–6 | 6–10 |
| Win rate target | 58%+ | 62%+ | 65%+ |
| Net edge per trade | 5–8% | 8–12% | 12%+ |
| Kelly fraction | 0.25× | 0.33× | 0.5× |

> **Disclaimer:** This system is for educational and research purposes. Prediction markets involve real financial risk. Never deploy capital you cannot afford to lose. Past model skill does not guarantee future edge.

---

## 🤝 Rebuild Guide — From Zero

If you need to rebuild AQL from scratch using only this README:

```
1. Create folder structure as shown in Project Directory
2. Copy all source files from AQL_COMPLETE_SOURCE.py (tagged by # FILE N)
3. Create config/__init__.py, core/__init__.py, market/__init__.py,
   notifications/__init__.py (empty files)
4. Create data/ directory and an empty data/state.json containing: {}
5. Fill requirements.txt and Dockerfile from AQL_DEPLOY_FILES.txt
6. Copy .env.example and populate with real credentials
7. git init → commit → push to GitHub
8. Railway: New Project → Deploy from GitHub → Set env vars
9. Watch #aql-terminal for 🚀 AQL NODE ONLINE confirmation
```

---

<div align="center">

---

**AQL NODE** — *Built on a phone. Deployed to the cloud. Powered by supercomputers.*

```
ECMWF × GFS × NOAA  →  Triple-Lock  →  Kelly  →  Alpha
```

*"The atmosphere doesn't care about market sentiment. AQL listens to the atmosphere."*

---

[![Made with Python](https://img.shields.io/badge/Made%20with-Python%203.11-3776AB?style=flat-square&logo=python)](https://python.org)
[![Deployed on Railway](https://img.shields.io/badge/Deployed%20on-Railway-0B0D0E?style=flat-square&logo=railway)](https://railway.app)
[![Markets on Polymarket](https://img.shields.io/badge/Markets-Polymarket-6C47FF?style=flat-square)](https://polymarket.com)

</div>
