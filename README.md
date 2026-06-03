# VCPilot 📈

> **Minervini-grade algorithmic trading on ASX stocks — fully automated, locally deployable.**

VCPilot implements Mark Minervini's SEPA (Specific Entry Point Analysis) methodology as a production-grade automated trading system. It screens the ASX universe daily, detects Volatility Contraction Patterns (VCP), manages risk with precision, and executes trades via Interactive Brokers — all controlled remotely via WhatsApp.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Docker Compose                     │
│  ┌──────────┐  ┌──────────┐  ┌────────────────────┐ │
│  │TimescaleDB│  │  Redis   │  │  IBKR Gateway      │ │
│  │(PostgreSQL)  │ (Celery) │  │  (paper/live)      │ │
│  └──────────┘  └──────────┘  └────────────────────┘ │
│  ┌──────────────────────────────────────────────┐   │
│  │  App Container                               │   │
│  │  ├── Celery Worker (screening/trading/report) │   │
│  │  ├── Celery Beat (scheduler — AEST aligned)  │   │
│  │  └── Data fetcher (yfinance + FMP)           │   │
│  └──────────────────────────────────────────────┘   │
│  ┌──────────────┐   ┌───────────────────────────┐  │
│  │  Streamlit   │   │  WAHA (WhatsApp HTTP API)  │  │
│  │  Dashboard   │   │  → WhatsApp Agent          │  │
│  └──────────────┘   └───────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

---

## Quick Start

### Prerequisites
- Docker Desktop (WSL2 backend on Windows)
- Git
- Interactive Brokers account (paper account for development)

### 1. Clone and configure
```bash
git clone https://github.com/anupamwagle/vcpilot.git
cd vcpilot
cp .env.example .env
# Edit .env — set IBKR credentials, WhatsApp number, passwords
```

### 2. Start core services
```bash
docker compose up db redis app dashboard waha -d
```

### 3. Initialise database + seed Minervini rules
```bash
docker compose run --rm app python -m scripts.init_db
```

### 4. Open dashboard
```
http://localhost:8501
```

### 5. Start trading services (paper mode — ALWAYS start here)
```bash
# Confirm IBKR_PAPER_MODE=true in .env first
docker compose --profile trading up ibkr-gateway -d
docker compose up celery-worker celery-beat -d
```

---

## Minervini Rules Implemented

All rules are configurable via the Rules Config dashboard page with global enable/disable and per-tier threshold overrides.

**Trend Template** (8 criteria — all must pass): Price vs 200/150/50MA alignment, 200MA slope, 52-week range position, RS ≥ 70

**Fundamentals**: EPS growth ≥ 25% (recent + annual), EPS acceleration, Revenue growth ≥ 25%, ROE ≥ 17%, improving margins, institutional ownership

**VCP Pattern**: 3+ tightening contractions, volume dry-up ≤ 50% avg, breakout volume ≥ 150% avg, entry within 5% of pivot

**Market Regime**: ASX200 above 200MA, ≥ 60% stocks above 200MA, ≤ 4 distribution days in 25 sessions

**Exit Rules — Defensive**: Hard stop (mandatory), time stop (not up 10% in 3 weeks), earnings avoidance, 50MA break on volume

**Exit Rules — Offensive**: Partial exit at 20%, full exit at 40%, climax top detection, parabolic move, 3-weeks-tight hold rule

**Risk**: Max 2% capital per trade, max 30% per position, max 15% portfolio heat, pyramid into winners (2 add-ons, +2% profit first)

---

## WhatsApp Commands

| Command | Description |
|---|---|
| `STATUS` | System overview |
| `POSITIONS` | Open positions with P&L |
| `SIGNALS` | Today's signals |
| `MARKET` | Market regime |
| `PAUSE` / `RESUME` | Toggle trading |
| `SKIP BHP` | Cancel today's signal |
| `EXIT BHP` | Emergency close position |
| `STOP BHP 45.50` | Update stop loss |
| `RULE <id> ON\|OFF` | Toggle a rule |
| `CONFIG <key> <val>` | Update system config |
| `REPORT` | Daily P&L |
| `HELP` | All commands |

---

## Schedule (AEST)

| Time | Task |
|---|---|
| 5:00pm Mon–Fri | Refresh price data |
| 5:15pm Mon–Fri | Evaluate market regime |
| 5:30pm Mon–Fri | Run Minervini screener |
| 6:00pm Mon–Fri | Daily WhatsApp report |
| Every 5 min (market hours) | Entry trigger + exit rule checks |
| Sunday 8pm | Refresh ASX200 universe |

---

## Cost

| Item | Cost |
|---|---|
| yfinance | Free |
| FMP free tier (supplemental fundamentals) | Free |
| IBKR commissions (ASX) | $6 min or 0.08% |
| Local infrastructure | Free |
| Cloud (Phase 3) | ~$10–15/month |

---

## Project Structure

```
vcpilot/
├── app/
│   ├── agent/        # WhatsApp command handler
│   ├── broker/       # IBKR (ib_insync)
│   ├── data/         # yfinance fetcher, ASX calendar
│   ├── models/       # SQLAlchemy models
│   ├── notifications/# WhatsApp (WAHA)
│   ├── risk/         # Position sizing, portfolio heat
│   ├── screener/     # Minervini rules, VCP, exit rules
│   └── tasks/        # Celery tasks
├── dashboard/        # Streamlit admin UI (7 pages)
├── docker/           # Dockerfiles
├── migrations/       # DB schema + TimescaleDB setup
├── scripts/          # init_db.py, seed_config.py
├── .env.example
├── docker-compose.yml
└── requirements.txt
```

---

## Disclaimer

VCPilot is for personal use and educational purposes only. Not financial advice. Trading involves significant risk of loss. Always paper trade before going live.