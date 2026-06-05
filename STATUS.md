# VCPilot — Operational Status

> Last updated: 5 June 2026 AEST. Update this file when major milestones are reached.

---

## Current Phase: 2 — Multi-tenant SaaS Layer

### ✅ Done

- **Performance & Error Page Enhancements (4 Jun 2026):**
  - Optimized the `/watchlist` page load from seconds to milliseconds by using a Redis-cached universe lookup (`get_cached_stock_names`) and eager-loading labels with SQLAlchemy's `joinedload(Watchlist.label)`.
  - Implemented Redis-based caching for latest `PriceBar` stats (`latest_price_bar:{ticker}`) with a 5-minute expiration, used in both the watchlist view and `_enrich_rule_results` rule evaluation to prevent N+1 queries.
  - Registered a custom FastAPI `RequestValidationError` exception handler mapping validation errors to the premium Flowbite-styled `error.html` template.
  - Relocated the `🛠️ SIMULATOR` badge in `base.html` outside of role checks, rendering it for all roles (including superadmins) at the top of the app when `IBKR_SIMULATE=true` is enabled.

- **SaaS / Multi-tenancy Core & Security:**
  - **Signals Overrides & Entry Check Improvements (4 Jun 2026):**
    - Updated the Signals page rule results and breakout check badges to display overridden/disabled rules in orange (`var(--warn-bg)`, `var(--warn)`) with a `➔ ✗` arrow indicator.
    - Refactored `has_overrides` calculation: the active override warning badge now automatically hides if all rules pass naturally (reverting when there are no failed rules overridden).
    - Added a `⚡ Promoted` label to the Signals page for signals manually promoted from the watchlist.
    - Aligned entry check and exit check Celery task logs to print timestamps in the local `Australia/Sydney` (AEST) timezone.
    - Prevented "No entry check yet" UI confusion by logging detailed audit checks per signal when tasks bypass execution due to BEAR regime, max positions, or paused states.
    - **Sidebar Market Regime Indicator**: Moved Bull/Bear/Caution regime badge from the top navbar to the sidebar logo area so it's always visible next to "VCPilot".
    - **Company Name Labels on Signals**: Fixed missing company names on signals by adding lazy backfill from yfinance when a stock's name is not yet populated in the DB. Also added auto-population during watchlist-to-signal promotion.
    - **Production & Cloudflare Tunnel Readiness**:
      - Configured Uvicorn inside `docker/Dockerfile.dashboard` and `docker-compose.yml` with `--proxy-headers` and `--forwarded-allow-ips='*'` to correctly handle reverse proxies and SSL termination at the edge (Cloudflare).
      - Set up dynamic reloading: Uvicorn auto-reload is enabled in development mode and automatically disabled when `APP_ENV=production` is set.
      - Fixed hardcoded reset-password URL schemes in `dashboard/templates/superadmin/users.html` to respect the request's dynamic schema (`http`/`https`).
  - Multi-tenant model layout: `Organization`, `OrganizationTier` (Bronze, Silver, Gold), and RBAC (`User`, `Role`, `Permission`).
  - Database schema migrated and seeded via `migrate_saas.py` mapping default single-tenant records to a `Default Org` and generating a tenant operator `admin@vcpilot.com`.
  - Scoped data queries by `organization_id` for all models (`Account`, `SystemConfig`, `Signal`, `Watchlist`, `Position`, `Trade`, `AuditLog`).
  - Scoped Celery tasks looping over active organizations to screen and trade using individual configuration sets and rules.
  - Read-only rule config page for Organisation Admins with custom rules thresholds loaded dynamically per organization tier.
  - **Multi-Tenant WhatsApp Integration**: Scoped the WhatsApp Admin status page (`/admin/whatsapp`), the WAHA sessions (`org_{org_id}`), and the command bot (`/webhook/whatsapp` and `AgentCommandHandler`) to the active tenant organization context. Hook messages automatically parse the target organization from the session name, retrieve configurations (enabled/JID admin number) per tenant, verify the sender, and execute isolated commands. Scoped background tasks like daily reports and regime updates notify all active organizations' configured admins.
  - **Super Admin Panel UI:** Manage organizations, create and bootstrap tenants, edit rules and overrides per tier, and manage global users and roles.
  - **Organization-Scoped Rules Customization:** Added `organization_id` column to `RuleConfig`, composite unique constraints, and automated defaults cloning on bootstrap. Organization Admins can customize rules (toggle enablement, update thresholds) with full database-level isolation.
  - Refactored `/login` supporting both `.env` configured Super Admin credentials and hashed database passwords for organization users.
  - **Email-based OTP Login:** Added digit-based OTP passcode generation, email notification templates, and fallback url debug parameters.
  - **Passwordless User & Tenant Bootstrap:** Password input is removed from creation screens. Auto-generates random dummy hashes.
  - **Password Reset Flow:** Implemented reset token generation, setup links, and manual copy-link container fallbacks.
  - **Global Users Filters:** Refactored users panel with inline search and tenant organization dropdown menus.
  - **Active Tenant Switcher:** Enabled context switching for Super Admins in the header top bar, granting them full view/edit privileges scoped to the selected organization.
  - **Branding updates:** Modern SVG branding logo and favicon integration.

- Full project scaffold (52 files)
- Docker Compose stack: database, redis, app, worker, beat, api, whatsapp, ibkr
- All SQLAlchemy models (account, config, market, signal, trade, audit)
- Minervini rule engine: trend template, VCP detector, fundamentals, market regime, exit rules
- 40+ RuleConfig rows seeded with Minervini defaults and thresholds
- FastAPI dashboard (replaced Streamlit): 4 trading pages + 4 admin pages
- Light/dark theme (CSS variables — Flowbite blue/white default)
- Worker status detection (online/starting/offline based on heartbeat age)
- Trading blocked automatically when worker offline
- Market regime trigger buttons (no longer stuck on "UNKNOWN")
- Watchlist auto-population explained in UI
- Audit log (append-only, all events)
- WhatsApp notifier (WAHA client — 6 typed alert methods)
- WhatsApp agent command handler (13 commands, ready to wire to webhook)
- IBKR broker wrapper (bracket orders, paper/live toggle, simulation fallback)
- Risk manager (position sizing, portfolio heat, pyramid rules)
- yfinance data fetcher (price, fundamentals, RS ratings, ASX calendar)
- Celery tasks: screening, trading, reporting, health check
- CLAUDE.md (agent context document)
- **Bug fixes (3 Jun 2026):**
  - Replaced all hardcoded Tailwind color classes (`bg-green-500`, `bg-yellow-400`, `bg-red-500`) in `base.html` sidebar and `health.html` status cards with CSS variable equivalents — was causing hardcoded bright green dots in light mode
  - Added `color-scheme` + `<select> option` CSS to `base.html` so native browser dropdowns respect the light/dark theme
  - Added `?msg=screen` flash message handler in both `base.html` and `signals.html` so screener button gives feedback
  - Fixed `/action/run-screener` in `main.py`: was calling `run_daily_screen.delay()` which has a `today_is_trading_day()` gate and silently does nothing on non-trading days; now calls `_run_screen_force` (bypasses gate) and redirects with `?msg=screen`
  - Fixed `/action/force-screen` to wrap `.delay()` in try/except so worker connectivity issues don't crash the route
  - Fixed extra stray `</div>` in `watchlist.html` that was prematurely closing the card and breaking layout
  - `health.html` was truncated at line 41 in bash sandbox (file system caching issue); confirmed complete via file tools (217 lines)
- **Bug fixes (3 Jun 2026 — Session 2):**
  - **Root cause of silent screener failure found and fixed:** `screening.py` had duplicate function definitions (`run_full_setup`, `_run_screen_force`, `_upsert_watchlist` all appeared twice) AND orphaned code outside any function at line ~429 — this caused a Python `SyntaxError` on import, which means the Celery worker could NOT load the `screening` module, so `_run_screen_force.delay()` silently queued a task that could never execute. File was rewritten clean.
  - **`main.py` duplicate route handlers removed:** Routes for `POST /admin/rules/{rule_id}/threshold`, `GET /admin/config`, `POST /admin/config/{config_id}/update`, and `GET /admin/audit` were all defined twice (exact copies). Removed all duplicates — file is now 648 lines (was 718).
  - Celery worker now starts with all 11 tasks registered and zero import errors. `_run_screen_force` is confirmed registered.
  - All `wsl docker compose` used as the command prefix (Docker not in Windows PATH).
  - **`admin/rules.html` Jinja2 `TypeError` fixed:** `sum(attribute='__len__')` on `dict.values()` doesn't work — replaced with `namespace` counter loop.
  - **`admin/rules.html` orphaned style block fixed:** `{% if loop.last %}style="..."{% endif %}` was rendering as literal text — moved into inline style using `{% if not loop.last %}border-bottom...{% endif %}`.
  - **`admin/config.html` `TypeError` fixed:** `cfg.value|length` crashes when `cfg.value` is `None` — added `(cfg.value or '')` guard. Also fixed border on last row using `loop.last` in inline style.
  - **New page: `/admin/tasks` — Live Task Log:** Auto-polling (every 3s) page that streams new audit events from the DB as tasks run. Shows data state counters (stocks, bars, signals, watchlist) that auto-update. Quick-trigger buttons to launch any task. All without a page reload. Added "Task Log · Live" nav entry in sidebar.

- **Bug fixes + improvements (3 Jun 2026 — Session 3):**
  - **Root cause of 20-stock universe identified:** Wikipedia returns HTTP 403 Forbidden to `pd.read_html()` (no User-Agent). Fixed `get_asx200_tickers()` to use `requests` with browser UA first, then pass HTML to `pd.read_html()`. Now fetches all 200 ASX200 constituents correctly.
  - **`_run_screen_force` completely rewritten:** Previous version only ran trend template and only added to watchlist (no signals, no fundamentals, no VCP). New version: (1) pre-fetches RS ratings for all stocks in batches, (2) runs full pipeline: trend template → fundamentals → VCP → signal, (3) writes a `SCREENER_TICKER` audit row per stock showing exactly which rules passed/failed and why (🟢 SIGNAL / 🔵 WATCHLIST / 🟡 FAIL fundamentals / 🔴 FAIL trend / ⚪ SKIP), visible in real-time on Task Log page.
  - **New `SCREENER_TICKER` audit action:** Added to `AuditAction` enum in Python and migrated the PostgreSQL `auditaction` enum via `ALTER TYPE`.
  - **Task Log page enhanced:** SCREENER_TICKER rows have shaded background and emoji-based colour coding so you can immediately see what happened to each stock as the screener runs.

1. **WhatsApp integration fixed (3 Jun 2026 — Session 3 cont.):**
  - **Root cause 1 — wrong session name:** `WAHA_SESSION=vcpilot` → WAHA Core only supports `"default"`. Fixed in `.env` and `config.py`.
  - **Root cause 2 — wrong JID format:** `@s.whatsapp.net` → correct format is `@c.us`. Fixed for `61450325233@c.us`.
  - **Root cause 3 — webhook never wired:** `POST /webhook/whatsapp` route built — WAHA posts here for every incoming message → validates sender → dispatches to `AgentCommandHandler` → replies via `notifier.send()`.
  - **Root cause 4 — session never auto-started:** Added `WHATSAPP_START_SESSION=default` and `WHATSAPP_HOOK_URL/EVENTS` to `docker-compose.yml` so WAHA auto-starts and auto-configures the webhook on boot.
  - **Root cause 5 — QR code display error:** WAHA API `/api/{session}/auth/qr` returns raw PNG bytes. Modified `get_qr()` method in `whatsapp.py` to check response header/content and convert raw image bytes to a base64 encoded string, resolving "QR not available yet" issue on `/admin/whatsapp`.
  - **New page `/admin/whatsapp`:** Session status, QR code display, setup checklist, send-test button, recent command history. Added "WhatsApp" nav entry in sidebar.
  - **`WhatsAppNotifier` enhanced:** Added `ensure_session()`, `get_qr()`, `get_session_status()` methods. Auto-derives JID from phone number. Better error logging.
  - **Admin phone configured:** `+61450325233` → `61450325233@c.us`
  - **Status:** Fully functional, waiting for user QR code scan.

- **Bug fixes + improvements (3 Jun 2026 — Session 4):**
  - **WhatsApp org-level setup UX fixed:** `whatsapp.html` no longer tells org admins to set `.env` variables they can't access — now links directly to `/admin/config` with clear instructions. Added a prominent setup notice when admin phone is not yet configured. Updated checklist item labels to match their actual config source.
  - **`admin_tasks_poll` superadmin bug fixed:** The live task log poll endpoint was incorrectly returning HTTP 403 for Super Admin sessions (inverted guard — `== "superadmin"` instead of `!= "superadmin"`). Now all authenticated users can poll, and the org scope is correctly applied.
  - **Per-org worker heartbeat:** `health_check` Celery task now writes both a global `last_heartbeat` (backward compatible) AND a per-org `last_heartbeat` row for every active organisation. The `_global()` function in `main.py` now prefers the per-org heartbeat row, falling back to global for old deployments. This means each org's Health page accurately reflects whether the shared worker is alive.
  - **Global rules → org rules sync:** Added `POST /superadmin/rules/sync-all` endpoint that propagates global template rule settings (`enabled_globally`, `threshold`, `tier_overrides`) to all org-level copies. Added *Soft Sync* (skips org-customised rows) and *Force Sync* (overwrites all) buttons to the superadmin/rules page. `synced` and `skipped` counts shown in the success banner.
  - **Minervini-style position close:** Added `POST /positions/{pos_id}/close` route. Open positions table now has a **Close** button per row. Clicking reveals an inline form with all Minervini exit reasons grouped into *Defensive* (stop loss, time stop, earnings, 50MA break, market regime) and *Offensive* (target 1/2, climax top, parabolic, 3-weeks-tight) categories, plus a Manual option. An optional exit price field overrides the last known price. Confirming marks the position CLOSED, creates a Trade record, writes audit log, and sends WhatsApp alert. Inline Minervini exit framework guidance shown in form for reference.
  - **Unskip signal:** Added `POST /signals/{signal_id}/unskip` route and `↩ Unskip` button in `signals.html` for SKIPPED signals. Added `UNSKIP <TICKER>` WhatsApp command to `AgentCommandHandler`. Signal is restored to `PENDING` so it can be triggered in the intraday entry check.
  - **`promoted` flash message:** `signals.html` now shows a success banner when redirected from watchlist promote (`?msg=promoted`).

- **Features + fixes (4 Jun 2026 — Session 6):**
  - **Watchlist Labels/Tags:** Multi-label watchlist grouping system. New `WatchlistLabel` model with colour picker (8-shade palette). Default labels seeded per org: Favourites (amber), High Priority (red), VCP Forming (blue), Under Review (violet). Coloured filter chips at the top of the Watchlist page let you filter by label instantly. Each stock card shows a colour dot + label badge. Inline label selector per card (select → auto-submit). New label panel accessible via "+ New label" button. "Add Manually" form now includes a label selector. `screen_single_ticker` Celery task accepts `label_id` so manual adds land in the correct group. Migration adds `watchlist_labels` table + `label_id` FK on `watchlist`.
  - **Background job visibility fixed:** `check_entry_triggers` and `check_exit_rules_task` now write a `TASK_RUN` audit log entry on every invocation — even when the market is closed or there are no positions. Previously these tasks fired silently (only `logger.debug`) when the market was shut, so the Task Log showed nothing except heartbeats. Now every 5-min run appears in the log with a timestamp and status message.
  - **Per-org timezone config:** Added `org_timezone` `SystemConfig` key defaulting to `Australia/Sydney` (AEST). Seeded in `seed_config.py` and `migrate_saas.py`. Appears in `/admin/config` under the General group. Celery Beat schedules remain global on AEST (correct for ASX), but each org can store a different timezone for use in reports and WhatsApp alert timestamps.

- **Bug fixes + improvements (4 Jun 2026 — Session 5):**
  - **Manual triggers now org-scoped:** `_run_screen_force` and `send_daily_report` Celery tasks now accept an optional `organization_id` parameter. When triggered manually from the dashboard, they pass the current user's org so only that org's data is processed. Scheduled Beat tasks still loop all active orgs (no org_id passed). `evaluate_market_regime_task` and `refresh_price_data` remain global (shared data). `force-screen`, `run-screener`, and `send-report` action routes now all pass `org_id`.
  - **`user_id` added to AuditLog:** New `user_id` FK column (nullable, `ON DELETE SET NULL`) on `audit_logs` table. Added via `migrate_saas.py`. All manual dashboard actions (pause, resume, skip, unskip, close position, promote watchlist) now write `user_id` and the user's email as `actor` instead of the generic `"dashboard"` string.
  - **Audit Log actor/user filter:** New `actor` filter field on `/admin/audit` — filter by email, `system`, `agent`, etc. Result limit raised to 200.
  - **Full mobile-first UI rebuild:** `base.html` completely rewritten:
    - Sidebar is now always a **slide-in drawer** with dark overlay, close button inside the nav, and `Escape` key support. Opens on all screen sizes via hamburger button.
    - On large screens (≥1024px), the drawer auto-opens and the main content margin adjusts via JS — no CSS-only layout dependence.
    - All nav links call `closeSidebar()` on tap — drawer closes cleanly after navigation on mobile.
    - Viewport uses `100dvh` for iOS browser chrome safety.
    - Navbar trimmed: pause/resume shows icon-only on small screens. Regime badge hidden on mobile if Caution (saves space). Org selector width capped at 140px.
    - Mobile CSS additions: min-height 2.5rem tap targets for buttons, 16px input font-size (prevents iOS auto-zoom), `table-responsive` class for horizontal scroll on all tables, `actor-cell` truncation class.
    - iOS scroll locking when drawer is open (`overflow:hidden` on body).
  - **Company name pipeline fixed:** `get_fundamentals()` now returns `company_name`, `sector`, `industry` from yfinance `info.longName` (zero extra API calls). Both `_run_screen_force` and `run_daily_screen` persist these to `Stock.name`/`.sector`/`.industry` for every stock that passes trend template. Home, positions, signals, and watchlist pages all show the company name below the ticker code.
  - **WAHA Plus + per-org sessions:** Switched to `devlikeapro/waha-plus:latest` so each org uses its own session (`org_1`, `org_2`, …). Session names seeded as `org_{id}` on org creation and migration. App startup no longer auto-starts any session — each org admin triggers their own QR scan via `/admin/whatsapp`.

- **Data Log + Superadmin Market Data (5 Jun 2026):**
  - **`entry_check_logs` DB table:** New structured table capturing per-org, per-signal intraday metric snapshots on every 5-min entry check run (price, pivot, volume ratio, MAs, RS, per-rule pass/fail JSON, data source, delay metadata).
  - **Intraday price fetcher:** `get_intraday_price()` in `fetcher.py` — tries IBKR real-time snapshot (`get_market_snapshot()`) first; falls back to yfinance 15-min interval bars (ASX free tier ≈ 15-20 min delayed); last-resort EOD close fallback. Returns `data_source`, `delay_mins`, `bar_timestamp`.
  - **`check_entry_triggers` upgraded:** Now uses intraday price instead of prior EOD close for breakout confirmation. Writes structured rows to `entry_check_logs` in addition to AuditLog. Entry price used for position sizing is now the intraday price.
  - **IBKR `get_market_snapshot()`:** New method on `IBKRBroker` using `reqMktData(snapshot=True)` for real-time last/bid/ask/volume. Returns `None` gracefully when IBKR not connected or simulation mode.
  - **Admin Data Log (`/admin/data-log`):** New page showing live intraday entry check snapshots per signal. Per-rule green ✓ / red ✗ badges. Time filter chips (Latest / −15 / −30 / −60 min / Today). Ticker dropdown filter. Confirmed-only toggle. Auto-refresh every 30s via `/admin/data-log/poll` JSON endpoint. Click any row to expand full rule detail panel. yfinance delay banner warns users of data latency. Data source badge: ⚡ IBKR Live / 🕐 ~20min delay / 📉 EOD Close.
  - **Superadmin Market Data (`/superadmin/data`):** Two-tab page under SaaS Management. Tab 1 (ASX Universe): all active stocks with latest PriceBar metrics — sortable by ticker/RS/price/volume/market cap, searchable, sector filter, pagination (50/page), MA50/150/200 colour-coded vs close price, RS badge coloured by tier (≥80 green, 60-80 amber, <60 red). Tab 2 (Custom Stocks): per-org stocks not in ASX200 universe added via Watchlist, with org count badge for stocks tracked by multiple orgs.
  - **Sidebar:** Added "Data Log" link under Admin section; added "Market Data" link under SaaS Management section.
  - **`main.py` truncation fixed:** Recovered all missing superadmin routes (`/superadmin/organizations/{org_id}`, `/superadmin/rules/*`, `/superadmin/users/*`) that were truncated in the committed file. Also completed the `reset_password_post` function which was also truncated. File is now complete at 3130 lines.

- **Bug fixes (5 Jun 2026):**
  - **`ibkr_simulate` badge now DB-driven:** `IBKR_SIMULATE` env var is now mirrored in `SystemConfig` as a global key (`organization_id=NULL`). `settings.ibkr_simulate_live` property checks DB first, falls back to env. `_global()` reads it via `cfg()` so the badge updates immediately when toggled from the UI — no container restart needed.
  - **Superadmin simulation panel wired up:** `POST /superadmin/config/simulation` route added. The mock clock / regime / simulator form on `/superadmin/rules` was POSTing to a missing endpoint (404). Route now saves `mock_time_enabled`, `mock_current_time`, `last_market_regime`, and `ibkr_simulate` to global `SystemConfig` rows and writes an audit log. IBKR Simulation Mode toggle added to the form.
  - **`_global()` passes mock context to all templates:** `mock_time_enabled`, `mock_current_time`, `mock_market_regime` (= `last_market_regime`) added to the global template context so the simulation panel renders correct current values.
  - **Admin config Apply fixed for global configs:** `POST /admin/config/{config_id}/update` was filtering by `organization_id == org_id` — if a superadmin viewed global configs (mock clock, regime, `ibkr_simulate`) with `org_id=None`, the row lookup matched but if `org_id` differed from `organization_id=NULL` it silently failed. Route now fetches by ID only, then enforces a role-based ownership check (superadmins can save any row; org users restricted to their own org).
  - **Superadmin can see `system` group configs:** Admin config page now shows `group="system"` entries (mock clock, ibkr_simulate, etc.) to superadmins; still hidden for regular org users.
  - **Global system configs seeded by migration:** `migrate_saas.py` now seeds `mock_time_enabled`, `mock_current_time`, `ibkr_simulate` as global rows on startup if missing.

### 🔄 In Progress / Next Steps

1. **Scan WhatsApp QR Code** — Go to `/admin/whatsapp` in the VCPilot dashboard. You will now see the QR code image. Scan it with your phone using WhatsApp → Linked Devices. Once scanned, the status will show **Connected** and the checklist will turn green.
   
2. **Send Test Message** — Click the **Send Test Message** button on `/admin/whatsapp` or message `HELP` to VCPilot from your WhatsApp number to verify the bot answers.

3. **Run first full screen** — Go to `/admin/health` → "Run Full Setup" to fetch the ASX200 universe, download price history, evaluate market regime, and run the screener. This will populate your Signals and Watchlist tabs. You can monitor progress on the **Task Log · Live** page.

4. **Connect IBKR Gateway** — Paper account setup needed:
   - Configure IBKR username, password, and account number on `/admin/config` in the dashboard
   - Ensure `IBKR_USERNAME` and `IBKR_PASSWORD` in `.env` are also set for initial Docker Compose gateway boot, then run: `wsl docker compose --profile trading up ibkr -d`
   - Verify connection on `/admin/health` page

5. **Delete old Streamlit files** (cosmetic):
   - Run `wsl bash -c "rm -rf dashboard/Home.py dashboard/pages/"`

### ❌ Not Built (Phase 2+)

- Backtest page (Vectorbt — stub at `/backtest`)
- Stop order modification via IBKR (`sync_stop_orders` is placeholder)
- Pyramid add-on order logic
- CGT report export (PDF or Excel)
- IBKR position reconciliation on startup
- Sector RS ranking (rule seeded but not implemented in screener)
- Intraday price feed (currently EOD only — entry checks use last close price)

---

## Services Status (as of last update)

| Service | Status | Notes |
|---|---|---|
| `vcpilot-database` | ✅ Healthy | TimescaleDB running, tables created |
| `vcpilot-redis` | ✅ Healthy | Celery broker ready |
| `vcpilot-app` | ✅ Completed | Setup and migrations completed successfully |
| `vcpilot-worker` | ✅ Started | Celery worker running |
| `vcpilot-beat` | ✅ Started | AEST schedule active |
| `vcpilot-api` | ✅ Started | http://localhost:8501 |
| `vcpilot-whatsapp` | ✅ Started | http://localhost:3000 — session not yet configured |
| `vcpilot-ibkr` | ⏸ Not started | Run with `--profile trading` when ready |

---

## Data State

| Item | State |
|---|---|
| ASX200 universe | Empty — run `refresh_universe` task first |
| Price history | Empty — run `refresh_price_data` after universe |
| Minervini rules | ✅ 40+ rules seeded |
| Market regime | Not evaluated — trigger manually on health page |
| Signals | None yet — run screener after data loaded |

---

## Known Issues / Technical Debt

| Issue | Severity | Fix |
|---|---|---|
| Old Streamlit files in `dashboard/` | Low | Delete manually from WSL |
| `sync_stop_orders` is a placeholder | Medium | Implement IBKR modify order API |
| ~~Entry triggers use last EOD close, not live price~~ | ~~Medium~~ | ✅ Fixed — `get_intraday_price()` uses IBKR real-time or yfinance 15-min; EOD is last-resort fallback |
| ~~WhatsApp webhook not wired~~ | ~~High~~ | ✅ Fixed |
| `evaluate_market_regime_task` needs price bars in DB | Medium | Documented on health page; run Full Setup first |
| ~~Screener button silently did nothing on non-trading days~~ | ~~High~~ | ✅ Fixed — now uses `_run_screen_force` |
| ~~`screening.py` duplicate functions caused SyntaxError on import~~ | ~~Critical~~ | ✅ Fixed — file rewritten clean |
| ~~`main.py` duplicate route handlers~~ | ~~Medium~~ | ✅ Fixed — removed all duplicates |
| ~~Hardcoded Tailwind colors showing green/yellow/red in light mode~~ | ~~Medium~~ | ✅ Fixed — CSS variables throughout |
| ~~Stray `</div>` breaking watchlist card layout~~ | ~~Low~~ | ✅ Fixed |

---

## Trading Config (current defaults)

| Parameter | Value | Configurable? |
|---|---|---|
| Mode | Paper / Live | `/admin/config` (IBKR Paper Mode) |
| Max risk per trade | 2% of capital | Rules Config → POSITION_SIZING |
| Max open positions | 5 | Rules Config → PORTFOLIO |
| Max portfolio heat | 15% | Rules Config → PORTFOLIO |
| Stop loss | Hard stop (mandatory) | Cannot be disabled |
| Profit target 1 | 20% (partial 50%) | Rules Config → EXIT_OFFENSIVE |
| Profit target 2 | 40% (full exit) | Rules Config → EXIT_OFFENSIVE |
| Weekly capital injection | $1,000 AUD | `/admin/config` (Weekly Capital Injection) |
| Starting capital | $1,000 AUD | Seeded default account (database level) |
