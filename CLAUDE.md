# VCPilot — Agent Context & Developer Guide

> Read this file first before touching any code. It captures every architectural decision, current state, and pattern used throughout the project.

---

## What This Is

VCPilot is a multi-market automated stock trading system built on **Mark Minervini's SEPA (Specific Entry Point Analysis)** methodology — specifically the Volatility Contraction Pattern (VCP). It supports ASX equities, US equities (NYSE/NASDAQ), and has a crypto trading foundation. Users build custom watchlists by adding instruments from any supported exchange; VCPilot fetches price data on-demand, screens against Minervini rules, generates signals, and executes bracket orders through Interactive Brokers (equities) or ccxt (crypto). It is controlled remotely via WhatsApp.

**Owner:** admin@astradigital.com.au (Australia — AU-based, not US)  
**Repo:** github.com/anupamwagle/vcpilot  
**Working folder:** C:\vcpilot (WSL: /mnt/c/vcpilot)

---

## Architecture

```
Docker Compose (9 services):
  database        TimescaleDB (PostgreSQL 16 + timescaledb extension)
  redis           Redis 7 — Celery broker + result backend
  app             Database setup & migration runner — runs init_db + migrate_saas
  worker-equities Celery worker for equities (queues: screening_equities, trading_equities, reporting, default)
  worker-crypto   Celery worker for crypto (queues: trading_crypto, screening_crypto)
  beat            Celery Beat — AEST-aligned schedule
  api             FastAPI + Jinja2 + Flowbite/Tailwind — port 8501
  whatsapp        WAHA (WhatsApp HTTP API, self-hosted) — port 3000
  ibkr            IBKR Gateway (--profile trading only, not started by default)
```

**Data flow:**
```
yfinance (EOD history) → Celery screen → Minervini rule engine → Watchlist
Intraday (Broker API / IR API / yfinance) → Signal monitor → Entry trigger
Position tracked (Trailing Stop / ATR) → Exit rules → Trade closed → Audit logged
Comms Hub (WhatsApp/Telegram) report
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
| Notifications | app/notifications/ | WhatsApp/WAHA & Telegram (Two-way) |
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
│   │   │                     compute_rs_ratings(), get_asx200_tickers(), get_asx200_metadata()
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

### 32. ASX Universe Scope — Small Cap Coverage
`asx_universe_scope` is a `SystemConfig` key (per org, default `"ASX200"`) controlling which stocks are loaded by `refresh_universe` and screened by `run_daily_screen`/`_run_screen_force`.

| Value | Stocks | Source | Screener runtime |
|---|---|---|---|
| `ASX200` | ~200 | Wikipedia ASX200 | ~5 min |
| `ASX300` | ~300 | Wikipedia ASX300 | ~8 min |
| `ALL_LISTED` | ~2,200+ | ASX website CSV | ~15–45 min |

`refresh_universe(scope, organization_id)` — accepts an explicit scope or reads from SystemConfig. Sets `in_asx200`, `in_asx300`, `in_index`, `index_name`, `market_cap` on Stock rows. For `ALL_LISTED` it chains `get_asx300_metadata()` (for index flags) then `get_asx_all_listed()` (for the full list).

Config seeded in `seed_config.py` and `migrate_saas.py`. Admin Config shows a smart select dropdown. Health page has "🌏 Refresh ASX Universe" button with scope selector.

### 33. Watchlist Sector Label Auto-Categorisation
`infer_sector_label(sector, industry) -> str | None` in `app/data/fetcher.py` maps GICS sector/industry strings to 24 label categories using priority-ordered keyword matching.

**Auto-assignment flow:**
- `_upsert_watchlist()` — calls `_auto_assign_sector_label()` on every screener add/update (only fills unlabelled items)
- `screen_single_ticker()` — calls `_auto_assign_sector_label()` when no explicit `label_id` provided by user
- `recategorise_watchlist_labels(organization_id, force)` Celery task — bulk-assigns labels to existing watchlist (force=True overwrites all)

**Label priority:** Only fills blank `label_id`. User-set labels (Favourites, High Priority, VCP Forming, Under Review) are preserved unless `force=True`.

`_get_or_create_sector_label(name, org_id, db)` — looks up label by name, creates with preset colour if missing (sort_order=100).

19 ASX sector labels seeded per org in `migrate_saas.py` at sort_order 20–38. Health page has "🏷 Re-categorise Labels" button.

### 29. Crypto Universe Bootstrap
There is no scheduled `refresh_universe` equivalent for crypto — unlike ASX which scrapes Wikipedia weekly. Instead:
- `get_top_crypto_tickers(exchange_key)` in `fetcher.py` returns the top-100 crypto tickers (hardcoded by market cap) in yfinance format. `TOP_CRYPTO_SYMBOLS` is the base list.
- `refresh_crypto_universe(exchange_key)` Celery task seeds `Stock` records for these 100 tokens.
- `refresh_price_data(exchange_key=CRYPTO_*)` auto-calls `refresh_crypto_universe` inline when zero stocks found — no manual step needed.
- Health page has a **"🪙 Seed Crypto Universe"** button per exchange for explicit manual seeding.
- `/action/refresh-data` for crypto exchanges chains `refresh_crypto_universe → refresh_price_data`.

**Crypto first-run order**: Seed Universe → Refresh Data → Evaluate Regime → Force Screen.

**Critical gotchas:**
- Signal `exchange_key` must be the Stock's actual key (e.g. `CRYPTO_INDEPENDENTRESERVE`), NOT the generic `"CRYPTO"` sweep key. `_run_screen_force` always resolves via `stock_obj.exchange_key`.
- Trading task crypto filter: `Signal.exchange_key.in_(["CRYPTO","CRYPTO_INDEPENDENTRESERVE","CRYPTO_BINANCE","CRYPTO_COINBASE","CRYPTO_KRAKEN"])` — not `.like("CRYPTO_%")` which misses the generic key.
- Crypto is always a trading day (calendar returns `True`). `refresh_price_data` skips the trading-day gate for all CRYPTO* exchange keys.
- Date gate in `refresh_price_data` is relaxed for crypto: accepts yesterday's bar (yfinance returns UTC date, can lag AEST by one day).

### 9. Screener action routes — always use `_run_screen_force`, not `run_daily_screen`
Dashboard screener buttons (`/action/run-screener`, `/action/force-screen`) both call `_run_screen_force.delay()`.  
**Never** wire a UI button to `run_daily_screen.delay()` — that task has a `today_is_trading_day()` guard at the top and silently returns on weekends/holidays with no user feedback.  
`_run_screen_force` bypasses the gate and is the correct target for any manual trigger.  
Both routes wrap `.delay()` in a `try/except` so a Redis/worker outage doesn't crash the HTTP response — the task will queue when the worker comes online.

### 10. WhatsApp — org-level setup vs .env
Each org configures its own WhatsApp phone number via `/admin/config` (key: `whatsapp_admin_number`, value: digits-only e.g. `61450325233`). The `whatsapp_api_key` and `whatsapp_session_name` are pre-seeded at org creation from `.env` defaults — org admins do NOT need to touch `.env`. For WAHA Core (single Docker instance) all orgs share session `"default"`. For WAHA Plus, set a unique `whatsapp_session_name` per org.

### 11. Worker heartbeat — global + per-org
`health_check` writes `last_heartbeat` with `organization_id=NULL` (global, legacy) AND `last_heartbeat` per-org for every active org. `_global()` in `main.py` reads the per-org row first, falls back to global. This means each org's Health page shows the correct worker status independently.

### 12. Global rules → org rules inheritance
Global rules (`organization_id IS NULL`) are the master templates. Org rules are **cloned** from globals on org creation. They are independent after that. To push global changes to all orgs, use `POST /superadmin/rules/sync-all` (soft) or `/superadmin/rules/sync-all?force=1` (hard). The superadmin rules page has dedicated buttons.

### 15. Manual triggers are org-scoped
`_run_screen_force(organization_id=None)` and `send_daily_report(organization_id=None)` accept an optional org_id. When called from the dashboard action routes, they always pass the current user's org_id. When called by Celery Beat (scheduled), no org_id is passed → both tasks loop all active orgs. `refresh_price_data` and `evaluate_market_regime_task` are always global (shared data, no org scoping). Never call `_run_screen_force.delay()` without `organization_id` from dashboard routes — it would run for all orgs unnecessarily.

### 16. AuditLog includes user_id
`AuditLog.user_id` (FK → users.id, nullable, SET NULL on delete) captures the logged-in user for every manual action. `AuditLog.actor` now stores the user's email (e.g. `admin@astradigital.com.au`) instead of the generic string `"dashboard"`. Automated tasks still use `"system"`, `"agent"`, etc. The audit log page (`/admin/audit`) has an actor/user filter field.

### 17. Mobile UI — sidebar as a drawer
The sidebar is always a slide-in drawer (never fixed to the left of the viewport). JS function `openSidebar()`/`closeSidebar()` handles transform, overlay visibility, and body scroll-lock. On ≥1024px, `checkBreakpoint()` auto-opens the drawer and sets `margin-left: 16rem` on `#main-content`. On mobile, the drawer starts closed. All nav links call `closeSidebar()` on click. The overlay div `#sidebar-overlay` catches outside taps. Do NOT use `lg:ml-64` Tailwind class for main content margin — the JS controls this dynamically.

### 13. Position exit — Minervini SEPA
`POST /positions/{pos_id}/close` accepts `exit_reason` (ExitReason enum key) and optional `exit_price`. The positions page renders an inline close form per row with all Minervini exit reasons grouped as Defensive / Offensive. Confirming: marks Position CLOSED, creates Trade record, writes audit, sends WhatsApp exit alert.
Exit reasons: `STOP_LOSS`, `TRAILING_STOP`, `TIME_STOP`, `EARNINGS_AVOID`, `MARKET_REGIME`, `PROFIT_TARGET_1`, `PROFIT_TARGET_2`, `CLIMAX_TOP`, `THREE_WEEKS_TIGHT`, `MANUAL`.

### 14. Old Streamlit files still exist
`dashboard/Home.py` and `dashboard/pages/` still exist (can't delete via sandbox — Windows/WSL permission issue). They are ignored because the Dockerfile runs `uvicorn dashboard.main:app`, not streamlit. Delete them manually from WSL: `rm -rf /mnt/c/vcpilot/dashboard/Home.py /mnt/c/vcpilot/dashboard/pages/`

### 18. Watchlist Labels (multi-group watchlist)
`WatchlistLabel` model in `app/models/signal.py` — one row per user-defined label per org. Columns: `id`, `organization_id`, `name`, `color` (hex), `is_default`, `sort_order`. Migration adds `watchlist_labels` table and `label_id` FK on `watchlist`. Default labels seeded per org via `migrate_saas.py`: Favourites (amber `#f59e0b`, is_default=True), High Priority (red), VCP Forming (blue), Under Review (violet). Routes: `GET /watchlist?label={id}` filters by label; `POST /watchlist/labels/create` creates a label; `POST /watchlist/{id}/set-label` assigns a label to a stock. `screen_single_ticker` accepts `label_id` so manual adds land in the right group. `LABEL_COLOUR_PALETTE` in `signal.py` lists the 8 preset hex colours shown in the colour picker.

### 19. Per-org timezone (org_timezone)
`org_timezone` is a `SystemConfig` key seeded per org with value `Australia/Sydney`. Appears in `/admin/config` under General. Used for timestamps in WhatsApp reports. Celery Beat schedules are global on `timezone="Australia/Sydney"` in `celery_app.py` — this is correct since ASX is in Sydney and all orgs trade on ASX. Do NOT change the Beat timezone — change `org_timezone` in SystemConfig if a user's local reporting timezone differs.

### 20. Background job audit trail (entry/exit tasks)
`check_entry_triggers` and `check_exit_rules_task` write a `TASK_RUN` `AuditLog` row on every invocation, including when the market is closed. Timestamps are formatted in AEST (`Australia/Sydney`) timezone to align with the ASX. Furthermore, if `check_entry_triggers` skips checking because the market is in a BEAR regime, has reached max positions, or has trading paused, it writes a detailed `TASK_RUN` audit log row for each pending signal explaining the skip reason to populate the UI and prevent "No entry check yet" blank states.

**Entry check log format (not triggered):** `❌ {ticker} @ ${price} [{data_source}] | pivot ${pivot} — {reason1}; {reason2}`  
**Entry check log format (triggered):** `✅ {ticker}: breakout confirmed @ ${price} [{data_source}] — submitting order`  
**Exit check log format (holding):** `Exit check @ {HH:MM}: holding | Price ${price} | P&L {pct}% | ({criteria_summary})`  
**Exit check log format (exit triggered):** `Exit check @ {HH:MM}: EXIT triggered — {reason} | Price ${price} | P&L {pct}% | Reason: {message}`

The Positions page exit check sub-row surfaces these details as structured chips: timestamp | price | P&L% | reason. Color-coded red for exit triggers, neutral for holding.


### 23. Intraday Price Fetcher — `get_intraday_price()`
`app/data/fetcher.py` exports `get_intraday_price(ticker, organization_id)` which:
1. If IBKR connected → calls `IBKRBroker.get_market_snapshot()` (real-time, 0 min delay)
2. Else → yfinance `history(period="2d", interval="15m")` — ASX free tier ≈ 15-20 min delayed
3. Last resort → returns `ok=False` so caller falls back to EOD close

Always returns `{price, volume, data_source, delay_mins, bar_timestamp, ok}`. Used by `check_entry_triggers` every 5 min during market hours. `data_source` and `delay_mins` flow through to `entry_check_logs` and are surfaced in the Data Log UI with a delay banner.

### 24. Entry Check Log — `entry_check_logs` table
`app/models/market.py::EntryCheckLog` — per-org, per-signal intraday snapshot written on every `check_entry_triggers` run per pending signal. Stores: `price_current`, `price_pivot`, `price_vs_pivot` (% above/below pivot), `vol_ratio`, MAs, RS, `breakout_confirmed`, `rule_results` (JSON), `data_source`, `data_delay_mins`, `bar_timestamp`. Indexed on `(organization_id, checked_at DESC)`. The Admin Data Log page queries this table; the AuditLog continues to receive entries in parallel.

### 21. Redis Caching & Eager Loading (Performance Optimization)
To ensure the dashboard remains fast and responsive:
- Cache stock name mappings in Redis (`stock_names_map`) for 1 hour using `get_cached_stock_names(db)`.
- Cache the latest price bar details in Redis (`latest_price_bar:{ticker}`) for 5 minutes (or 1 hour if not found) during market hours.
- Use `joinedload()` (e.g., `joinedload(Watchlist.label)`) to eager-load relationships inside loops to prevent N+1 query bottlenecks.
- Throttle external API fetches (like yfinance/FMP) on missing data using a 24-hour marker (`missing_name_fetch:{ticker}`).

### 25. Multi-Market Architecture — Exchange, Currency, On-Demand Data

**ExchangeConfig** (`app/models/exchange.py`) is a global table managed by super admin. One row per trading venue. Super admin enables/disables exchanges. Orgs activate from the enabled set via `active_exchanges` SystemConfig key.

**Ticker conventions (canonical yfinance format):**
| Exchange    | yfinance ticker | Display code | IBKR contract          |
|-------------|-----------------|--------------|------------------------|
| ASX         | `BHP.AX`        | `BHP`        | `Stock("BHP","ASX","AUD")` |
| NYSE/NASDAQ | `AAPL`          | `AAPL`       | `Stock("AAPL","SMART","USD")` |
| Crypto      | `BTC-USD`       | `BTC`        | ccxt `BTC/USDT` symbol |

**`normalize_ticker(user_input, exchange_key)`** in `fetcher.py` converts raw user input to yfinance format. Always call this before storing or fetching.

**On-demand data flow:** User adds AAPL + NYSE on /watchlist → `screen_single_ticker.delay("AAPL", exchange_key="NYSE")` → fetches 2yr yfinance history → stores in central `price_bars` table (shared across orgs) → runs Minervini rules → Signal or Watchlist entry. Price data is NEVER per-org — it lives in global `stocks` + `price_bars` tables.

**FX rate:** `get_fx_rate("AUD", "USD")` fetches AUDUSD=X from yfinance, cached 1hr in Redis. Never hardcode FX. `calculate_position_size()` accepts `currency` + `fx_rate_aud` params and returns both native and AUD-equivalent values. Portfolio heat always aggregates in AUD.

**MarketRegimeRecord** replaces the single `last_market_regime` SystemConfig key. One row per evaluation per exchange. `evaluate_market_regime_task(exchange_key="ASX")` writes here + updates `last_market_regime_ASX` SystemConfig key per org (for dashboard display). Crypto regime only checks index-above-200MA (skips breadth + distribution days).

**Celery Beat — multi-market schedules (all AEST):**
- ASX: data 5pm, screener 5:30pm, entry/exit every 5min 10am–4:12pm Mon–Fri
- NYSE: data 7am Tue–Sat, screener 7:30am, entry/exit checks 11pm–6am (NYSE session)
- Crypto: entry/exit every 15min 24/7, data refresh midnight UTC
- All trading tasks accept `exchange_key` kwarg.

**IBKR multi-exchange routing:** `IBKRBroker._build_contract(ticker, exchange_key)` routes correctly. Strip `.AX` or `-USD` suffix before passing to IBKR. US orders use `"SMART"` primary exchange with USD currency.

**Crypto broker:** `app/broker/crypto.py::CryptoBroker` wraps ccxt. Simulate mode when no credentials. `_yfinance_to_ccxt("BTC-USD")` → `"BTC/USDT"`. Bracket orders emulated: entry limit + stop-market + take-profit limit (not native OCO). Org admin provides API key/secret via `/admin/config`.

**New global config keys (super admin, no org_id):** None added — ExchangeConfig rows replace the need for global keys.

**New per-org SystemConfig keys:**
- `active_exchanges` — comma-separated: `"ASX"`, `"ASX,NYSE"`, `"ASX,CRYPTO_BINANCE"`
- `working_capital_currency` — base currency code for position sizing (e.g. AUD, USD, USDT, BNB; read-only for org admins, updated by super admin)
- `ibkr_account_usd` — USD account (leave blank to use same account as AUD)
- `fx_audusd_override` — manual rate override for testing
- `crypto_exchange_key` — active crypto exchange: `"CRYPTO_BINANCE"`
- `crypto_api_key` / `crypto_api_secret` / `crypto_testnet`
- `last_market_regime_ASX` / `last_market_regime_NYSE` / `last_market_regime_NASDAQ`

### 26. Exchange Filter Bar — `_get_exchange_filters()` + `components/exchange_filter.html`
`_get_exchange_filters(org_id, db)` in `dashboard/main.py` reads `active_exchanges` SystemConfig for the org and returns filter tab options: `[{key, label, flag, asset_type}]`. Always includes "All". Groups NYSE+NASDAQ as "US", all `CRYPTO_*` keys as "Crypto". If only ASX is active, returns only `[{All}]` — no filter bar renders.

`_apply_exchange_filter(query, model, exchange_filter)` applies the filter to a SQLAlchemy query:
- `"ASX"` → `model.exchange_key == "ASX"`
- `"US"` → `model.exchange_key.in_(["NYSE","NASDAQ"])`
- `"CRYPTO"` → `model.asset_type == "CRYPTO"`
- `"ALL"` / `""` → no filter (return query unchanged)

The template include `dashboard/templates/components/exchange_filter.html` renders the pill tabs using `exchange_filters`, `active_exchange_filter`, `base_url`, and optional `extra_params`. Included in Watchlist, Signals, and Positions pages. The filter is a `?exchange=` query param.

### 27. Admin Config — `FIELD_HINTS` Pattern
`admin_config` GET route in `main.py` defines a `FIELD_HINTS` dict mapping config `key` → UI control metadata:
- `control`: `"text"` | `"number"` | `"password"` | `"select"` | `"timezone_select"` | `"exchange_multiselect"` | `"crypto_exchange_select"` | `"readonly"`
- `placeholder`, `prefix`, `example`, `hint_extra`, `link_url`, `link_text` — rendered as hints in the template
- `options` — list of `(value, label)` tuples for select controls

Each config row dict passed to the template includes a `"hint"` key containing the matching metadata (or `{}` if not in `FIELD_HINTS`). The template branches on `ctrl = hint.get('control', 'text')` and renders the appropriate control. To add a new config key with smart UI, just add an entry to `FIELD_HINTS` in the route — no template changes needed.

### 28. Crypto Labels Auto-Seeding
When `active_exchanges` is updated via `/admin/config` to include a `CRYPTO_*` key, the config update route automatically seeds four crypto watchlist labels (Crypto Core / DeFi / Altcoins / Crypto Watch) for that org if they don't already exist. Also seeded by `migrate_saas.py` on startup for any org whose `active_exchanges` already includes crypto. Labels use sort_order 10–13 so they appear after the default equity labels (0–3).

### 30. ⚠️ `Position` vs `Trade` — exit detail belongs on `Trade`, never on `Position`
This exact confusion caused a critical live-trading bug (see STATUS.md, "Critical Bug Audit" 8 Jun Session 4): `sync_stop_orders` and the MCP `close_position` tool both tried to set `exit_price`/`exit_reason`/`closed_at`/`realised_pnl`/`opened_at` on a `Position` object and pass those as `Trade(...)` kwargs. **None of these are `Position` columns, and `Trade` doesn't accept them as constructor kwargs either** — the real `Trade` columns are `entry_date`/`exit_date`/`hold_days`/`entry_price`/`exit_price`/`gross_pnl_aud`/`net_pnl_aud`/`pnl_pct`/`initial_stop`/`exit_reason`/`cgt_eligible_discount`. SQLAlchemy raised `AttributeError`/`TypeError` on every attempt, swallowed by a broad `except Exception` — so stopped-out positions simply **stayed open forever with no visible error**, which is about the worst failure mode possible once real money is involved.

**The correct, proven pattern when closing a position** (used in the dashboard's `/positions/{id}/close` route, now also in `sync_stop_orders` and MCP `close_position`):
1. Flip `position.status = TradeStatus.CLOSED` (that's the *only* exit-related field `Position` carries, besides what it already has from being open).
2. Create a new `Trade` row with the real columns above — this is where `exit_price`, `exit_reason`, `exit_date`, P&L, etc. live.
3. Write an `AuditLog` entry.

`tests/test_position_close_paths.py` contains **schema guard tests** (`test_position_model_has_no_phantom_close_fields`, `test_trade_model_does_not_accept_phantom_kwargs`) that fail loudly if this pattern is ever violated again — run them after touching any code that closes a position.

### 31. Regression test suite — `tests/` (pytest)
A pytest suite covering the critical watchlist→signal→position→trade lifecycle lives in `tests/`. It runs the **real production code** (Celery tasks via `.run()`, FastAPI routes via direct async invocation, MCP tools via monkeypatched context) against an **isolated in-memory SQLite DB** — zero risk to the live org database.

**How it works (`tests/conftest.py`):**
- `create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)` — in-memory DB shared across connections in a test
- An autouse fixture monkeypatches `app.database.SessionLocal` to a sessionmaker bound to this test engine, so **every** code path under test (tasks, routes, MCP tools) transparently writes to the isolated DB instead of the live one
- Seed fixtures: `org_and_account` (Organization + AccountTier + Account), `open_crypto_position` (an OPEN TRX-AUD Position), `watching_trx_item` (a WATCHING Watchlist row)
- This works because every SQLAlchemy model in this project is Postgres/SQLite-portable — no JSONB/ARRAY/UUID/Postgres-only types

**Run with:** `pytest` from the project root (or `wsl bash -c "cd /mnt/c/vcpilot && pytest"`). Config in `pytest.ini` (`testpaths = tests`).

**Test files:**
- `test_watchlist_promotion.py` — dashboard rollback on Celery queue failure, happy-path success, duplicate-signal no-op handling, no-price-data rollback
- `test_position_close_paths.py` — schema guards (see #30) + end-to-end `sync_stop_orders` and MCP `close_position` tests + invalid-exit-reason rejection
- `test_crypto_position_classification.py` — crypto vs equity `Position` classification regression
- `test_mcp_get_positions.py` — `get_positions(include_closed=True)` correctness and 30-day cutoff filtering

**When to extend this suite:** any time you touch the watchlist→signal→position→trade lifecycle, `sync_stop_orders`, MCP trading tools, or anything that writes `Trade`/`Position`/`AuditLog` rows — these are the paths where a silent failure means real capital sits unmanaged. Add a test before/alongside the fix, following the monkeypatch patterns already established (`app.utils.time_helper.get_current_date`, `app.data.fetcher.get_intraday_price`, `get_notifier`/`get_mcp_context`/`assert_scope`).

### 22. Custom Exception Handlers (FastAPI/Starlette)
FastAPI/Starlette exceptions are captured dynamically to render Flowbite/Tailwind custom error pages instead of exposing raw JSON payloads:
- `StarletteHTTPException` (custom 404, etc.)
- `RequestValidationError` (custom 422 validation errors)
- Generic `Exception` (custom 500 internal server errors)
Detailed debug tracebacks are exposed in a collapsible console view if `settings.app_env == "development"`, and hidden in production.

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

## Celery Beat Schedule (AEST) — Updated

### ASX / Equities
| Time | Task |
|---|---|
| Sunday 8:00pm | `refresh_universe` — update ASX200 constituents from Wikipedia |
| Mon–Fri 5:00pm | `refresh_price_data(ASX)` — yfinance EOD for full ASX universe |
| Mon–Fri 5:15pm | `evaluate_market_regime_task(ASX)` — BULL/CAUTION/BEAR |
| Mon–Fri 5:30pm | `run_daily_screen(ASX)` — full Minervini screen, generate signals |
| Mon–Fri 6:00pm | `send_daily_report` — WhatsApp P&L summary |
| Every 5 min (10am–4:12pm Mon–Fri) | `check_entry_triggers(ASX)` — intraday breakout detection |
| Every 5 min (10am–4:12pm Mon–Fri) | `check_exit_rules_task(ASX)` — evaluate exit conditions |
| Every 15 min (market hours Mon–Fri) | `sync_stop_orders` — ASX stop sync (IBKR modify-order TBD) |
| Every 10 min | `health_check` — heartbeat to SystemConfig |

### CRYPTO (Independent Reserve) — 24/7
| Time | Task |
|---|---|
| Every 5 min (24/7) | `check_entry_triggers(CRYPTO)` — live IR price vs pivot |
| Every 5 min (24/7) | `check_exit_rules_task(CRYPTO)` — evaluate exit conditions |
| Every 5 min (24/7) | `sync_stop_orders` — crypto ATR trailing stop + stop-out detection |
| Every 5 min (24/7) | `update_position_pnl_task` — refresh current_price + unrealised_pnl in DB |
| 12:30am, 6:30am, 12:30pm, 6:30pm | `refresh_price_data(CRYPTO)` — 6-hour price refresh |
| 12:45am, 6:45am, 12:45pm, 6:45pm | `run_daily_screen(CRYPTO)` — 4× daily VCP screen |

### NYSE/NASDAQ (US Equities)
| Time | Task |
|---|---|
| Tue–Sat 7:00am | `refresh_price_data(NYSE)` — yfinance EOD for US universe |
| Tue–Sat 7:30am | `run_daily_screen(NYSE)` — US Minervini screen |
| 11pm Mon–Fri, 12-6am Tue–Sat | `check_entry_triggers(NYSE)` — NYSE session hours (AEST) |

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
- `GET /admin/data-log` — Data Log: intraday entry check snapshots from `entry_check_logs`; filters by time window/ticker/confirmed; auto-refresh via `/admin/data-log/poll`

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
- `GET /superadmin/data` — Market Data: Tab 1 = ASX universe with latest PriceBar metrics (sortable/searchable/paginated); Tab 2 = custom stocks per org not in ASX200

**Action endpoints (POST → redirect):**
- `/action/pause`, `/action/resume` — Toggle trading (scoped to organization)
- `/action/run-screener` — Queue `_run_screen_force.delay()` (bypasses trading-day gate)
- `/action/evaluate-regime` — Queue `evaluate_market_regime_task.delay()`
- `/action/ping-worker` — Queue `health_check.delay()`
- `/action/refresh-data` — Queue `refresh_price_data.delay()`
- `/action/send-report` — Queue `send_daily_report.delay()`
- `POST /positions/{pos_id}/close` — Manually close position with Minervini exit reason
- `POST /signals/{signal_id}/skip` — Skip a PENDING signal
- `POST /signals/{signal_id}/unskip` — Restore a SKIPPED signal to PENDING
- `POST /superadmin/rules/sync-all` — Push global template rules to all orgs (`?force=1` to overwrite org-customised rows)

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

## Session Handoff — Where We Are (12 Jun 2026)

**Current operational state (pick up here in next session):**

- **Dashboard UX Polish (12 Jun 2026 — Session 2):**
  - Auto page-refresh timers removed from `watchlist.html` and `signals.html` — no more forced page reloads. Silent AJAX polling kept on signals (30s) and data-log (30s).
  - Filter persistence: exchange + label filter state survives navigation via `localStorage` (`wl_filters`, `home_wl_filters`). Nav links intercept clicks and restore saved params.
  - Live screener progress widget on home dashboard: clicking Run Screener opens an inline log panel streaming `SCREENER_TICKER` audit events in real-time. New `POST /action/force-screen-async` JSON endpoint anchors the poll.
- **Crypto universe expanded (12 Jun 2026 — Session 2):**
  - IR: `get_ir_supported_tickers()` calls IR's live public API to get the exact list (~40 AUD pairs). `IR_SYMBOL_MAP` (39 coins) is authoritative and replaces old stale inline dict.
  - Generic exchanges: `TOP_CRYPTO_SYMBOLS` expanded from 100 → ~295 symbols.
  - Central Ops now shows per-exchange breakdown (seeded count + with price bars).
  - **Action required:** Click "Re-seed Crypto Universe" in Central Ops for IR to update the DB with the live coin list, then "Refresh Price Data".
- **Fixed Watchlist Exchange Filtering Bug:** Resolved the issue where crypto (e.g., TRX-AUD, SOL-USD) and US stock (e.g., AAPL, MSFT) tickers on the watchlist defaulted to `exchange_key="ASX"` and `asset_type="EQUITY"` inside the database because `_upsert_watchlist` and `screen_single_ticker`'s update branch did not propagate these columns (defaulting to model values). Also made `toggle_favourite` in `dashboard/main.py` multi-market aware. Ran a recovery script to retroactively update all 13 incorrect watchlist records in the DB.
- **Added Exchange Filters to Dashboard Watchlist Card:** Implemented the top-level exchange filters (All / ASX / US / Crypto) on the main dashboard (`/`) Watchlist Market Data section. The filter is fully integrated with the asynchronous `wlFilter` transition, preserving the active state of labels, custom stocks, and exchange selections together.

### AW Org (id=10) — Verified Live

| Item | State |
|---|---|
| Exchange | ASX + CRYPTO_INDEPENDENTRESERVE (IR) |
| Capital | A$5,000 (paper=True) |
| Crypto rules | 11 ON (6 original + 5 enhanced: RSI/MACD/vol/RR/BTC-RS) |
| Equity rules | 45 ON |
| IR universe | ⚠️ Needs re-seed via Central Ops (code now uses IR live API ~40 AUD pairs) |
| IR live prices | Confirmed: BTC $89,847 \| ETH $2,393 \| SOL $94 \| XRP $1.63 |
| Market regime | CAUTION (BTC -21% vs 200MA) — no signals, correct |
| Celery beat | 5-min entry/exit/stop/P&L crypto; 4× daily screener |

### Step 2 Pre-flight Checklist

Before the next session can begin trading, complete ALL of:

- [ ] `wsl bash /mnt/c/vcpilot/refresh_asx.sh` — verify ASX pipeline end-to-end
- [ ] `/admin/config` → set `crypto_api_key`, `crypto_api_secret`, `crypto_testnet=false` (IR live)
- [ ] `/admin/config` → set `ibkr_username`, `ibkr_password`, `ibkr_account`, `ibkr_paper_mode=true`
- [ ] `wsl docker compose --profile trading up ibkr -d` → start IBKR paper gateway
- [ ] `/admin/whatsapp` → scan QR code for AW org
- [ ] `/superadmin/organizations` → AW → MCP Credentials → Generate (all scopes) → configure in Claude Desktop
- [ ] Fund IR account → set `Account.is_paper=False` when ready for live

### Next Session Prompt

> "VCPilot Step 2 — live trading session. AW org (id=10) is ready. MCP is connected.  
> Market regime: CAUTION. Run `get_portfolio_stats()` and `get_market_regime('CRYPTO_INDEPENDENTRESERVE')`  
> then let's review any pending signals and decide on entries."

### Recovery Watchlist (when to expect first signals)

```
BTC needs to recover +21% to A$113,533 to trigger BULL regime
First signals expected from: BTC, DOGE, LINK, XRP (closest to 200MA)
Watch: check_entry_triggers fires every 5 min 24/7 — it will auto-detect breakouts
```

### Utility Scripts (in /mnt/c/vcpilot/)

| Script | Purpose |
|---|---|
| `refresh_aw.sh` | Full 7-step pipeline refresh for AW org (crypto) |
| `diag_aw.sh` | Complete diagnostic of AW org state |
| `fix_aw3.sh` | Used to seed enhanced rules + fix watchlist |
| `refresh_asx.sh` | ASX universe → price → regime → screen + IBKR test |

---

## What's NOT Built Yet (Phase 4+)

- [ ] Backtest page (Vectorbt integration — stub exists at `/admin/backtest`)  
- [ ] IBKR stop order modification API (`sync_stop_orders` works for crypto; equity stop sync TBD)
- [ ] Pyramid add-on order logic in `trading.py` (rule seeded, task logic TBD)
- [ ] CGT report export
- [ ] Multi-account support (tier system designed, single account only)
- [ ] Cloud deployment (Railway/DigitalOcean + Cloudflare tunnel)
- [ ] Sector RS ranking (entry rule seeded but not implemented in screener)
- [ ] IBKR position sync on startup (reconcile DB vs live IBKR positions)
- [ ] Intraday 4h/1h crypto screener (currently EOD/daily only)

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

**Reverse Proxy / Cloudflare Tunnel Support** — Uvicorn is configured with `--proxy-headers` and `--forwarded-allow-ips='*'` to transparently parse and trust forwarding headers (`X-Forwarded-Proto`, `X-Forwarded-For`). Auto-reload is disabled automatically when `APP_ENV=production` is set to optimize container CPU utilization.

