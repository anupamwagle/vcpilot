# VCPilot 📈

> **Institutional-grade algorithmic trading on ASX stocks — fully automated, locally deployable.**

AstraTrade is a production-grade automated trading system. It screens the ASX universe daily, detects Volatility Contraction Patterns (VCP), manages risk with precision, and executes trades via Interactive Brokers (equities) or Independent Reserve (crypto via ccxt) — all controlled remotely via WhatsApp.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                   Docker Compose                     │
│  ┌──────────┐  ┌──────────┐  ┌────────────────────┐ │
│  │ Database │  │  Redis   │  │  IBKR (Gateway)    │ │
│  │(Postgres)│  │          │  │  (paper/live)      │ │
│  └──────────┘  └──────────┘  └────────────────────┘ │
│  ┌──────────────────────────────────────────────┐   │
│  │  Worker & Beat (Celery)                      │   │
│  │  ├── worker-equities (equities screen/trade) │   │
│  │  ├── worker-crypto (crypto trade 24/7)       │   │
│  │  └── beat (scheduler — AEST aligned)         │   │
│  └──────────────────────────────────────────────┘   │
│  ┌──────────────────────┐   ┌──────────────────────┐  │
│  │  API Service (UI)    │   │  WhatsApp Service    │  │
│  │  FastAPI + Jinja2    │   │  → WhatsApp Agent    │  │
│  │  port 8501           │   │  port 3000           │  │
│  └──────────────────────┘   └──────────────────────┘  │
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
# Edit .env — set system ports, database credentials, and SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD
```

### 2. Start services
```bash
# Automatically initializes tables, seeds configs, and runs SaaS migrations
docker compose up -d
```

### 3. Open dashboard
```
http://localhost:8501
```
- **Email OTP Login:** The default authentication method. Enter your email on the **Email OTP** tab on `/login`. A 6-digit passcode will be emailed to you (or displayed in the terminal logs and redirected URL query parameters in development mode). Enter the passcode to authenticate.
- **Traditional Password Sign-In:** Available on the **Password** tab on `/login`.
  - **Super Admin Credentials:** Set via `.env` (`SUPERADMIN_EMAIL` and `SUPERADMIN_PASSWORD`). Allows you to manage tenants, rules, and global users.
  - **Organization Admins & Users:** Seeded or created passwordlessly by the Super Admin.
- **Passwordless Creation & Password Setup Reset:** No passwords are input during tenant or user creation. Users are created with a secure random hash. When creating a user, the admin can toggle the option to automatically send an onboarding welcome email. If SMTP is offline or the email is skipped, a copyable manual link is generated in the UI. Trigger the **Reset Password** flow from the Super Admin panel to send a setup link.
- **Organization Switcher:** Super Admins can switch context to any tenant organization using the top-right header selector to view scoped dashboards and configure settings.

### 4. Start trading services (paper mode — ALWAYS start here)
```bash
# Configure credentials & settings (IBKR account, username, password, paper mode) on the http://localhost:8501/admin/config page under your Tenant Admin session first
docker compose --profile trading up ibkr -d
```

---

## AstraTrade Rules Implemented

All rules are configurable per organization via the Rules Config page, allowing Organisation Admins and Super Admins to enable/disable rules or update thresholds independently. The Super Admin can also customize the default tier configurations inherited by organizations.

**Trend Template** (8 criteria — all must pass): Price vs 200/150/50MA alignment, 200MA slope, 52-week range position, RS ≥ 70

**Fundamentals**: EPS growth ≥ 25% (recent + annual), EPS acceleration, Revenue growth ≥ 25%, ROE ≥ 17%, improving margins, institutional ownership

**VCP Pattern**: 3+ tightening contractions, volume dry-up ≤ 50% avg, breakout volume ≥ 150% avg, entry within 5% of pivot

**Market Regime**: ASX200 above 200MA, ≥ 60% stocks above 200MA, ≤ 4 distribution days in 25 sessions

**Exit Rules — Defensive**: Hard stop (mandatory), time stop (not up 10% in 3 weeks), earnings avoidance, 50MA break on volume

**Exit Rules — Offensive**: Partial exit at 20%, full exit at 40%, climax top detection, parabolic move, 3-weeks-tight hold rule

**Risk**: Max 2% capital per trade, max 30% per position, max 15% portfolio heat, pyramid into winners (2 add-ons, +2% profit first)

---

## WhatsApp Commands

> [!NOTE]
> WhatsApp commands are routed to the specific tenant organization context based on the session (`org_{org_id}`) and restricted to the sender matching that organization's configured `whatsapp_admin_number`.

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

## Data Sources & Delays

| Source | Used for | Delay |
|---|---|---|
| yfinance (EOD) | Daily price bars, MAs, screener | Next day |
| yfinance (15-min interval) | Intraday entry check price | ~15–20 min (ASX free tier) |
| IBKR real-time | Intraday entry check price (if connected) | Real-time (0 min) |
| FMP free tier | Supplemental fundamentals for shortlisted stocks | ~EOD |

> **Note:** The entry check task runs every 5 minutes during market hours. With yfinance (default), the price checked is approximately 15–20 minutes behind the live market. Connect IBKR Gateway for real-time data. The Admin → Data Log page shows the data source and delay on every snapshot so you always know exactly how fresh the data is.

---

## Schedule (AEST)

### ASX / Equities (Australia)
| Time | Task |
|---|---|
| Sunday 8:00pm | Refresh ASX200 universe (constituents from Wikipedia) |
| Mon–Fri 5:00pm | Refresh EOD price data (yfinance) |
| Mon–Fri 5:15pm | Evaluate market regime (BULL/CAUTION/BEAR) |
| Mon–Fri 5:30pm | Run daily screener (generate signals) |
| Mon–Fri 6:00pm | Daily WhatsApp report (P&L summary) |
| Every 5 min (10am–4:12pm Mon–Fri) | Intraday entry breakout checks & defensive exit rules |
| Every 15 min (market hours Mon–Fri) | Sync stop orders (IBKR) |

### NYSE / NASDAQ (US Equities)
| Time | Task |
|---|---|
| Tue–Sat 7:00am | Refresh EOD price data (yfinance) |
| Tue–Sat 7:30am | Run daily screener (generate signals) |
| Mon–Fri 11:00pm – Tue–Sat 6:00am | Intraday entry breakout checks & defensive exit rules (NYSE session) |

### Crypto (Independent Reserve & Others — 24/7)
| Time | Task |
|---|---|
| Every 5 min (24/7) | Intraday entry breakout checks & defensive exit rules |
| Every 5 min (24/7) | Sync stop orders (trailing stop tracking & stop-out detection) |
| Every 5 min (24/7) | Refresh current price & unrealised position P&L |
| 12:30am, 6:30am, 12:30pm, 6:30pm | Refresh price data (6-hour interval) |
| 12:45am, 6:45am, 12:45pm, 6:45pm | Run screener (4× daily VCP screening) |

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
│   ├── screener/     # AstraTrade rules, VCP, exit rules
│   └── tasks/        # Celery tasks
├── dashboard/        # FastAPI + Jinja2 dashboard (4 trading + 4 admin pages)
├── docker/           # Dockerfiles
├── migrations/       # DB schema + TimescaleDB setup
├── scripts/          # init_db.py, seed_config.py
├── tests/            # pytest regression suite — critical trading paths
├── .env.example
├── docker-compose.yml
├── pytest.ini
└── requirements.txt
```

---

## Testing

VCPilot ships with a pytest regression suite (`tests/`) covering the critical watchlist → signal → position → trade lifecycle — the paths where a silent bug means real capital goes unmanaged (e.g. a stop-loss that never fires, or a manual action that appears to do nothing). Tests run the real production code against an isolated in-memory database, so they're safe to run anytime with no risk to live data.

```bash
pip install -r requirements.txt   # installs pytest + pytest-mock
pytest                            # runs the full suite from the project root
```

Covered scenarios include: watchlist → signal promotion (including the rollback behaviour when the task queue is unavailable), automated and manual position-closing (stop-loss exits, MCP tool calls), and crypto vs equity position classification. See `STATUS.md` for the bug-fix history these tests guard against, and `CLAUDE.md` § Testing for how to extend the suite.

---

## Production & Cloudflare Tunnel Deployment

To run VCPilot in a production environment behind a reverse proxy like Cloudflare Tunnel:

1. **Set Production Mode**:
   In your `.env` file, change the environment to:
   ```bash
   APP_ENV=production
   ```
   This disables auto-reloading for the web server, saves CPU usage, and stops printing verbose SQL queries to logs.

2. **Configure Cloudflare Tunnel**:
   - Point your Cloudflare Tunnel hostname (e.g. `https://vcpilot.yourdomain.com`) directly to the local container or host port `http://localhost:8501`.
   - The web container is automatically configured with `--proxy-headers` and `--forwarded-allow-ips='*'` to correctly translate Cloudflare's `X-Forwarded-Proto` and `X-Forwarded-For` headers. This ensures that session cookies, URL schemas (`https`), and redirect headers work flawlessly without loops.

3. **WhatsApp Webhooks**:
   - Webhooks from the self-hosted WhatsApp container (`vcpilot-whatsapp` at port `3000`) to the API container (`vcpilot-api` at port `8501`) communicate internally within the Docker bridge network (`vcpilot-net`).
   - The default `WAHA_HOOK_URL=http://api:8501/webhook/whatsapp` in `.env` is fully container-to-container and doesn't need to be exposed to the public internet, guaranteeing fast and secure webhook delivery.

---

## Disclaimer

VCPilot is for personal use and educational purposes only. Not financial advice. Trading involves significant risk of loss. Always paper trade before going live.