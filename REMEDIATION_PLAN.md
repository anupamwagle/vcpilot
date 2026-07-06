# AstraTrade — Enterprise Remediation Plan (audit of 5 Jul 2026)

> Produced by a full static audit of the repo at commit `707b4a6` (+ uncommitted working-tree changes).
> **Prime directive for any agent applying these fixes: the app is LIVE on prod (NAS, bind-mounted,
> auto-reloading). Every change must be behaviour-preserving unless the item explicitly says otherwise.
> Work one item per branch/commit, run `pytest` (inside the app container) after each, and never touch
> `audit_logs` rows, DB data, or the Celery schedule semantics.**
>
> Severity: 🔴 Critical · 🟠 High · 🟡 Medium · 🔵 Low
> Effort: S (<1h) · M (half day) · L (multi-day)

---

## Phase 0 — Guardrails (do these BEFORE any fix)

- **P0.1 — Commit the in-flight work.** The working tree has ~300 uncommitted inserted lines across
  [app/tasks/trading.py](app/tasks/trading.py), [app/models/trade.py](app/models/trade.py),
  [scripts/migrate_saas.py](scripts/migrate_saas.py), [web/main.py](web/main.py) and 3 test files
  (the CLAUDE.md #34 signal/position exclusivity + #35 BROKER_SYNC + available-capital work).
  Because prod bind-mounts and auto-reloads pulled code, uncommitted local code means repo ≠ prod.
  Run the test suite, then commit this work as its own commit before starting anything below.
  While reviewing it, verify in the new available-capital block in `check_entry_triggers`:
  (a) `submitted_value_this_run` is actually incremented after each successful order submission,
  (b) the `get_fx_rate(base_currency, asset_currency)` direction is correct for every base/asset pair
  (AUD→USD returns USD-per-AUD; the code multiplies AUD capital by it to get local currency — correct
  for AUD/USD, but confirm for USDT/crypto bases), (c) the hardcoded `0.65` fallback is acceptable.
- **P0.2 — Establish a runnable test baseline.** `pytest` is not installed on the host; it must run in
  a container: `docker compose run --rm --no-deps worker-equities pytest -q`. Record the pass/fail
  baseline. CLAUDE.md documents 8 pre-existing failures (entry-trigger, price-range, org-membership,
  IBKR contract routing, VCP persistence, activity logging) — triage list is item B1.
- **P0.3 — Rotate `APP_SECRET_KEY`** if not already done (a tracked `env.txt` leak was removed 1 Jul 2026).
  Rotating invalidates sessions and MCP JWTs (both signed with it) — do it at a quiet hour.

---

## 1. Security fixes

### 🔴 Critical

- **S1 — Telegram webhook accepts forged commands. (S)**
  [web/main.py:6511](web/main.py:6511) `POST /webhook/telegram` authenticates only by matching the
  `chat_id` *inside the request body* against org config. Anyone who can reach the endpoint can POST
  a fake Telegram update with a known/guessed chat_id and execute trading commands (PAUSE, EXIT, STOP,
  RULE…). **Fix:** generate a per-deployment secret, pass it as `secret_token` in the `setWebhook` call
  ([web/main.py:6625](web/main.py:6625)), and reject any webhook request whose
  `X-Telegram-Bot-Api-Secret-Token` header doesn't match (constant-time compare). Store the secret in
  SystemConfig (`telegram_webhook_secret`, auto-generated if blank). Re-register the webhook after
  deploy. The polling fallback (`poll_telegram_updates`) is unaffected.

- **S2 — OTP leaks into the redirect URL in production. (S)**
  [web/main.py:880](web/main.py:880): `debug_param = f"&debug_otp={otp}" if (settings.smtp_host == "smtp.gmail.com" and not email_sent) or (not settings.smtp_username) else ""`.
  If SMTP is unconfigured or a send fails, the live OTP for **any user's email** is exposed in the URL
  (and the verify page renders it) — full account takeover. **Fix:** gate strictly on
  `settings.app_env == "development"`; in production return a "check your email / delivery failed,
  contact admin" error instead. Same for the `debug_otp` query param handling at
  [web/main.py:887](web/main.py:887).

- **S3 — No rate limiting or lockout on any auth surface. (M)**
  `/login`, `/login/request-otp`, `/login/verify-otp`, `/reset-password`, `/mcp/oauth/token` allow
  unlimited attempts. The OTP is 6 digits with a 10-minute window — brute-forceable. **Fix:**
  Redis-backed counters: (a) invalidate the OTP after 5 failed verifies; (b) lock an account for
  15 min after 10 failed password attempts (audit-log the lockout); (c) per-IP throttle (e.g. `slowapi`
  or a small middleware) of ~10 req/min on the auth routes. Keep responses identical on success paths.

- **S4 — Cross-tenant config leak in `Settings._get_db_config`. (M — needs care)**
  [app/config.py:78](app/config.py:78) queries `system_configs` by key **with no `organization_id`
  filter**, so `settings.ibkr_password`, `settings.telegram_bot_token`, `settings.ibkr_paper_mode`,
  `settings.working_capital` etc. return an *arbitrary org's* row once more than one org has the key.
  Wrong tenant's broker credentials / paper-mode flag could drive trading. Today only one org (AW,
  id=10) is live, which is why it "works" — **do not simply add `organization_id IS NULL`, that changes
  current behaviour.** **Fix:** (a) add an `organization_id: int | None` parameter to `_get_db_config`
  and each property, defaulting to the current behaviour behind a deprecation log; (b) audit every call
  site of these `settings.*` properties (broker, notifier, tasks) and pass the org context explicitly —
  most already have `organization_id` in scope; (c) for genuinely global keys (`mock_time_enabled`,
  `ibkr_simulate`) filter `organization_id IS NULL`. Ship with a regression test that seeds two orgs
  with different `ibkr_account` values and asserts each org resolves its own.

### 🟠 High

- **S5 — Default/hardcoded secrets. (S)**
  `os.getenv("APP_SECRET_KEY", "changeme-secret")` at [web/main.py:20](web/main.py:20) and
  [app/mcp/auth.py:45](app/mcp/auth.py:45); `superadmin_password: str = "superadmin-pass"` at
  [app/config.py:61](app/config.py:61); `VNC_SERVER_PASSWORD: changeme` hardcoded in
  [docker-compose.yml:267](docker-compose.yml:267). **Fix:** on startup, if `app_env == "production"`
  and `APP_SECRET_KEY` is missing or starts with `changeme`, log CRITICAL and refuse to start (raise).
  Same check for `SUPERADMIN_PASSWORD`. Move the VNC password to `${VNC_PASSWORD:-changeme}` in compose
  and document it in `.env.example`.

- **S6 — Env-superadmin login uses plaintext, timing-unsafe compare. (S)**
  [web/main.py:777](web/main.py:777): `password == settings.superadmin_password`. **Fix:** use
  `hmac.compare_digest(password.encode(), settings.superadmin_password.encode())` and compare the
  email case-insensitively as now. (Longer term: retire the env-superadmin path in favour of the DB
  Super Admin role — separate, opt-in change.)

- **S7 — Session hardening: fixation, no expiry, stale role. (M)**
  [web/main.py:20](web/main.py:20) `SessionMiddleware` has no `https_only`, no `max_age`, and login
  handlers mutate the existing session instead of clearing it (fixation). `user_role` is cached in the
  session forever, so revoking someone's Super Admin role does nothing until they log out. **Fix:**
  (a) `request.session.clear()` at the top of every successful login path before setting keys;
  (b) `max_age=8*3600` (or config-driven); (c) `https_only=bool(int(os.getenv("SESSION_SECURE", "0")))`
  so prod-behind-TLS can enable it without breaking plain-HTTP LAN dev; (d) in `_global()`/`_auth`,
  re-verify the superadmin role from DB for `user_id`-bearing sessions (cache 60 s in Redis to avoid
  a per-request query).

- **S8 — No CSRF protection on ~70 state-changing POST routes; `GET /logout` is state-changing. (M)**
  SameSite=lax (Starlette default) blocks most cross-site POSTs, but there is no token defence at all.
  **Fix (incremental, low-risk):** (1) convert `/logout` to POST (keep a GET that renders a
  confirm/auto-submit form for old links); (2) add a double-submit CSRF token: middleware sets a
  signed cookie, a Jinja global injects `<input type="hidden" name="csrf_token">` into forms via
  `base.html`, and a dependency validates it on POST for session-authenticated HTML routes only
  (exclude `/webhook/telegram`, `/mcp/oauth/token`, JSON polling endpoints).

- **S9 — Open redirect via `next`. (S)**
  `RedirectResponse(next, 302)` with unvalidated `next` in `/login`, `/login/verify-otp`
  ([web/main.py:754](web/main.py:754), 786, 809, 934). **Fix:** a `_safe_next(next)` helper — allow
  only values starting with `/` and not `//` or containing `\`; otherwise fall back to `/`.

- **S10 — Tracebacks returned to clients. (S)**
  [web/main.py:4290](web/main.py:4290) (`/stock-story`) returns `{"error", "trace": traceback.format_exc()}`
  **unconditionally**; similar blocks near 4505 and 5019; the global 500 handler leaks the traceback
  whenever `app_env == "development"` — which is the **default** if `.env` forgets `APP_ENV`.
  **Fix:** gate every traceback-in-response on `settings.app_env == "development"`; keep server-side
  `logger.error` untouched. Also see M3 (fail-fast env validation).

- **S11 — Secrets written to logs. (S)**
  [web/main.py:~8868](web/main.py:8868) logs the OAuth `code` and `code_verifier` at INFO;
  [app/database.py:18](app/database.py:18) `echo=(app_env == "development")` prints every SQL statement
  including `system_configs` values (IBKR/Telegram/crypto secrets) — again, development is the default.
  **Fix:** remove `code`/`code_verifier` from the log line (log presence booleans instead); make SQL
  echo opt-in via a dedicated `SQL_ECHO=1` env var instead of piggy-backing on app_env.

### 🟡 Medium

- **S12 — RBAC exists but is essentially unenforced. (L)**
  `_has_permission` is used **4 times** in a 9,194-line route file. `/admin/config` (GET+POST — IBKR
  creds, crypto API secrets), `/admin/rules/*`, all `/action/*` triggers, `/positions/{id}/close|purge`
  check only `_auth(request)` — any Viewer-role org member can reconfigure the broker or close
  positions. **Fix:** add FastAPI dependencies `require_user`, `require_perm("manage_config")`,
  `require_superadmin` and apply per route group: `/admin/config*` + `/admin/rules*` →
  `manage_config`; `/action/*`, `/positions/*/close|purge`, promote/skip routes → `trade`;
  keep read pages on plain auth. Seed the permissions in `migrate_saas.py` idempotently and make sure
  the existing org-admin users of org 10 receive them in the same migration (zero behaviour change for
  current users).

- **S13 — Secrets stored and displayed in plaintext SystemConfig. (M)**
  `ibkr_password`, `crypto_api_secret`, `telegram_bot_token`, SMTP creds live unencrypted in the DB and
  are rendered into `/admin/config` inputs. **Fix:** Fernet envelope encryption (`cryptography` lib,
  key from `CONFIG_ENCRYPTION_KEY` env; if unset, log a warning and keep plaintext for compatibility).
  Encrypt-on-write, decrypt-on-read inside a single accessor; migrate existing rows lazily. UI: render
  password-control values as `••••` + "leave blank to keep" semantics (the update route already
  receives full values — treat empty submit as no-op for password controls).

- **S14 — Password-reset link poisoning + plaintext reset tokens. (S)**
  [web/main.py:7735](web/main.py:7735) builds the reset link from the `Host` header (spoofable —
  especially with `--forwarded-allow-ips '*'`, see S15). The manual fallback also puts the token in a
  redirect URL (`?saved=reset_manual&token=…`) → access logs/history. Tokens are stored in plaintext.
  **Fix:** introduce a `public_base_url` SystemConfig/env (the MCP base-url setting at
  `/superadmin/mcp/base-url` already exists — reuse it) and build links from it; store
  `sha256(token)` in `reset_token` and hash the incoming token on lookup; keep the manual-copy flow but
  render the link once in the response body, not in a query param.

- **S15 — Proxy-header trust wide open. (S)**
  Both uvicorn commands use `--proxy-headers --forwarded-allow-ips '*'`
  ([docker-compose.yml:220](docker-compose.yml:220), 251, and both Dockerfiles' CMD). Any direct client
  can spoof `X-Forwarded-For` (poisons the activity/audit IP logging) and `X-Forwarded-Proto`.
  **Fix:** set `--forwarded-allow-ips` to the docker bridge subnet / reverse-proxy IP via env var
  `FORWARDED_ALLOW_IPS` with a safe default of the compose network range.

- **S16 — Internal services exposed on the host/LAN. (M)**
  Postgres (`${POSTGRES_PORT:-5432}`), Redis 6389 (**no password** — LAN access to the Celery broker
  ≈ arbitrary task injection), IBKR Gateway 4001/4002 (**LAN access = order placement API**), VNC 5900,
  noVNC 6085 are all published. **Fix:** (a) add `requirepass` to Redis
  (`command: redis-server --port 6389 --requirepass ${REDIS_PASSWORD}`) and update `REDIS_URL` to
  `redis://:${REDIS_PASSWORD}@redis:6389/0` — coordinate both in one deploy; (b) change published ports
  to loopback binds (`127.0.0.1:5432:5432`, `127.0.0.1:4001-4002:…`) or delete the `ports:` entries
  where only inter-container access is needed (workers reach IBKR via the compose network). Keep
  `web`/`mcp` published. Verify nothing on the NAS host itself dials these ports first.

- **S17 — Prod runs `--reload`/watchmedo as root with a bind-mounted repo. (M — architecture A8)**
  Documented tradeoff, but enterprise-grade prod should run baked images, non-root, no reload.
  Handled by A8 (dev/prod compose split) — don't change ad hoc.

- **S18 — Weak crypto parameters in password/OTP handling. (S)**
  [app/models/auth.py](app/models/auth.py): PBKDF2-SHA256 at 100k iterations (OWASP 2024: 600k);
  `check_bytes == hash_bytes` and the OTP compare at [web/main.py:913](web/main.py:913) are not
  constant-time. **Fix:** `hmac.compare_digest` in `verify_password` and the OTP check; bump new hashes
  to 600k iterations by encoding the count into the stored string (`iterations:salt:hash`), keep
  verifying legacy 2-part format, and transparently re-hash on successful login.

### 🔵 Low

- **S19 — MCP JWT hygiene. (S)** JWT secret is the same `APP_SECRET_KEY` used for session cookies;
  the revocation DB check in [app/mcp/server.py:97](app/mcp/server.py:97) is fail-open (DB error ⇒
  request proceeds). **Fix:** support optional `MCP_JWT_SECRET` env (fallback to APP_SECRET_KEY for
  compatibility); make the revocation check fail-closed with a 503 on DB error.
- **S20 — Dependency pinning & scanning. (S)** `ccxt`, `yfinance>=1.4.1`, `mcp>=1.2.0`, `PyJWT>=2.8.0`,
  `pydantic>=…` are unpinned in [requirements.txt](requirements.txt); no lockfile, no audit. **Fix:**
  pin exact versions currently installed in the prod image (`pip freeze` inside the container), add
  `pip-audit` to CI (A7).
- **S21 — Repo hygiene. (S)** Tracked junk: [web/main_diff.txt](web/main_diff.txt); untracked clutter:
  `fix_aw3.log`, `scratch/`, one-off `*.sh` at root. Remove the diff file from git, gitignore
  `scratch/` and `*.log`, move the utility scripts to `scripts/ops/`.
- **S22 — `/openapi.json` still public. (S)** [web/main.py:19](web/main.py:19) sets `docs_url=None,
  redoc_url=None` but not `openapi_url=None` — the full route schema is downloadable. Add
  `openapi_url=None`.

---

## 2. Bugs — discovered + documented, with fix outline

- **B1 🔴 — 8 documented failing tests (trading-critical paths). (L)** From CLAUDE.md (verified list,
  re-run per P0.2 to confirm still current):
  `test_activity_logging.py::test_skipped_path_not_logged`,
  `test_entry_triggers.py::test_entry_check_portfolio_heat_within_limit_allows_entry`,
  `test_entry_triggers.py::test_entry_check_breakout_confirmed_opens_position`,
  `test_multi_org_membership.py::test_org_create_with_existing_email_adds_membership_no_400`,
  `test_price_range_rule.py::test_check_entry_triggers_within_range_still_opens_position`,
  `test_us_equity_universe.py::TestIBKRContractRouting::test_asx_still_routes_correctly`,
  `test_watchlist_vcp_persistence.py::test_upsert_watchlist_persists_vcp_geometry`,
  `test_watchlist_vcp_persistence.py::test_enrich_compute_path_computes_and_writes_back`.
  Three of these cover **whether entries open positions at all** — triage each: decide test-stale vs
  code-broken, fix accordingly, one commit per test. Do not weaken assertions to make them pass.
- **B2 🔴 — Cross-tenant `_get_db_config`** — same as S4 (it is both a bug and a vuln).
- **B3 🟠 — 85 silent `except Exception: pass` blocks, 10 in [app/tasks/trading.py](app/tasks/trading.py). (M)**
  This exact pattern caused the stuck-open-positions live bug (CLAUDE.md #30). **Fix:** in `app/tasks/`,
  `app/broker/`, `app/trading/` replace bare `pass` with `logger.warning(..., exc_info=True)` minimum,
  plus a `TASK_ERROR` AuditLog row on order/position/trade paths. Leave genuinely-optional paths
  (cache warm, `last_used_at` update) as logged-but-continue. UI/template helper paths in `web/main.py`
  can keep swallowing but must log at DEBUG.
- **B4 🟠 — Naive `datetime.utcnow()` everywhere (deprecated in 3.12). (M)**
  OTP/reset expiry, JWT iat/exp, audit timestamps. Works today because it's consistently naive-UTC.
  **Fix:** central `app/utils/time_helper.utc_now()` returning naive UTC (preserving DB column
  compatibility), swap call sites mechanically; migrate to tz-aware only alongside an Alembic column
  change (A2). Do not mix aware/naive in one comparison.
- **B5 🟡 — Compose healthcheck hardcodes credentials.** [docker-compose.yml:58](docker-compose.yml:58)
  `pg_isready -U vcpilot -d vcpilot` ignores `${POSTGRES_USER}` (the commented-out correct line is
  right there). Fix the interpolation (compose healthchecks do expand env vars from the host env/.env).
- **B6 🟡 — `.env.example` drift.** `POSTGRES_PORT=5439` vs `DATABASE_URL=…:5432…`; missing
  `REDIS_PASSWORD` (after S16), `FORWARDED_ALLOW_IPS` (S15), `SESSION_SECURE` (S7),
  `CONFIG_ENCRYPTION_KEY` (S13), `VNC_PASSWORD` (S5). Update the example + README as those items land.
- **B7 🟡 — QNAP-specific absolute path in compose.** [docker-compose.yml:289](docker-compose.yml:289)
  `novnc` mounts `/share/Container/vcpilot/novnc` — breaks every non-NAS machine. **Fix:**
  `${NOVNC_WEB_DIR:-./docker/novnc}:/novnc:ro` with the default folder committed (or switch to an
  image that ships its own web root).
- **B8 🟡 — Schema managed by `create_all` + ad-hoc script, Alembic installed but unused.** See A2/M1.
- **B9 🟡 — MCP auth middleware is `BaseHTTPMiddleware` wrapping an SSE stream.**
  [app/mcp/server.py:41](app/mcp/server.py:41) — BaseHTTPMiddleware is known to interfere with
  long-lived streaming responses (buffering/cancellation issues) on some Starlette versions, and it
  holds the DB check inline per request. **Fix:** rewrite as pure ASGI middleware (function wrapping
  `app(scope, receive, send)`), behaviour identical. Test SSE with a real Claude Desktop session.
- **B10 🟡 — Dangerous global toggles silently honoured in prod.** `mock_time_enabled`,
  `mock_current_time`, `ibkr_simulate` are read from DB at runtime ([app/config.py:167-188](app/config.py:167)).
  A leftover row from testing silently fakes the clock or simulates fills. **Fix:** if
  `app_env == "production"` and any of these are truthy, emit a CRITICAL log + AuditLog on every
  worker/web startup and surface a red banner on `/admin/health` (don't hard-disable — superadmins use
  `ibkr_simulate` deliberately).
- **B11 🟡 — Every `settings.<db property>` access is a fresh DB query.** Hot paths (notifier, broker,
  entry checks) hammer `system_configs`. **Fix:** 30-second in-process TTL cache inside
  `_get_db_config` (dict + monotonic timestamp; thread-safe enough for this use). Bounded staleness is
  acceptable — config edits already take effect "eventually" across processes.
- **B12 🔵 — Reset-token in redirect query string** — covered by S14.
- **B13 🔵 — Failed-login audit rows use `TASK_ERROR`/`CONFIG_CHANGED` actions** — add proper
  `AuditAction.LOGIN`/`LOGIN_FAILED` enum members (Postgres enum needs a migrate_saas/Alembic step);
  keep writing the old values until the enum lands to avoid breaking audit-page filters.

---

## 3. Architecture — path to enterprise grade (behaviour-preserving refactors)

Ordered so each step stands alone.

- **A1 (L) — Split `web/main.py` (9,194 lines) into routers.** Create `web/routers/{auth,trading,trader,admin,superadmin,actions,oauth,webhooks}.py` using `APIRouter`, moving routes verbatim (same paths, same function bodies), shared helpers (`_auth`, `_global`, `_worker_status`, `FIELD_HINTS`, exchange-filter helpers) into `web/deps.py` / `web/helpers.py`. Zero logic change; verify with a route-table snapshot test (`[ (r.path, sorted(r.methods)) for r in app.routes ]` before == after). Do this EARLY — every later fix becomes easier to review.
- **A2 (L) — Adopt Alembic (already in requirements).** (1) `alembic init`; (2) autogenerate a baseline revision against the current prod schema and stamp prod (`alembic stamp head`) — no DDL executed; (3) new schema changes become revisions; (4) keep `scripts/migrate_saas.py` for *data/seeding* only, its schema-DDL migrations frozen; (5) `migrate` service command becomes `alembic upgrade head && python -m scripts.init_db --seed-only && python -m scripts.migrate_saas`. `Base.metadata.create_all` stays temporarily as a no-op safety net for existing tables.
- **A3 (M) — Central auth dependencies** (delivers S12): `require_user`, `require_perm(name)`, `require_superadmin` in `web/deps.py`; replace the copy-pasted `if not _auth(request): return RedirectResponse(...)` blocks route-by-route.
- **A4 (M) — Org-scoped config service** (delivers S4/B11): `app/services/org_config.py` with `get_org_config(org_id, key, default)` + Redis/in-proc cache + typed getters; `Settings` DB-properties delegate to it and log a deprecation warning with the caller.
- **A5 (M) — Secrets at rest** — S13.
- **A6 (M) — Observability.** `/healthz` (process up) + `/readyz` (DB + Redis ping) endpoints on web and mcp; compose healthchecks pointing at them; Sentry SDK (env-gated DSN, off by default); optional Prometheus: `prometheus-fastapi-instrumentator` + `celery-exporter` behind a `metrics` compose profile. Log rotation via docker `logging: {driver: json-file, options: {max-size: 10m, max-file: "3"}}` on every service.
- **A7 (M) — CI.** `.github/workflows/ci.yml`: ruff (lint only, no autofix initially), `pip-audit`, and pytest inside the `docker/Dockerfile.app` image (build once, run tests). Gate merges on it. Mark the B1 failures as `xfail(strict=False)` with ticket references until fixed, so CI is green from day one.
- **A8 (M) — Dev/prod compose split.** Keep `docker-compose.yml` as prod-safe base (no bind mounts, no `--reload`, non-root images with fixed UID matching an env-provided `APP_UID`, pinned image digests, resource `limits:`); add `docker-compose.override.yml` (auto-loaded in dev) restoring bind mounts + reload + watchmedo. **Cutover is a deliberate prod change** — schedule it; until then prod keeps current behaviour. This also retires S17 and the root-user Dockerfile note.
- **A9 (S) — Backups/DR.** Nightly `pg_dump -Fc` sidecar (compose profile `ops`) to a mounted backup dir with 14-day retention; document restore; enable Redis AOF (`--appendonly yes`) so beat schedules/queues survive restarts.
- **A10 (M) — Trading safety rails.** Global + per-org `kill_switch` SystemConfig checked at the top of `check_entry_triggers`/`place_order`; max-daily-loss guard (sum today's realised+unrealised, halt entries past threshold, Telegram alert); idempotency: unique constraint or pre-check on (org, ticker, signal_id) open BUY orders — the uncommitted P0.1 work already adds the pre-check, add the DB constraint via Alembic.
- **A11 (S) — Security headers middleware.** `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`; HSTS only when `SESSION_SECURE=1`; CSP in Report-Only first (Tailwind CDN + TradingView + inline scripts will need allowances — do NOT enforce until report data reviewed).
- **A12 (S) — Beat task overlap locks.** Redis `SET NX EX` lock (key per task+exchange) around `run_daily_screen`, `check_entry_triggers`, `sync_stop_orders`, `refresh_price_data` bodies so a slow run and the next tick can't double-execute order-placing logic.
- **A13 (S) — Vendor the frontend CDN deps** (Tailwind CDN, Flowbite) into `/static` for supply-chain + offline resilience; keep exact versions currently served.

---

## 4. Migration / deployment — seamless dev, test, prod

- **M1 (L) — Alembic baseline** — see A2. This is the core of "deploys seamlessly": one command (`alembic upgrade head`) takes any environment (empty dev DB, stale test DB, live prod) to the current schema deterministically.
- **M2 (M) — Fresh-environment proof.** Add a CI job (and a `make bootstrap-dev`) that stands up the full compose stack against an empty volume, waits for `migrate` to exit 0, hits `/healthz`, `/login`, and runs the test suite. This is the regression net for "works on dev/test/prod". Fix whatever it flushes out (expected: B7 novnc path, seed idempotency edge cases).
- **M3 (S) — Fail-fast env validation.** On startup (web + workers): validate required env in production (`APP_SECRET_KEY` non-default, `DATABASE_URL`, `REDIS_URL`, `SUPERADMIN_PASSWORD` non-default) and print a single clear error listing what's missing. Change `Settings.app_env` handling so an *unset* `APP_ENV` logs a loud warning (default stays `development` to preserve current behaviour, per S10/S11 the dangerous branches are gated properly instead).
- **M4 (S) — Environment matrix via compose.** `docker-compose.yml` (prod base) + `docker-compose.override.yml` (dev) + `docker-compose.test.yml` (ephemeral DB/Redis, no ports, runs pytest) — see A8. `--profile trading` stays as-is. Parameterize the novnc mount (B7) and VNC password (S5).
- **M5 (S) — Harden `deploy.sh`.** `set -euo pipefail`; run `alembic upgrade head` (via the migrate service) before rolling services; post-deploy smoke test (`curl -fsS localhost:8501/healthz`); on failure print rollback instructions (`git checkout <prev tag>` — bind-mount reload makes rollback instant today; after A8 cutover, `docker compose up -d` previous image tag).
- **M6 (S) — Pin infrastructure images.** `timescale/timescaledb:latest-pg16`, `ghcr.io/gnzsnz/ib-gateway:latest`, `theasp/novnc:latest`, `redis:7-alpine` → pin to the digests currently running on prod (read them off the NAS with `docker inspect --format '{{index .RepoDigests 0}}'`) so dev/test/prod run identical bits.
- **M7 (S) — Seeding discipline.** Make `scripts/migrate_saas.py` idempotency explicit: every migration function guarded by a recorded version row (it partially does this — normalise it), add `--dry-run` flag that prints planned changes, and ensure `seed_config.py` never overwrites existing org values (verify; fix if it does).

---

## Recommended execution order (for Haiku/Sonnet batches)

| Batch | Items | Risk to prod |
|---|---|---|
| 0 | P0.1, P0.2, P0.3 | none (process) |
| 1 — critical security, small diffs | S2, S9, S10, S11, S22, S6, S5, B5 | very low |
| 2 — critical security, coordinated | S1 (webhook re-register), S3, S16 (Redis password = one coordinated deploy) | low, needs a deploy window |
| 3 — auth/session | S7, S8, S18, S14, S15 | low |
| 4 — tenant correctness | S4/B2 + A4, B11 | medium — needs the two-org regression test |
| 5 — bug triage | B1 (one commit per test), B3, B9, B10, B13 | low |
| 6 — structure | A1, A3/S12, A2/M1, M3, M7 | low (pure refactor + tooling) |
| 7 — platform | A6, A7, M2, M4, M5, M6, A9, A11, A12, A13, S20, S21, B6, B7 | low |
| 8 — deliberate cutovers | A8/S17, A5/S13, A10, B4 | scheduled prod changes |

**Per-item guardrails to include in every hand-off prompt:**
1. Read CLAUDE.md first; obey its patterns (CSS vars, `_run_screen_force` not `run_daily_screen`, Position vs Trade closing pattern, append-only audit_logs).
2. One item per branch; run `docker compose run --rm --no-deps worker-equities pytest -q` and compare against the P0.2 baseline — no new failures.
3. Never change route paths, template context keys, Celery task names/signatures, or DB values without an explicit migration step in the same item.
4. Prod auto-reloads on pull — do not merge anything to `main` that isn't safe to go live immediately.
