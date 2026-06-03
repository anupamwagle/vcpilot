# VCPilot — Agent Context & Developer Guide

> Read this file first before touching any code. It captures every architectural decision, current state, and pattern used throughout the project.

---

## What This Is

VCPilot is a fully automated ASX (Australian Securities Exchange) stock trading system built on **Mark Minervini's SEPA (Specific Entry Point Analysis)** methodology — specifically the Volatility Contraction Pattern (VCP). It screens ASX stocks daily, detects VCP formations, sizes positions using Minervini's risk rules, and executes bracket orders through Interactive Brokers. It is controlled remotely via WhatsApp.

**Owner:** admin@astradigital.com.au (Australia — AU-based, not US)  
**Repo:** github.com/anupamwagle/vcpilot  
**Working folder:** C:\vcpilot (WSL: /mnt/c/vcpilot)

---

## Architecture

```
Docker Compose (8 services):
  database        TimescaleDB (PostgreSQL 16 + timescaledb extension)
  redis           Redis 7 — Celery broker + result backend
  app             Database setup & migration runner — runs init_db + migrate_saas
  worker          Celery worker (queues: screening, trading, reporting, default)
  beat            Celery Beat — AEST-aligned schedule
  api             FastAPI + Jinja2 + Flowbite/Tailwind — port 8501
  whatsapp        WAHA (WhatsApp HTTP API, self-hosted) — port 3000
  ibkr            IBKR Gateway (--profile trading only, not started by default)
```

**Data flow:**
```
yfinance (EOD data) → Celery screening task → Minervini rule engine
→ Signal generated → Risk manager sizes position → IBKR bracket order
→ Position tracked → Exit rules evaluated → Trade closed → Audit logged
→ WhatsApp report via WAHA
```

---

## Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Ecosystem for quant/finance |
| Web framework | FastAPI + Jinja2 | Lightweight, async, clean APIs |
| UI | Tailwind CSS CDN + Flowbite 2.3 | Flowbite blue/white default, dark mode toggle |
| Task queue | Celery 5 + Celery Beat | Known to developer (Django/Celery background) |
| Broker | Redis 7 | Simple, reliable |
| Database | PostgreSQL 16 + TimescaleDB | Time-series for price_bars hypertable |
| ORM | SQLAlchemy 2.0 | Sync sessions (Celery compatible) |
| Price data | yfinance (free, unlimited EOD) | No cost, covers all ASX tickers |
| Fundamentals | yfinance quarterly_financials | Free, sufficient for Minervini criteria |
| Supplemental data | FMP free tier (250 calls/day) | Only for shortlisted stocks |
| Broker API | ib_insync → IBKR Gateway | Developer familiar with IBKR |
| Notifications | WAHA → WhatsApp | Self-hosted, no Meta approval needed |
| Containers | Docker Compose | Local-first, cloud-deployable |

---

## Project Structure

```
vcpilot/
├── CLAUDE.md              ← YOU ARE HERE
├── STATUS.md              ← Current operational status
├── README.md              ← User-facing documentation
├── .env.example           ← All environment variables documented
├── docker-compose.yml     ← 8 services, ibkr-gateway on --profile trading
├── requirements.txt       ← Python deps (FastAPI, Celery, yfinance, ib_insync, etc.)
│
├── app/                   ← Core Python application
│   ├── config.py          ← Dynamic settings (loads DB SystemConfig first, falls back to .env)
│   │                         For multi-tenant SaaS, settings check organization context.
│   ├── database.py        ← SQLAlchemy engine + get_db() context manager
│   │                         IMPORTANT: expire_on_commit=False (prevents DetachedInstanceError)
│   ├── models/            ← SQLAlchemy models
│   │   ├── account.py     ← Account, Organization, OrganizationTier (BRONZE/SILVER/GOLD)
│   │   ├── auth.py        ← User, Role, Permission (RBAC models & password hashing utility)
│   │   ├── config.py      ← SystemConfig (per-org), RuleConfig (Minervini rules with tier overrides)
│   │   ├── market.py      ← Stock (universe), PriceBar (TimescaleDB hypertable)
│   │   ├── signal.py      ← Signal (screener output, scoped), Watchlist (scoped)
│   │   ├── trade.py       ← Order, Position (open, scoped), Trade (closed/CGT, scoped)
│   │   └── audit.py       ← AuditLog (APPEND ONLY — never update/delete rows, scoped)
│   │
│   ├── data/
│   │   ├── fetcher.py     ← yfinance wrapper: get_price_history(), get_fundamentals(),
│   │   │                     compute_rs_ratings(), get_asx200_tickers()
│   │   └── calendar.py    ← pandas_market_calendars ASX — is_trading_day(), market_is_open_now()
│   │
│   ├── screener/          ← Minervini rule engine
│   │   ├── rules.py       ← RuleEngine: loads RuleConfig, checks enabled/tier (BRONZE/SILVER/GOLD)
│   │   ├── trend_template.py ← 8 Minervini trend criteria + RS rating
│   │   ├── fundamentals.py   ← EPS growth, sales growth, ROE, margins, inst. ownership
│   │   ├── vcp.py            ← VCP detection: contractions, volume dry-up, pivot price
│   │   ├── market_regime.py  ← BULL/CAUTION/BEAR: index MA, breadth, distribution days
│   │   └── exit_rules.py     ← Defensive + offensive exit signals (stop, time, targets)
│   │
│   ├── risk/
│   │   └── manager.py     ← Position sizing (risk-based), portfolio heat, pyramid rules
│   │
│   ├── broker/
│   │   └── ibkr.py        ← IBKRBroker: connect(), submit_bracket_order(), get_positions() (scoped)
│   │                         Falls back to _simulate_order() when IBKR not connected
│   │
│   ├── notifications/
│   │   └── whatsapp.py    ← WhatsAppNotifier via WAHA REST API (resolves settings per-tenant)
│   │
│   ├── agent/
│   │   └── commands.py    ← AgentCommandHandler: 13 WhatsApp commands (scoped)
│   │                         STATUS, POSITIONS, SIGNALS, MARKET, PAUSE, RESUME,
│   │                         REPORT, SKIP, EXIT, STOP, RULE, CONFIG, HELP
│   │
│   └── tasks/
│       ├── celery_app.py  ← Celery app + Beat schedule (AEST-aligned)
│       ├── screening.py   ← multi-tenant daily screen looping active organizations
│       ├── trading.py     ← multi-tenant intraday check loops
│       └── reporting.py   ← send_daily_report, health_check (heartbeat every 10 min)
│
├── dashboard/             ← FastAPI web app (replaces Streamlit — do NOT use Streamlit here)
│   ├── main.py            ← All FastAPI routes + global context scoping + Super Admin endpoints
│   └── templates/
│       ├── base.html      ← Flowbite sidebar layout + CSS variable theming system
│       │                     IMPORTANT: Theme via CSS vars (:root / html.dark), NOT Tailwind dark:
│       ├── login.html     ← Themed login page (supports Email + Password)
│       ├── trading/       ← Client area routes: /, /positions, /signals, /watchlist
│       │   ├── home.html
│       │   ├── positions.html
│       │   ├── signals.html
│       │   └── watchlist.html
│       ├── admin/         ← Admin/Operator area routes (read-only rules for Org Admins)
│       │   ├── health.html
│       │   ├── rules.html
│       │   ├── config.html
│       │   └── audit.html
│       └── superadmin/    ← Super Admin only area routes (SaaS management)
│           ├── organizations.html
│           ├── org_detail.html
│           ├── rules.html
│           └── users.html
│
├── migrations/
│   └── 001_init.sql       ← TimescaleDB hypertable setup function (called by init_db.py)
│
└── scripts/
    ├── init_db.py         ← Creates tables + calls seed_config. Run once on first start.
    ├── seed_config.py     ← Seeds 40+ RuleConfig rows, 4 AccountTier rows, 1 Account, and all SystemConfig defaults.
    └── migrate_saas.py    ← Python SaaS/Multi-tenant migration and seeding script.
```

---

## Critical Patterns & Gotchas

### 1. SQLAlchemy session — `expire_on_commit=False`
```python
# database.py
SessionLocal = sessionmaker(..., expire_on_commit=False)
```
This is intentional. Without it, accessing ORM object attributes after the `with get_db()` block closes causes `DetachedInstanceError`. All templates receive plain Python dicts, but the setting prevents issues in Celery tasks too.

### 2. FastAPI DB dependency
```python
def get_db():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
```
Used as `Depends(get_db)` in routes. **Do not** use the `with get_db()` context manager in FastAPI routes — use the dependency instead.

### 3. Theme system — CSS variables, NOT Tailwind dark:
All theming in templates must use CSS variables defined in `base.html`:
```html
<!-- CORRECT -->
<p style="color:var(--text-muted)">...</p>
<div class="card">...</div>
<span style="background:var(--pos)"></span>   ← status dot

<!-- WRONG — hardcoded colours break light/dark switching -->
<p class="text-gray-400">...</p>
<div class="bg-gray-800 border-gray-700">...</div>
<span class="bg-green-500"></span>             ← NEVER use Tailwind colour classes for status
```
Key CSS vars: `--bg`, `--surface`, `--surface-alt`, `--border`, `--text`, `--text-muted`, `--text-subtle`, `--t-accent` (trading blue/emerald), `--a-accent` (admin violet), `--pos`, `--neg`, `--warn`.  
Light default = Flowbite blue/white. Dark = charcoal/emerald. Toggle via `html.dark` class on `<html>`.

`<select>` elements get `class="input"` + a `color-scheme` CSS rule in `base.html` so OS-native dropdowns respect the theme. Do not add separate colour classes to `<select>`.

### 4. Worker status detection
```python
# dashboard/main.py
def _worker_status(heartbeat_str: str) -> str:
    # "online"   = heartbeat within 15 min
    # "starting" = never received (system just booted, wait ~10 min)
    # "offline"  = heartbeat > 15 min ago
```
Trading is blocked (`trading_active = False`) when worker is not "online". Heartbeat task fires every 10 minutes via Celery Beat. On fresh start, use "Ping Worker" button on `/admin/health` to trigger it immediately.

### 5. Market regime is "Not evaluated" on fresh start
`evaluate_market_regime_task` only runs at 5:15pm AEST on weekdays. On first start, hit the "Evaluate Market" button on `/admin/health`. It requires price data in the DB first — run "Refresh Price Data" before evaluating regime.

### 6. Watchlist is automatic — no approval required
The screener adds stocks passing 6+/8 trend criteria (but not full VCP) to watchlist automatically. They auto-graduate to Signals when VCP completes. Users do NOT approve watchlist items daily.

### 7. AuditLog is append-only
Never `UPDATE` or `DELETE` rows in `audit_logs`. It is the compliance and debugging trail. All system events, config changes, rule toggles, order fills, and agent commands go here.

### 8. IBKR Gateway startup
The `ibkr` service uses Docker profile `trading`:
```bash
docker compose --profile trading up ibkr -d
```
It does NOT start with `docker compose up`. This is intentional — prevents accidental live connections. The broker falls back to `_simulate_order()` when IBKR is not connected. Always start paper mode first: `IBKR_PAPER_MODE=true`, `IBKR_PORT=4002`.

### 9. Screener action routes — always use `_run_screen_force`, not `run_daily_screen`
Dashboard screener buttons (`/action/run-screener`, `/action/force-screen`) both call `_run_screen_force.delay()`.  
**Never** wire a UI button to `run_daily_screen.delay()` — that task has a `today_is_trading_day()` guard at the top and silently returns on weekends/holidays with no user feedback.  
`_run_screen_force` bypasses the gate and is the correct target for any manual trigger.  
Both routes wrap `.delay()` in a `try/except` so a Redis/worker outage doesn't crash the HTTP response — the task will queue when the worker comes online.

### 10. Old Streamlit files still exist
`dashboard/Home.py` and `dashboard/pages/` still exist (can't delete via sandbox — Windows/WSL permission issue). They are ignored because the Dockerfile runs `uvicorn dashboard.main:app`, not streamlit. Delete them manually from WSL: `rm -rf /mnt/c/vcpilot/dashboard/Home.py /mnt/c/vcpilot/dashboard/pages/`

---

## Core Configuration & Environment Variables

Key credentials, trading settings, and API keys reside in the database (`SystemConfig` table) at the organization level. They are dynamically resolved at runtime and can be adjusted via `/admin/config` in the dashboard or via WhatsApp commands:
- `ibkr_account` (IBKR Account Number)
- `ibkr_username` (IBKR Gateway login username)
- `ibkr_password` (IBKR Gateway login password)
- `ibkr_paper_mode` (True/False - controls port 4002 paper vs 4001 live)
- `whatsapp_admin_number` (Your admin phone number, auto-derives admin JID)
- `fmp_api_key` (Financial Modeling Prep API key)
- `weekly_injection_aud` (Weekly Capital Injection)

Infrastructure/system settings remain in `.env`:
| Variable | Default | Notes |
|---|---|---|
| `IBKR_PORT` | `4002` | 4002=paper, 4001=live |
| `DASHBOARD_PASSWORD` | `changeme` | Set this before exposing dashboard |
| `WAHA_API_KEY` | `changeme-waha-key` | Any string, used by WAHA container |
| `APP_SECRET_KEY` | `changeme-secret` | Session cookie signing key — use `openssl rand -hex 32` |

---

## Minervini Rules — What's Seeded

`scripts/seed_config.py` seeds 40+ `RuleConfig` rows on first run:

**TREND_TEMPLATE (9 rules):** price vs 200/150/50MA, 200MA slope, 52-week range, RS ≥ 70  
**FUNDAMENTAL (7 rules):** EPS growth, EPS acceleration, annual EPS, sales growth, ROE, margins, inst. ownership  
**VCP (6 rules):** min contractions, base weeks, max weeks, volume dry-up, breakout volume, max extension  
**MARKET_REGIME (3 rules):** index above 200MA, breadth ≥ 60%, distribution days ≤ 4  
**ENTRY (2 rules):** sector leadership, no extended stocks  
**EXIT_DEFENSIVE (5 rules):** stop loss (mandatory), time stop, time stop weeks, earnings avoid, break below 50MA  
**EXIT_OFFENSIVE (7 rules):** profit target 1 (20%), target 1 sell %, profit target 2 (40%), climax top, climax min run, parabolic move, 3-weeks-tight  
**POSITION_SIZING (4 rules):** max risk % per trade, max position %, pyramid min profit, pyramid max count  
**PORTFOLIO (2 rules):** max positions, max portfolio heat  

All rules have `enabled_globally=True` by default. Admin can toggle any non-mandatory rule via `/admin/rules`. `exit_stop_loss` is `is_mandatory=True` — cannot be disabled.

---

## Celery Beat Schedule (AEST)

| Time | Task |
|---|---|
| Sunday 8:00pm | `refresh_universe` — update ASX200 constituents from Wikipedia |
| Mon–Fri 5:00pm | `refresh_price_data` — yfinance EOD for full universe |
| Mon–Fri 5:15pm | `evaluate_market_regime_task` — BULL/CAUTION/BEAR |
| Mon–Fri 5:30pm | `run_daily_screen` — full Minervini screen, generate signals |
| Mon–Fri 6:00pm | `send_daily_report` — WhatsApp P&L summary |
| Every 5 min (10am–4:12pm) | `check_entry_triggers` — intraday breakout detection |
| Every 5 min (10am–4:12pm) | `check_exit_rules_task` — evaluate exit conditions |
| Every 15 min (market hours) | `sync_stop_orders` — sync stops with IBKR |
| Every 10 min | `health_check` — heartbeat to SystemConfig |

---

## Authentication & Security

- **OTP Login**: Email-based One-Time Passcode (OTP) is the default sign-in option for all users. A 6-digit code is generated and sent via SMTP. In development, if SMTP is not configured or fails, the code is appended to the redirected URL as `&debug_otp=CODE` and printed to the console logs.
- **Traditional Password Login**: Available as a fallback tab on the `/login` page.
- **Passwordless User & Org Creation**: To eliminate security risks, password inputs are removed from the User and Organization creation pages. Instead, user records are created with a secure random hash.
- **Password Reset Flow**: Standard and admin users can trigger a password setup reset link. If SMTP is active, they receive a reset link; otherwise, the Super Admin is shown a copyable manual setup URL (e.g. `/reset-password?token=TOKEN`) to send manually.
- **Super Admin Active Organization Switcher**: Database users with the `Super Admin` role (or matching credentials in `.env`) can switch their active organization context via a dropdown in the top-right header, enabling them to browse and manage standard dashboards for any organization.

---

## Dashboard Routes

**Client (Trading) Area:**
- `GET /` — Home: P&L stats, today's signals, open positions, quick actions (scoped to organization)
- `GET /positions` — Open positions + closed trades + stats (scoped to organization)
- `GET /signals` — Today's signals with rule badge breakdown, skip button (scoped to organization)
- `GET /watchlist` — Automatic + manual watchlist (scoped to organization)

**Admin (Operator) Area:**
- `GET /admin/health` — Worker status, market regime, manual triggers, schedule reference (scoped to organization)
- `GET /admin/rules` — View and edit rules and tier-level configurations (scoped to organization, editable by Organisation Admins and Super Admins)
- `POST /admin/rules/{rule_id}/toggle` — Toggle a rule for the active organization
- `POST /admin/rules/{rule_id}/threshold` — Update a rule threshold for the active organization
- `GET /admin/config` — Edit tenant-scoped settings (IBKR account, WhatsApp details, weekly capital)
- `GET /admin/audit` — Filterable audit log (scoped to organization)
- `GET /admin/tasks` — Live Task Log: streams new audit events (scoped to organization)

**Super Admin Area (SaaS Operators):**
- `GET /superadmin/organizations` — List organizations and allocate tiers (Bronze, Silver, Gold)
- `POST /superadmin/organizations/create` — Create tenant organizations and bootstrap default settings + admins
- `GET /superadmin/organizations/{org_id}` — View organization users, roles, accounts, and scoped logs
- `GET /superadmin/rules` — View and edit global Minervini rules configurations per tier (Bronze/Silver/Gold)
- `POST /superadmin/rules/{rule_id}/toggle-global` — Toggle global rules status
- `POST /superadmin/rules/{rule_id}/toggle-tier` — Toggle rules status for a specific tier
- `POST /superadmin/rules/{rule_id}/threshold` — Set rule thresholds for a specific tier
- `GET /superadmin/users` — List and manage users and roles globally
- `POST /superadmin/users/create` — Create a new user under a tenant organization
- `POST /superadmin/users/{user_id}/update-role` — Update a user's role globally

**Action endpoints (POST → redirect):**
- `/action/pause`, `/action/resume` — Toggle trading (scoped to organization)
- `/action/run-screener` — Queue `run_daily_screen.delay()`
- `/action/evaluate-regime` — Queue `evaluate_market_regime_task.delay()`
- `/action/ping-worker` — Queue `health_check.delay()`
- `/action/refresh-data` — Queue `refresh_price_data.delay()`
- `/action/send-report` — Queue `send_daily_report.delay()`

---

## First-Time Setup Sequence

```bash
# 1. Clone and configure
cp .env.example .env
# Edit .env: set passwords, IBKR details, WhatsApp number

# 2. Start all core services (migrations/seeding run automatically on startup via app)
docker compose up -d

# 3. Trigger initial data load (manual — normally runs at 5pm AEST)
# Go to http://localhost:8501/admin/health → click "Refresh Price Data"
# Then click "Evaluate Market"
# Then click "Run Screener Now"

# 4. Start IBKR Gateway (paper mode)
# Ensure IBKR_PAPER_MODE=true, IBKR_USERNAME/PASSWORD set in .env
docker compose --profile trading up ibkr -d

# 5. Monitor via dashboard at http://localhost:8501
```

---

## WhatsApp Setup (WAHA)

1. Open `/admin/whatsapp` on the VCPilot dashboard under the active organization.
2. If the session isn't running, click **Start Session**. This initializes a session named `org_{org_id}` (e.g. `org_1`, `org_2`) scoped to the organization.
3. Scan the QR code shown on the page using your WhatsApp phone (Linked Devices).
4. Each organization must configure its own `whatsapp_admin_number` in `/admin/config` (or through the database `SystemConfig` table) in order to send notifications and receive commands.
5. Once connected, send `HELP` or `STATUS` from the configured admin number to the organization's WhatsApp bot.

The WAHA webhook routes incoming messages to `http://api:8501/webhook/whatsapp`. The webhook extracts the organization ID from the session name, resolves the organization settings, verifies that the message sender matches that organization's configured admin number, and executes the commands scoped to that organization's data via `AgentCommandHandler`.

---

## What's NOT Built Yet (Phase 2+)

- [ ] Backtest page (Vectorbt integration — stub exists at `/admin/backtest`)  
- [ ] Stop order modification via IBKR (`sync_stop_orders` is a placeholder)
- [ ] Pyramid add-on order logic in `trading.py`
- [ ] CGT report export
- [ ] Multi-account support (tier system is designed, single account only)
- [ ] Cloud deployment (Railway/DigitalOcean)
- [ ] Sector RS ranking (entry rule `entry_sector_leadership` not implemented in screener)
- [ ] IBKR position sync on startup (reconcile DB vs live IBKR positions)
- [ ] Intraday price feed (currently EOD only — entry checks use last close)

---

## Cost

| Item | Monthly |
|---|---|
| yfinance | $0 |
| FMP API (free tier) | $0 |
| IBKR (paper) | $0 |
| IBKR commissions (ASX live) | $6 min or 0.08% per trade |
| Local Docker | $0 |
| Cloud Phase 3 (Railway/DO) | ~$15 |

---

## Key Design Decisions (Rationale)

**FastAPI over Django** — Lighter weight, no ORM overhead, async native. Developer knows Celery from Django but the web layer here is thin (no forms-heavy CMS).

**Streamlit replaced by FastAPI+Jinja2** — Streamlit's component model caused `DetachedInstanceError` with SQLAlchemy, had no proper auth, and couldn't support the clean client/admin split. FastAPI gives full control.

**CSS variables over Tailwind dark:** — Flowbite components use CSS-only theming. Using `style="color:var(--text)"` is more reliable than maintaining dual Tailwind class pairs across 10+ templates.

**yfinance over paid APIs** — 250 FMP calls/day is enough when: yfinance handles all price/volume/MA data (unlimited), and FMP is only used for supplemental fundamentals on the shortlisted ~10-20 stocks per day.

**WAHA over Meta Cloud API** — No Meta Business verification required, runs in Docker, free. Migrate to Meta Cloud API when going SaaS (Phase 3). Same WAHA API interface, minimal code change.

**TimescaleDB over plain PostgreSQL** — `price_bars` is queried heavily by date range and ticker. TimescaleDB hypertable partitions by date (3-month chunks), dramatically faster for 2yr × 200 stocks of daily bars.

**Celery Beat over Airflow** — Developer knows Celery from Django. Airflow is overkill for 7 scheduled tasks. Celery Beat runs inside the same container ecosystem with no extra infra.
