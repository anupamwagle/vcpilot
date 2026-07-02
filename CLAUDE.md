# AstraTrade — Agent Context & Developer Guide

> Read this file first before touching any code. It captures every architectural decision, current state, and pattern used throughout the project.

---

## What This Is

AstraTrade is a multi-market automated stock trading system built on **Mark Minervini's SEPA (Specific Entry Point Analysis)** methodology — specifically the Volatility Contraction Pattern (VCP). It supports ASX equities, US equities (NYSE/NASDAQ), and has a crypto trading foundation. Users build custom watchlists by adding instruments from any supported exchange; AstraTrade fetches price data on-demand, screens against Minervini rules, generates signals, and executes bracket orders through Interactive Brokers (equities) or ccxt (crypto). It is controlled remotely via Telegram.

**Owner:** admin@astradigital.com.au (Australia — AU-based, not US)  
**Repo:** github.com/anupamwagle/vcpilot  
**Working folder:** C:\vcpilot (WSL: /mnt/c/vcpilot)

---

## Architecture

AstraTrade is a modular monorepo: one shared codebase and one Postgres DB, but each layer below is its own docker-compose service with its own restart/scaling lifecycle — independently deployable without a rewrite into true network-separated microservices.

```
Docker Compose (9 services):
  database        TimescaleDB (PostgreSQL 16 + timescaledb extension) — shared data layer
  redis           Redis 7 — cache + Celery broker/result backend
  migrate         One-shot database setup & migration runner — runs init_db + migrate_saas (runs once, exits)
  web             Frontend + API layer: FastAPI + Jinja2 + Flowbite/Tailwind — port 8501
                  (folder: web/ — renamed from "dashboard" since that folder is the whole
                  web/API layer, not just the dashboard/home page at GET /)
  mcp-server      MCP tool-calling surface (/mcp/sse, /mcp/messages) — port 8502
                  Additive/opt-in: web also mounts the same MCP app in-process by
                  default. Independently deployable when you want to scale/restart MCP
                  traffic without touching the web app — see app/mcp/standalone.py.
  worker-equities Celery worker for equities (queues: screening_equities, trading_equities, reporting, default)
  worker-crypto   Celery worker for crypto (queues: trading_crypto, screening_crypto)
  beat            Celery Beat — AEST-aligned schedule
  ibkr            IBKR Gateway (--profile trading only, not started by default)
```

All containers except `database`/`redis`/`ibkr`/`novnc` bind-mount the repo root (`.:/app`) and auto-restart their own process on `.py` changes (uvicorn `--reload` for `web`/`mcp-server`; `watchmedo auto-restart` wrapping the Celery `worker-equities`/`worker-crypto`/`beat` commands — see `watchdog[watchmedo]` in `requirements.txt`). A `git pull` on the host is picked up with **no `docker compose restart` and no rebuild** — only a `requirements.txt` or Dockerfile change needs `docker compose up --build`. Same behaviour in prod and dev; there's no separate dev compose file.

**Layering, independent of the compose topology above:**
| Layer | Where it lives | Notes |
|---|---|---|
| Frontend + API | `web/` | Server-rendered FastAPI+Jinja2 — deliberate design (see Key Design Decisions), not a REST-only backend. Runs as the `web` docker-compose service (`docker/Dockerfile.web`) |
| MCP server | `app/mcp/` | Auth (`auth.py`), tool definitions (`tools.py`), Starlette app (`server.py`), standalone entrypoint (`standalone.py`) |
| Shared domain | `app/models/`, `app/screener/`, `app/risk/`, `app/data/`, `app/broker/`, `app/agent/`, `app/notifications/` | Imported by both `web/` and the Celery workers — this is the "core library" of the monorepo |
| Broker integration | `app/broker/ibkr.py` (ib_insync client), `app/broker/crypto.py` (ccxt client) | Library code called in-process by the equities/crypto workers — not separate network services, since they're just SDK clients around IBKR Gateway / exchange REST APIs |
| Workers | `app/tasks/` | Celery tasks, split into `worker-equities` and `worker-crypto` containers so each asset class scales/restarts independently |
| Cache | Redis | Celery broker + result backend + app-level caching (`app/utils/cache.py`) |
| Database | TimescaleDB/Postgres | Single shared DB across web, workers, and mcp-server — org-scoped via `organization_id`, not per-service schemas |

**Data flow:**
```
yfinance (EOD history) → Celery screen → Minervini rule engine → Watchlist
Intraday (Broker API / IR API / yfinance) → Signal monitor → Entry trigger
Position tracked (Trailing Stop / ATR) → Exit rules → Trade closed → Audit logged
Telegram report
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
| Notifications | app/notifications/ | Telegram (two-way — alerts + remote commands) |
| Containers | Docker Compose | Local-first, cloud-deployable |

---

## Project Structure

```
vcpilot/
├── CLAUDE.md              ← YOU ARE HERE
├── STATUS.md              ← Current operational status
├── README.md              ← User-facing documentation
├── .env.example           ← All environment variables documented
├── docker-compose.yml     ← 9 services, ibkr-gateway on --profile trading
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
│   │   └── telegram.py    ← TelegramNotifier via Telegram Bot API (resolves settings per-tenant,
│   │                         supports comma-separated multi-user chat_ids)
│   │
│   ├── agent/
│   │   └── commands.py    ← AgentCommandHandler: 13 Telegram commands (scoped)
│   │                         STATUS, POSITIONS, SIGNALS, MARKET, PAUSE, RESUME,
│   │                         REPORT, SKIP, EXIT, STOP, RULE, CONFIG, HELP
│   │
│   ├── mcp/               ← MCP server — independently deployable (see Architecture)
│   │   ├── auth.py        ← JWT create/decode, MCPContext ContextVar, scope checks
│   │   ├── tools.py       ← Tool implementations (get_positions, place_order, etc.)
│   │   ├── server.py      ← create_mcp_app() — auth middleware + FastMCP SSE app
│   │   └── standalone.py  ← Entrypoint for the standalone mcp-server container
│   │
│   └── tasks/
│       ├── celery_app.py  ← Celery app + Beat schedule (AEST-aligned)
│       ├── screening.py   ← multi-tenant daily screen looping active organizations
│       ├── trading.py     ← multi-tenant intraday check loops
│       └── reporting.py   ← send_daily_report, health_check (heartbeat every 10 min),
│                             send_notification_message, poll_telegram_updates
│
├── web/                   ← FastAPI web app (replaces Streamlit — do NOT use Streamlit here)
│   │                         Runs as the `web` docker-compose service. Named "web", not
│   │                         "dashboard" — the dashboard/home page (GET /) is just one
│   │                         route among the many this folder serves (trading, admin,
│   │                         superadmin, trader terminal).
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
# web/main.py
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

### 33. Watchlist Sector Label Auto-Categorisation (multi-exchange — ASX, US, crypto)
Classification is layered so every exchange resolves to a usable label, not just the ~80 hand-picked ASX tickers the old override-only approach covered:

1. **Crypto** — `infer_crypto_category(ticker)` in `app/data/fetcher.py` maps the ticker base symbol via the static `CRYPTO_CATEGORY_MAP` (~150 coins across Crypto Core, Layer 1, Layer 2, DeFi, Stablecoin, Exchange Token, Meme Coin, Gaming & Metaverse, AI & Data, Oracle & Infra, Privacy Coin, Payments) and **always** returns a label — unmapped coins fall back to `"Altcoins"`. Crypto never has yfinance sector/industry data, so this path bypasses keyword matching entirely. Routed via `asset_type == "CRYPTO"` or a recognised ticker suffix (`-USD`/`-AUD`/`-USDT`/`-BTC`/`-ETH`).
2. **ASX ticker overrides** — `ASX_TICKER_SECTOR_OVERRIDES` dict, ~80 well-known tickers (e.g. `CBA`/`WBC`/`ANZ`/`NAB` → `Banks`) for cases needing commodity-level granularity (Gold vs Lithium vs Iron Ore) that GICS alone can't always capture. Checked before keyword matching, works even with blank sector/industry.
3. **Keyword matching** — `infer_sector_label(sector, industry)` matches `Stock.sector`/`Stock.industry` text against `_SECTOR_LABEL_RULES` (priority-ordered keyword list). Reliable for US/NYSE/NASDAQ (yfinance populates both fields directly).
4. **ASX GICS backfill** — `get_asx_gics_map()` fetches the ASX's own official GICS *industry-group* CSV (`get_asx_all_listed()`) — far more precise than Wikipedia's ASX200/300 table, which only carries the broad Level-1 *sector* (e.g. every bank, insurer, and fund manager all just say `"Financials"`). The `refresh_asx_sector_data` Celery task backfills blank `Stock.sector`/`Stock.industry` from this map for the full ASX universe so step 3 has something precise to match. Deliberately **not** called inline from `infer_sector_label_for_ticker()` or `_auto_assign_sector_label()` — keeps those hot-path/unit-tested functions network-free. Instead it's chained ahead of the bulk recategorise task (see below).
5. **Broad-sector fallback** — `_SECTOR_FALLBACK_BY_BROAD_SECTOR` maps the 11 GICS Level-1 sector names (e.g. `"Financials"`, `"Technology"`) straight to a label as a last resort, so even a stock with no industry match still gets a sensible label as long as the top-level sector is known. Applied only inside `infer_sector_label_for_ticker()`, after keyword matching fails — `infer_sector_label()` itself still returns `None` for unrecognised input (preserved for test compatibility).

`infer_sector_label_for_ticker(ticker, sector, industry, asset_type=None)` in `app/data/fetcher.py` is the single entry point that runs all of the above in order (crypto → override → keyword → GICS-informed keyword → broad fallback).

**Auto-assignment flow:**
- `_upsert_watchlist()` — calls `_auto_assign_sector_label()` on every screener add/update (only fills unlabelled items), passing `stock.asset_type` through
- `screen_single_ticker()` — calls `_auto_assign_sector_label()` when no explicit `label_id` provided by user
- `recategorise_watchlist_labels(organization_id, force)` Celery task — bulk-assigns labels to existing watchlist (force=True overwrites all)

**Label priority:** Only fills blank `label_id`. User-set labels (Favourites, High Priority, VCP Forming, Under Review) and the crypto category labels reused as defaults (Crypto Core, DeFi, Altcoins, Crypto Watch) are preserved unless `force=True`.

`_get_or_create_sector_label(name, org_id, db)` — looks up label by name, creates with preset colour if missing (sort_order=100). Colour map now covers crypto categories too (Layer 1, Layer 2, Stablecoin, Exchange Token, Meme Coin, Gaming & Metaverse, AI & Data, Oracle & Infra, Privacy Coin, Payments).

19 ASX sector labels seeded per org in `migrate_saas.py` at sort_order 20–38. Health page "🏷 Re-categorise Labels" button now chains `refresh_asx_sector_data` → `recategorise_watchlist_labels` via `celery.chain` (see `/action/recategorise-labels` in `web/main.py`), so every run backfills ASX GICS data before re-labelling.

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

### 10. Telegram — org-level setup, multi-user chat_ids
Each org configures its own Telegram bot via `/admin/config` (keys: `telegram_bot_token`, `telegram_chat_id`). `telegram_chat_id` accepts a **comma-separated list** — one chat per org user (each DMs the bot individually) or a single shared group chat ID. `TelegramNotifier.chat_ids` parses this list; `send()` with no explicit `chat_id` broadcasts to every entry, while command replies (webhook/poller) go only to the sender's chat. Both `webhook_telegram` (`dashboard/main.py`) and `poll_telegram_updates` (`app/tasks/reporting.py`) resolve the org by checking list membership, not exact string equality — see CLAUDE.md's "Telegram Setup for Org Admins" section.

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
`POST /positions/{pos_id}/close` accepts `exit_reason` (ExitReason enum key) and optional `exit_price`. The positions page renders an inline close form per row with all Minervini exit reasons grouped as Defensive / Offensive. Confirming: marks Position CLOSED, creates Trade record, writes audit, sends Telegram exit alert.
Exit reasons: `STOP_LOSS`, `TRAILING_STOP`, `TIME_STOP`, `EARNINGS_AVOID`, `MARKET_REGIME`, `PROFIT_TARGET_1`, `PROFIT_TARGET_2`, `CLIMAX_TOP`, `THREE_WEEKS_TIGHT`, `MANUAL`.

### 14. Old Streamlit files — removed
The legacy `dashboard/Home.py` and `dashboard/pages/` Streamlit files were deleted in the 1 Jul 2026 cleanup session — no longer present. The web app runs via `uvicorn web.main:app` (see `docker/Dockerfile.web`), never streamlit. (Note: the FastAPI web app's folder itself was later renamed from `dashboard/` to `web/` on 2 Jul 2026 — see Session Handoff.)

### 18. Watchlist Labels (multi-group watchlist)
`WatchlistLabel` model in `app/models/signal.py` — one row per user-defined label per org. Columns: `id`, `organization_id`, `name`, `color` (hex), `is_default`, `sort_order`. Migration adds `watchlist_labels` table and `label_id` FK on `watchlist`. Default labels seeded per org via `migrate_saas.py`: Favourites (amber `#f59e0b`, is_default=True), High Priority (red), VCP Forming (blue), Under Review (violet). Routes: `GET /watchlist?label={id}` filters by label; `POST /watchlist/labels/create` creates a label; `POST /watchlist/{id}/set-label` assigns a label to a stock. `screen_single_ticker` accepts `label_id` so manual adds land in the right group. `LABEL_COLOUR_PALETTE` in `signal.py` lists the 8 preset hex colours shown in the colour picker.

### 19. Per-org timezone (org_timezone)
`org_timezone` is a `SystemConfig` key seeded per org with value `Australia/Sydney`. Appears in `/admin/config` under General. Used for timestamps in Telegram reports. Celery Beat schedules are global on `timezone="Australia/Sydney"` in `celery_app.py` — this is correct since ASX is in Sydney and all orgs trade on ASX. Do NOT change the Beat timezone — change `org_timezone` in SystemConfig if a user's local reporting timezone differs.

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
`_get_exchange_filters(org_id, db)` in `web/main.py` reads `active_exchanges` SystemConfig for the org and returns filter tab options: `[{key, label, flag, asset_type}]`. Always includes "All". Groups NYSE+NASDAQ as "US", all `CRYPTO_*` keys as "Crypto". If only ASX is active, returns only `[{All}]` — no filter bar renders.

`_apply_exchange_filter(query, model, exchange_filter)` applies the filter to a SQLAlchemy query:
- `"ASX"` → `model.exchange_key == "ASX"`
- `"US"` → `model.exchange_key.in_(["NYSE","NASDAQ"])`
- `"CRYPTO"` → `model.asset_type == "CRYPTO"`
- `"ALL"` / `""` → no filter (return query unchanged)

The template include `web/templates/components/exchange_filter.html` renders the pill tabs using `exchange_filters`, `active_exchange_filter`, `base_url`, and optional `extra_params`. Included in Watchlist, Signals, and Positions pages. The filter is a `?exchange=` query param.

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

Key credentials, trading settings, and API keys reside in the database (`SystemConfig` table) at the organization level. They are dynamically resolved at runtime and can be adjusted via `/admin/config` in the dashboard or via Telegram commands:
- `ibkr_account` (IBKR Account Number)
- `ibkr_username` (IBKR Gateway login username)
- `ibkr_password` (IBKR Gateway login password)
- `ibkr_paper_mode` (True/False - controls port 4002 paper vs 4001 live)
- `telegram_bot_token` (Telegram Bot Token from @BotFather)
- `telegram_chat_id` (Comma-separated Telegram chat ID(s) to send alerts to)
- `fmp_api_key` (Financial Modeling Prep API key)
- `weekly_injection_aud` (Weekly Capital Injection)

Infrastructure/system settings remain in `.env`:
| Variable | Default | Notes |
|---|---|---|
| `IBKR_PORT` | `4002` | 4002=paper, 4001=live |
| `DASHBOARD_PASSWORD` | `changeme` | Set this before exposing dashboard |
| `MCP_SERVER_PORT` | `8502` | Standalone MCP server container port (optional, additive — see Architecture) |
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
| Mon–Fri 6:00pm | `send_daily_report` — Telegram P&L summary |
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

## Web App Routes

**Client (Trading) Area:**
- `GET /` — Home: P&L stats, today's signals, open positions, quick actions (scoped to organization)
- `GET /positions` — Open positions + closed trades + stats (scoped to organization)
- `GET /signals` — Today's signals with rule badge breakdown, skip button (scoped to organization)
- `GET /watchlist` — Automatic + manual watchlist (scoped to organization)
- `GET /trader/watchlist` — Watchlist Terminal: dedicated fullscreen dark terminal for the watchlist screen. Label-grouped left panel (equity top / crypto bottom), TradingView chart with MA50/150/200 + RSI, full Minervini rule breakdown right panel, one-click promote-to-signal. Polled live via `/trader/watchlist/data` (30s) and `/trader/prices` (10s).
- `GET /trader/watchlist/data` — JSON payload: label-grouped watchlist items with rule_results, PriceBar MA metrics, vol ratio, RS, 52W range position, pending signal flags.
- `POST /trader/watchlist/promote/{item_id}` — JSON promote (in-terminal, no redirect). Same rollback-on-failure safety as `/watchlist/{id}/promote`.
- `GET /trader` — Bloomberg-style fullscreen trader terminal: TradingView chart + live signal/watchlist/position lists + contextual monitor panel (Entry/Signal/Exit). Standalone dark page (no base.html).
- `GET /trader/prices` — JSON: live prices for all active tickers (watchlist + signals + positions). Polled every 10s by trader terminal.
- `GET /trader/exit-checks` — JSON: latest exit-rule AuditLog entry per open position. Polled every 30s by Exit Monitor panel.

**Admin (Operator) Area:**
- `GET /admin/health` — Worker status, market regime, manual triggers, schedule reference (scoped to organization)
- `GET /admin/rules` — View and edit rules and tier-level configurations (scoped to organization, editable by Organisation Admins and Super Admins)
- `POST /admin/rules/{rule_id}/toggle` — Toggle a rule for the active organization
- `POST /admin/rules/{rule_id}/threshold` — Update a rule threshold for the active organization
- `GET /admin/config` — Edit tenant-scoped settings (IBKR account, Telegram details, weekly capital)
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
# Edit .env: set passwords, IBKR details

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

## Telegram Setup for Org Admins

AstraTrade is controlled remotely via Telegram — a bot per deployment, with each organization configuring its own bot token and chat ID(s). Telegram supports **multiple users per organization** out of the box: `telegram_chat_id` accepts a comma-separated list, so each org member can DM the bot from their own account and independently both receive alerts and issue commands (STATUS, POSITIONS, PAUSE, etc. — see `app/agent/commands.py`).

### First-time setup (single user or first user in an org)

1. Open Telegram and message **[@BotFather](https://t.me/BotFather)** → `/newbot` → follow the prompts. BotFather gives you a **bot token** (looks like `123456789:AAH...`).
2. In AstraTrade, go to `/admin/config` under **Alert & Chat Channels** and set `telegram_bot_token` to that token.
3. Open a DM with your new bot in Telegram and send it any message (e.g. `/start`) — this is required before Telegram will let you retrieve your chat ID.
4. Retrieve your chat ID: visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser (replace `<YOUR_TOKEN>`) and look for `"message":{"chat":{"id": ...}}` in the JSON — that number is your chat ID. (Alternatively, forward any message to **[@userinfobot](https://t.me/userinfobot)**, which replies with your ID directly.)
5. Set `telegram_chat_id` in `/admin/config` to that number.
6. Go to `/admin/comms` and click **Register Webhook** (requires the AstraTrade instance to be reachable over HTTPS — this is why Cloudflare Tunnel or a reverse proxy with TLS is needed for production).
7. Click **Test Notify** to confirm delivery, then send `HELP` from Telegram to confirm two-way commands work.

### Adding a second (or third, etc.) user to the same org

This is the case that used to silently break — a second user's messages were dropped because only one exact chat ID was ever checked. It's fixed now:

1. Have the new user open a DM with the same bot and send it any message.
2. Retrieve their chat ID the same way as step 4 above.
3. In `/admin/config`, edit `telegram_chat_id` to a **comma-separated list** of every user's chat ID, e.g.:
   ```
   111111111,222222222
   ```
4. Save. No need to re-register the webhook — it's already pointed at AstraTrade. Both users can now message the bot independently and will both receive every broadcast alert (signals, order fills, exit alerts, daily reports).

Note: command **replies** (e.g. the response to `STATUS`) go only to whichever chat sent the command, not to every configured chat — only broadcast alerts go to everyone.

### Alternative: a shared Telegram group instead of a chat-ID list

If your team prefers one shared thread instead of separate DMs, you can use a Telegram **group** instead:

1. Create a Telegram group and add your bot to it.
2. Add every org user who should see alerts/commands to the group.
3. Send any message in the group, then use the same `getUpdates` URL from step 4 above — group chat IDs are negative numbers (e.g. `-100123456789`).
4. Set `telegram_chat_id` to just that single (negative) group ID — no comma-separated list needed, since the group itself is shared by all members.

This trades individual DMs for one shared thread where everyone sees the same alerts and each other's commands — useful for small trading teams, less private than individual DMs.

### How it works under the hood

The Telegram webhook (`POST /webhook/telegram` in `web/main.py`) receives every incoming message, resolves the organization by checking whether the sender's `chat_id` is a member of any org's comma-separated `telegram_chat_id` list, then dispatches to `AgentCommandHandler` (scoped to that org) and replies only to the sender's chat via `TelegramNotifier.send(response, chat_id=...)`. A polling fallback (`poll_telegram_updates` Celery task, every 10s) uses the same list-membership check and works without HTTPS, for local/dev use. Outbound alerts (signals, fills, exits, daily reports) call `TelegramNotifier.send(message)` with no explicit `chat_id`, which broadcasts to every chat in the list.

---

## Session Handoff — Where We Are (2 Jul 2026)

**Current operational state (pick up here in next session):**

- **Web layer renamed `dashboard/` → `web/` + live code reload on `git pull` (2 Jul 2026):**
  - `dashboard/` folder renamed to `web/` (`git mv`, history preserved) — `web/main.py` + `web/templates/`. The old name was misleading: this folder is the entire FastAPI+Jinja2 frontend/API layer (trading + admin + superadmin + trader terminal routes), not just the dashboard/home page served at `GET /`. `app/` is unchanged — it remains the shared domain library (models, screener, risk, broker, tasks, mcp, etc.), separate from the web layer.
  - docker-compose service `dashboard` → `web` (container `vcpilot-dashboard` → `vcpilot-web`), `docker/Dockerfile.dashboard` → `docker/Dockerfile.web`. `docker/Dockerfile.app` (used by `migrate`/`worker-equities`/`worker-crypto`/`beat`) is untouched — no naming collision, since it was never tied to the `dashboard`/`web` service. `DASHBOARD_PORT`/`DASHBOARD_PASSWORD` env var names kept as-is on purpose, to avoid breaking existing `.env` files.
  - Updated all `dashboard.main` imports/path strings across ~10 test files (`test_central_operations.py`, `test_multi_org_membership.py`, `test_watchlist_promotion.py`, `test_watchlist_vcp_persistence.py`, `test_telegram.py`, `test_sync_all_rules.py`, `test_trader_details.py`, `test_superadmin_activity.py`, `test_activity_logging.py`, `test_asx_universe_and_labels.py`, `test_us_market_audit_fixes.py`, `test_us_equity_universe.py`) and `web/test_bootstrap.py` → `web.main` / `web/templates` / `web/main.py`.
  - **Live code reload — `git pull` needs no restart, same behaviour in prod and dev:** every long-running service (`web`, `mcp-server`, `worker-equities`, `worker-crypto`, `beat`, `migrate`) now bind-mounts the repo root (`.:/app`, via the `x-repo-volume` anchor — resolved relative to wherever `docker-compose.yml` lives, so run `docker compose` from the repo root) instead of only baking code into the image at build time. `web`/`mcp-server` run uvicorn with `--reload --reload-dir /app` unconditionally (previously `--reload` was only enabled outside `APP_ENV=production` — now always on, since one compose file serves both). Celery has no native hot-reload, so `worker-equities`/`worker-crypto`/`beat` commands are wrapped in `watchmedo auto-restart --directory=/app --pattern=*.py --recursive --` (new `watchdog[watchmedo]==4.0.1` dependency in `requirements.txt`), which restarts the celery process itself (not the container) on any `.py` change. Net effect: pulling latest code on the host is picked up automatically everywhere — `deploy.sh`'s build/restart is now only needed after a `requirements.txt` or Dockerfile change (see the header comment in `docker-compose.yml`).
  - **⚠️ Open issue on the QNAP NAS**: `migrate` failed once with `ModuleNotFoundError: No module named 'scripts'` — the relative `.:/app` bind mount didn't appear to resolve to the repo root under Container Station's dockerd, even though `pwd` in the shell was correct (`/share/Container/vcpilot/dev_user/vcpilot`). A `HOST_REPO_PATH` env-var workaround was tried and then deliberately reverted (relative `.` should just work from the right directory — no per-host config wanted). Not yet root-caused — if it recurs, check `docker inspect vcpilot-migrate --format '{{json .Mounts}}'` to see what source path Docker actually used, and confirm `docker compose` is being run from the exact repo directory (not through a wrapper/alias that changes cwd).
  - `docker-compose-nas.yml` was already deleted from the working tree (uncommitted) before this session — not touched here; apply the same `dashboard`→`web` rename to it if/when it's restored.

- **Enterprise-grade refactor: WhatsApp removed, mobile app removed, Telegram multi-user fix, MCP server made independently deployable (1 Jul 2026):**
  - **WhatsApp/WAHA fully removed** — `WhatsAppNotifier`, the `/webhook/whatsapp` + `/admin/whatsapp` routes, the WAHA docker-compose service, and all associated SystemConfig keys/seeding are gone. `get_notifier()` (`app/notifications/__init__.py`) now always returns `TelegramNotifier` — Telegram is the sole notification/remote-control channel.
  - **Telegram multi-user-per-org bug fixed** — `telegram_chat_id` now accepts a comma-separated list. Previously only one exact chat_id could ever match, so a second org user DMing the bot was silently dropped. `TelegramNotifier.chat_ids` parses the list; `send()` broadcasts alerts to every configured chat while command replies still go only to the sender. Both the webhook (`dashboard/main.py::webhook_telegram`) and the polling fallback (`app/tasks/reporting.py::poll_telegram_updates`) resolve org by list membership now, not exact-string match. Full org-admin setup walkthrough (including the multi-user and shared-group options) is in this file's "Telegram Setup for Org Admins" section.
  - **React Native mobile app removed** — `mobile/` (Expo app) and its `app/api/mobile.py` JWT-authenticated backend are gone; they were undocumented and duplicated what the mobile-responsive web dashboard already covers (see pattern #17).
  - **MCP server made independently deployable** — new `app/mcp/standalone.py` entrypoint + `docker/Dockerfile.mcp` + `mcp-server` compose service (port 8502) serve the MCP tool-calling surface (`/mcp/sse`, `/mcp/messages`) as its own container. This is additive/opt-in — the dashboard still mounts the same MCP app in-process by default, so nothing changes unless you deliberately cut over (requires a reverse-proxy change; see the module docstring). OAuth token issuance (`/mcp/oauth/token`) and the `/authorize` consent page stay on the dashboard since they need its login session.
  - **`docker-compose.yml` `api` service renamed to `dashboard`** to match the folder it actually runs — pure naming fix, zero import risk.
  - **Root-level cleanup** — deleted legacy Streamlit files (`dashboard/Home.py`, `dashboard/pages/`), a leaked `env.txt` secret that was tracked in git (rotate `APP_SECRET_KEY` if you haven't already), `docker-compose.bak.yml`, and various superseded one-off debug scripts/logs.
  - **⚠️ `docker-compose-nas.yml` still needs the same `api`→`dashboard` rename and `waha-data` volume removal** — a persistent file lock (something on the host has it open) blocked every write attempt this session. Apply that diff manually, or re-run this cleanup once whatever has it open is closed.
  - **⚠️ Found but NOT fixed — flagged as a separate task**: `dashboard/main.py` has a large block of routes defined TWICE (~lines 6647-8263 and again ~8634-9942), including `/superadmin/users`, `/superadmin/data`, `/reset-password`, `/superadmin/exchanges`, `/superadmin/operations`, several `/superadmin/action/*` routes, `/authorize`, and `/mcp/oauth/token`. FastAPI/Starlette serves the FIRST-registered definition for any duplicated path, so the second copy is dead code — and the two copies aren't identical, meaning some intended fixes may never have actually shipped. Needs a careful, dedicated investigation (diff each pair, determine which is correct, remove the dead one) — do not assume the earlier copy is automatically right.
  - **8 pre-existing pytest failures, unrelated to this refactor** (verified against a clean `git stash` baseline before and after every change in this session): `test_activity_logging.py::test_skipped_path_not_logged`, `test_entry_triggers.py::test_entry_check_portfolio_heat_within_limit_allows_entry`, `test_entry_triggers.py::test_entry_check_breakout_confirmed_opens_position`, `test_multi_org_membership.py::test_org_create_with_existing_email_adds_membership_no_400`, `test_price_range_rule.py::test_check_entry_triggers_within_range_still_opens_position`, `test_us_equity_universe.py::TestIBKRContractRouting::test_asx_still_routes_correctly`, `test_watchlist_vcp_persistence.py::test_upsert_watchlist_persists_vcp_geometry`, `test_watchlist_vcp_persistence.py::test_enrich_compute_path_computes_and_writes_back`.

- **Exchange-scoped crypto universe + IR API spam fix (15 Jun 2026 — Session 2):**
  - **`_get_ir_live_price` — no more fallback for unknown coins** (`app/data/fetcher.py`): Removed the `base.lower()` fallback that was firing an IR API call for every coin not in `IR_SYMBOL_MAP` (NEAR, LOOM, STRK, PYUSD etc.). `IR_SYMBOL_MAP` is now authoritative — if a coin isn't in it, return `None` immediately with zero network calls. Eliminates the 400-flood log spam.
  - **`refresh_crypto_universe` — orphan cleanup** (`app/tasks/screening.py`): After seeding the exchange's supported coins, now marks `is_active=False` on any Stock row with that `exchange_key` NOT in the new list, deletes their `WATCHING`/`ALERTED` watchlist entries, and expires their `PENDING` signals — with audit log entries per removal. Run "Re-seed Crypto Universe" from Health page to purge NEAR/LOOM/STRK/PYUSD from the DB.
  - **MEXC pair whitelist** (`app/tasks/screening.py` + `dashboard/main.py`): New `mexc_trading_pairs` SystemConfig key (comma-separated, e.g. `BTC-USD,ETH-USD,SOL-USD`). If set, `refresh_crypto_universe` for MEXC filters the 300-coin list down to only those pairs before seeding. Shown in `/admin/config` under Crypto with usage hint. Leave blank to use full list.
  - **Trader Watchlist `asset_type` inference** (`dashboard/main.py`): `_build_item` now uses ticker suffix as authoritative override — any `-AUD/-USD/-USDT` ticker returns `asset_type="CRYPTO"` regardless of DB column value (covers rows stored as `"EQUITY"` due to Jun 2026 screener bug). `crypto_count`/`equity_count` stats use the same logic. Crypto section now shows all coins.
  - **Stablecoin TradingView chart** (`trader_watchlist.html`): Expanded `STABLECOINS` set (`USDT`, `USDC`, `PYUSD`, `RLUSD`, `DAI`, `BUSD`, `TUSD`, `FRAX`, `LUSD`, `GUSD`) routes to `KRAKEN:XXXUSD` instead of `BINANCE:XXXUSDT` (which doesn't exist for stablecoins).
  - **70 regression tests passing** (28 IR + 42 MEXC). Test `test_ir_live_price_unknown_coin_uses_base_lowercase` → renamed `test_ir_live_price_unknown_coin_returns_none_immediately` to reflect correct no-API-call behaviour.

- **Independent Reserve (IR) Live Price Pipeline — Full Fix (15 Jun 2026):**
  - **`refresh_live_prices_cache_task` NULL asset_type fix** (`app/tasks/trading.py`): tickers with `asset_type=NULL` or `asset_type="EQUITY"` but `-AUD`/`-USD`/`-USDT` format are now correctly inferred as CRYPTO and included in the 5-min cache refresh. Previously these were silently skipped.
  - **Inline live fetch on cache miss** (`dashboard/main.py`): `/trader/prices` (10s poll) and `/trader/watchlist/data` (30s poll) now call `get_intraday_price(ticker, asset_type="CRYPTO")` inline on cache miss instead of silently falling to EOD. The `/watchlist` HTML route pre-fetches all cold-cache crypto tickers in parallel via `ThreadPoolExecutor(max_workers=8)` before rendering.
  - **`is_crypto_wl` inference**: all three route handlers detect CRYPTO from ticker format (`endswith("-AUD"/-USD/-USDT)`) — covers NULL rows from the Jun 2026 DB bug.
  - **TradingView chart fix** (`dashboard/templates/trading/trader_watchlist.html`): `BINANCE:BTCAUD` (non-existent) → `BINANCE:BTCUSDT`. Stablecoins → `KRAKEN:USDTUSD`. Matches the working logic in `trader.html`.
  - **28 regression tests** in `tests/test_ir_integration.py` — all pass. 70 total across MEXC + IR suites, 0 failures.

- **MEXC Exchange Integration & 5-Min Price Refresh Fix (15 Jun 2026):**
  - `_get_mexc_live_price(ticker)` in `app/data/fetcher.py` — MEXC public REST API, no auth, 0-delay, converts `BTC-USD` → `BTCUSDT`.
  - `get_intraday_price()` routes `-USD` crypto through MEXC (priority 2, after IR, before yfinance).
  - `CryptoBroker` (`app/broker/crypto.py`) — MEXC ccxt options + testnet→simulation guard.
  - `refresh_live_prices_cache_task` (new Celery task, every 5 min, `trading_crypto` queue) — seeds `live_price:{ticker}` Redis cache for ALL crypto watchlist + signal tickers. This fixes the 5-min UI refresh bug.
  - `update_position_pnl_task` — now also writes `live_price:{ticker}` to Redis after each fetch.
  - Admin config: MEXC option in `crypto_exchange_key` select; label auto-seeding on `active_exchanges` update.
  - `EXCHANGE_BENCHMARKS["CRYPTO_MEXC"]`, `CRYPTO_USD_EXCHANGES`, `CRYPTO_AUD_EXCHANGES` sets added.
  - 42 tests in `tests/test_mexc_integration.py` — all pass.
  - **To enable MEXC for an org:** Super Admin enables MEXC exchange → Org Admin adds `CRYPTO_MEXC` to `active_exchanges` in `/admin/config` → adds `crypto_api_key` + `crypto_api_secret` → set `crypto_testnet=false` for live trading (testnet forces simulation on MEXC).

- **Trader Watchlist Terminal (14 Jun 2026):**
  - New `/trader/watchlist` dedicated fullscreen screen for monitoring the watchlist. Bloomberg dark terminal style identical to `/trader`.
  - Left panel: label-grouped watchlist (equity top half / crypto bottom half, or filtered via ALL/EQUITY/CRYPTO tabs). Search, live prices, trend score badges, RS, VCP count per card.
  - Center: TradingView chart (MA50/150/200 amber/violet/red + Volume + RSI). Metrics bar: vs MA50/150/200, Vol Ratio, RS Rating, 52W range bar.
  - Right: full Minervini rule breakdown by category (Trend Template/VCP/Fundamentals/Crypto) with pass/fail/N-A per rule. Score chips summary. `▲ Promote to Signal` button (JSON endpoint, no redirect). Remove button.
  - Backend: `GET /trader/watchlist/data`, `POST /trader/watchlist/promote/{id}`.
  - `◈ WL` nav link added to existing trader terminal.

- **Trader Terminal complete (14 Jun 2026):**
  - Bloomberg-style fullscreen live trading view at `/trader` (dark standalone page, not extending `base.html`).
  - Three-column grid: left = tabbed lists (Signals / Watchlist / Open), centre = TradingView chart, right = contextual monitor panel.
  - Chart: toolbars hidden, MA50/150/200 + Volume auto-loaded as studies, timezone from org config, VCP price lines (pivot/stop/T1/T2) drawn via `createPositionLine()`.
  - Contextual right panel: Entry Monitor (signals tab) → Signal Monitor (watchlist tab) → Exit Monitor (positions tab).
  - Live price polling every 10s via `/trader/prices` covering all active tickers (watchlist + signals + positions). Signal prices and position P&L update live.
  - New `/trader/exit-checks` endpoint for exit monitor panel.
  - TRIGGERED signals excluded from all active views (signals page + trader).
  - Favicon, AEST timezone, scroll ticker tape with live prices all working.

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
| IR live prices | ✅ Fixed — all 3 UI endpoints live, no more 400-spam for unlisted coins |
| IR charts | ✅ Fixed — TV uses `BINANCE:BTCUSDT`; stablecoins → `KRAKEN:XXXUSD` |
| IR universe | ⚠️ Run "Re-seed Crypto Universe" on Health page to purge NEAR/LOOM/STRK/PYUSD |
| Market regime | CAUTION (BTC -21% vs 200MA) — no signals, correct |
| Celery beat | 5-min entry/exit/stop/P&L crypto; 4× daily screener |
| Test coverage | 70 tests passing (28 IR + 42 MEXC) |

### Step 2 Pre-flight Checklist

Before the next session can begin trading, complete ALL of:

- [ ] `wsl bash /mnt/c/vcpilot/refresh_asx.sh` — verify ASX pipeline end-to-end
- [ ] `/admin/config` → set `crypto_api_key`, `crypto_api_secret`, `crypto_testnet=false` (IR live)
- [ ] `/admin/config` → set `ibkr_username`, `ibkr_password`, `ibkr_account`, `ibkr_paper_mode=true`
- [ ] `wsl docker compose --profile trading up ibkr -d` → start IBKR paper gateway
- [ ] `/admin/config` → set `telegram_bot_token`, `telegram_chat_id` and `/admin/comms` → Register Webhook for AW org
- [ ] `/superadmin/organizations` → AW → MCP Credentials → Generate (all scopes) → configure in Claude Desktop
- [ ] Fund IR account → set `Account.is_paper=False` when ready for live

### Next Session Prompt

> "AstraTrade — continuing from 1 Jul 2026. AW org (id=10). Trader Terminal + Watchlist Terminal live.
> IR crypto pipeline fully fixed: live prices, TV charts, exchange-scoped universe (purges non-IR coins on re-seed). MEXC integrated.
> WhatsApp fully removed — Telegram is the sole channel, now supports multiple org users via comma-separated telegram_chat_id.
> Mobile app removed. MCP server now independently deployable (opt-in, see mcp-server compose service).
> The web app's folder was renamed `dashboard/` → `web/` and the docker-compose service `dashboard` → `web` on 2 Jul 2026; code is now bind-mounted with live reload so `git pull` needs no restart — see Session Handoff above.
> ⚠️ Action needed: (1) apply the `dashboard`→`web` rename (this repo now uses `web`, not the older `api`/`dashboard` names) + waha-data removal to docker-compose-nas.yml manually if/when that file is restored (it was deleted from the working tree), (2) rotate APP_SECRET_KEY (a leaked env.txt was removed from git), (3) go to Health page → 'Re-seed Crypto Universe' to purge NEAR/LOOM/STRK/PYUSD from DB, (4) there's a separate flagged task investigating a large block of duplicate/dead routes in web/main.py — check its status.
> Then: `get_portfolio_stats()` and `get_market_regime('CRYPTO_INDEPENDENTRESERVE')` to review state."

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

**Telegram over WhatsApp/WAHA** — WhatsApp (via the self-hosted WAHA container) wasn't proving useful in practice and added real operational weight (a whole extra Docker service, per-org WAHA sessions, QR-code pairing flow) for a channel nobody used. Telegram's Bot API needs no self-hosted gateway, supports genuine multi-user orgs via a comma-separated chat ID list (or a shared group chat), and is the sole notification/remote-control channel now.

**TimescaleDB over plain PostgreSQL** — `price_bars` is queried heavily by date range and ticker. TimescaleDB hypertable partitions by date (3-month chunks), dramatically faster for 2yr × 200 stocks of daily bars.

**Celery Beat over Airflow** — Developer knows Celery from Django. Airflow is overkill for 7 scheduled tasks. Celery Beat runs inside the same container ecosystem with no extra infra.

**Reverse Proxy / Cloudflare Tunnel Support** — Uvicorn is configured with `--proxy-headers` and `--forwarded-allow-ips='*'` to transparently parse and trust forwarding headers (`X-Forwarded-Proto`, `X-Forwarded-For`). Auto-reload is disabled automatically when `APP_ENV=production` is set to optimize container CPU utilization.

