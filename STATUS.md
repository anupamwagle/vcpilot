# VCPilot — Operational Status

> Last updated: 8 June 2026 AEST. Update this file when major milestones are reached.

---

## Current Phase: 3 — Multi-Market Support (ASX + US Equities + Crypto Foundation)

### ✅ Done

- **Critical Bug Audit & Regression Test Suite (8 Jun 2026 — Session 4):**
  - **Trigger:** User manually promoted TRX-AUD from Watchlist → Signals to watch it closely and "nothing happened." Given the imminent move to live trading with real capital, this prompted a full audit of the watchlist→signal→position→trade lifecycle for similar silent-failure bugs — 5 critical bug clusters were found and fixed, and a pytest regression suite was built to lock them in.
  1. **Silent watchlist-promotion failures (the reported bug) — two distinct causes:**
     - `dashboard/main.py::watchlist_promote`: wrote `w.status = SIGNALLED` optimistically *before* queuing the Celery task. If Redis/the worker was unreachable, the watchlist item flipped to SIGNALLED but no signal was ever created — no visible error. Fixed: `.delay()` now wrapped in try/except; on failure the status reverts to `WATCHING`, a `TASK_ERROR` audit row records the broker error, and the page redirects with `?msg=promotion_failed`.
     - `app/tasks/trading.py::promote_watchlist_item_task`: when a signal already existed for that ticker+date, the task silently skipped creating a duplicate but still flipped the item to SIGNALLED with a generic "promoted" audit message — exactly the "I clicked it and nothing happened" experience reported. Fixed: now writes a distinct `TASK_ERROR` audit entry naming the existing signal's ID/status, and a clearly-worded `MANUAL_OVERRIDE` message ("existing signal reused — no duplicate created. Check the Signals page for the existing entry").
  2. **Stopped-out crypto positions never actually closed — the most dangerous bug found.** `sync_stop_orders` (the automated stop-loss monitor) and the MCP `close_position` tool both wrote exit details onto non-existent `Position` attributes (`exit_price`/`exit_reason`/`closed_at`/`realised_pnl`/`opened_at` — these are `Trade` columns, not `Position` columns) and passed invalid kwargs into `Trade()`. SQLAlchemy raised `AttributeError`/`TypeError` on every attempt, silently swallowed by a broad `except Exception` — so a position that hit its stop simply **stayed open indefinitely with zero visible error**. This is about as bad as a trading-bot bug gets: real capital stays exposed past its stop. Fixed both paths to write exit detail onto `Trade` using the real columns (`entry_date`/`exit_date`/`hold_days`/`gross_pnl_aud`/`net_pnl_aud`/`pnl_pct`/`exit_price`/`exit_reason`/`initial_stop`/`cgt_eligible_discount`) and correctly flip `Position.status = CLOSED`.
  3. **MCP `get_positions(include_closed=True)` crash:** queried/serialised columns that don't exist (`Trade.closed_at`, `Trade.realised_pnl`, `Position.stop_price`, `Position.target_price`, `Position.opened_at`, `Position.pnl_pct`). Any agent or admin asking "show me closed trades" got a hard `AttributeError` instead of data. Fixed to map from the real columns (`Trade.exit_date`/`net_pnl_aud`, `Position.current_stop`/`target_1`/`entry_date`/`unrealised_pct`).
  4. **Crypto position classification — verified, not broken, but now guarded.** Cross-checked `_is_crypto_position()` against the real `Position` construction path in entry-trigger code (`exchange_key`/`asset_type` propagation). No live bug, but it sits directly on the same code path as #2, so regression coverage was added to ensure a future change can't silently misroute crypto exits to the equities path (or vice versa).
  5. Confirmed the prior session's `watchlist_promote` rollback-on-queue-failure fix is in place in production code, and gave it test coverage (it had none).
  - **New regression test suite (`tests/`)** — runs the *real* production code paths (Celery tasks via `.run()`, FastAPI routes via direct async invocation, MCP tools via monkeypatched context) against an isolated in-memory SQLite DB (`StaticPool` + monkeypatched `SessionLocal`) — zero risk to the live org DB:
    - `tests/conftest.py` — engine/session fixtures + `org_and_account`, `open_crypto_position`, `watching_trx_item` seed fixtures
    - `tests/test_watchlist_promotion.py` — 4 tests: dashboard rollback on queue failure, happy-path success, duplicate-signal no-op (the reported bug), no-price-data rollback
    - `tests/test_position_close_paths.py` — schema guards that fail loudly if the phantom-field pattern ever reappears, plus end-to-end `sync_stop_orders` and MCP `close_position` tests asserting the position closes AND a correct `Trade` row is written, plus invalid-exit-reason rejection
    - `tests/test_crypto_position_classification.py` — crypto vs equity `Position` classification regression
    - `tests/test_mcp_get_positions.py` — no-crash + correct field-mapping + 30-day cutoff filtering for `get_positions(include_closed=True)`
    - Added `pytest==8.2.2` + `pytest-mock==3.14.0` to `requirements.txt` and a `pytest.ini`. Run with `pytest` from the project root, or `wsl bash -c "cd /mnt/c/vcpilot && pytest"`.
  - **Bottom line:** bug #2 (stopped-out positions never closing) meant a stop-loss could appear "set" in the UI but silently never execute — leaving real capital exposed past its intended exit indefinitely. All 5 issues are fixed and now covered by regression tests that assert against the *real* `Position`/`Trade` schema, so a future schema drift that reintroduces this pattern fails the test suite immediately instead of failing silently in production.

- **Expanded Crypto Universe to 100 Tokens (8 Jun 2026):**
  - Expanded `TOP_CRYPTO_SYMBOLS` in `app/data/fetcher.py` from 50 to 100 tokens to increase scanner coverage.
  - Updated `refresh_crypto_universe` task in `app/tasks/screening.py` and documentation references to reflect the top-100 expansion.
  - Triggered the bootstrap task to automatically seed the 50 new crypto tokens (totaling 100 tokens) for the active `CRYPTO_INDEPENDENTRESERVE` exchange.

- **Watchlist Exchange Filtering Bug Fix (8 Jun 2026):**
  - **Watchlist Tickers Defaulting to ASX / EQUITY bug fixed:** When non-ASX stocks (US equities like AAPL, MSFT) or crypto tickers (like TRX-AUD, SOL-USD) were added automatically by the screener via `_upsert_watchlist` or manually updated via `screen_single_ticker`, they defaulted to `exchange_key="ASX"` and `asset_type="EQUITY"` in the `watchlist` database table. This was because `_upsert_watchlist` did not set these columns, relying on SQL defaults. Fixed by querying the global `Stock` table directly inside `_upsert_watchlist` to retrieve and save the correct `exchange_key`, `asset_type`, and `currency`.
  - **`toggle_favourite` multi-market fix:** Updated the `/watchlist/toggle-favourite` route to query the `Stock` table first to verify the correct ticker, exchange, asset type, and currency instead of blindly appending `.AX`.
  - **Database Cleanup:** Successfully executed a recovery migration script (`fix_db.py`) to update 13 existing incorrect watchlist entries (SOL-USD, ETH-USD, BTC-USD, TRX-AUD, AAPL, MSFT, AMZN, GOOGL) to their correct `exchange_key`, `asset_type`, and `currency` values, resolving the bug where they incorrectly showed up under the ASX exchange filter tab.
  - **Dashboard Watchlist Card Exchange Filtering:** Added the top-level exchange filters (All / ASX / US / Crypto) to the Watchlist Market Data section on the main Dashboard homepage (`/`). Wired it to navigate asynchronously using the `wlFilter` function to preserve label filter and custom stock toggle state.

- **Superadmin Page Fixes & Market Data Revamp (8 Jun 2026):**
  - **`/superadmin/exchanges` 500 fixed:** `_is_superadmin()` helper was called in the exchanges routes but was never defined — added after `_auth()` in `main.py`.
  - **`/superadmin/data?tab=crypto` and `?tab=custom` 500 fixed:** SQL query referenced `w.added_at` column which doesn't exist — corrected to `w.created_at AS added_at`. Added proper `elif tab == "crypto"` and `elif tab == "us"` handlers in the route.
  - **Market Data page revamped:** Four exchange-based tabs: 🇦🇺 ASX Universe / 🇺🇸 US Stocks / ₿ Crypto / ⭐ Custom. Summary cards now show per-exchange stock counts. Crypto tab shows assets from DB (consistent with other tabs). US tab filters by `exchange_key IN ('NYSE','NASDAQ')`. Exchange badges on custom stocks rows. CSS defined inline via `<style>` block (`tab-pill`, `rs-badge`, `badge-sm`, `hover-row`).
  - **`/superadmin/rules` crypto support added:** Added `"CRYPTO": "Crypto Rules"` to `CATEGORY_LABELS` and `"CRYPTO": "₿"` to `CATEGORY_ICONS`. Added `asset_types` field to rule dicts so "Equity only" / "Crypto only" badges render.
  - **`CRYPTO_MEXC` added to `ExchangeKey` enum** in `app/models/exchange.py` — was seeded in `migrate_saas.py` but missing from the enum.
  - **Signals and Positions pages wired for exchange filtering:** Both `/signals` and `/positions` routes now accept `?exchange=ASX|US|CRYPTO|ALL` query param. Filter is applied at DB query level (`Signal.exchange_key`, `Position.exchange_key`, `Trade.exchange_key`). Each data row now carries `exchange_key`, `asset_type`, `currency`, `flag_emoji` (looked up from ExchangeConfig). Closed trades on the positions page are exchange-filtered too. Both routes pass `exchange_filters`, `active_exchange_filter`, `base_url` to their templates — the exchange filter pill bar (already present in both templates via `components/exchange_filter.html`) now renders correctly and filters data. Home page intentionally left without per-exchange filter (it's a summary view).
  - **Audit log exchange filter bug fixed:** When filtering by Crypto (or ASX), ticker-less TASK_RUN events (e.g. "[ASX] Entry check…", "[CRYPTO] Entry check…") were all shown regardless of the selected exchange, because the filter only checked `ticker IS NULL` without checking the message prefix. Fixed: null-ticker rows now additionally require the message to match the selected exchange prefix (`[ASX]`, `[CRYPTO]`, `[NYSE]`, `[NASDAQ]`). Audited other pages — watchlist/signals/positions/data-log exchange filters are not affected by this pattern.

- **Split Celery Workers & Crypto Order Routing (7 Jun 2026):**
  - **Worker Split:** Divided the single Celery worker service into `vcpilot-worker-equities` (queues: default, reporting, screening_equities, trading_equities) and `vcpilot-worker-crypto` (queues: trading_crypto, screening_crypto).
  - **Queue Routing:** Configured separate queues for equities screening/trading and crypto screening/trading in Celery Beat schedules and general task routing.
  - **Trading Tasks Upgraded:** Added `exchange_key` parameter to `check_entry_triggers` and `check_exit_rules_task`, filtering signals/positions dynamically by exchange context.
  - **Order Routing Integration:** Fully wired `CryptoBroker` vs `IBKRBroker` routing based on asset type / exchange key in entry/exit triggers, ensuring crypto bracket orders route to unified ccxt endpoints.
  - **Crypto Fundamentals & Fee Polish:** Bypassed fundamental rule screening for crypto in `run_daily_screen` and `_run_screen_force` using a preloaded stocks map, and updated exit task P&L math to exclude equities commissions for crypto.
  - **24/7 Crypto Schedules:** Updated the calendar checker to verify `exchange_key == "CRYPTO"`, allowing crypto tasks (entry checks, exit checks, positions syncing, logs, signals, watchlist updates) to run 24/7.
  - **Dynamic Working Capital Currency:** Added `working_capital_currency` config key (default: `"AUD"`), allowing tenant organizations to set their capital in custom base currencies (e.g. USDT, BNB, USD). Non-superadmins are restricted from editing the currency key, and the dashboard dynamically adapts currency prefixes and labels throughout.
  - **Refactored Sizing & FX Engines:** Position sizing calculations run natively in the chosen currency and support fractional units for crypto, while overall portfolio metrics and database logs still normalize to AUD. Supported yfinance crypto tickers and recursive USD-bridged FX rates in the data fetcher.
  - **Expanded Schema Precision:** Upgraded database schema columns (`threshold`, `threshold_min`, `threshold_max` in `rule_configs`) and SQLAlchemy models to `NUMERIC(20,4)` to prevent database numeric overflows when seeding crypto rule thresholds (e.g. market cap ≥ $100M, volume ≥ $5M). Seeded new rules and successfully completed database initialization.

- **Multi-Market UX Polish + Admin Config Redesign (7 Jun 2026):**
  - **Exchange filter bar** on Watchlist, Signals, and Positions pages. Pill tabs (All / 🇦🇺 ASX / 🇺🇸 US / ₿ Crypto) generated from org's `active_exchanges` config. Persists as `?exchange=` query param. Label filter chips on Watchlist preserve active exchange. Reusable `components/exchange_filter.html` include.
  - **Exchange badges** on every signal card and position row — flag emoji + exchange code badge (🇺🇸 NYSE, ₿ BINANCE etc.) for non-ASX instruments. USD prefix on price fields for US/crypto positions. `.AX` and `-USD` suffixes stripped from display tickers throughout.
  - **Crypto default watchlist labels** — Crypto Core / DeFi / Altcoins / Crypto Watch seeded automatically when a CRYPTO exchange is added to `active_exchanges`. Seeded at two points: `migrate_saas.py` on startup and live via `/admin/config` `active_exchanges` update route.
  - **Admin Config page redesign** — context-aware controls replacing plain text inputs: exchange multi-select chip toggle for `active_exchanges`, timezone dropdown for `org_timezone` (13 labelled options), number input with `AUD $` prefix for `working_capital_aud`, crypto exchange dropdown for `crypto_exchange_key`, password show/hide toggle for all secret fields, read-only badge for system-managed fields (regime, heartbeat). Every field now shows: status badge (● Set / ○ Not configured), inline hint text, format example, and external link where relevant.
  - **`FIELD_HINTS` pattern** in `admin_config` route — defines per-key control type, placeholder, example, hint text, and links. Passed to template as `hint` dict per config row. Extensible for future config keys.
  - **MEXC added** as a supported crypto exchange (ExchangeConfig row seeded, ccxt_provider = "mexc", sort_order = 45 between Binance and Coinbase).
  - **Startup bug fixes:**
    - `market_is_open_now()` in `calendar.py` was missing `exchange_key="ASX"` default → `trading.py` crashed on startup with `TypeError`.
    - `app/models/__init__.py` was missing `ExchangeConfig`, `MarketRegimeRecord`, `EntryCheckLog`, `WatchlistLabel` imports → tables not registered with SQLAlchemy `create_all()`.
    - Migration 002 runner split SQL on `;` which broke `DO $$ ... $$` blocks → new columns never added. Rewritten as pure Python `ALTER TABLE` calls.
    - `ExchangeConfig` model was using SQLAlchemy `Enum(AssetType)` columns (creates PostgreSQL ENUM type) but migration created VARCHAR columns → type mismatch 500 on `/superadmin/exchanges`. Fixed by using `Column(String(16))` consistently.
    - `_is_superadmin()` helper function was missing from `main.py` → NameError on exchange routes. Added at line 346.
    - WSL2 `netsh portproxy` was pointing to stale IP after Docker restart → updated to current `wsl hostname -I` IP.

- **Multi-Market Architecture — Phase 3 Foundation (6 Jun 2026):**
  - **New `app/models/exchange.py`**: `ExchangeConfig` (global, super admin managed) + `MarketRegimeRecord` (per-exchange regime history table). Replaces single global `last_market_regime` SystemConfig key.
  - **Exchange enum (`ExchangeKey`)**: ASX, NYSE, NASDAQ, CRYPTO_BINANCE, CRYPTO_COINBASE, CRYPTO_KRAKEN.
  - **Model upgrades**: `Stock`, `PriceBar`, `Watchlist`, `Signal`, `Order`, `Position`, `Trade` — all now carry `exchange_key`, `asset_type`, `currency` columns. Prices widened to NUMERIC(14,4) for crypto ranges. Qty widened to NUMERIC(20,8) for fractional crypto.
  - **Migration `migrations/002_multi_market.sql`**: Idempotent DDL for all new columns + tables. Seeded default ExchangeConfig rows for ASX/NYSE/NASDAQ/BINANCE/COINBASE/KRAKEN.
  - **`app/data/fetcher.py` additions**: `normalize_ticker(user_input, exchange_key)` converts raw user input to yfinance canonical format. `get_fx_rate(from, to)` with Redis+memory caching. `get_batch_prices_rate_limited()` for large universe batching. RS ratings now exchange-scoped.
  - **`app/data/calendar.py` refactor**: Exchange factory — `is_trading_day(exchange, dt)`, `market_is_open_now(exchange)`. Supports ASX/NYSE/NASDAQ/CRYPTO (24/7). Old ASX helpers preserved for backward compat.
  - **`app/broker/crypto.py`** (new): `CryptoBroker` via ccxt. Supports bracket-equivalent orders (entry limit + stop-market + take-profit). Simulation fallback when no credentials. `_yfinance_to_ccxt()` format conversion.
  - **`app/broker/ibkr.py` updates**: `_build_contract(ticker, exchange_key)` routes ASX→`Stock(sym,"ASX","AUD")` and NYSE/NASDAQ→`Stock(sym,"SMART","USD")`. `submit_bracket_order()` accepts `exchange_key`. `get_open_positions()` accepts `exchange_key` filter.
  - **`app/screener/market_regime.py` updates**: `evaluate_market_regime()` accepts `exchange_key`. Crypto skips breadth + distribution day rules. Per-exchange log messages.
  - **`app/risk/manager.py` upgrades**: `SizingResult` now includes `capital_local`, `currency`, `fx_rate_aud`. `calculate_position_size()` is currency-aware — converts capital to native currency, returns AUD equivalent. `calculate_portfolio_heat()` normalises all positions to AUD.
  - **`app/tasks/screening.py` upgrades**: `screen_single_ticker()` now exchange-aware — accepts `exchange_key`, `asset_type`, `currency`. Skips fundamentals for CRYPTO. Creates Stock with exchange metadata. `refresh_price_data()` accepts `exchange_key` param. `evaluate_market_regime_task()` writes `MarketRegimeRecord` + per-org SystemConfig keys per exchange.
  - **`app/tasks/celery_app.py` additions**: US market schedule (NYSE: data refresh 7am AEST Tue–Sat, screener 7:30am, entry/exit checks 11pm–6am AEST). Crypto schedule (15-min checks, midnight data refresh).
  - **Config additions** (`seed_config.py` + `migrate_saas.py`): `active_exchanges`, `ibkr_account_usd`, `fx_audusd_override`, `crypto_exchange_key`, `crypto_api_key`, `crypto_api_secret`, `crypto_testnet`, `last_market_regime_ASX/NYSE/NASDAQ`.
  - **Dashboard — Watchlist add form**: Exchange dropdown (shows enabled exchanges). Ticker input updates placeholder/hint on exchange selection. Exchange badge on each watchlist card (🇦🇺/🇺🇸/₿).
  - **Dashboard — `/superadmin/exchanges`** (new page): Table of all ExchangeConfig rows. Enable/disable toggles. Inline configure panel for crypto (ccxt_provider, sandbox mode). Info panel explaining the 3-step setup flow.
  - **Sidebar**: "Exchanges" link added under SaaS Management section.
  - **Data flow (on-demand)**: User adds AAPL/NYSE → `screen_single_ticker.delay("AAPL", exchange_key="NYSE")` → fetches 2yr yfinance history → stores in central `price_bars` table → runs AstraTrade rules → lands in Watchlist or Signals. Central data shared across all orgs.

- **Position Sizing Diagnosis & Sizing Synchronization (6 Jun 2026):**
  - **Position Sizing Diagnosis**: Investigated why positions were opened for ~$25k instead of ~$1,250 (which corresponds to 25% of the updated $5k working capital). Verified that the positions were entered on June 4th and early June 5th when the active account's capital was set to $100,000. The settings were updated to $5,000 at 7:42 AM on June 5th, which was after the positions had already been opened. Position quantities are calculated at trade entry and are not retroactively resized.
  - **WhatsApp Config Sizing Sync**: Updated the WhatsApp `CONFIG` command in [commands.py](file:///c:/vcpilot/app/agent/commands.py) to synchronize the active `Account.capital_aud` column when `working_capital_aud` is updated, ensuring parity with the Web UI settings behavior.
  - **Manual Watchlist Sizing**: Enriched the manual watchlist promotion background task (`promote_watchlist_item_task` in [trading.py](file:///c:/vcpilot/app/tasks/trading.py)) to compute AstraTrade position sizing using the active account's capital at the time of promotion, populating `suggested_size_shares`, `suggested_size_aud`, and `risk_per_trade_aud` fields.
  - **Fix `/action/force-position-sync` 500 error**: Fixed the 500 database error when forcing position sync by defining the missing `sync_ibkr_positions_task` Celery task in [trading.py](file:///c:/vcpilot/app/tasks/trading.py).
  - **Global Task Trigger Safety Wraps**: Wrapped all Celery `.delay()` calls for manual action triggers (e.g. screener runs, breakout checks, exit checks, stop syncs, etc.) inside [main.py](file:///c:/vcpilot/dashboard/main.py) with try/except blocks to log backend exceptions and redirect cleanly in case of Redis/Celery outages instead of displaying a 500 page.

- **Super Admin Market Data & Onboarding Improvements (6 Jun 2026):**
  - **Custom Stocks Tab SQL Bug Fix**: Fixed a `ProgrammingError` on the Custom Stocks tab of `/superadmin/data` by replacing the invalid column reference `w.added_at` with `w.created_at AS added_at`.
  - **Stock Universe Name & Sector Population**: Added a new Wikipedia metadata scraper `get_asx200_metadata()` and updated the `refresh_universe` Celery task. When the universe is fetched or setup runs, it now automatically populates and updates company names and sectors for all 200 constituents in the database.
  - **Optional User Onboarding Email**: Added a "Send onboarding email to user" checkbox (default checked) to the passwordless user creation form in the Super Admin page. Modified the backend handler to honor this toggle and direct admins to copyable manual setup URLs when skipped/failed.

- **Positions & Signals UX + Log Improvements (6 Jun 2026):**
  - **Positions: "Invested $" column**: Added `Invested $` column to the Open Positions table (entry price × quantity) so it's immediately clear how much capital is deployed per position. Tooltip shows the formula on hover.
  - **Signals: TRIGGERED signals now visible**: Fixed a query bug — when both signals were TRIGGERED they disappeared from the Signals page because the filter only matched `PENDING` or `signal_date == today`. Added `SignalStatus.TRIGGERED` to the OR filter so triggered signals always appear alongside pending ones.
  - **Exit check log enriched**: The exit check display on the Positions page now shows: timestamp | current price | P&L% | reason (e.g. "stop not hit; P/T not reached"). Color-coded red when exit is triggered (🚨), neutral otherwise. The underlying `trading.py` message now includes price, P&L%, and a summary of why hold/exit.
  - **Breakout check: failed criteria inline**: When an entry check fails, the Signals page now shows an inline "Not met:" summary in the header row alongside the timestamp — e.g. `✗ Price Below Pivot — $5.20 < $5.35`. Failed rule badges still appear below for detail. Background tints red (not-triggered) vs green (triggered) for instant visual distinction.

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
  - Notifications & Communications: WhatsApp (WAHA) and Telegram (Two-way bot).
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
- AstraTrade rule engine: trend template, VCP detector, fundamentals, market regime, exit rules
- 40+ RuleConfig rows seeded with AstraTrade defaults and thresholds
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
  - **AstraTrade position close:** Added `POST /positions/{pos_id}/close` route. Open positions table now has a **Close** button per row. Clicking reveals an inline form with all AstraTrade exit reasons grouped into *Defensive* (stop loss, time stop, earnings, 50MA break, market regime) and *Offensive* (target 1/2, climax top, parabolic, 3-weeks-tight) categories, plus a Manual option. An optional exit price field overrides the last known price. Confirming marks the position CLOSED, creates a Trade record, writes audit log, and sends WhatsApp alert. Inline AstraTrade exit framework guidance shown in form for reference.
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

- **24/7 Pipeline Hardening — Step 1 Complete (8 Jun 2026 — Session 2):**
  - **`Position` model field mismatch fixed everywhere**: `sync_stop_orders` and MCP `place_order` both now use `initial_stop`/`current_stop` (the actual model columns) instead of the non-existent `stop_price`. Was causing DB crash on position creation and stop check.
  - **IR AUD currency fixed in `check_entry_triggers`**: Was hardcoding `"USD"` for all crypto. Now resolves from `signal.ticker` suffix (`-AUD` → AUD, `-USD` → USD) and `CRYPTO_AUD_EXCHANGES` set. Correct position sizing for IR.
  - **Crypto regime check decoupled from ASX**: `check_entry_triggers` was checking global ASX `last_market_regime` to gate crypto entries. A bear ASX day would block all IR trades. Fixed to use `last_market_regime_CRYPTO_INDEPENDENTRESERVE` per org. Defaults BULL if not yet evaluated (crypto always open).
  - **Independent Reserve live price API integrated** (`app/data/fetcher.py`): New `_get_ir_live_price(ticker)` calls IR's free public API (`/Public/GetMarketSummary`) for AUD pairs. Returns `LastPrice`, `DayHighestBidPrice`, `DayLowestOfferPrice`, 0-delay, no auth. `get_intraday_price()` priority now: IR API → IBKR → yfinance 15-min → EOD fallback. Includes XBT mapping for Bitcoin.
  - **`update_position_pnl_task`** (new): Runs every 5 min, fetches live prices for all open positions, writes `current_price`, `unrealised_pnl_local`, `unrealised_pnl`, `unrealised_pct` to Position rows. Converts P&L to AUD via FX rate for non-AUD positions. UI positions page now shows live P&L without page reload.
  - **Celery Beat schedule overhaul for crypto**:
    - Entry checks: 15 min → **5 min** (24/7)
    - Exit checks: 15 min → **5 min** (24/7)
    - Stop sync: added **5 min** crypto beat (was only equities 15 min during market hours)
    - P&L refresh: new **5 min** beat for all exchanges
    - Data refresh: daily midnight → **every 6 hours** (midnight, 6am, noon, 6pm AEST)
    - Screener: daily → **4× daily** (after each data refresh: 12:45am, 6:45am, 12:45pm, 6:45pm AEST)

- **Live Trading Prep — Platform 100% Operational (8 Jun 2026):**
  - **IR XBT fix**: `_yfinance_to_ccxt()` in `broker/crypto.py` now maps `BTC` → `XBT` for IR. IR uses the ISO 4217 code `XBT`, not `BTC`. All Bitcoin orders on IR route as `XBT/AUD`. Other symbols unchanged.
  - **`place_order` MCP tool rewritten**: Previous version called `check_entry_triggers.delay(force_signal_id=...)` — a param that doesn't exist. Replaced with full direct execution: fetches live price → validates extension (<10% above pivot) → calculates position size → submits bracket order via CryptoBroker → creates Position DB record → marks signal TRIGGERED → sends WhatsApp notification. Returns entry price, qty, stop, target, broker name, order_ref.
  - **5 new enhanced "Wall St" trading rules** added to `crypto_rules.py` and seeded in `seed_config.py`:
    - `crypto_rsi_momentum` — RSI(14) ≥ 50: price must be in upward momentum phase
    - `crypto_macd_bullish` — MACD (12/26/9) histogram must be positive (bullish cross)
    - `crypto_volume_surge` — Volume ≥ 1.5× 20-day avg: breakout must have conviction
    - `crypto_min_rr_ratio` — Minimum 2.5:1 risk/reward: no bad-ratio trades
    - `crypto_btc_relative_strength` — Non-BTC must outperform or match Bitcoin over 50 days
  - **`sync_stop_orders` implemented** (was a placeholder): Now actively monitors all open crypto positions every 15 min. Checks live price vs stop; closes position and creates Trade record on stop-out. Applies ATR-based trailing stop: after 1 ATR gain → trail to breakeven; after 2 ATR gain → trail to entry + 0.5 ATR. Sends WhatsApp alert on stop-out.
  - **MCP server verified**: OAuth token endpoint `/mcp/oauth/token` exists. Credential management in superadmin org detail page. 17 tools registered including the fixed `place_order`.

- **Independent Reserve as Default Crypto Exchange (8 Jun 2026):**
  - **Primary crypto exchange** changed from Binance (USD/USDT) to **Independent Reserve (AUD)** across all defaults.
  - `CRYPTO_INDEPENDENTRESERVE` is now `is_enabled=TRUE` in ExchangeConfig seeds and `sort_order=40` (first).
  - `active_exchanges` default: `"ASX,CRYPTO_INDEPENDENTRESERVE"`. `crypto_exchange_key` default: `"CRYPTO_INDEPENDENTRESERVE"`.
  - `get_top_crypto_tickers()` and `refresh_crypto_universe()` default to IR; generates `-AUD` yfinance tickers.
  - All code defaults (`main.py`, `screening.py`, `fetcher.py`, `broker/crypto.py`) updated.
  - **Watchlist live price ticker** rewritten: dropped Binance API (USD/USDT) in favour of **CoinGecko AUD** (`vs_currency=aud`). Shows A$ prices, AUD volumes, 1h/24h/7d % change, market cap, 7d sparklines. Refresh rate extended to 60s (CoinGecko free tier rate limit). Badge updated to "Independent Reserve · data: CoinGecko (AUD)".
  - Migration adds step to enable IR and promote it to `sort_order=40` for any existing deployment.

- **Crypto Pipeline End-to-End Fix (8 Jun 2026):**
  - **Root cause identified**: `refresh_price_data(exchange_key="CRYPTO_BINANCE")` returned immediately because no `Stock` records with `exchange_key="CRYPTO_BINANCE"` existed. No AuditLog was written, so the Data Log showed nothing.
  - **`get_top_crypto_tickers(exchange_key)`** added to `fetcher.py` — returns top 50 crypto tickers in yfinance format (`BTC-USD`, `ETH-USD`, …) for the given exchange. AUD suffix for Independent Reserve.
  - **`TOP_CRYPTO_SYMBOLS`** constant added to `fetcher.py` — 50 well-known tokens by market cap (BTC, ETH, BNB, SOL, XRP, ADA, AVAX, DOGE, TRX, LINK, ...).
  - **`refresh_crypto_universe` Celery task** added to `screening.py` — seeds (or refreshes) the top-50 crypto `Stock` records for a given exchange. Writes AuditLog on completion.
  - **`refresh_price_data` auto-bootstrap**: when called for a CRYPTO exchange with zero stocks found, it now auto-calls `refresh_crypto_universe` inline before proceeding — no manual extra step needed. Also writes an AuditLog on no-tickers failure so the Data Log always shows why nothing happened.
  - **Trading-day gate fixed for crypto**: CRYPTO exchanges no longer fall through to the ASX calendar check. Crypto data refresh now runs 24/7 without being blocked on weekends or ASX holidays.
  - **Date-gate relaxed for crypto price bars**: equities still require `bar_date == today`; crypto accepts yesterday's bar too (yfinance lag tolerance).
  - **Signal exchange_key normalized**: `_run_screen_force` now stores the stock's actual `exchange_key` (e.g. `CRYPTO_BINANCE`) on the Signal, not the generic `"CRYPTO"` sweep key. Prevents trading task mismatch.
  - **Trading task crypto filter expanded**: `Signal.exchange_key.in_(["CRYPTO", "CRYPTO_BINANCE", ...])` now covers both the generic key (scheduled tasks) and specific keys (dashboard manual triggers). Same fix applied to Position filter.
  - **Celery Beat**: Added `run-daily-screen-crypto` (12:45am AEST, daily) after the data refresh. Moved crypto price refresh to `screening_equities` queue (no separate queue needed for data tasks).
  - **`/action/refresh-data`**: For crypto exchanges, now chains `refresh_crypto_universe → refresh_price_data` so universe is always bootstrapped first.
  - **`/action/seed-crypto`** (new endpoint): Manually seed the crypto universe for a selected exchange. Redirects with `?msg=crypto_seed`.
  - **Health page**: New **"🪙 Seed Crypto Universe"** button (exchange-select form) visible when a crypto exchange is active. Updated flash message for `?msg=data` to inform users that crypto auto-seeds.

- **AW Org Live Verification — Step 1 COMPLETE (8 Jun 2026 — Session 3):**
  - End-to-end pipeline verified via WSL scripts against running Docker stack.
  - **AW org (id=10)**: GOLD tier, capital A$5,000 (paper=True), active_exchanges=ASX,CRYPTO_INDEPENDENTRESERVE.
  - **50 IR tokens seeded**: All 50 Stock records in DB. 47 tickers returned price bars from yfinance.
  - **IR live prices confirmed**: BTC A$89,847 | ETH A$2,393 | SOL A$94 | XRP A$1.63 | DOGE A$0.12 | LINK A$11.16 (all 0-delay from IR public API).
  - **11 crypto rules active for AW**: 6 original + 5 enhanced (RSI, MACD, vol surge, R/R, BTC RS). All ON.
  - **Market regime**: CAUTION (BTC-AUD $89,723 vs 200MA $113,533 = -21%). ASX also BEAR. No signals generated — correct AstraTrade behaviour in bear market.
  - **Entry check loop confirmed**: Celery beat firing every 5 min. Audit log shows checks at 03:27 and 03:30 AEST.
  - **Watchlist clean**: 67 items (66 equity + BTC-AUD crypto). Stale ETH-USD removed.
  - **Celery beat schedules**: 5-min entry/exit/stop sync/P&L for crypto, 4× daily screener, 6h data refresh.
  - **Scripts saved** for future use: `/mnt/c/vcpilot/refresh_aw.sh`, `diag_aw.sh`, `fix_aw3.sh`, `refresh_asx.sh`.
  - **ATOM not on IR** — confirmed returns None from IR API. Expected.
  - **Closest to recovery** (best relative strength): BTC -21% | DOGE -24.7% | LINK -26.6% | XRP -30.5% vs 200MA.

### 🔄 Step 2 Pre-requisites (START HERE IN NEW SESSION)

**Before funding and going live — complete these in order:**

1. **Run ASX refresh** (run this first in new session):
   ```bash
   wsl bash /mnt/c/vcpilot/refresh_asx.sh 2>&1 | tee /mnt/c/vcpilot/refresh_asx.log
   ```
   Runs: universe → price data → regime eval → force screen → IBKR simulation test.

2. **Configure IR API credentials** — Go to `/admin/config` (logged in as AW admin):
   - `crypto_api_key` = your IR API key
   - `crypto_api_secret` = your IR API secret
   - `crypto_testnet` = `false`

3. **Configure IBKR** (for ASX equities) — Go to `/admin/config`:
   - `ibkr_username`, `ibkr_password`, `ibkr_account`
   - `ibkr_paper_mode` = `true` (start with paper)
   - Then: `wsl docker compose --profile trading up ibkr -d`

4. **Scan WhatsApp QR** — Go to `/admin/whatsapp` → scan QR → test with `HELP` message.

5. **Generate MCP credentials for AW** — Go to `/superadmin/organizations` → AW org → MCP Credentials section → Generate → grant ALL scopes. Configure in Claude Desktop Settings → MCP → VCPilot.

6. **Switch account to live** — Once funded, go to DB (superadmin) and set `Account.is_paper=False` for AW org.

7. **Trigger Step 2** — In a new Claude session, say:
   > "VCPilot Step 2 — live session. AW org ready. MCP connected. Let's trade."

### Recovery milestones to watch (crypto)

```
BTC-AUD now:  A$89,723   (CAUTION — no new entries)
+5%        →  A$94,210   (still below 200MA)
+10%       →  A$98,696
+15%       → A$103,182
+20%       → A$107,668
BULL zone  → A$113,533   (+21% — screener auto-generates signals)
```

First signals likely from: BTC-AUD, DOGE-AUD, LINK-AUD, XRP-AUD (closest to 200MA).

### ❌ Not Built (Phase 4+)

- Backtest page (Vectorbt — stub at `/backtest`)
- IBKR stop order modification (sync_stop_orders works for crypto; IBKR modify-order TBD)
- Pyramid add-on order logic (rule seeded, task logic TBD)
- CGT report export
- IBKR position reconciliation on startup
- Sector RS ranking (rule seeded, not implemented)
- Cloud deployment (Railway/DO with Cloudflare tunnel)
- Intraday 4h/1h crypto screener (EOD/daily only currently)

### ❌ Not Built (Phase 4+)

- Backtest page (Vectorbt — stub at `/backtest`)
- IBKR stop order modification API (sync_stop_orders works for crypto; IBKR modify-order TBD)
- Pyramid add-on order logic (rule seeded, logic in trading.py TBD)
- CGT report export (PDF or Excel)
- IBKR position reconciliation on startup
- Sector RS ranking (rule seeded but not implemented in screener)
- Cloud deployment (Railway/DigitalOcean with Cloudflare tunnel)
- Intraday 4h/1h crypto screener (currently EOD/daily only)

---

## Services Status (as of 8 Jun 2026 — Session 3)

| Service | Status | Notes |
|---|---|---|
| `vcpilot-database` | ✅ Healthy | TimescaleDB running, all tables migrated |
| `vcpilot-redis` | ✅ Healthy | Celery broker ready |
| `vcpilot-app` | ✅ Completed | migrate_saas.py ran successfully — all rules seeded |
| `vcpilot-worker-equities` | ✅ Running | 5-min P&L refresh + ASX entry/exit checks |
| `vcpilot-worker-crypto` | ✅ Running | 5-min crypto entry/exit/stop checks |
| `vcpilot-beat` | ✅ Running | 5-min crypto beat + 4× daily screener active |
| `vcpilot-api` | ✅ Running | http://localhost:8501 |
| `vcpilot-whatsapp` | ✅ Running | http://localhost:3000 — QR not yet scanned for AW |
| `vcpilot-ibkr` | ⏸ Not started | Need: `docker compose --profile trading up ibkr -d` |

## Data State (AW Org — id=10)

| Item | State |
|---|---|
| ASX200 universe | Needs `refresh_asx.sh` to confirm/refresh |
| ASX price bars | Run `refresh_asx.sh` to verify |
| ASX market regime | BEAR (re-evaluate via `refresh_asx.sh`) |
| ASX signals (AW) | 0 — BEAR regime blocks entries |
| IR crypto universe | ✅ 100 tokens seeded |
| IR price bars | ✅ 47 tickers with 2yr history |
| IR market regime | ✅ CAUTION (BTC -21% vs 200MA) |
| IR signals (AW) | 0 — CAUTION regime, no VCP breakouts |
| IR watchlist (AW) | 1 item: BTC-AUD |
| AstraTrade rules | ✅ 56 total | 11 crypto | 45 equity |
| IBKR connection | ⏸ Not connected (simulation mode active) |
| IR API credentials | ⚠️ Needs config in /admin/config |
| WhatsApp | ⚠️ QR not scanned for AW org |
| MCP credentials | ⚠️ Not generated for AW org yet |

---

## Data State

| Item | State |
|---|---|
| ASX200 universe | Empty — run `refresh_universe` task first |
| Price history | Empty — run `refresh_price_data` after universe |
| AstraTrade rules | ✅ 40+ rules seeded |
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
| ~~`/superadmin/exchanges` 500 — `_is_superadmin` undefined~~ | ~~High~~ | ✅ Fixed — added helper function |
| ~~`/superadmin/data?tab=crypto` 500 — `w.added_at` SQL bug~~ | ~~High~~ | ✅ Fixed — `w.created_at AS added_at` + proper tab handlers |
| ~~`/superadmin/rules` no crypto category shown~~ | ~~Medium~~ | ✅ Fixed — CRYPTO added to CATEGORY_LABELS |
| ~~US/crypto order routing wired in `trading.py`~~ | ~~Medium~~ | ✅ Fixed — `check_entry_triggers` and `check_exit_rules_task` use signal/position `exchange_key` and route to ccxt/IBKR |
| ~~`trading.py` calls `market_is_open_now()` with exchange~~ | ~~Low~~ | ✅ Fixed — `check_entry_triggers` and `check_exit_rules_task` pass `exchange_key` parameter |
| ~~Screener button silently did nothing on non-trading days~~ | ~~High~~ | ✅ Fixed — now uses `_run_screen_force` |
| ~~`screening.py` duplicate functions caused SyntaxError on import~~ | ~~Critical~~ | ✅ Fixed — file rewritten clean |
| ~~Stopped-out crypto positions never closed (`sync_stop_orders`/MCP `close_position` wrote to non-existent `Position` fields, error swallowed)~~ | ~~Critical~~ | ✅ Fixed (8 Jun, Session 4) — exit detail now written to `Trade`'s real columns; regression-tested |
| ~~Watchlist→Signal manual promotion silently "did nothing" (status flipped to SIGNALLED before/without a real signal being created)~~ | ~~Critical~~ | ✅ Fixed (8 Jun, Session 4) — dashboard rollback on queue failure + duplicate-signal audit messaging; regression-tested |
| ~~MCP `get_positions(include_closed=True)` crashed with `AttributeError: Trade.closed_at`~~ | ~~High~~ | ✅ Fixed (8 Jun, Session 4) — mapped to real `Trade`/`Position` columns; regression-tested |
| ~~Stock prices accidentally routed to Independent Reserve crypto API (showing as 'independentreserve' with crypto prices on ASX tickers)~~ | ~~High~~ | ✅ Fixed (9 Jun, Session 1) — `get_intraday_price()` refined to guard IR logic with `asset_type=="CRYPTO"` |
| ~~`main.py` duplicate route handlers~~ | ~~Medium~~ | ✅ Fixed — removed all duplicates |
| ~~Hardcoded Tailwind colors showing green/yellow/red in light mode~~ | ~~Medium~~ | ✅ Fixed — CSS variables throughout |
| **Two-way Communications Hub:** Renamed "WhatsApp" to "Communications" Hub. Added Telegram two-way support via interactive console and real-time webhook. Admins can now register Telegram webhooks via the UI. | Medium | ✅ Implemented |
| **Telegram Bot Integration:** Fully implemented remote command handling for Telegram (STATUS, POSITIONS, etc.), matching the WhatsApp agent capability. | Medium | ✅ Implemented |

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
