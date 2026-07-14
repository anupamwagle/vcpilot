"""
AstraTrade Dashboard — FastAPI + Flowbite/Tailwind
Mobile-first. Split: /trading (client) and /admin (operator).
"""
import os, sys
sys.path.insert(0, "/app")

from datetime import date, datetime, timedelta
from app.utils.time_helper import get_current_date
from fastapi import FastAPI, Request, Form, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import desc, or_
from sqlalchemy.exc import IntegrityError
from loguru import logger

_app_secret_key = os.getenv("APP_SECRET_KEY", "changeme-secret")
if os.getenv("APP_ENV", "development") == "production":
    if not _app_secret_key or _app_secret_key.startswith("changeme"):
        logger.critical("APP_SECRET_KEY is missing or using its default value — refusing to start in production. Set a strong secret (e.g. `openssl rand -hex 32`).")
        raise RuntimeError("APP_SECRET_KEY must be set to a non-default value in production")
    _superadmin_password = os.getenv("SUPERADMIN_PASSWORD", "superadmin-pass")
    if not _superadmin_password or _superadmin_password in ("superadmin-pass", "changeme") or _superadmin_password.startswith("changeme"):
        logger.critical("SUPERADMIN_PASSWORD is missing or using its default value — refusing to start in production.")
        raise RuntimeError("SUPERADMIN_PASSWORD must be set to a non-default value in production")

app = FastAPI(title="AstraTrade", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=_app_secret_key)


@app.on_event("startup")
async def _startup_safety_checks():
    from app.utils.startup_checks import warn_if_dangerous_toggles_enabled
    warn_if_dangerous_toggles_enabled("web")

# ---------------------------------------------------------------------------
# User Activity Logging — records who used which feature (access + changes),
# with parameters and source IP, into audit_logs. Surfaced in the Super Admin
# User Activity Console (/superadmin/activity) with a per-user filter.
# ---------------------------------------------------------------------------
from urllib.parse import parse_qs as _parse_qs

# Ordered longest-prefix-first so the most specific feature wins.
_FEATURE_MAP = [
    ("/superadmin/activity",      "User Activity Console"),
    ("/superadmin/organizations", "Organisations"),
    ("/superadmin/phantom-positions", "Phantom Positions"),
    ("/superadmin/operations",    "Central Operations"),
    ("/superadmin/exchanges",     "Exchanges"),
    ("/superadmin/rules",         "Global Rules"),
    ("/superadmin/users",         "User Management"),
    ("/superadmin/data",          "Market Data"),
    ("/superadmin/mcp",           "MCP Credentials"),
    ("/superadmin",               "Super Admin"),
    ("/admin/data-log",           "Data Log"),
    ("/admin/config",             "Org Config"),
    ("/admin/health",             "Health & Ops"),
    ("/admin/rules",              "Rules"),
    ("/admin/audit",              "Audit Log"),
    ("/admin/tasks",              "Task Log"),
    ("/admin/backtest",           "Backtest"),
    ("/admin",                    "Admin"),
    ("/trader/watchlist",         "Watchlist Terminal"),
    ("/trader",                   "Trader Terminal"),
    ("/positions",                "Positions"),
    ("/signals",                  "Signals"),
    ("/watchlist",                "Watchlist"),
    ("/action",                   "Quick Action"),
    ("/",                         "Dashboard Home"),
]

# Never log these (auth/secret flows, static assets, high-frequency polling/JSON).
_ACTIVITY_SKIP_PREFIXES = (
    "/static", "/favicon", "/login", "/logout", "/forgot-password",
    "/reset-password", "/request-otp", "/verify-otp", "/webhook", "/api/", "/healthz",
)
_ACTIVITY_SKIP_SUBSTR = (
    "/poll", "/data", "/prices", "/exit-checks", "/ping-worker", "/heartbeat",
)
# Parameters whose values must never be stored.
_SENSITIVE_PARAM_KEYS = (
    "password", "passwd", "secret", "client_secret", "api_key", "apikey",
    "api_secret", "token", "otp", "passcode", "code", "vnc_password",
    "ibkr_password", "crypto_api_secret",
)


def _activity_feature_for(path: str) -> str:
    for prefix, name in _FEATURE_MAP:
        if path == prefix or path.startswith(prefix + "/") or (prefix == "/" and path == "/"):
            return name
    return "Other"


def _activity_should_skip(path: str) -> bool:
    if any(path.startswith(p) for p in _ACTIVITY_SKIP_PREFIXES):
        return True
    if any(s in path for s in _ACTIVITY_SKIP_SUBSTR):
        return True
    return False


def _redact_params(params: dict) -> dict:
    out = {}
    for k, v in params.items():
        lk = str(k).lower()
        if any(s in lk for s in _SENSITIVE_PARAM_KEYS):
            out[k] = "***redacted***"
        else:
            val = v[0] if isinstance(v, list) and len(v) == 1 else v
            sval = str(val)
            out[k] = sval if len(sval) <= 300 else sval[:300] + "…"
    return out


class ActivityLoggerMiddleware:
    """
    Pure-ASGI middleware that records user activity (feature access + changes)
    into audit_logs. Implemented at the raw ASGI layer — NOT via @app.middleware
    / BaseHTTPMiddleware — because reading & replaying the request body under
    BaseHTTPMiddleware corrupts the receive stream ("Unexpected message received:
    http.request"). Here we buffer the body and replay it cleanly, then defer to
    the real receive for disconnect events.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        method = scope.get("method", "GET")
        if _activity_should_skip(path):
            return await self.app(scope, receive, send)

        headers = {k.decode("latin1").lower(): v.decode("latin1")
                   for k, v in scope.get("headers", [])}
        ctype = headers.get("content-type", "")
        try:
            clen = int(headers.get("content-length") or 0)
        except ValueError:
            clen = 0

        captured_body = b""
        buffer_body = (
            method in ("POST", "PUT", "PATCH", "DELETE")
            and "application/x-www-form-urlencoded" in ctype
            and 0 < clen <= 20000
        )

        inner_receive = receive
        if buffer_body:
            more = True
            while more:
                message = await receive()
                if message["type"] == "http.request":
                    captured_body += message.get("body", b"")
                    more = message.get("more_body", False)
                elif message["type"] == "http.disconnect":
                    more = False

            _sent = {"done": False}

            async def replay_receive():
                if not _sent["done"]:
                    _sent["done"] = True
                    return {"type": "http.request", "body": captured_body, "more_body": False}
                return await receive()  # subsequent calls: real disconnect events

            inner_receive = replay_receive

        status_holder = {"code": None}

        async def wrapped_send(message):
            if message["type"] == "http.response.start":
                status_holder["code"] = message.get("status")
            await send(message)

        await self.app(scope, inner_receive, wrapped_send)

        try:
            form_params = {}
            if captured_body:
                form_params = dict(_parse_qs(captured_body.decode("utf-8", "ignore")))
            _record_activity_scope(scope, headers, status_holder["code"], method, path, form_params)
        except Exception as e:
            logger.debug(f"activity logger record failed: {e}")


def _record_activity_scope(scope, headers, status, method: str, path: str, form_params: dict):
    sess = scope.get("session") or {}
    if not sess.get("authenticated"):
        return  # only log authenticated users

    from app.database import SessionLocal
    from app.models.audit import AuditLog, AuditAction

    # Source IP — honour reverse-proxy headers (Cloudflare / X-Forwarded-For).
    ip = headers.get("x-forwarded-for", "").split(",")[0].strip()
    if not ip:
        client = scope.get("client")
        ip = client[0] if client else None

    is_change = method in ("POST", "PUT", "PATCH", "DELETE")
    feature = _activity_feature_for(path)

    params = {}
    qs = scope.get("query_string", b"")
    if qs:
        params.update(_redact_params(dict(_parse_qs(qs.decode("utf-8", "ignore")))))
    if form_params:
        params.update(_redact_params(form_params))

    param_summary = ", ".join(f"{k}={v}" for k, v in list(params.items())[:8])
    msg = f"{method} {path}" + (f" — {param_summary}" if param_summary else "")

    db = SessionLocal()
    try:
        db.add(AuditLog(
            action=AuditAction.FEATURE_ACTION if is_change else AuditAction.FEATURE_ACCESS,
            organization_id=sess.get("organization_id"),
            user_id=sess.get("user_id"),
            actor=sess.get("email", "user"),
            feature=feature,
            http_method=method,
            ip_address=ip,
            message=msg[:1000],
            detail={"path": path, "method": method, "params": params, "status": status},
        ))
        db.commit()
    except Exception as e:
        db.rollback()
        logger.debug(f"_record_activity write failed: {e}")
    finally:
        db.close()


# Register as the OUTERMOST middleware so request.session (set by SessionMiddleware
# during the inner call) is populated by the time we log, post-response.
app.add_middleware(ActivityLoggerMiddleware)


# Cache prefixes that SHOULD keep normal browser/CDN caching (versioned assets).
_CACHEABLE_PREFIXES = ("/static", "/favicon")


class NoStoreCacheMiddleware:
    """
    Pure-ASGI middleware that marks every authenticated, per-tenant response as
    non-cacheable (`Cache-Control: no-store` + `Vary: Cookie`).

    WHY: dashboard pages are scoped to the active organization, which lives in the
    session cookie. Without an explicit cache directive the browser's HTTP cache /
    back-forward (bfcache) — and any edge cache (Cloudflare tunnel) — may serve the
    PREVIOUS organization's already-rendered page after the user switches orgs via
    the dropdown. The server re-scopes correctly, but the user never sees it: the UI
    "doesn't refresh". It is also a cross-tenant leak risk (one org's page served
    from cache in another org's context). `no-store` additionally makes the page
    bfcache-ineligible, so the switch always re-fetches.

    Static, content-addressable assets keep their normal caching.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or any(
            scope.get("path", "").startswith(p) for p in _CACHEABLE_PREFIXES
        ):
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for (k, v) in message.get("headers", [])
                    if k.lower() not in (b"cache-control", b"pragma", b"expires")
                ]
                headers.append((b"cache-control", b"no-store, no-cache, must-revalidate, private"))
                headers.append((b"pragma", b"no-cache"))
                headers.append((b"expires", b"0"))
                # Ensure any shared cache keys on the session cookie.
                vary_done = False
                for i, (k, v) in enumerate(headers):
                    if k.lower() == b"vary":
                        if b"cookie" not in v.lower():
                            headers[i] = (k, v + b", Cookie")
                        vary_done = True
                        break
                if not vary_done:
                    headers.append((b"vary", b"Cookie"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


# Added last → OUTERMOST middleware, so the cache headers are applied to the final
# response regardless of which inner layer produced it.
app.add_middleware(NoStoreCacheMiddleware)


from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exceptions import RequestValidationError
from app.utils.cache import cache

# ── Known crypto base-symbol set (e.g. "TRX", "BTC") ─────────────────────────
# Used to catch legacy Signal/Watchlist rows that predate ticker-suffix
# normalisation and still have a bare ticker (e.g. "TRX") + exchange_key="ASX"
# left over from the Jun 2026 screener bug. Suffix checks ("-AUD"/"-USD"/
# "-USDT") alone miss these bare-symbol rows.
from app.data.fetcher import TOP_CRYPTO_SYMBOLS as _CRYPTO_BASE_SYMBOLS
_CRYPTO_TICKER_SET = frozenset(_CRYPTO_BASE_SYMBOLS)

def _looks_like_crypto_ticker(ticker: str) -> bool:
    """True if ticker has a crypto suffix or its bare base matches a known coin symbol."""
    if not ticker:
        return False
    t = ticker.upper()
    if t.endswith(("-AUD", "-USD", "-USDT")):
        return True
    base = t.split("-")[0].split("/")[0]
    return base in _CRYPTO_TICKER_SET

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    status_code = exc.status_code
    if status_code == 404:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "error_code": "404",
                "error_title": "Page Not Found",
                "error_heading": "We lost this signal!",
                "error_message": "The page you are looking for does not exist or has been moved. Use the button below to return to safety.",
            },
            status_code=404
        )
    return templates.TemplateResponse(
        "error.html",
        {
            "request": request,
            "error_code": str(status_code),
            "error_title": f"Error {status_code}",
            "error_heading": "HTTP Error",
            "error_message": exc.detail or "An HTTP exception occurred while processing this request.",
        },
        status_code=status_code
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    logger.error(f"Validation error: {exc}")
    error_detail = None
    from app.config import settings
    if settings.app_env == "development":
        error_detail = str(exc.errors())
    return templates.TemplateResponse(
        "error.html",
        {
            "request": request,
            "error_code": "422",
            "error_title": "Invalid Request",
            "error_heading": "Incorrect Coordinates!",
            "error_message": "The request payload or parameters were invalid. Please verify your inputs.",
            "error_detail": error_detail,
        },
        status_code=422
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    import traceback
    logger.error(f"Unhandled server error: {exc}\n{traceback.format_exc()}")
    from app.config import settings
    error_detail = None
    if settings.app_env == "development":
        error_detail = f"{str(exc)}\n\n{traceback.format_exc()}"
    return templates.TemplateResponse(
        "error.html",
        {
            "request": request,
            "error_code": "500",
            "error_title": "Server Error",
            "error_heading": "Engine Malfunction!",
            "error_message": "An internal server error occurred. AstraTrade has logged the issue and our team has been alerted.",
            "error_detail": error_detail,
        },
        status_code=500
    )

def get_cached_stock_names(db: Session) -> dict[str, str]:
    """Retrieve stock ticker-to-name mapping from Redis cache (expires in 1 hour)."""
    from app.models.market import Stock
    cached = cache.get("stock_names_map")
    if cached is not None:
        return cached
    stocks = db.query(Stock).all()
    stock_names = {s.ticker: (s.name or "") for s in stocks}
    cache.set("stock_names_map", stock_names, expire_seconds=3600)
    return stock_names


def get_cached_wl_labels(org_id: int, db: Session) -> list[dict]:
    """Retrieve watchlist labels for an org from Redis cache (expires in 5 min).
    Returns list of dicts with id, name, color, is_default, sort_order.
    sort_order is used to infer label asset-type: 10-13 = crypto, 20-38 = ASX sector, 0-3 = default.
    """
    from app.models.signal import WatchlistLabel
    from sqlalchemy import func as _wlfunc
    cache_key = f"wl_labels:{org_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    labels = db.query(WatchlistLabel).filter(
        WatchlistLabel.organization_id == org_id
    ).order_by(_wlfunc.lower(WatchlistLabel.name)).all()
    result = [
        {"id": l.id, "name": l.name, "color": l.color, "is_default": l.is_default, "sort_order": l.sort_order}
        for l in labels
    ]
    cache.set(cache_key, result, expire_seconds=300)
    return result

templates = Jinja2Templates(directory="/app/web/templates")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "changeme")

# ---------------------------------------------------------------------------
# Jinja2 filter: {{ some_utc_str | fmt_dt(display_tz) }}
# Converts a UTC ISO timestamp string to the given IANA timezone for display.
# ---------------------------------------------------------------------------
def _jinja_fmt_dt(utc_str, tz: str = "UTC") -> str:
    return _fmt_dt(str(utc_str) if utc_str else "", tz)

templates.env.filters["fmt_dt"] = _jinja_fmt_dt


def _get_display_tz(org_id, db) -> str:
    """Read org_timezone SystemConfig for the given org. Falls back to UTC."""
    from app.models.config import SystemConfig
    cfg = db.query(SystemConfig).filter(
        SystemConfig.key == "org_timezone",
        SystemConfig.organization_id == org_id,
    ).first()
    return (cfg.value or "UTC") if cfg else "UTC"


# ---------------------------------------------------------------------------
# DB dependency
# ---------------------------------------------------------------------------
def get_db():
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Worker status helper
# ---------------------------------------------------------------------------
def _worker_status(heartbeat_str: str) -> str:
    """
    online   — heartbeat within last 15 min
    starting — never received (system just booted)
    offline  — heartbeat older than 15 min
    Stored value is always a UTC ISO string (e.g. 2026-06-04T05:22:00).
    """
    if not heartbeat_str or heartbeat_str.strip() in ("", "Never"):
        return "starting"
    try:
        last = datetime.fromisoformat(heartbeat_str.strip()[:19])
        age  = datetime.utcnow() - last
        if age > timedelta(minutes=15):
            return "offline"
        return "online"
    except Exception:
        return "starting"


def _ordinal(n: int) -> str:
    """1 -> '1st', 2 -> '2nd', 3 -> '3rd', 4 -> '4th', 11/12/13 -> 'th', 21 -> '21st', etc."""
    n = int(n)
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _friendly_time_str(dt) -> str:
    """'2:40pm' — 12-hour, no leading zero, lowercase am/pm, no space."""
    hour12 = dt.strftime("%I").lstrip("0") or "12"
    return f"{hour12}:{dt.strftime('%M')}{dt.strftime('%p').lower()}"


def _friendly_date_str(d) -> str:
    """'22nd June 2026' — ordinal day, full month name, full year."""
    return f"{_ordinal(d.day)} {d.strftime('%B')} {d.year}"


def _friendly_dt_str(dt) -> str:
    """'2:40pm 22nd June 2026'."""
    return f"{_friendly_time_str(dt)} {_friendly_date_str(dt)}"


def _fmt_dt(utc_iso: str, tz_name: str = "UTC") -> str:
    """
    Convert a stored UTC ISO timestamp string to a friendly, human-readable
    string in the given IANA timezone (e.g. 'Australia/Sydney'), e.g.
    '2:40pm 22nd June 2026'.
    Returns empty string if input is blank or unparseable.
    """
    if not utc_iso or not utc_iso.strip():
        return ""
    try:
        import pytz
        tz = pytz.timezone(tz_name) if tz_name else pytz.utc
        naive = datetime.fromisoformat(utc_iso.strip()[:19])
        utc_dt = pytz.utc.localize(naive)
        local_dt = utc_dt.astimezone(tz)
        return _friendly_dt_str(local_dt)
    except Exception:
        return utc_iso[:16]              # fall back to raw truncated string


def _fmt_date(date_val) -> str:
    """
    Friendly date-only formatter, e.g. '22nd June 2026'.
    Accepts a date/datetime object, an ISO date string ('2026-06-22'), or
    None/blank (returns '').
    """
    if date_val is None or date_val == "":
        return ""
    try:
        if isinstance(date_val, str):
            d = datetime.fromisoformat(date_val.strip()[:10]).date()
        elif isinstance(date_val, datetime):
            d = date_val.date()
        else:
            d = date_val  # already a date object
        return _friendly_date_str(d)
    except Exception:
        return str(date_val)


def _jinja_friendly_date(raw) -> str:
    """Jinja filter for templates that hold raw date/datetime objects directly (no tz conversion)."""
    return _fmt_date(raw)


def _jinja_friendly_time(raw) -> str:
    """Jinja filter: time-only friendly format from a raw datetime object (no tz conversion)."""
    if not raw:
        return ""
    try:
        return _friendly_time_str(raw)
    except Exception:
        return str(raw)


def _jinja_friendly_dt(raw) -> str:
    """Jinja filter: full friendly datetime from a raw datetime object (no tz conversion)."""
    if not raw:
        return ""
    try:
        return _friendly_dt_str(raw)
    except Exception:
        return str(raw)


templates.env.filters["friendly_date"] = _jinja_friendly_date
templates.env.filters["friendly_time"] = _jinja_friendly_time
templates.env.filters["friendly_dt"] = _jinja_friendly_dt


# ---------------------------------------------------------------------------
# Global context (navbar + every template)
# ---------------------------------------------------------------------------
def _global(request: Request, db: Session) -> dict:
    from app.config import settings
    from app.models.config import SystemConfig
    from app.models.trade import Position, TradeStatus
    from app.models.signal import Signal, SignalStatus

    org_id = request.session.get("organization_id")

    def cfg(key, default=""):
        if key in ("last_market_regime", "last_regime_check", "mock_time_enabled",
                   "mock_current_time", "ibkr_simulate", "mock_market_regime"):
            c = db.query(SystemConfig).filter(SystemConfig.key == key, SystemConfig.organization_id == None).first()
        elif key == "last_heartbeat":
            # Prefer per-org heartbeat (written by updated health_check task);
            # fall back to the legacy global row so old deployments still work.
            c = db.query(SystemConfig).filter(SystemConfig.key == key, SystemConfig.organization_id == org_id).first()
            if not c:
                c = db.query(SystemConfig).filter(SystemConfig.key == key, SystemConfig.organization_id == None).first()
        else:
            c = db.query(SystemConfig).filter(SystemConfig.key == key, SystemConfig.organization_id == org_id).first()
        return c.value if c else default

    raw_hb       = cfg("last_heartbeat", "")
    display_tz   = cfg("org_timezone", "UTC") or "UTC"
    hb_display   = _fmt_dt(raw_hb, display_tz)
    wstatus      = _worker_status(raw_hb)
    trading_paused = cfg("trading_paused", "false").lower() == "true"

    # Resolve active market regime across all active exchanges; use mock when sim clock is on
    mock_time_on = cfg("mock_time_enabled", "false").lower() == "true"
    if mock_time_on:
        regime_raw = cfg("mock_market_regime", "") or cfg("last_market_regime", "")
        regime_is_simulated = True
    else:
        # Derive overall regime = worst across all active exchanges for this org
        _active_excs = [e.strip() for e in (cfg("active_exchanges", "ASX") or "ASX").split(",") if e.strip()]
        _regime_order = {"BEAR": 0, "CAUTION": 1, "BULL": 2, "UNKNOWN": 3}
        _regimes = []
        for _exc in _active_excs:
            _rc = db.query(SystemConfig).filter(
                SystemConfig.key == f"last_market_regime_{_exc}",
                SystemConfig.organization_id == org_id,
            ).first()
            if _rc and _rc.value and _rc.value not in ("UNKNOWN", ""):
                _regimes.append(_rc.value)
        # Fall back to legacy global key for ASX if no per-exchange keys found
        if not _regimes:
            _legacy = db.query(SystemConfig).filter(
                SystemConfig.key == "last_market_regime",
                SystemConfig.organization_id == None,
            ).first()
            if _legacy and _legacy.value:
                _regimes.append(_legacy.value)
        regime_raw = min(_regimes, key=lambda r: _regime_order.get(r, 3)) if _regimes else ""
        regime_is_simulated = False

    is_paper = os.getenv("IBKR_PAPER_MODE", "true").lower() == "true"
    if org_id:
        from app.models.account import Account
        account = db.query(Account).filter(Account.organization_id == org_id, Account.is_active == True).first()
        if account:
            is_paper = account.is_paper

    open_count = 0
    signal_count = 0
    if org_id:
        open_count = db.query(Position).filter(Position.status == TradeStatus.OPEN, Position.organization_id == org_id).count()
        signal_count = db.query(Signal).filter(
            Signal.status == SignalStatus.PENDING,
            Signal.organization_id == org_id
        ).count()

    all_orgs = []
    user_orgs = []
    user_role = request.session.get("user_role")
    if user_role == "superadmin":
        from app.models.account import Organization
        all_orgs = db.query(Organization).filter(Organization.is_active == True).order_by(Organization.name).all()
    elif request.session.get("user_id"):
        # Multi-org: orgs this regular user may switch between (empty/one => no switcher).
        from app.services.membership import switchable_orgs
        try:
            user_orgs = switchable_orgs(db, request.session.get("user_id"))
        except Exception:
            user_orgs = []

    # User display info for sidebar footer
    user_email = request.session.get("email", "")
    user_name  = ""
    if request.session.get("user_id"):
        from app.models.auth import User
        u = db.query(User).filter(User.id == request.session.get("user_id")).first()
        if u:
            user_name = u.name or ""
    if not user_name:
        # No DB user row (e.g. the pure .env-credential superadmin login) — fall
        # back to the email prefix so the footer shows an identity, not the role
        # (the role/org already renders on the line below, see base.html:810).
        user_name = user_email.split("@")[0] if user_email else ("Super Admin" if user_role == "superadmin" else "User")

    user_id = request.session.get("user_id")

    favourited_tickers = set()
    if org_id:
        from app.models.signal import Watchlist, WatchlistStatus, WatchlistLabel
        res = db.query(Watchlist.ticker).join(
            WatchlistLabel, Watchlist.label_id == WatchlistLabel.id
        ).filter(
            Watchlist.organization_id == org_id,
            Watchlist.status == WatchlistStatus.WATCHING,
            WatchlistLabel.is_default == True
        ).all()
        favourited_tickers = {r[0] for r in res}

    return {
        "request": request,
        "favourited_tickers": favourited_tickers,
        "path": str(request.url.path),
        "regime": regime_raw,
        "regime_is_simulated": regime_is_simulated,
        "regime_set": bool(regime_raw and regime_raw not in ("UNKNOWN", "")),
        "trading_paused": trading_paused,
        "is_paper": is_paper,
        "heartbeat": hb_display or "Never",
        "display_tz": display_tz,
        "worker_status": wstatus,
        "trading_active": (not trading_paused) and (wstatus == "online"),
        "open_count": open_count,
        "signal_count": signal_count,
        "user_role": user_role,
        "user_id": user_id,
        "user_name": user_name,
        "user_email": user_email,
        "org_name": request.session.get("organization_name", ""),
        "all_orgs": all_orgs,
        "user_orgs": user_orgs,
        "current_org_id": org_id,
        "ibkr_simulate": cfg("ibkr_simulate", "false").lower() == "true",
        "mock_time_enabled": mock_time_on,
        "mock_current_time": cfg("mock_current_time", ""),
        "mock_market_regime": cfg("mock_market_regime", "BULL"),
        "onboarding_completed": cfg("onboarding_completed", "false").lower() == "true",
    }


def _auth(request: Request):
    return request.session.get("authenticated", False)


def _safe_next(next_url: str) -> str:
    """Only allow same-site relative redirect targets to prevent open redirects."""
    if not isinstance(next_url, str) or not next_url or not next_url.startswith("/") or next_url.startswith("//") or "\\" in next_url:
        return "/"
    return next_url


def _get_or_create_telegram_webhook_secret(org_id: int, db: Session) -> str:
    """Per-org secret passed to Telegram's setWebhook and checked on every
    incoming webhook request via the X-Telegram-Bot-Api-Secret-Token header,
    so a forged POST with a guessed chat_id can't execute trading commands."""
    import secrets as _secrets
    from app.models.config import SystemConfig
    cfg = db.query(SystemConfig).filter(
        SystemConfig.key == "telegram_webhook_secret",
        SystemConfig.organization_id == org_id,
    ).first()
    if cfg and cfg.value:
        return cfg.value
    new_secret = _secrets.token_urlsafe(32)
    if cfg:
        cfg.value = new_secret
    else:
        db.add(SystemConfig(
            key="telegram_webhook_secret",
            value=new_secret,
            label="Telegram Webhook Secret",
            group="system",
            is_secret=True,
            organization_id=org_id,
        ))
    db.commit()
    return new_secret


def _is_superadmin(request: Request) -> bool:
    return request.session.get("user_role") == "superadmin"


def _has_permission(request: Request, db: Session, perm_name: str) -> bool:
    user_role = request.session.get("user_role")
    if user_role == "superadmin":
        return True
    user_id = request.session.get("user_id")
    if not user_id:
        return False
    from app.models.auth import User
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return False
    for r in user.roles:
        for p in r.permissions:
            if p.name == perm_name:
                return True
    return False


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
async def login_get(request: Request, next: str = Query(""), switch: str = Query("")):
    # Allow showing the login page even when authenticated so users can switch accounts.
    # Only auto-redirect if they aren't explicitly trying to switch.
    if _auth(request) and not switch:
        if next:
            return RedirectResponse(_safe_next(next), 302)
        return RedirectResponse("/", 302)
    current_email = request.session.get("email", "") if _auth(request) else ""
    return templates.TemplateResponse("login.html", {"request": request, "error": None, "next": next, "current_email": current_email})


@app.post("/login")
async def login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
    db: Session = Depends(get_db)
):
    import hmac
    from app.config import settings
    from app.models.auth import User, verify_password
    from app.models.account import Organization
    from app.utils.rate_limit import check_ip_throttle, increment as _rl_increment, reset as _rl_reset, is_set as _rl_is_set, set_with_ttl as _rl_set_with_ttl

    email_clean = email.strip().lower()

    from app.models.audit import AuditLog, AuditAction

    if not check_ip_throttle(request, "login", max_requests=10, window_seconds=60):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Too many attempts. Please wait a minute and try again.", "next": next}, status_code=429)

    lock_key = f"login_lock:{email_clean}"
    if _rl_is_set(lock_key):
        db.add(AuditLog(action=AuditAction.TASK_ERROR, actor=email_clean, message="Login attempt blocked — account temporarily locked after repeated failures"))
        db.commit()
        return templates.TemplateResponse("login.html", {"request": request, "error": "Too many failed attempts. Try again in 15 minutes.", "next": next}, status_code=429)

    # 1. Check Super Admin from .env
    if email_clean == settings.superadmin_email.strip().lower() and hmac.compare_digest(password.encode(), settings.superadmin_password.encode()):
        _rl_reset(f"login_fail:{email_clean}")
        default_org = db.query(Organization).order_by(Organization.id).first()
        request.session["authenticated"] = True
        request.session["user_role"] = "superadmin"
        request.session["organization_id"] = default_org.id if default_org else 1
        request.session["organization_name"] = default_org.name if default_org else "Default Org"
        request.session["email"] = settings.superadmin_email
        db.add(AuditLog(action=AuditAction.CONFIG_CHANGED, actor=settings.superadmin_email, message="Super Admin logged in from web dashboard", organization_id=default_org.id if default_org else 1))
        db.commit()
        if next:
            return RedirectResponse(_safe_next(next), 302)
        return RedirectResponse("/", 302)

    # 2. Check Database Users
    user = db.query(User).filter(User.email == email_clean).first()
    if user and verify_password(password, user.password_hash):
        if not user.is_active:
            db.add(AuditLog(action=AuditAction.TASK_ERROR, actor=email_clean, message="Failed login attempt - user account is disabled"))
            db.commit()
            return templates.TemplateResponse("login.html", {"request": request, "error": "User account is disabled", "next": next}, status_code=401)

        _rl_reset(f"login_fail:{email_clean}")

        # Check if user has "Super Admin" role in DB
        is_super = any(r.name == "Super Admin" for r in user.roles)

        request.session["authenticated"] = True
        request.session["user_role"] = "superadmin" if is_super else "user"
        request.session["user_id"] = user.id
        request.session["organization_id"] = user.organization_id
        request.session["organization_name"] = user.organization.name
        request.session["email"] = user.email
        db.add(AuditLog(action=AuditAction.CONFIG_CHANGED, actor=user.email, user_id=user.id, organization_id=user.organization_id, message=f"User {user.email} logged in from web dashboard"))
        db.commit()
        if next:
            return RedirectResponse(_safe_next(next), 302)
        return RedirectResponse("/", 302)

    fail_count = _rl_increment(f"login_fail:{email_clean}", 15 * 60)
    if fail_count >= 10:
        _rl_set_with_ttl(lock_key, 15 * 60)
        db.add(AuditLog(action=AuditAction.TASK_ERROR, actor=email_clean, message=f"Account locked for 15 minutes after {fail_count} failed login attempts"))
    db.add(AuditLog(action=AuditAction.TASK_ERROR, actor=email_clean, message=f"Failed login attempt for email {email_clean}"))
    db.commit()
    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid email or password", "next": next}, status_code=401)


@app.get("/logout")
async def logout(request: Request, db: Session = Depends(get_db)):
    email = request.session.get("email")
    user_id = request.session.get("user_id")
    org_id = request.session.get("organization_id")
    if email:
        from app.models.audit import AuditLog, AuditAction
        db.add(AuditLog(action=AuditAction.CONFIG_CHANGED, actor=email, user_id=user_id, organization_id=org_id, message=f"User {email} logged out"))
        db.commit()
    request.session.clear()
    return RedirectResponse("/login", 302)


# ---------------------------------------------------------------------------
# OTP Login & Switch Org
# ---------------------------------------------------------------------------
@app.post("/login/request-otp")
async def login_request_otp(
    request: Request,
    email: str = Form(...),
    next: str = Form(""),
    db: Session = Depends(get_db)
):
    import secrets
    from datetime import datetime, timedelta
    from app.models.auth import User
    from app.utils.email import send_email
    from app.config import settings
    from app.utils.rate_limit import check_ip_throttle

    if not check_ip_throttle(request, "login_request_otp", max_requests=10, window_seconds=60):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Too many attempts. Please wait a minute and try again.", "next": next}, status_code=429)

    email_clean = email.strip().lower()
    user = db.query(User).filter(User.email == email_clean).first()
    if not user:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Email address not found", "next": next}, status_code=404)
    if not user.is_active:
        return templates.TemplateResponse("login.html", {"request": request, "error": "User account is disabled", "next": next}, status_code=401)

    # Generate 6-digit OTP code
    otp = f"{secrets.randbelow(900000) + 100000}"
    user.otp_code = otp
    user.otp_expires_at = datetime.utcnow() + timedelta(minutes=10)
    from app.models.audit import AuditLog, AuditAction
    db.add(AuditLog(action=AuditAction.CONFIG_CHANGED, actor=user.email, user_id=user.id, organization_id=user.organization_id, message=f"OTP passcode requested for login"))
    db.commit()

    # Send email
    subject = "Your AstraTrade One-Time Passcode (OTP)"
    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #e5e7eb; border-radius: 8px;">
        <h2 style="color: #1d4ed8; margin-bottom: 20px;">AstraTrade OTP Login</h2>
        <p>You requested a one-time passcode to sign in to your AstraTrade account.</p>
        <p>Please use the passcode below to complete your login:</p>
        <div style="background: #f3f4f6; padding: 15px; border-radius: 8px; text-align: center; font-size: 24px; font-weight: bold; letter-spacing: 4px; margin: 20px 0; color: #111827;">
            {otp}
        </div>
        <p style="color: #6b7280; font-size: 14px;">This passcode is temporary and will expire in 10 minutes.</p>
        <p>If you did not request this, you can safely ignore this email.</p>
    </div>
    """
    
    email_sent = send_email(user.email, subject, html_content)

    if not email_sent and settings.app_env != "development":
        return templates.TemplateResponse("login.html", {"request": request, "error": "Failed to send OTP email. Please contact your administrator.", "next": next}, status_code=500)

    # In development mode only, append debug otp to url if email delivery is offline/disabled
    debug_param = f"&debug_otp={otp}" if settings.app_env == "development" and ((settings.smtp_host == "smtp.gmail.com" and not email_sent) or (not settings.smtp_username)) else ""

    import urllib.parse
    next_param = f"&next={urllib.parse.quote(next)}" if next else ""
    return RedirectResponse(f"/login/verify-otp?email={user.email}{debug_param}{next_param}", 302)


@app.get("/login/verify-otp", response_class=HTMLResponse)
async def login_verify_otp_get(
    request: Request,
    email: str = Query(...),
    debug_otp: str = Query(None),
    next: str = Query("")
):
    from app.config import settings
    if settings.app_env != "development":
        debug_otp = None
    return templates.TemplateResponse("verify_otp.html", {"request": request, "email": email, "debug_otp": debug_otp, "error": None, "next": next})


@app.post("/login/verify-otp")
async def login_verify_otp_post(
    request: Request,
    email: str = Form(...),
    otp_code: str = Form(...),
    next: str = Form(""),
    db: Session = Depends(get_db)
):
    from datetime import datetime
    from app.models.auth import User
    from app.utils.rate_limit import check_ip_throttle, increment as _rl_increment, reset as _rl_reset

    if not check_ip_throttle(request, "verify_otp", max_requests=10, window_seconds=60):
        return templates.TemplateResponse("verify_otp.html", {"request": request, "email": email, "error": "Too many attempts. Please wait a minute and try again.", "next": next}, status_code=429)

    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if not user:
        return templates.TemplateResponse("verify_otp.html", {"request": request, "email": email, "error": "User session expired. Please request a new OTP.", "next": next}, status_code=400)

    from app.models.audit import AuditLog, AuditAction
    if not user.otp_code or user.otp_code != otp_code.strip() or not user.otp_expires_at or user.otp_expires_at < datetime.utcnow():
        fail_key = f"otp_fail:{user.email}"
        fail_count = _rl_increment(fail_key, 15 * 60)
        if fail_count >= 5 and user.otp_code:
            user.otp_code = None
            user.otp_expires_at = None
            db.add(AuditLog(action=AuditAction.TASK_ERROR, actor=user.email, user_id=user.id, organization_id=user.organization_id, message=f"OTP invalidated after {fail_count} failed verification attempts — request a new one"))
        db.add(AuditLog(action=AuditAction.TASK_ERROR, actor=user.email, user_id=user.id, organization_id=user.organization_id, message=f"Failed OTP passcode verification attempt"))
        db.commit()
        return templates.TemplateResponse("verify_otp.html", {"request": request, "email": email, "error": "Invalid or expired OTP code", "next": next}, status_code=400)

    _rl_reset(f"otp_fail:{user.email}")

    # Clear OTP
    user.otp_code = None
    user.otp_expires_at = None
    db.add(AuditLog(action=AuditAction.CONFIG_CHANGED, actor=user.email, user_id=user.id, organization_id=user.organization_id, message=f"OTP passcode verified successfully"))
    db.commit()

    is_super = any(r.name == "Super Admin" for r in user.roles)

    # Log user in
    request.session["authenticated"] = True
    request.session["user_role"] = "superadmin" if is_super else "user"
    request.session["user_id"] = user.id
    request.session["organization_id"] = user.organization_id
    request.session["organization_name"] = user.organization.name
    request.session["email"] = user.email

    if next:
        return RedirectResponse(_safe_next(next), 302)
    return RedirectResponse("/", 302)


@app.post("/superadmin/switch-org")
async def superadmin_switch_org(request: Request, organization_id: int = Form(...), db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    from app.models.account import Organization
    org = db.query(Organization).filter(Organization.id == organization_id).first()
    if org:
        request.session["organization_id"] = org.id
        request.session["organization_name"] = org.name
        from app.models.audit import AuditLog, AuditAction
        db.add(AuditLog(
            action=AuditAction.CONFIG_CHANGED,
            actor=request.session.get("email", "superadmin"),
            user_id=request.session.get("user_id"),
            organization_id=org.id,
            message=f"Super Admin switched active organization context to {org.name}"
        ))
        db.commit()

    referer = request.headers.get("referer", "/")
    return RedirectResponse(referer, 303)


@app.post("/switch-org")
async def switch_org(request: Request, organization_id: int = Form(...), db: Session = Depends(get_db)):
    """
    Regular-user org switcher (multi-org). Sets the active organization in the
    session, but ONLY if the logged-in user is actually a member of the target org.
    Super admins use /superadmin/switch-org (which can switch to ANY org).
    """
    if not _auth(request):
        return RedirectResponse("/login", 302)

    # Super admins have their own switcher with full access.
    if request.session.get("user_role") == "superadmin":
        return await superadmin_switch_org(request, organization_id, db)

    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/login", 302)

    from app.models.auth import User
    from app.models.account import Organization

    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.is_member_of(organization_id):
        # Not a member — refuse and leave the active org unchanged.
        logger.warning(
            f"User {request.session.get('email')} tried to switch to org {organization_id} "
            f"they are not a member of — denied"
        )
        referer = request.headers.get("referer", "/")
        sep = "&" if "?" in referer else "?"
        return RedirectResponse(f"{referer}{sep}switch_error=not_member", 303)

    org = db.query(Organization).filter(Organization.id == organization_id).first()
    if org and org.is_active:
        request.session["organization_id"] = org.id
        request.session["organization_name"] = org.name
        from app.models.audit import AuditLog, AuditAction
        db.add(AuditLog(
            action=AuditAction.CONFIG_CHANGED,
            actor=request.session.get("email", ""),
            user_id=user_id,
            organization_id=org.id,
            message=f"User switched active organization to {org.name}",
        ))
        db.commit()

    referer = request.headers.get("referer", "/")
    return RedirectResponse(referer, 303)



# ===========================================================================
# EXCHANGE FILTER HELPERS
# ===========================================================================

def _get_exchange_filters(org_id: int, db) -> list:
    from app.models.config import SystemConfig
    cfg = db.query(SystemConfig).filter(SystemConfig.key=="active_exchanges",SystemConfig.organization_id==org_id).first()
    excs = [(cfg.value if cfg else "ASX") or "ASX"]
    excs = [e.strip().upper() for e in excs[0].split(",") if e.strip()]
    has_us = any(e in excs for e in ("NYSE","NASDAQ"))
    has_crypto = any(e.startswith("CRYPTO") for e in excs)
    if not has_us and not has_crypto:
        return [{"key":"ALL","label":"All","flag":"","asset_type":"ALL"}]
    r = [{"key":"ALL","label":"All","flag":"","asset_type":"ALL"}]
    if "ASX" in excs: r.append({"key":"ASX","label":"ASX","flag":"🇦🇺","asset_type":"EQUITY"})
    if has_us: r.append({"key":"US","label":"US","flag":"🇺🇸","asset_type":"EQUITY"})
    if has_crypto: r.append({"key":"CRYPTO","label":"Crypto","flag":"₿","asset_type":"CRYPTO"})
    return r

# ===========================================================================
# CLIENT AREA — TRADING
# ===========================================================================

@app.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    exchange: str = Query("ALL"),
    wl_exchange: str = Query(None),
    db: Session = Depends(get_db)
):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")

    from app.models.trade import Position, Trade, TradeStatus
    from app.models.signal import Signal, SignalStatus
    from app.models.account import Account
    from app.models.market import Stock

    ctx = _global(request, db)

    account = db.query(Account).filter(Account.is_active == True, Account.organization_id == org_id).first()
    capital = float(account.capital_aud) if account else 1000.0

    # NOTE: the home dashboard's "Today's Signals" / "Open Positions" / P&L
    # cards always show ALL exchanges, unfiltered. There is no on-page control
    # for the `exchange` param at this scope — it used to be silently shared
    # with the Watchlist card's exchange tabs (see wl_exchange below), which
    # caused clicking a Watchlist tab to also hide signals/positions from
    # other markets, and that filter then persisted indefinitely via the
    # home_wl_filters localStorage key. Use /signals, /positions, or the
    # per-page exchange filters for a scoped view.

    # Open positions
    pos_q = db.query(Position).filter(Position.status == TradeStatus.OPEN, Position.organization_id == org_id)
    positions = pos_q.all()

    # Signals (Show today's signals OR active pending signals, excluding triggered)
    from sqlalchemy import or_
    sig_q = db.query(Signal).filter(
        or_(
            Signal.signal_date == get_current_date(),
            Signal.status == SignalStatus.PENDING
        ),
        Signal.status != SignalStatus.TRIGGERED,
        Signal.organization_id == org_id
    )
    signals = sig_q.all()

    # Company names scoped to only the tickers we're about to render (positions
    # + signals) instead of pulling the entire Stock universe — on ALL_LISTED
    # orgs that table can hold 2,000+ rows and was loaded on every home load.
    _home_tickers = {p.ticker for p in positions} | {s.ticker for s in signals}
    stock_names = {}
    if _home_tickers:
        stock_names = {
            s.ticker: (s.name or "")
            for s in db.query(Stock).filter(Stock.ticker.in_(_home_tickers)).all()
        }

    pos_data, total_risk = [], 0.0
    for p in positions:
        entry = float(p.entry_price or 0)
        curr  = float(p.current_price or entry)
        stop  = float(p.current_stop or 0)
        qty   = float(p.qty or 0)
        pnl   = (curr - entry) * qty
        if entry > 0 and stop > 0:
            total_risk += (entry - stop) * qty
        pos_data.append({
            "ticker": p.ticker,
            "company_name": stock_names.get(p.ticker, ""),
            "qty": qty,
            "entry": entry, "current": curr, "stop": stop,
            "invested_aud": round(entry * qty, 2),
            "pnl_pct": round((curr - entry) / entry * 100, 2) if entry else 0,
            "pnl_aud": round(pnl, 2),
            "days": (get_current_date() - p.entry_date).days if p.entry_date else 0,
            "is_paper": p.is_paper,
        })

    sig_data = [{
        "id": s.id, "ticker": s.ticker,
        "company_name": stock_names.get(s.ticker, ""),
        "pivot": float(s.pivot_price or 0),
        "stop": float(s.stop_price or 0),
        "rs": float(s.rs_rating or 0),
        "vcp": f"{s.vcp_contractions}c·{s.vcp_weeks}w" if s.vcp_contractions else "—",
        "size": s.suggested_size_shares or 0,
        "risk_aud": float(s.risk_per_trade_aud or 0),
        "status": s.status.value,
        "trend_score": s.trend_score or 0,
    } for s in signals]

    # P&L
    today_trades_q = db.query(Trade).filter(Trade.exit_date == get_current_date(), Trade.organization_id == org_id)
    all_trades_q   = db.query(Trade).filter(Trade.organization_id == org_id)
    today_trades = today_trades_q.all()
    all_trades   = all_trades_q.all()

    # Calculate realised P&L
    total_realised_pnl = sum(float(t.net_pnl_aud or 0) for t in all_trades)
    today_realised_pnl = sum(float(t.net_pnl_aud or 0) for t in today_trades)

    # Bulk PriceBar lookup for prev_close (daily P&L)
    from app.models.market import PriceBar
    from sqlalchemy import and_, func
    _pos_tickers = list({p.ticker for p in positions})
    prev_close_map = {}
    if _pos_tickers:
        latest_dates_sub = db.query(
            PriceBar.ticker,
            func.max(PriceBar.date).label("max_date")
        ).filter(
            PriceBar.ticker.in_(_pos_tickers),
            PriceBar.date < get_current_date()
        ).group_by(PriceBar.ticker).subquery()

        prev_bars = db.query(PriceBar).join(
            latest_dates_sub,
            and_(
                PriceBar.ticker == latest_dates_sub.c.ticker,
                PriceBar.date == latest_dates_sub.c.max_date
            )
        ).all()
        prev_close_map = {b.ticker: float(b.close) for b in prev_bars if b.close is not None}

    # Calculate today's unrealised P&L
    today_unrealised_pnl = 0.0
    for p in positions:
        entry = float(p.entry_price or 0)
        curr  = float(p.current_price or entry)
        qty   = float(p.qty or 0)
        fx    = float(p.current_fx_rate or p.entry_fx_rate or 1.0) if getattr(p, "currency", "AUD") != "AUD" else 1.0

        if p.entry_date == get_current_date():
            pos_today_pnl = (curr - entry) * qty * fx
        else:
            prev_close = prev_close_map.get(p.ticker)
            if prev_close is not None:
                pos_today_pnl = (curr - prev_close) * qty * fx
            else:
                pos_today_pnl = (curr - entry) * qty * fx
        today_unrealised_pnl += pos_today_pnl

    total_unrealised_pnl = sum(float(p.unrealised_pnl or 0) for p in positions)
    today_pnl = today_realised_pnl + today_unrealised_pnl
    total_pnl = total_realised_pnl + total_unrealised_pnl

    # ── Automated System Checks ──
    from app.models.audit import AuditLog, AuditAction
    from app.models.config import SystemConfig

    def get_latest_check(action, message_like=None, exch=None):
        q = db.query(AuditLog).filter(
            or_(AuditLog.organization_id == org_id, AuditLog.organization_id == None),
            AuditLog.action == action
        )
        if message_like:
            q = q.filter(AuditLog.message.ilike(f"%{message_like}%"))
        if exch:
            q = q.filter(AuditLog.message.ilike(f"%{exch}%"))
        return q.order_by(desc(AuditLog.created_at)).first()

    def _regime_val_home(exch_key):
        try:
            rc = db.query(SystemConfig).filter(
                SystemConfig.key == f"last_market_regime_{exch_key}",
                SystemConfig.organization_id == org_id
            ).first()
            return (rc.value if rc and rc.value else "—") or "—"
        except Exception:
            return "—"

    # Detect active markets for this org
    try:
        _ae_cfg_h = db.query(SystemConfig).filter(
            SystemConfig.key == "active_exchanges", SystemConfig.organization_id == org_id
        ).first()
        _active_excs_h = [e.strip() for e in ((_ae_cfg_h.value or "ASX") if _ae_cfg_h else "ASX").split(",") if e.strip()]
    except Exception:
        _active_excs_h = ["ASX"]
    _has_asx_h   = "ASX" in _active_excs_h
    _has_us_h    = any(e in ("NYSE", "NASDAQ") for e in _active_excs_h)
    _has_crypto_h = any(e.startswith("CRYPTO") for e in _active_excs_h)
    _crypto_exc_h = next((e for e in _active_excs_h if e.startswith("CRYPTO")), "CRYPTO")

    checks = []

    if _has_asx_h:
        checks += [
            {"name": "Universe Sync (ASX)", "frequency": "Weekly · Sun 8:00pm",
             "log": get_latest_check(AuditAction.TASK_RUN, "Universe", exch="ASX") or
                    get_latest_check(AuditAction.SYSTEM_STARTED, "Universe")},
            {"name": "Price Data (ASX)", "frequency": "Mon–Fri · 5:00pm",
             "log": get_latest_check(AuditAction.TASK_RUN, "Price data", exch="ASX")},
            {"name": "Market Regime (ASX)", "frequency": "Mon–Fri · 5:15pm",
             "log": get_latest_check(AuditAction.MARKET_REGIME_CHANGE, exch="ASX"),
             "active_regime": _regime_val_home("ASX"), "regime_is_simulated": False},
            {"name": "Screener (ASX)", "frequency": "Mon–Fri · 5:30pm",
             "log": get_latest_check(AuditAction.SCREENER_RUN, exch="ASX")},
            {"name": "Entry Checks (ASX)", "frequency": "Every 5 min · 10am–4:12pm",
             "log": get_latest_check(AuditAction.TASK_RUN, "Entry check", exch="ASX")},
            {"name": "Exit Checks (ASX)", "frequency": "Every 5 min · 10am–4:12pm",
             "log": get_latest_check(AuditAction.TASK_RUN, "Exit check", exch="ASX")},
        ]

    if _has_us_h:
        checks += [
            {"name": "Price Data (US)", "frequency": "Tue–Sat · 7:00am",
             "log": get_latest_check(AuditAction.TASK_RUN, "Price data", exch="NYSE")},
            {"name": "Market Regime (US)", "frequency": "Tue–Sat · 7:15am",
             "log": get_latest_check(AuditAction.MARKET_REGIME_CHANGE, exch="NYSE"),
             "active_regime": _regime_val_home("NYSE"), "regime_is_simulated": False},
            {"name": "Screener (US)", "frequency": "Tue–Sat · 7:30am",
             "log": get_latest_check(AuditAction.SCREENER_RUN, exch="NYSE")},
            {"name": "Entry + Exit Checks (US)", "frequency": "Every 5 min · 11:30pm–6:05am",
             "log": get_latest_check(AuditAction.TASK_RUN, "Entry check", exch="NYSE")},
        ]

    if _has_crypto_h:
        checks += [
            {"name": "Price Data (Crypto)", "frequency": "Every 4h · 24/7",
             "log": get_latest_check(AuditAction.TASK_RUN, "Price data", exch="CRYPTO")},
            {"name": "Market Regime (Crypto)", "frequency": "Every 4h · 24/7",
             "log": get_latest_check(AuditAction.MARKET_REGIME_CHANGE, exch="CRYPTO"),
             "active_regime": _regime_val_home(_crypto_exc_h), "regime_is_simulated": False},
            {"name": "Screener (Crypto)", "frequency": "Every 4h · 6× daily",
             "log": get_latest_check(AuditAction.SCREENER_RUN, exch="CRYPTO")},
            {"name": "Entry + Exit Checks (Crypto)", "frequency": "Every 5 min · 24/7",
             "log": get_latest_check(AuditAction.TASK_RUN, "Entry check", exch="CRYPTO")},
        ]

    # ── Watchlist Market Data Table ──
    from app.models.signal import Watchlist, WatchlistStatus, WatchlistLabel
    from app.models.market import Stock, PriceBar
    from sqlalchemy import func, and_
    from sqlalchemy.orm import joinedload

    # Labels from Redis cache — includes sort_order for exchange-aware filtering in template
    wl_labels_data = get_cached_wl_labels(org_id, db)

    # Label counts: one GROUP BY query → {label_id: count} for badge display
    _wl_cnt_rows = (
        db.query(Watchlist.label_id, func.count(Watchlist.id))
        .filter(
            Watchlist.organization_id == org_id,
            Watchlist.status == WatchlistStatus.WATCHING,
            Watchlist.label_id.isnot(None),
        )
        .group_by(Watchlist.label_id)
        .all()
    )
    wl_label_counts = {row[0]: row[1] for row in _wl_cnt_rows}
    wl_total_watching = (
        db.query(func.count(Watchlist.id))
        .filter(Watchlist.organization_id == org_id, Watchlist.status == WatchlistStatus.WATCHING)
        .scalar() or 0
    )

    active_label = request.query_params.get("label")
    active_label_id = int(active_label) if (active_label and active_label.isdigit()) else None
    only_custom = request.query_params.get("custom") == "true"

    # Resolve the exchange filter early so it can be pushed into the SQL query.
    # Without this, the top-25 LIMIT was applied before the Python-level exchange
    # filter, so clicking "ASX" could show 0 items if the 25 most-recently-added
    # rows were all US or Crypto stocks.
    wl_active_exchange_filter = (wl_exchange or "ALL").upper()

    wq = db.query(Watchlist).options(joinedload(Watchlist.label)).filter(
        Watchlist.status == WatchlistStatus.WATCHING,
        Watchlist.organization_id == org_id
    )
    if active_label_id is not None:
        wq = wq.filter(Watchlist.label_id == active_label_id)

    # Push exchange filter into SQL so LIMIT operates on the right subset.
    if wl_active_exchange_filter == "ASX":
        # Include rows where exchange_key is NULL (pre-fix ASX rows stored without key)
        wq = wq.filter(or_(Watchlist.exchange_key == "ASX", Watchlist.exchange_key == None))
    elif wl_active_exchange_filter == "US":
        wq = wq.filter(Watchlist.exchange_key.in_(["NYSE", "NASDAQ"]))
    elif wl_active_exchange_filter == "CRYPTO":
        wq = wq.filter(or_(
            Watchlist.asset_type == "CRYPTO",
            Watchlist.ticker.like("%-AUD"),
            Watchlist.ticker.like("%-USD"),
            Watchlist.ticker.like("%-USDT"),
        ))

    # Dashboard preview only — cap rows so the home page doesn't render the
    # entire watchlist (can be hundreds of rows on ALL_LISTED orgs) on every
    # load. The full, paginated list lives on /watchlist.
    WL_HOME_PREVIEW_LIMIT = 25
    wl_items = wq.order_by(desc(Watchlist.created_at)).limit(WL_HOME_PREVIEW_LIMIT).all()
    wl_tickers = [w.ticker for w in wl_items]
    wl_stock_map = {}
    wl_bar_map = {}

    if wl_tickers:
        wl_stocks = db.query(Stock).filter(Stock.ticker.in_(wl_tickers)).all()
        wl_stock_map = {s.ticker: s for s in wl_stocks}

        latest_dates = db.query(
            PriceBar.ticker,
            func.max(PriceBar.date).label("max_date")
        ).filter(PriceBar.ticker.in_(wl_tickers)).group_by(PriceBar.ticker).subquery()

        wl_bars = db.query(PriceBar).join(
            latest_dates,
            and_(
                PriceBar.ticker == latest_dates.c.ticker,
                PriceBar.date == latest_dates.c.max_date,
            )
        ).all()
        wl_bar_map = {b.ticker: b for b in wl_bars}

    watchlist_rows = []
    for w in wl_items:
        s = wl_stock_map.get(w.ticker)
        bar = wl_bar_map.get(w.ticker)

        is_custom = s and not s.in_asx200
        if only_custom and not is_custom:
            continue

        _h_rr = w.rule_results or {}
        _h_trend_keys = [k for k in _h_rr if k.startswith("trend_")]
        _h_trend_passed = sum(1 for k in _h_trend_keys if (
            _h_rr[k].get("passed") if isinstance(_h_rr[k], dict) else bool(_h_rr[k])
        ))
        _h_trend_total = len(_h_trend_keys)
        _h_rs  = float(bar.rs_rating) if bar and bar.rs_rating else 0
        _h_vol = float(bar.vol_ratio) if bar and bar.vol_ratio is not None else None
        if (_h_trend_total > 0 and _h_trend_passed >= _h_trend_total
                and _h_rs >= 80 and (_h_vol is None or _h_vol <= 0.6)):
            _h_tier = "A"
        elif _h_trend_total > 0 and _h_trend_passed >= max(_h_trend_total - 1, 1) and _h_rs >= 70:
            _h_tier = "B"
        else:
            _h_tier = "C"

        watchlist_rows.append({
            "ticker": w.ticker,
            "company_name": s.name if s else "",
            "sector": s.sector if s else "",
            "in_asx200": s.in_asx200 if s else False,
            "is_custom": is_custom,
            "label": {"id": w.label.id, "name": w.label.name, "color": w.label.color} if w.label else None,
            "exchange_key": getattr(w, "exchange_key", "ASX") or "ASX",
            "asset_type": ("CRYPTO" if w.ticker.endswith(("-AUD","-USD","-USDT")) else getattr(w, "asset_type", "EQUITY") or "EQUITY"),
            "close": float(bar.close) if bar and bar.close else None,
            "volume": int(bar.volume) if bar and bar.volume else None,
            "ma_50": float(bar.ma_50) if bar and bar.ma_50 else None,
            "ma_150": float(bar.ma_150) if bar and bar.ma_150 else None,
            "ma_200": float(bar.ma_200) if bar and bar.ma_200 else None,
            "vol_ratio": float(bar.vol_ratio) if bar and bar.vol_ratio else None,
            "rs_rating": float(bar.rs_rating) if bar and bar.rs_rating else None,
            "pct_from_52w_high": float(bar.pct_from_52w_high) if bar and bar.pct_from_52w_high else None,
            "atr_14": float(bar.atr_14) if bar and bar.atr_14 else None,
            "bar_date": _fmt_date(bar.date) if bar else "",
            "setup_tier": _h_tier,
        })

    # Working capital currency
    try:
        _wcc_cfg = db.query(SystemConfig).filter(SystemConfig.key == "working_capital_currency", SystemConfig.organization_id == org_id).first()
        working_capital_currency = (_wcc_cfg.value if _wcc_cfg and _wcc_cfg.value else "AUD") or "AUD"
    except Exception:
        working_capital_currency = "AUD"

    # Build per-exchange regime list for the Market Regime stat card
    _exc_flag_label = {
        "ASX": ("🇦🇺", "ASX"), "NYSE": ("🇺🇸", "NYSE"), "NASDAQ": ("🇺🇸", "NASDAQ"),
        "CRYPTO_INDEPENDENTRESERVE": ("₿", "IR"), "CRYPTO_BINANCE": ("₿", "Binance"),
        "CRYPTO_COINBASE": ("₿", "Coinbase"), "CRYPTO_KRAKEN": ("₿", "Kraken"),
    }
    try:
        _ae_cfg = db.query(SystemConfig).filter(
            SystemConfig.key == "active_exchanges", SystemConfig.organization_id == org_id
        ).first()
        _ae_str = (_ae_cfg.value if _ae_cfg and _ae_cfg.value else "ASX") or "ASX"
        _active_excs_home = [e.strip() for e in _ae_str.split(",") if e.strip()]
    except Exception:
        _active_excs_home = ["ASX"]
    regimes_list = []
    for _exc in _active_excs_home:
        try:
            _rc = db.query(SystemConfig).filter(
                SystemConfig.key == f"last_market_regime_{_exc}",
                SystemConfig.organization_id == org_id,
            ).first()
            _val = (_rc.value if _rc and _rc.value else "—") or "—"
        except Exception:
            _val = "—"
        _flag, _label = _exc_flag_label.get(_exc, ("", _exc.replace("CRYPTO_", "")))
        regimes_list.append({"key": _exc, "flag": _flag, "label": _label, "val": _val})

    total_invested = round(sum(p.get("invested_aud", 0) for p in pos_data), 2)
    available_capital = round(capital - total_invested, 2)

    ctx.update({
        "capital": capital,
        "working_capital_currency": working_capital_currency,
        "total_invested": total_invested,
        "available_capital": available_capital,
        "positions": pos_data,
        "signals": sig_data,
        "regimes": regimes_list,
        "portfolio_heat": round(total_risk / capital * 100, 1) if capital else 0,
        "today_pnl": round(today_pnl, 2),
        "today_realised_pnl": round(today_realised_pnl, 2),
        "today_unrealised_pnl": round(today_unrealised_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "total_realised_pnl": round(total_realised_pnl, 2),
        "total_unrealised_pnl": round(total_unrealised_pnl, 2),
        "trade_count": len(all_trades),
        "checks": checks,
        "watchlist_rows": watchlist_rows,
        "wl_preview_limit": WL_HOME_PREVIEW_LIMIT,
        "wl_labels": wl_labels_data,
        "wl_label_counts": wl_label_counts,
        "wl_total_watching": wl_total_watching,
        "wl_active_label": active_label_id,
        "wl_only_custom": only_custom,
        "wl_exchange_filters": _get_exchange_filters(org_id, db),
        "wl_active_exchange": wl_active_exchange_filter,
    })
    return templates.TemplateResponse("trading/home.html", ctx)


@app.get("/positions", response_class=HTMLResponse)
async def positions(request: Request, db: Session = Depends(get_db),
                    exchange: str = Query("ALL")):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.models.trade import Position, Trade, TradeStatus, exit_reason_rationale
    from app.models.market import Stock
    from app.models.account import Account

    ctx = _global(request, db)

    stock_names = {s.ticker: (s.name or "") for s in db.query(Stock).all()}

    # Build flag emoji lookup from ExchangeConfig
    flag_map: dict[str, str] = {}
    try:
        from app.models.exchange import ExchangeConfig as _EC
        for ec in db.query(_EC).all():
            flag_map[ec.exchange_key] = ec.flag_emoji or ""
    except Exception:
        pass

    af = (exchange or "ALL").upper()
    pos_q = db.query(Position).filter(Position.status == TradeStatus.OPEN, Position.organization_id == org_id)
    try:
        if af == "ASX":
            pos_q = pos_q.filter(Position.exchange_key == "ASX", ~Position.ticker.like("%-%"))
        elif af == "CRYPTO":
            # Suffix-tolerant: rows created before the Jun 2026 exchange_key/asset_type
            # fix may carry EQUITY/ASX defaults — ticker format is authoritative.
            pos_q = pos_q.filter(or_(
                Position.asset_type == "CRYPTO",
                Position.ticker.like("%-AUD"),
                Position.ticker.like("%-USD"),
                Position.ticker.like("%-USDT"),
            ))
        elif af == "US":
            pos_q = pos_q.filter(Position.exchange_key.in_(["NYSE", "NASDAQ"]))
    except Exception:
        pass
    positions = pos_q.all()

    # Bulk PriceBar lookup for setup_tier (RS + vol_ratio)
    _pos_tickers = list({p.ticker for p in positions})
    _pos_bar_map: dict[str, dict] = {}
    _chart_bars_by_ticker: dict[str, list[dict]] = {}
    if _pos_tickers:
        from sqlalchemy import func as _pfunc
        from app.models.market import PriceBar as _PB
        _psub = (
            db.query(_PB.ticker, _pfunc.max(_PB.date).label("max_date"))
            .filter(_PB.ticker.in_(_pos_tickers))
            .group_by(_PB.ticker)
            .subquery()
        )
        for _pb in db.query(_PB).join(_psub, (_PB.ticker == _psub.c.ticker) & (_PB.date == _psub.c.max_date)).all():
            _pos_bar_map[_pb.ticker] = {
                "rs": float(_pb.rs_rating or 0),
                "vol_ratio": float(_pb.vol_ratio) if _pb.vol_ratio is not None else None,
            }

        # Bulk-fetch historical bars for the per-position VCP chart
        from datetime import timedelta
        _chart_cutoff = get_current_date() - timedelta(days=240)
        _hist_rows = (
            db.query(_PB)
            .filter(_PB.ticker.in_(_pos_tickers), _PB.date >= _chart_cutoff)
            .order_by(_PB.ticker, _PB.date.asc())
            .all()
        )
        for _hb in _hist_rows:
            _chart_bars_by_ticker.setdefault(_hb.ticker, []).append({
                "date":      str(_hb.date),
                "close":     float(_hb.close or 0),
                "high":      float(_hb.high or _hb.close or 0),
                "low":       float(_hb.low or _hb.close or 0),
                "volume":    float(_hb.volume or 0),
                "vol_ratio": float(_hb.vol_ratio) if _hb.vol_ratio is not None else None,
            })
        for _tkr in list(_chart_bars_by_ticker.keys()):
            _chart_bars_by_ticker[_tkr] = _chart_bars_by_ticker[_tkr][-150:]

    pos_data = []
    total_risk = 0.0
    for p in positions:
        entry = float(p.entry_price or 0)
        curr  = float(p.current_price or entry)
        # Overlay the live_price Redis cache when present — update_position_pnl_task
        # writes it for BOTH crypto and equity positions. Without this, a freshly
        # (re)imported position shows CURRENT == entry and P&L 0.0% until the DB
        # column catches up, even while alerts elsewhere quote the real price.
        try:
            _lp = cache.get(f"live_price:{p.ticker}")
            if _lp and not _lp.get("_failed") and _lp.get("price"):
                curr = float(_lp["price"])
        except Exception:
            pass
        stop  = float(p.current_stop or 0)
        qty   = float(p.qty or 0)
        if entry > 0 and stop > 0:
            total_risk += (entry - stop) * qty
        
        # Query last 3 exit checks for this position — filter by ticker + message pattern
        # (avoids dependency on entity_type/entity_id columns which may not be in DB)
        exit_checks = []
        try:
            from app.models.audit import AuditLog, AuditAction
            from sqlalchemy import or_
            log_entries = db.query(AuditLog).filter(
                AuditLog.organization_id == org_id,
                AuditLog.action == AuditAction.TASK_RUN,
                AuditLog.ticker == p.ticker,
                or_(
                    AuditLog.entity_id == str(p.id),
                    AuditLog.message.ilike("Exit check @ %"),
                ),
            ).order_by(desc(AuditLog.created_at)).limit(3).all()
            for log_entry in log_entries:
                d = log_entry.detail or {}
                msg = log_entry.message or ""
                # Derive result from detail JSON; fall back to message text
                result = d.get("result", "")
                if not result:
                    result = "exit_triggered" if "EXIT triggered" in msg else ("holding" if "holding" in msg else "")
                close_price = d.get("close")
                pnl_pct = d.get("pnl_pct")
                # Extract pnl from message if not in detail: "P&L +1.2%"
                if pnl_pct is None and "P&L " in msg:
                    try:
                        pnl_pct = float(msg.split("P&L ")[1].split("%")[0])
                    except Exception:
                        pass
                # Extract price from message if not in detail: "Price $1.234"
                if close_price is None and "Price $" in msg:
                    try:
                        close_price = float(msg.split("Price $")[1].split(" ")[0].split("|")[0].strip())
                    except Exception:
                        pass
                # Build reason string
                reason_str = ""
                if result == "exit_triggered" and "EXIT triggered — " in msg:
                    reason_str = msg.split("EXIT triggered — ")[1].split(" | ")[0]
                elif result == "holding":
                    reason_str = "holding — no exit criteria met"
                elif result == "skipped":
                    reason_str = d.get("reason", "skipped")
                elif result == "error":
                    reason_str = d.get("error", "check error")
                exit_checks.append({
                    "id": log_entry.id,
                    "time": _fmt_dt(str(log_entry.created_at), ctx.get("display_tz", "UTC")),
                    "message": msg,
                    "result": result,
                    "close": close_price,
                    "pnl_pct": pnl_pct,
                    "hold_days": d.get("hold_days"),
                    "reason": reason_str,
                })
        except Exception:
            pass

        ek = getattr(p, "exchange_key", "ASX") or "ASX"
        at = getattr(p, "asset_type",   "EQUITY") or "EQUITY"
        _p_bar = _pos_bar_map.get(p.ticker, {})
        _p_rs  = _p_bar.get("rs", 0)
        _p_vol = _p_bar.get("vol_ratio")
        _p_tier = (
            "A" if _p_rs >= 80 and (_p_vol is None or _p_vol <= 0.6) else
            "B" if _p_rs >= 70 else
            "C"
        )
        _vcp_json = None
        _bars = _chart_bars_by_ticker.get(p.ticker, [])
        if _bars:
            _swing_highs = []
            _swing_lows = []
            _contractions = []
            try:
                import pandas as _pd
                import numpy as _np
                import json
                from app.screener.vcp import detect_vcp, _find_pivots
                from app.screener.rules import RuleEngine
                from app.models.account import Organization as _Org

                _df = _pd.DataFrame(_bars)
                _df["date"] = _pd.to_datetime(_df["date"])
                
                _org = db.query(_Org).get(org_id) if org_id else None
                _tier = _org.tier.value if (_org and _org.tier) else "BRONZE"
                _engine = RuleEngine(organization_id=org_id, tier=_tier, asset_type=at)
                
                _vcp_res, _ = detect_vcp(p.ticker, _df, _engine)
                _contractions = (_vcp_res.detail or {}).get("contractions", [])
                
                _highs = _df["high"].values
                _lows = _df["low"].values
                _win = 3 if len(_bars) < 60 else 5
                for _idx in _find_pivots(_np.array(_highs), direction="high", window=_win):
                    _swing_highs.append(_bars[_idx]["date"])
                for _idx in _find_pivots(_np.array(_lows), direction="low", window=_win):
                    _swing_lows.append(_bars[_idx]["date"])
                
                _vcp_chart_data = {
                    "ticker": p.ticker,
                    "series": _bars,
                    "pivot": float(p.entry_price or 0) or None,
                    "stop": float(p.current_stop or 0) or None,
                    "target": float(p.target_1 or 0) or None,
                    "contractions": _contractions,
                    "swing_highs": _swing_highs,
                    "swing_lows": _swing_lows,
                }
                _vcp_json = json.dumps(_vcp_chart_data)
            except Exception as _ex:
                logger.error(f"Failed to calculate interactive VCP chart details for position {p.ticker}: {_ex}")

        pos_data.append({
            "id": p.id, "ticker": p.ticker,
            "exchange_key":  ek,
            "asset_type":    at,
            "currency":      getattr(p, "currency", "AUD") or "AUD",
            "flag_emoji":    flag_map.get(ek, ""),
            "company_name": stock_names.get(p.ticker, ""),
            "qty": qty,
            "entry": entry, "current": curr,
            "stop": stop,
            "target_1": float(p.target_1 or 0),
            "invested_aud": round(entry * qty, 2),
            "market_value_aud": round(curr * qty, 2),
            "pnl_pct": round((curr - entry) / entry * 100, 2) if entry else 0,
            "pnl_aud": round((curr - entry) * qty, 2),
            "days": (get_current_date() - p.entry_date).days if p.entry_date else 0,
            "entry_date": _fmt_date(p.entry_date),
            "is_paper": p.is_paper,
            "exit_checks": exit_checks,
            "setup_tier": _p_tier,
            "vcp_chart_json": _vcp_json,
        })

    # Closed trades — also filter by exchange if selected
    trade_q = db.query(Trade).filter(Trade.organization_id == org_id).order_by(desc(Trade.exit_date))
    try:
        if af == "ASX":
            trade_q = trade_q.filter(Trade.exchange_key == "ASX", ~Trade.ticker.like("%-%"))
        elif af == "CRYPTO":
            # Suffix-tolerant (see positions filter above)
            trade_q = trade_q.filter(or_(
                Trade.asset_type == "CRYPTO",
                Trade.ticker.like("%-AUD"),
                Trade.ticker.like("%-USD"),
                Trade.ticker.like("%-USDT"),
            ))
        elif af == "US":
            trade_q = trade_q.filter(Trade.exchange_key.in_(["NYSE", "NASDAQ"]))
    except Exception:
        pass
    trades = trade_q.limit(50).all()
    trade_data = [{
        "ticker": t.ticker,
        "exchange_key":  getattr(t, "exchange_key", "ASX") or "ASX",
        "asset_type":    getattr(t, "asset_type", "EQUITY") or "EQUITY",
        "currency":      getattr(t, "currency", "AUD") or "AUD",
        "flag_emoji":    flag_map.get(getattr(t, "exchange_key", "ASX") or "ASX", ""),
        "company_name": stock_names.get(t.ticker, ""),
        "entry_date": _fmt_date(t.entry_date) if t.entry_date else "", "exit_date": _fmt_date(t.exit_date) if t.exit_date else "",
        "days": t.hold_days or 0,
        "entry": float(t.entry_price or 0), "exit": float(t.exit_price or 0),
        "pnl_pct": round(float(t.pnl_pct or 0) * 100, 2),
        "pnl_aud": round(float(t.net_pnl_aud or 0), 2),
        "reason": str(t.exit_reason).replace("ExitReason.", "").replace("_", " "),
        "rationale_summary": exit_reason_rationale(t.exit_reason)["summary"],
        "rationale_detail": exit_reason_rationale(t.exit_reason)["detail"],
        "cgt": t.cgt_eligible_discount,
        "is_paper": t.is_paper,
    } for t in trades]

    account = db.query(Account).filter(Account.is_active == True, Account.organization_id == org_id).first()
    capital = float(account.capital_aud) if account else 5000.0
    portfolio_heat = round(total_risk / capital * 100, 1) if capital else 0.0

    wins = [t for t in trades if float(t.net_pnl_aud or 0) > 0]
    losses = [t for t in trades if float(t.net_pnl_aud or 0) <= 0]
    avg_win = sum(float(t.net_pnl_aud or 0) for t in wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(float(t.net_pnl_aud or 0) for t in losses)) / len(losses) if losses else 0.0
    win_loss_ratio = round(avg_win / avg_loss, 2) if avg_loss else 0.0
    avg_hold_time = round(sum(t.hold_days for t in trades if t.hold_days is not None) / len(trades), 1) if trades else 0.0

    # Calculate total realised, unrealised, and total P&L across all matching records (not just limit 50)
    all_trade_q = db.query(Trade).filter(Trade.organization_id == org_id)
    try:
        if af == "ASX":
            all_trade_q = all_trade_q.filter(Trade.exchange_key == "ASX", ~Trade.ticker.like("%-%"))
        elif af == "CRYPTO":
            # Suffix-tolerant (see positions filter above)
            all_trade_q = all_trade_q.filter(or_(
                Trade.asset_type == "CRYPTO",
                Trade.ticker.like("%-AUD"),
                Trade.ticker.like("%-USD"),
                Trade.ticker.like("%-USDT"),
            ))
        elif af == "US":
            all_trade_q = all_trade_q.filter(Trade.exchange_key.in_(["NYSE", "NASDAQ"]))
    except Exception:
        pass
    all_trades_for_pnl = all_trade_q.all()

    total_realised_pnl = sum(float(t.net_pnl_aud or 0) for t in all_trades_for_pnl)
    total_unrealised_pnl = sum(float(p.unrealised_pnl or 0) for p in positions)
    total_pnl = total_realised_pnl + total_unrealised_pnl

    ef = _get_exchange_filters(org_id, db)
    ctx.update({
        "positions": pos_data, "trades": trade_data,
        "win_rate": round(len(wins) / len(trades) * 100) if trades else 0,
        "total_realised_pnl": round(total_realised_pnl, 2),
        "total_unrealised_pnl": round(total_unrealised_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "trade_count": len(all_trades_for_pnl),
        "portfolio_heat": portfolio_heat,
        "win_loss_ratio": win_loss_ratio,
        "avg_hold_time": avg_hold_time,
        "capital": capital,
        "exchange_filters":       ef,
        "active_exchange_filter": af,
        "base_url":               "/positions",
        "extra_params":           "",
    })
    return templates.TemplateResponse("trading/positions.html", ctx)


@app.get("/positions/open-orders")
async def positions_open_orders(request: Request):
    """Live IBKR working/open orders for the org's account, as JSON.

    Polled by the Open Orders panel on the Positions page. The IBKR call is
    synchronous ib_insync, which can't run inside the API's event loop, so we
    run it in a worker thread (it gets its own loop via IBKRBroker.connect()).
    """
    import asyncio
    from fastapi.responses import JSONResponse
    if not _auth(request):
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    org_id = request.session.get("organization_id")

    def _fetch():
        # asyncio.to_thread() worker threads have no event loop on Python 3.10+.
        # ib_insync needs one for the API handshake, so we install a fresh loop
        # before anything else touches asyncio (including ib_insync imports).
        import asyncio as _asyncio
        try:
            _asyncio.get_running_loop()
        except RuntimeError:
            _asyncio.set_event_loop(_asyncio.new_event_loop())

        try:
            from app.broker.ibkr import IBKRBroker
            with IBKRBroker(organization_id=org_id) as b:
                if not b.is_connected:
                    return {"connected": False, "account": b.account, "orders": []}
                orders = b.get_open_orders()
                acct = (b.account or "").strip()
                # Scope to this org's account when the gateway exposes multiple.
                if acct:
                    orders = [o for o in orders if not o.get("account") or o.get("account") == acct]

                # Enrich with DB linkage. IBKR doesn't expose placement time on
                # open orders, but our own Order rows carry submitted_at, and the
                # orderRef ("astratrade-{signal_id}" / "stopsell-{position_id}")
                # ties bracket legs back to their Signal — without this, working
                # entry brackets for TRIGGERED signals look like mystery orders,
                # since TRIGGERED signals are hidden from the Signals page and no
                # Position exists until the entry leg fills (CLAUDE.md #36/#39).
                try:
                    from app.database import get_db as _appdb
                    from app.models.trade import Order as _Order
                    from app.models.signal import Signal as _Signal
                    from sqlalchemy import or_ as _or
                    import pytz as _pytz
                    _tz = _pytz.timezone("Australia/Sydney")

                    perm_ids = [o["perm_id"] for o in orders if o.get("perm_id")]
                    oids     = [o["ibkr_order_id"] for o in orders if o.get("ibkr_order_id")]
                    sig_ids: set = set()
                    for o in orders:
                        ref = o.get("order_ref") or ""
                        if ref.startswith("astratrade-"):
                            try:
                                sig_ids.add(int(ref.split("-", 1)[1]))
                            except ValueError:
                                pass

                    by_perm, by_oid, by_sig, sigs = {}, {}, {}, {}
                    conds = []
                    if perm_ids: conds.append(_Order.perm_id.in_(perm_ids))
                    if oids:     conds.append(_Order.ibkr_order_id.in_(oids))
                    if sig_ids:  conds.append(_Order.signal_id.in_(sig_ids))
                    if conds:
                        from datetime import datetime as _dtm, timedelta as _tdl
                        # IBKR orderIds are session/client-scoped and RECYCLE across
                        # gateway restarts — matching an old DB row by orderId alone
                        # produced nonsense like "placed 30 Jun" on a DAY order.
                        # permId is globally unique and reconnect-stable, so those
                        # matches are always trusted; orderId/signal matches only
                        # count when the DB row is recent (all entries are DAY TIF).
                        _recent_cutoff = _dtm.utcnow() - _tdl(days=5)
                        with _appdb() as _db:
                            db_orders = _db.query(_Order).filter(
                                _Order.organization_id == org_id, _or(*conds)
                            ).order_by(_Order.id).all()
                            by_perm = {d.perm_id: d for d in db_orders if d.perm_id}
                            _recent = [d for d in db_orders
                                       if (d.created_at or _dtm.utcnow()) >= _recent_cutoff]
                            by_oid  = {d.ibkr_order_id: d for d in _recent if d.ibkr_order_id}
                            by_sig  = {d.signal_id: d for d in _recent if d.signal_id}
                            if sig_ids:
                                sigs = {s.id: s for s in _db.query(_Signal).filter(
                                    _Signal.id.in_(sig_ids)).all()}

                    def _fmt(ts):
                        if ts is None:
                            return None
                        try:
                            return _pytz.utc.localize(ts).astimezone(_tz).strftime("%d %b %H:%M")
                        except Exception:
                            return str(ts)[:16]

                    for o in orders:
                        ref = o.get("order_ref") or ""
                        sid = None
                        if ref.startswith("astratrade-"):
                            try:
                                sid = int(ref.split("-", 1)[1])
                            except ValueError:
                                pass
                        # Bracket CHILD legs have no DB Order row of their own —
                        # fall back to the parent order (matched via signal_id)
                        # for the placement timestamp.
                        dbo = (by_perm.get(o.get("perm_id"))
                               or by_oid.get(o.get("ibkr_order_id"))
                               or (by_sig.get(sid) if sid else None))
                        o["placed_at"] = _fmt((dbo.submitted_at or dbo.created_at) if dbo else None)
                        if sid:
                            sig = sigs.get(sid)
                            st = str(sig.status).replace("SignalStatus.", "") if sig else "?"
                            o["source"] = f"Signal #{sid} · {st}"
                        elif ref.startswith("stopsell-"):
                            o["source"] = f"Stop-sell · position #{ref.split('-', 1)[1]}"
                        elif dbo:
                            o["source"] = "AstraTrade"
                        else:
                            o["source"] = "External/manual"
                except Exception as _enrich_e:
                    logger.debug(f"open-orders DB enrichment failed: {_enrich_e}")

                return {"connected": True, "account": acct, "orders": orders}
        except Exception as e:
            return {"connected": False, "error": str(e)[:200], "orders": []}

    try:
        data = await asyncio.to_thread(_fetch)
    except Exception as e:
        from loguru import logger
        logger.debug(f"open-orders fetch failed: {e}")
        data = {"connected": False, "error": str(e)[:200], "orders": []}
    return JSONResponse(data)


@app.post("/positions/open-orders/{ibkr_order_id}/cancel")
async def cancel_open_order(ibkr_order_id: int, request: Request, db: Session = Depends(get_db)):
    """Cancel an active order on IBKR and sync DB if it was an entry order."""
    import asyncio
    from fastapi.responses import JSONResponse
    if not _auth(request):
        return JSONResponse({"error": "unauthenticated"}, status_code=401)

    org_id = request.session.get("organization_id")

    def _do_cancel():
        # Setup event loop for ib_insync worker thread
        import asyncio as _asyncio
        try:
            _asyncio.get_running_loop()
        except RuntimeError:
            _asyncio.set_event_loop(_asyncio.new_event_loop())

        from app.broker.ibkr import IBKRBroker
        with IBKRBroker(organization_id=org_id) as b:
            if not b.is_connected:
                return False, "Not connected to IBKR"
            # cancel_order now returns (bool, str)
            return b.cancel_order(ibkr_order_id)
            
    try:
        success, error = await asyncio.to_thread(_do_cancel)
        if not success:
            return JSONResponse({"error": error or f"Order {ibkr_order_id} not found in IBKR open orders — it may have already filled or been cancelled at the broker."}, status_code=400)
            
        # Sync DB: If this was the parent entry order, wipe the Phantom Position
        from app.models.trade import Order, OrderStatus, Position
        from app.models.signal import Signal, SignalStatus
        order = db.query(Order).filter(Order.ibkr_order_id == ibkr_order_id, Order.organization_id == org_id).first()
        if order and order.status == OrderStatus.SUBMITTED:
            order.status = OrderStatus.CANCELLED
            
            # Find and delete the linked phantom position
            pos = db.query(Position).filter(
                Position.ticker == order.ticker,
                Position.organization_id == org_id,
                Position.status == "OPEN"
            ).first()
            if pos:
                db.delete(pos)
                
            # Revert the signal back to PENDING
            sig = db.query(Signal).filter(
                Signal.ticker == order.ticker,
                Signal.organization_id == org_id,
                Signal.status == SignalStatus.TRIGGERED
            ).order_by(Signal.id.desc()).first()
            if sig:
                sig.status = SignalStatus.PENDING
                
            db.commit()
            
        return JSONResponse({"status": "success"})
    except Exception as e:
        from loguru import logger
        logger.error(f"Failed to cancel order {ibkr_order_id}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/positions/open-orders/cancel-all")
async def cancel_all_open_orders(request: Request):
    """Cancel ALL open orders on the account using IBKR reqGlobalCancel().

    This is the only reliable way to cancel orders placed by any client session
    (bracket children, orders from disconnected workers, TWS orders).
    Individual cancel (cancel_order) only works for orders placed by the current
    client ID session.
    """
    import asyncio
    from fastapi.responses import JSONResponse
    if not _auth(request):
        return JSONResponse({"error": "unauthenticated"}, status_code=401)

    org_id = request.session.get("organization_id")

    def _do_cancel_all():
        import asyncio as _asyncio
        try:
            _asyncio.get_running_loop()
        except RuntimeError:
            _asyncio.set_event_loop(_asyncio.new_event_loop())
        from app.broker.ibkr import IBKRBroker
        with IBKRBroker(organization_id=org_id) as b:
            if not b.is_connected:
                return False, "Not connected to IBKR"
            return b.cancel_all_orders()

    try:
        success, message = await asyncio.to_thread(_do_cancel_all)
        if not success:
            return JSONResponse({"error": message}, status_code=400)
        return JSONResponse({"status": "success", "message": message})
    except Exception as e:
        from loguru import logger
        logger.error(f"cancel_all_open_orders failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


def _enrich_rule_results(ticker: str, rule_results_dict: dict, db_session, target_date=None, overrides=None, _bar_data: dict | None = None) -> list[dict]:
    """
    Enrich rule results with actual values from the price bar on the given date (or latest).

    Pass `_bar_data` (a plain dict with close/ma_50/ma_150/ma_200/high_52w/low_52w/rs_rating)
    to skip all DB and Redis lookups — the caller already has the data.
    """
    from app.models.market import PriceBar

    class _DictBar:
        """Lightweight wrapper so the enrichment code can use attribute access."""
        __slots__ = ("close","ma_50","ma_150","ma_200","high_52w","low_52w","rs_rating")
        def __init__(self, data: dict):
            for k in self.__slots__:
                setattr(self, k, data.get(k, 0) or 0)

    bar = None

    # Fast path: caller supplied bar data directly — no DB or cache hit needed.
    if _bar_data:
        bar = _DictBar(_bar_data)
    elif target_date:
        # Specific-date query (signals page, signal_date) — check cache first.
        cache_key = f"price_bar:{ticker}:{target_date}"
        cached = cache.get(cache_key)
        if cached:
            bar = _DictBar(cached)
        else:
            bar_obj = db_session.query(PriceBar).filter(
                PriceBar.ticker == ticker, PriceBar.date == target_date
            ).first()
            if bar_obj:
                d = {
                    "close": float(bar_obj.close or 0), "ma_50": float(bar_obj.ma_50 or 0),
                    "ma_150": float(bar_obj.ma_150 or 0), "ma_200": float(bar_obj.ma_200 or 0),
                    "high_52w": float(bar_obj.high_52w or 0), "low_52w": float(bar_obj.low_52w or 0),
                    "rs_rating": float(bar_obj.rs_rating or 0),
                }
                cache.set(cache_key, d, expire_seconds=86400)  # signal bars are immutable
                bar = _DictBar(d)
    else:
        cache_key = f"latest_price_bar:{ticker}"
        cached = cache.get(cache_key)
        if cached:
            bar = _DictBar(cached)
        else:
            bar_obj = db_session.query(PriceBar).filter(PriceBar.ticker == ticker).order_by(desc(PriceBar.date)).first()
            if bar_obj:
                d = {
                    "close": float(bar_obj.close or 0), "ma_50": float(bar_obj.ma_50 or 0),
                    "ma_150": float(bar_obj.ma_150 or 0), "ma_200": float(bar_obj.ma_200 or 0),
                    "high_52w": float(bar_obj.high_52w or 0), "low_52w": float(bar_obj.low_52w or 0),
                    "rs_rating": float(bar_obj.rs_rating or 0),
                }
                cache.set(cache_key, d, expire_seconds=300)
                bar = _DictBar(d)
            else:
                cache.set(cache_key, {}, expire_seconds=3600)
    
    enriched = []
    for rid, robj in rule_results_dict.items():
        passed = robj.get("passed", False) if isinstance(robj, dict) else bool(robj)
        val_str = ""
        
        # Determine labels
        clean_label = rid.replace("trend_", "").replace("fundamental_", "").replace("vcp_", "").replace("crypto_", "").replace("_", " ")
        
        if bar:
            close = float(bar.close or 0)
            ma50 = float(bar.ma_50 or 0)
            ma150 = float(bar.ma_150 or 0)
            ma200 = float(bar.ma_200 or 0)
            high_52w = float(bar.high_52w or 0)
            low_52w = float(bar.low_52w or 0)
            rs = float(bar.rs_rating or 0)
            
            if rid == "trend_price_above_200ma":
                val_str = f"${close:.2f} > ${ma200:.2f}" if passed else f"${close:.2f} ≤ ${ma200:.2f}"
            elif rid == "trend_price_above_150ma":
                val_str = f"${close:.2f} > ${ma150:.2f}" if passed else f"${close:.2f} ≤ ${ma150:.2f}"
            elif rid == "trend_ma150_above_ma200":
                val_str = f"${ma150:.2f} > ${ma200:.2f}" if passed else f"${ma150:.2f} ≤ ${ma200:.2f}"
            elif rid == "trend_ma200_trending_up":
                val_str = f"200MA ${ma200:.2f}"
            elif rid == "trend_ma50_above_ma150_200":
                val_str = f"50MA ${ma50:.2f} > 150/200MA"
            elif rid == "trend_price_above_ma50":
                val_str = f"${close:.2f} > ${ma50:.2f}" if passed else f"${close:.2f} ≤ ${ma50:.2f}"
            elif rid == "trend_pct_above_52w_low":
                pct = ((close - low_52w) / low_52w * 100) if low_52w > 0 else 0
                val_str = f"+{pct:.1f}% from low (${low_52w:.2f})"
            elif rid == "trend_pct_below_52w_high":
                pct = ((high_52w - close) / high_52w * 100) if high_52w > 0 else 0
                val_str = f"-{pct:.1f}% from high (${high_52w:.2f})"
            elif rid == "trend_rs_rating_min":
                val_str = f"RS: {rs:.0f}"
            
        # Fallback to saved message/value if no bar-derived string (e.g. crypto rules)
        if not val_str and isinstance(robj, dict):
            if robj.get("message"):
                val_str = str(robj["message"])
            elif robj.get("value") is not None:
                val_str = str(robj["value"])
            
        enriched.append({
            "rule_id": rid,
            "label": clean_label,
            "passed": passed,
            "overridden": overrides.get(rid) == False if overrides else False,
            "detail": val_str,
        })
    return enriched


def _build_vcp_chart_svg(ticker: str, bars: list[dict], pivot: float, stop: float, target1: float) -> str | None:
    """
    Build a real-data price/volume SVG chart for a signal card, visually styled like the
    FAQ Q1 illustrative VCP diagram (cyan price line, gray/green volume bars, dashed
    coloured reference lines) but driven entirely by actual PriceBar history for this ticker.

    bars: ascending-by-date list of dicts with close/high/low/volume/vol_ratio.
    Returns raw SVG markup (caller must render with `| safe`), or None if too little history.
    """
    if not bars or len(bars) < 15:
        return None

    n = len(bars)
    closes = [b["close"] for b in bars]
    highs  = [b.get("high") or b["close"] for b in bars]
    lows   = [b.get("low") or b["close"] for b in bars]
    vols   = [b.get("volume") or 0 for b in bars]
    vol_ratios = [b.get("vol_ratio") for b in bars]

    # ── Layout (mirrors the FAQ .chart-wrap convention: viewBox 0 0 700 220) ──
    W, H = 700, 220
    margin_l, margin_r = 36, 92
    chart_top, price_h = 14, 128
    vol_top = chart_top + price_h + 10
    vol_h = 40
    baseline_y = vol_top + vol_h
    plot_w = W - margin_l - margin_r

    all_prices = highs + lows + [p for p in (pivot, stop, target1) if p and p > 0]
    pmax = max(all_prices) if all_prices else 1.0
    pmin = min(all_prices) if all_prices else 0.0
    if pmax <= pmin:
        pmax = pmin + 1.0
    pad = (pmax - pmin) * 0.08
    pmax += pad
    pmin -= pad

    def x_of(i: int) -> float:
        return margin_l + (i / (n - 1)) * plot_w if n > 1 else margin_l

    def y_of(price: float) -> float:
        return chart_top + (pmax - price) / (pmax - pmin) * price_h

    price_pts = " ".join(f"{x_of(i):.1f},{y_of(closes[i]):.1f}" for i in range(n))

    # ── Volume bars — green when that day's vol_ratio hit breakout threshold ──
    vmax = max(vols) if vols and max(vols) > 0 else 1.0
    bar_w = max(plot_w / n * 0.7, 1.0)
    vol_bars = []
    for i in range(n):
        vh = (vols[i] / vmax) * vol_h
        vr = vol_ratios[i]
        color = "#4ade80" if (vr is not None and vr >= 150) else "#94a3b8"
        x = x_of(i) - bar_w / 2
        y = baseline_y - vh
        vol_bars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{max(vh,0):.1f}" fill="{color}" opacity=".8"/>')

    # ── Swing high/low markers — real contraction points via the same pivot
    #    detector the screener uses (app.screener.vcp._find_pivots) ──
    swing_markers = []
    try:
        import numpy as _np
        from app.screener.vcp import _find_pivots
        win = 3 if n < 60 else 5
        for i in _find_pivots(_np.array(highs), direction="high", window=win):
            swing_markers.append(f'<circle cx="{x_of(i):.1f}" cy="{y_of(highs[i]):.1f}" r="2.5" fill="#f59e0b"/>')
        for i in _find_pivots(_np.array(lows), direction="low", window=win):
            swing_markers.append(f'<circle cx="{x_of(i):.1f}" cy="{y_of(lows[i]):.1f}" r="2.5" fill="#f59e0b" opacity=".7"/>')
    except Exception:
        pass

    # ── Reference lines: pivot (orange) / stop (red) / target 1 (green) ──
    ref_lines = []
    def _ref(price: float, color: str, label: str):
        if not price or price <= 0:
            return
        y = y_of(price)
        ref_lines.append(
            f'<line x1="{margin_l}" y1="{y:.1f}" x2="{W - margin_r + 4:.1f}" y2="{y:.1f}" '
            f'stroke="{color}" stroke-width="1.1" stroke-dasharray="5,3"/>'
            f'<text x="{W - margin_r + 8:.1f}" y="{y + 3:.1f}" font-size="9.5" fill="{color}" font-weight="bold">{label} ${price:.3f}</text>'
        )
    _ref(pivot, "#f97316", "Pivot")
    _ref(stop, "#ef4444", "Stop")
    _ref(target1, "#4ade80", "T1")

    return (
        f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
        f'style="width:100%;max-width:{W}px;display:block;margin:0 auto">'
        f'<line x1="{margin_l}" y1="{baseline_y}" x2="{W - margin_r + 4}" y2="{baseline_y}" '
        f'stroke="#475569" stroke-width="1" stroke-dasharray="4,3"/>'
        f'<g>{"".join(vol_bars)}</g>'
        f'{"".join(ref_lines)}'
        f'<polyline points="{price_pts}" stroke="#22d3ee" stroke-width="2" fill="none"/>'
        f'<g>{"".join(swing_markers)}</g>'
        f'<circle cx="{x_of(n-1):.1f}" cy="{y_of(closes[-1]):.1f}" r="3" fill="#22d3ee"/>'
        f'<text x="{margin_l}" y="{chart_top - 4}" font-size="9.5" fill="#94a3b8">{ticker} · {n}d</text>'
        f'</svg>'
    )


# Display metadata for the Stock Story rule scorecard — category order + labels.
_SS_RULE_CATEGORY_ORDER = [
    ("TREND_TEMPLATE", "Trend Template"),
    ("VCP",            "Volatility Contraction (VCP)"),
    ("FUNDAMENTAL",    "Fundamentals"),
    ("CRYPTO",         "Crypto"),
    ("MARKET_REGIME",  "Market Regime"),
    ("ENTRY",          "Entry"),
    ("EARNINGS",       "Earnings"),
]


def _build_vcp_analysis(ticker: str, exchange_key: str, asset_type: str,
                        org_id: int, db) -> dict:
    """
    Recompute the Volatility Contraction Pattern for `ticker` from locally-stored
    price_bars (NO network) using the org's RuleEngine, so thresholds/enabled
    rules match exactly what the screener uses for this organisation.

    Returns the detected contraction legs (with dates + depths), pivot/stop,
    base length, volume dry-up, the price series for the chart, and the VCP
    rule pass/fail — everything the modal needs to draw + explain the pattern.
    Degrades to {"available": False, "reason": ...} when there isn't enough
    history to run the detector.
    """
    from app.models.market import PriceBar
    from app.models.account import Organization
    from app.screener.rules import RuleEngine
    from app.screener.vcp import detect_vcp
    import pandas as _pd

    rows = (db.query(PriceBar.date, PriceBar.open, PriceBar.high, PriceBar.low,
                     PriceBar.close, PriceBar.volume, PriceBar.vol_ratio,
                     PriceBar.avg_vol_50)
            .filter(PriceBar.ticker == ticker)
            .order_by(PriceBar.date.asc())
            .limit(450).all())
    if len(rows) < 15:
        return {"available": False,
                "reason": f"Only {len(rows)} days of price history stored — the VCP "
                          f"detector needs at least 15. It will populate as daily data accrues."}

    df = _pd.DataFrame([{
        "date": r[0], "open": float(r[1]) if r[1] is not None else None,
        "high": float(r[2]) if r[2] is not None else None,
        "low":  float(r[3]) if r[3] is not None else None,
        "close": float(r[4]) if r[4] is not None else None,
        "volume": float(r[5]) if r[5] is not None else 0.0,
        "vol_ratio": float(r[6]) if r[6] is not None else None,
    } for r in rows])
    avg_vol_50 = None
    if rows[-1][7] is not None:
        try:
            avg_vol_50 = float(rows[-1][7])
        except Exception:
            avg_vol_50 = None

    org = db.query(Organization).get(org_id) if org_id else None
    tier = org.tier.value if (org and org.tier) else "BRONZE"
    engine = RuleEngine(organization_id=org_id, tier=tier,
                        asset_type=(asset_type or "EQUITY"))

    vcp, vcp_rules = detect_vcp(ticker, df, engine, avg_vol_50)

    # Chart series — last ~252 sessions is plenty to frame the base.
    series = [{
        "date": str(r[0]),
        "close": float(r[4]) if r[4] is not None else None,
        "high": float(r[2]) if r[2] is not None else None,
        "low": float(r[3]) if r[3] is not None else None,
        "volume": float(r[5]) if r[5] is not None else 0.0,
        "vol_ratio": float(r[6]) if r[6] is not None else None,
    } for r in rows[-252:] if r[4] is not None]

    swing_highs = []
    swing_lows = []
    try:
        import numpy as _np
        from app.screener.vcp import _find_pivots
        sliced_rows = rows[-252:]
        highs = _np.array([float(r[2]) if r[2] is not None else float(r[4]) for r in sliced_rows])
        lows = _np.array([float(r[3]) if r[3] is not None else float(r[4]) for r in sliced_rows])
        win = 3 if len(sliced_rows) < 60 else 5
        for idx in _find_pivots(highs, direction="high", window=win):
            swing_highs.append(str(sliced_rows[idx][0]))
        for idx in _find_pivots(lows, direction="low", window=win):
            swing_lows.append(str(sliced_rows[idx][0]))
    except Exception as _ex:
        logger.warning(f"Failed to find swing points in _build_vcp_analysis: {_ex}")

    rules = []
    for rid, rr in vcp_rules.items():
        rules.append({
            "rule_id": rid,
            "passed": bool(getattr(rr, "passed", False)),
            "message": getattr(rr, "message", None),
        })

    return {
        "available": True,
        "detected": bool(vcp.detected),
        "pivot": vcp.pivot_price,
        "stop": vcp.stop_price,
        "target1": vcp.pivot_price * 1.20 if vcp.pivot_price else None,
        "contraction_count": vcp.contraction_count,
        "base_weeks": vcp.base_weeks,
        "volume_dried_up": bool(vcp.volume_dried_up),
        "final_contraction_pct": vcp.final_contraction_pct,
        "contractions": (vcp.detail or {}).get("contractions", []),
        "series": series,
        "rules": rules,
        "min_contractions": int(engine.threshold("vcp_min_contractions") or 3),
        "swing_highs": swing_highs,
        "swing_lows": swing_lows,
    }


def _build_rule_breakdown(ticker: str, org_id: int, db) -> dict:
    """
    Build the Minervini rule scorecard for `ticker` from the rule_results the
    screener already stored on the org's Watchlist (or Signal) row — i.e. the
    exact pass/fail decided under THIS organisation's rule config — joined with
    RuleConfig metadata (label, plain-English description, Minervini reference,
    threshold) so each tick can be explained, grouped by category.
    """
    from app.models.signal import Watchlist, Signal
    from app.models.config import RuleConfig

    # Prefer the watchlist row's results; fall back to the latest signal's.
    src = "watchlist"
    rr = {}
    wl = (db.query(Watchlist)
          .filter(Watchlist.organization_id == org_id, Watchlist.ticker == ticker)
          .first())
    if wl and wl.rule_results:
        rr = wl.rule_results
    else:
        sig = (db.query(Signal)
               .filter(Signal.organization_id == org_id, Signal.ticker == ticker)
               .order_by(Signal.id.desc()).first())
        if sig and sig.rule_results:
            rr = sig.rule_results
            src = "signal"

    if not rr:
        return {"available": False,
                "reason": "No screener evaluation stored yet for this ticker under your "
                          "organisation. It populates after the stock is screened."}

    # RuleConfig metadata: org-specific first, global as fallback per rule_id.
    meta = {}
    for r in db.query(RuleConfig).filter(RuleConfig.organization_id == org_id).all():
        meta[r.rule_id] = r
    for r in db.query(RuleConfig).filter(RuleConfig.organization_id == None).all():
        meta.setdefault(r.rule_id, r)

    # Reuse existing enrichment for the human-readable actual-vs-threshold detail.
    try:
        enriched = _enrich_rule_results(ticker, rr, db)
        detail_map = {e["rule_id"]: e.get("detail") for e in enriched}
    except Exception:
        detail_map = {}

    groups_map: dict = {}
    passed_total = 0
    rule_total = 0
    for rid, robj in rr.items():
        passed = robj.get("passed", False) if isinstance(robj, dict) else bool(robj)
        rule_total += 1
        if passed:
            passed_total += 1
        m = meta.get(rid)
        cat = m.category.value if (m and m.category) else "OTHER"
        label = m.label if m else rid.replace("_", " ").title()
        desc = (m.description if m else None) or None
        ref = (m.minervini_ref if m else None) or None
        thr = None
        if m is not None and m.threshold is not None:
            tl = m.threshold_label or "Threshold"
            thr = f"{tl}: {float(m.threshold):g}"
        detail = detail_map.get(rid)
        if not detail and isinstance(robj, dict):
            detail = robj.get("message") or (str(robj.get("value")) if robj.get("value") is not None else None)
        groups_map.setdefault(cat, []).append({
            "rule_id": rid, "label": label, "passed": passed,
            "detail": detail, "description": desc, "minervini_ref": ref,
            "threshold": thr,
        })

    # Order groups by the curated category order, appending any extras.
    ordered = []
    seen = set()
    for key, disp in _SS_RULE_CATEGORY_ORDER:
        if key in groups_map:
            items = groups_map[key]
            ordered.append({
                "category": key, "label": disp, "items": items,
                "passed": sum(1 for i in items if i["passed"]), "total": len(items),
            })
            seen.add(key)
    for key, items in groups_map.items():
        if key in seen:
            continue
        ordered.append({
            "category": key, "label": key.replace("_", " ").title(), "items": items,
            "passed": sum(1 for i in items if i["passed"]), "total": len(items),
        })

    return {"available": True, "source": src, "groups": ordered,
            "passed": passed_total, "total": rule_total}


@app.get("/signals", response_class=HTMLResponse)
async def signals(request: Request, db: Session = Depends(get_db),
                  exchange: str = Query("ALL"), tier: str = Query(None)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")
    ctx = _global(request, db)

    from app.models.signal import Signal, SignalStatus
    from app.models.audit import AuditLog, AuditAction
    from app.models.config import SystemConfig
    from sqlalchemy import or_
    import re

    af = (exchange or "ALL").upper()

    ef = _get_exchange_filters(org_id, db)

    # Resolve effective exchange — same logic as the template tabs, so counts
    # match what the default tab will actually show.
    _non_all = [f for f in ef if f["key"] != "ALL"]
    if af == "ALL" and _non_all:
        af = _non_all[0]["key"]

    # ── Fast count queries only — the full card data comes from /signals/items ──
    _base_q = db.query(Signal).filter(
        or_(
            Signal.signal_date == get_current_date(),
            Signal.status == SignalStatus.PENDING,
        ),
        Signal.status != SignalStatus.TRIGGERED,
        Signal.organization_id == org_id,
    )
    if af == "ASX":
        # Exclude bare/suffixed crypto tickers that landed with exchange_key="ASX"
        # due to the Jun 2026 screener/promotion bug (legacy rows pre-date ticker
        # suffix normalisation, e.g. "TRX" instead of "TRX-AUD").
        _base_q = _base_q.filter(
            Signal.exchange_key == "ASX",
            ~Signal.ticker.like("%-AUD"),
            ~Signal.ticker.like("%-USD"),
            ~Signal.ticker.like("%-USDT"),
            ~Signal.ticker.in_(_CRYPTO_TICKER_SET),
        )
    elif af == "CRYPTO":
        _base_q = _base_q.filter(or_(
            Signal.asset_type == "CRYPTO",
            Signal.ticker.like("%-AUD"),
            Signal.ticker.like("%-USD"),
            Signal.ticker.like("%-USDT"),
            Signal.ticker.in_(_CRYPTO_TICKER_SET),
        ))
    elif af == "US":
        _base_q = _base_q.filter(Signal.exchange_key.in_(["NYSE", "NASDAQ"]))

    _all_sigs = _base_q.with_entities(Signal.status).all()
    pending_count   = sum(1 for s in _all_sigs if s.status == SignalStatus.PENDING)
    triggered_count = sum(1 for s in _all_sigs if s.status == SignalStatus.TRIGGERED)
    skipped_count   = sum(1 for s in _all_sigs if s.status == SignalStatus.SKIPPED)
    has_signals     = bool(_all_sigs)

    # ── Last 3 screener runs — gives the user context for what they're looking
    # at, and why the page may be empty (regime filtering, no data, etc). Only
    # completion rows are shown (start rows are filtered out by message text).
    _tz_row = db.query(SystemConfig).filter(
        SystemConfig.key == "org_timezone", SystemConfig.organization_id == org_id,
    ).first()
    _display_tz = _tz_row.value if _tz_row else "Australia/Sydney"

    _runs_filter = db.query(AuditLog).filter(
        AuditLog.action == AuditAction.SCREENER_RUN,
        AuditLog.organization_id == org_id,
        ~AuditLog.message.like("%started%"),
    )
    # Scope to the active exchange tab — message is always "[{exchange_key}] ...".
    # Without this, the ASX tab was showing the last 3 runs org-wide (often all
    # CRYPTO, since crypto screens 4x/day vs ASX's 1x/day).
    if af == "ASX":
        _runs_filter = _runs_filter.filter(AuditLog.message.like("[ASX]%"))
    elif af == "US":
        _runs_filter = _runs_filter.filter(or_(
            AuditLog.message.like("[NYSE]%"),
            AuditLog.message.like("[NASDAQ]%"),
        ))
    elif af == "CRYPTO":
        _runs_filter = _runs_filter.filter(AuditLog.message.like("[CRYPTO%"))

    _recent_runs_q = _runs_filter.order_by(AuditLog.created_at.desc()).limit(3).all()

    recent_screen_runs = []
    for _log in _recent_runs_q:
        _msg = _log.message or ""
        _m_sig  = re.search(r"(\d+)\s+signals?", _msg)
        # Matches both the new wording ("N added/confirmed to watchlist this
        # run") and the old wording ("N watchlist") for any historical rows
        # already in the audit log from before this text was clarified.
        _m_wl   = re.search(r"(\d+)\s+(?:added/confirmed to watchlist this run|watchlist)", _msg)
        _m_skip = re.search(r"(\d+)\s+skipped", _msg)
        _m_exch = re.match(r"\[([^\]]+)\]", _msg)
        recent_screen_runs.append({
            "time":           _fmt_dt(str(_log.created_at), _display_tz),
            "exchange":       _m_exch.group(1) if _m_exch else "",
            "signals":        int(_m_sig.group(1)) if _m_sig else 0,
            # Per-run delta: tickers that qualified for the watchlist during
            # THIS run — not the total currently sitting on the watchlist.
            "watchlist_added": int(_m_wl.group(1)) if _m_wl else 0,
            "skipped":        int(_m_skip.group(1)) if _m_skip else None,
            "message":        _msg,
        })

    ctx.update({
        "signals":             [],   # skeleton — cards loaded via /signals/items
        "has_signals":         has_signals,
        "signal_date":         _fmt_date(get_current_date()),
        "recent_screen_runs":  recent_screen_runs,
        "pending_count":       pending_count,
        "triggered_count":     triggered_count,
        "skipped_count":       skipped_count,
        "exchange_filters":       ef,
        "active_exchange_filter": af,
        "active_tier":            (tier or "").upper() if tier in ("A", "B", "C") else None,
        "base_url":               "/signals",
        "extra_params":           "",
    })
    return templates.TemplateResponse("trading/signals.html", ctx)


@app.get("/signals/items", response_class=HTMLResponse)
async def signals_items(request: Request, db: Session = Depends(get_db),
                        exchange: str = Query("ALL"), tier: str = Query(None)):
    """Cached HTML fragment for signal cards — polled by signals.html on load."""
    if not _auth(request):
        return HTMLResponse("", status_code=401)
    org_id = request.session.get("organization_id")
    af = (exchange or "ALL").upper()
    _sig_tier = (tier or "").upper() if tier in ("A", "B", "C") else None

    _sig_ck = f"sig_items:{org_id}:{af}:{_sig_tier or 'all'}"
    _cached = cache.get_raw(_sig_ck)
    if _cached:
        return HTMLResponse(_cached)

    from app.models.signal import Signal, SignalStatus
    from app.models.config import RuleConfig
    from app.models.audit import AuditLog, AuditAction
    from app.models.account import Organization as _Org
    from sqlalchemy import or_

    sig_tz = _get_display_tz(org_id, db)

    # Load org rule metadata once for the override UI
    org_obj = db.query(_Org).filter(_Org.id == org_id).first()
    all_org_rules = db.query(RuleConfig).filter(RuleConfig.organization_id == org_id).order_by(RuleConfig.sort_order).all()
    rules_meta = {
        r.rule_id: {
            "label": r.label,
            "category": r.category.value,
            "is_mandatory": r.is_mandatory,
            "globally_enabled": r.enabled_globally,
            "asset_types": getattr(r, "asset_types", "BOTH") or "BOTH",
        }
        for r in all_org_rules
    }

    # Build flag emoji lookup from ExchangeConfig
    flag_map: dict[str, str] = {}
    try:
        from app.models.exchange import ExchangeConfig as _EC
        for ec in db.query(_EC).all():
            flag_map[ec.exchange_key] = ec.flag_emoji or ""
    except Exception:
        pass

    q = db.query(Signal).filter(
        or_(
            Signal.signal_date == get_current_date(),
            Signal.status == SignalStatus.PENDING,
        ),
        Signal.status != SignalStatus.TRIGGERED,
        Signal.organization_id == org_id,
    )
    if af == "ASX":
        # Exclude bare/suffixed crypto tickers that landed with exchange_key="ASX"
        # due to the Jun 2026 screener/promotion bug (legacy rows pre-date ticker
        # suffix normalisation, e.g. "TRX" instead of "TRX-AUD").
        q = q.filter(
            Signal.exchange_key == "ASX",
            ~Signal.ticker.like("%-AUD"),
            ~Signal.ticker.like("%-USD"),
            ~Signal.ticker.like("%-USDT"),
            ~Signal.ticker.in_(_CRYPTO_TICKER_SET),
        )
    elif af == "CRYPTO":
        from sqlalchemy import or_ as _or_sig
        q = q.filter(_or_sig(
            Signal.asset_type == "CRYPTO",
            Signal.ticker.in_(_CRYPTO_TICKER_SET),
            Signal.ticker.like("%-AUD"),
            Signal.ticker.like("%-USD"),
            Signal.ticker.like("%-USDT"),
        ))
    elif af == "US":
        q = q.filter(Signal.exchange_key.in_(["NYSE", "NASDAQ"]))
    sigs = q.all()

    # ── Self-heal legacy mis-tagged crypto signals ───────────────────────────
    # Rows created before ticker-suffix normalisation can have exchange_key="ASX"
    # / asset_type="EQUITY" with a bare crypto ticker (e.g. "TRX"). Correct them
    # in place so they file under the right exchange tab going forward and don't
    # need the runtime heuristic on every request.
    _healed = False
    for _s in sigs:
        if _looks_like_crypto_ticker(_s.ticker) and (_s.exchange_key == "ASX" or _s.asset_type != "CRYPTO"):
            _s.exchange_key = "CRYPTO_INDEPENDENTRESERVE"
            _s.asset_type = "CRYPTO"
            _healed = True
    if _healed:
        try:
            db.add(AuditLog(
                action=AuditAction.CONFIG_CHANGED,
                actor="system",
                organization_id=org_id,
                message="Auto-corrected mis-tagged legacy crypto signal(s) — exchange_key/asset_type fixed.",
            ))
            db.commit()
        except Exception:
            db.rollback()

    stock_names = get_cached_stock_names(db)

    # ── Pre-fetch audit entries (avoids N+1) ──────────────────────────────
    _sig_tickers = list({s.ticker for s in sigs})
    _all_audit_entries = []
    if _sig_tickers:
        _all_audit_entries = db.query(AuditLog).filter(
            AuditLog.organization_id == org_id,
            AuditLog.action == AuditAction.TASK_RUN,
            AuditLog.ticker.in_(_sig_tickers),
        ).order_by(desc(AuditLog.created_at)).limit(len(_sig_tickers) * 20).all()
    from collections import defaultdict as _defaultdict
    _audit_by_ticker: dict = _defaultdict(list)
    for _e in _all_audit_entries:
        if _e.ticker:
            _audit_by_ticker[_e.ticker].append(_e)

    # ── Pre-fetch regime configs in one query ─────────────────────────────
    from app.models.config import SystemConfig as _SC
    _regime_rows = db.query(_SC).filter(
        or_(
            (_SC.key.like("last_market_regime_%") & (_SC.organization_id == org_id)),
            (_SC.key == "last_market_regime") & (_SC.organization_id == None),
        )
    ).all()
    _regime_map: dict[str, str] = {rc.key: rc.value or "UNKNOWN" for rc in _regime_rows}

    # ── Bulk-fetch latest PriceBars ───────────────────────────────────────
    _sig_bar_lookup: dict[str, dict] = {}
    if _sig_tickers:
        from sqlalchemy import func as _sfunc
        from app.models.market import PriceBar
        _ssub = (
            db.query(PriceBar.ticker, _sfunc.max(PriceBar.date).label("max_date"))
            .filter(PriceBar.ticker.in_(_sig_tickers))
            .group_by(PriceBar.ticker)
            .subquery()
        )
        _sbars = (
            db.query(PriceBar)
            .join(_ssub, (PriceBar.ticker == _ssub.c.ticker) & (PriceBar.date == _ssub.c.max_date))
            .all()
        )
        for _sb in _sbars:
            _bd = {
                "close":     float(_sb.close or 0),
                "ma_50":     float(_sb.ma_50 or 0),
                "ma_150":    float(_sb.ma_150 or 0),
                "ma_200":    float(_sb.ma_200 or 0),
                "high_52w":  float(_sb.high_52w or 0),
                "low_52w":   float(_sb.low_52w or 0),
                "rs_rating": float(_sb.rs_rating or 0),
                "vol_ratio": float(_sb.vol_ratio) if _sb.vol_ratio is not None else None,
            }
            _sig_bar_lookup[_sb.ticker] = _bd
            _ck = f"latest_price_bar:{_sb.ticker}"
            if not cache.get(_ck):
                cache.set(_ck, _bd, expire_seconds=300)

    # ── Bulk-fetch historical bars for the per-signal VCP chart ──────────
    # (last ~150 trading days — wide enough to show contraction history + breakout)
    _chart_bars_by_ticker: dict[str, list[dict]] = {}
    if _sig_tickers:
        _chart_cutoff = get_current_date() - timedelta(days=240)
        _hist_rows = (
            db.query(PriceBar)
            .filter(PriceBar.ticker.in_(_sig_tickers), PriceBar.date >= _chart_cutoff)
            .order_by(PriceBar.ticker, PriceBar.date.asc())
            .all()
        )
        for _hb in _hist_rows:
            _chart_bars_by_ticker.setdefault(_hb.ticker, []).append({
                "date":      str(_hb.date),
                "close":     float(_hb.close or 0),
                "high":      float(_hb.high or _hb.close or 0),
                "low":       float(_hb.low or _hb.close or 0),
                "volume":    float(_hb.volume or 0),
                "vol_ratio": float(_hb.vol_ratio) if _hb.vol_ratio is not None else None,
            })
        for _tkr in list(_chart_bars_by_ticker.keys()):
            _chart_bars_by_ticker[_tkr] = _chart_bars_by_ticker[_tkr][-150:]

    # ── Favourites — tickers assigned to the "Favourites" label ─────────
    from app.models.signal import Watchlist, WatchlistStatus, WatchlistLabel
    _fav_label = db.query(WatchlistLabel.id).filter(
        WatchlistLabel.organization_id == org_id,
        WatchlistLabel.name == "Favourites",
    ).first()
    if _fav_label:
        _fav_rows = db.query(Watchlist.ticker).filter(
            Watchlist.organization_id == org_id,
            Watchlist.label_id == _fav_label.id,
        ).all()
        favourited_tickers = {r.ticker for r in _fav_rows}
    else:
        favourited_tickers = set()

    sig_data = []
    for s in sigs:
        company_name = stock_names.get(s.ticker, "")
        if not company_name:
            _nck = f"missing_name_fetch:{s.ticker}"
            if not cache.get(_nck):
                cache.set(_nck, "attempted", expire_seconds=86400)

        rr = s.rule_results or {}
        passed = sum(1 for v in rr.values() if v.get("passed"))
        overrides = s.rule_overrides or {}

        # ── Latest entry check ────────────────────────────────────────────
        last_check = None
        try:
            for entry in _audit_by_ticker.get(s.ticker, []):
                d = entry.detail or {}
                if d.get("signal_id") == s.id:
                    active_overrides = {k: v for k, v in (d.get("overrides_applied", {}) or {}).items() if v is False}
                    last_check = {
                        "time": _fmt_dt(str(entry.created_at), sig_tz),
                        "result": d.get("result", ""),
                        "message": entry.message,
                        "close": d.get("close"),
                        "pivot": d.get("pivot"),
                        "avg_vol": d.get("avg_vol"),
                        "data_source": d.get("data_source"),
                        "delay_mins": d.get("delay_mins"),
                        "rules": [
                            {
                                "rule_id": rid,
                                "label": rid.replace("vcp_", "").replace("_", " ").title(),
                                "passed": rd.get("passed", False),
                                "overridden": overrides.get(rid) == False,
                                "message": rd.get("message", ""),
                            }
                            for rid, rd in d["rules"].items()
                        ] if "rules" in d else None,
                        "overrides_applied": active_overrides,
                    }
                    break
        except Exception:
            pass

        # ── Override rule classification ──────────────────────────────────
        screener_pass_fail = {rid: bool(v.get("passed")) for rid, v in rr.items()}
        if last_check and last_check.get("rules"):
            for br in last_check["rules"]:
                screener_pass_fail[br["rule_id"]] = br["passed"]

        ek = getattr(s, "exchange_key", "ASX") or "ASX"
        at = getattr(s, "asset_type",   "EQUITY") or "EQUITY"
        _exc_key = ek
        _is_crypto_exc = _exc_key == "CRYPTO" or _exc_key.startswith("CRYPTO_")
        bear_rule_id  = "regime_bear_block_crypto" if _is_crypto_exc else "regime_bear_block_equities"
        if _is_crypto_exc:
            _eff_exc = _exc_key if _exc_key != "CRYPTO" else "CRYPTO_INDEPENDENTRESERVE"
            _current_regime = _regime_map.get(f"last_market_regime_{_eff_exc}", "BULL")
        else:
            _current_regime = _regime_map.get("last_market_regime", "UNKNOWN")
        _bear_rule_meta = rules_meta.get(bear_rule_id, {})
        if _current_regime == "BEAR" and _bear_rule_meta.get("globally_enabled", True):
            screener_pass_fail[bear_rule_id] = overrides.get(bear_rule_id) is False

        override_rules_failed = []
        override_rules_passed = []
        for rule_id, meta in rules_meta.items():
            if not meta["globally_enabled"]:
                continue
            rule_asset = meta.get("asset_types", "BOTH")
            if rule_asset == "CRYPTO" and at != "CRYPTO":
                continue
            if rule_asset == "EQUITY" and at == "CRYPTO":
                continue
            rule_passed = screener_pass_fail.get(rule_id)
            current_override = overrides.get(rule_id, None)
            entry = {
                "rule_id":    rule_id,
                "label":      meta["label"],
                "category":   meta["category"],
                "is_mandatory": meta["is_mandatory"],
                "rule_passed": rule_passed,
                "override":   current_override,
            }
            if rule_passed is False:
                override_rules_failed.append(entry)
            else:
                override_rules_passed.append(entry)

        notes_str = s.notes or ""
        is_promoted_manual = notes_str.startswith("[Manual Promotion]") or notes_str.startswith("Manually promoted from Watchlist")
        is_promoted_vcp    = notes_str.startswith("[VCP Screener]")
        has_overrides = any(overrides.get(rid) == False for rid, passed in screener_pass_fail.items() if passed is False)

        # ── Live price overlay from Redis cache (crypto) ──────────────────
        _close = float(s.close_price or 0)
        _live_ck = f"live_price:{s.ticker}"
        _live = cache.get(_live_ck)
        if _live and isinstance(_live, (int, float)) and _live > 0:
            _close = float(_live)

        _s_trend_total = 8 if at == "CRYPTO" else 9
        _s_rs    = float(s.rs_rating or 0)
        _s_trend = s.trend_score or 0
        _s_vol   = (_sig_bar_lookup.get(s.ticker) or {}).get("vol_ratio")
        if (_s_trend >= _s_trend_total and _s_rs >= 80
                and (_s_vol is None or _s_vol <= 0.6)):
            _s_tier = "A"
        elif _s_trend >= max(_s_trend_total - 1, 1) and _s_rs >= 70:
            _s_tier = "B"
        else:
            _s_tier = "C"

        _vcp_json = None
        _bars = _chart_bars_by_ticker.get(s.ticker, [])
        if _bars:
            _swing_highs = []
            _swing_lows = []
            _contractions = []
            try:
                import pandas as _pd
                import numpy as _np
                import json
                from app.screener.vcp import detect_vcp, _find_pivots
                from app.screener.rules import RuleEngine
                from app.models.account import Organization as _Org

                _df = _pd.DataFrame(_bars)
                _df["date"] = _pd.to_datetime(_df["date"])
                
                _org = db.query(_Org).get(org_id) if org_id else None
                _tier = _org.tier.value if (_org and _org.tier) else "BRONZE"
                _engine = RuleEngine(organization_id=org_id, tier=_tier, asset_type=s.asset_type or "EQUITY")
                
                _vcp_res, _ = detect_vcp(s.ticker, _df, _engine)
                _contractions = (_vcp_res.detail or {}).get("contractions", [])
                
                _highs = _df["high"].values
                _lows = _df["low"].values
                _win = 3 if len(_bars) < 60 else 5
                for _idx in _find_pivots(_np.array(_highs), direction="high", window=_win):
                    _swing_highs.append(_bars[_idx]["date"])
                for _idx in _find_pivots(_np.array(_lows), direction="low", window=_win):
                    _swing_lows.append(_bars[_idx]["date"])
                
                _vcp_chart_data = {
                    "ticker": s.ticker,
                    "series": _bars,
                    "pivot": float(s.pivot_price or 0) or None,
                    "stop": float(s.stop_price or 0) or None,
                    "target": float(s.target_price_1 or 0) or None,
                    "contractions": _contractions,
                    "swing_highs": _swing_highs,
                    "swing_lows": _swing_lows,
                }
                _vcp_json = json.dumps(_vcp_chart_data)
            except Exception as _ex:
                logger.error(f"Failed to calculate interactive VCP chart details for {s.ticker}: {_ex}")

        sig_data.append({
            "id": s.id, "ticker": s.ticker,
            "exchange_key":  ek,
            "asset_type":    at,
            "currency":      getattr(s, "currency", "AUD") or "AUD",
            "flag_emoji":    flag_map.get(ek, ""),
            "company_name":  company_name,
            "close": _close,
            "pivot": float(s.pivot_price or 0),
            "stop":  float(s.stop_price or 0),
            "target": float(s.target_price_1 or 0),
            "rs": float(s.rs_rating or 0),
            "trend_score": s.trend_score or 0,
            "fund_score":  s.fundamental_score or 0,
            "vcp_contractions": s.vcp_contractions or 0,
            "vcp_weeks": s.vcp_weeks or 0,
            "size": s.suggested_size_shares or 0,
            "risk_aud": float(s.risk_per_trade_aud or 0),
            "status": s.status.value,
            "sig_date": _fmt_date(s.signal_date) if s.signal_date else "",
            "rules_passed": passed,
            "rules_total": len(rr),
            "rule_results": _enrich_rule_results(s.ticker, rr, db, target_date=s.signal_date, overrides=overrides, _bar_data=_sig_bar_lookup.get(s.ticker)),
            "override_rules_failed": override_rules_failed,
            "override_rules_passed": override_rules_passed,
            "has_overrides": has_overrides,
            "is_promoted_manual": bool(is_promoted_manual),
            "is_promoted_vcp": bool(is_promoted_vcp),
            "last_check": last_check,
            "setup_tier": _s_tier,
            "vcp_chart_svg": _build_vcp_chart_svg(
                s.ticker,
                _chart_bars_by_ticker.get(s.ticker, []),
                float(s.pivot_price or 0),
                float(s.stop_price or 0),
                float(s.target_price_1 or 0),
            ),
            "vcp_chart_json": _vcp_json,
        })

    _status_order = {"PENDING": 0, "TRIGGERED": 1, "SKIPPED": 2}
    sig_data.sort(key=lambda x: _status_order.get(x["status"], 9))

    # ── Tier filter (post-sort, since setup_tier is derived not stored) ──────
    if _sig_tier:
        sig_data = [s for s in sig_data if s.get("setup_tier") == _sig_tier]

    _html_out = templates.get_template("components/signals_cards.html").render({
        "request": request,
        "signals": sig_data,
        "signal_date": _fmt_date(get_current_date()),
        "favourited_tickers": favourited_tickers,
        "path": "/signals",
    })
    cache.set_raw(_sig_ck, _html_out, expire_seconds=120)   # 2-min cache
    return HTMLResponse(_html_out)


@app.get("/signals/poll")
async def signals_poll(request: Request, db: Session = Depends(get_db)):
    """Return latest entry-check data for all PENDING signals — polled every 30s by the signals page."""
    from fastapi.responses import JSONResponse
    from app.models.signal import Signal, SignalStatus
    from app.models.audit import AuditLog, AuditAction
    if not _auth(request):
        return JSONResponse({}, status_code=401)
    org_id = request.session.get("organization_id")
    display_tz = _get_display_tz(org_id, db)
    pending = db.query(Signal).filter(
        Signal.status == SignalStatus.PENDING,
        Signal.organization_id == org_id,
    ).all()
    result = {}
    for s in pending:
        try:
            entries = db.query(AuditLog).filter(
                AuditLog.organization_id == org_id,
                AuditLog.action == AuditAction.TASK_RUN,
                AuditLog.ticker == s.ticker,
            ).order_by(desc(AuditLog.created_at)).limit(20).all()
            for entry in entries:
                d = entry.detail or {}
                if d.get("signal_id") == s.id:
                    result[str(s.id)] = {
                        "time":        _fmt_dt(str(entry.created_at), display_tz),
                        "result":      d.get("result", ""),
                        "close":       d.get("close"),
                        "pivot":       d.get("pivot"),
                        "data_source": d.get("data_source"),
                        "delay_mins":  d.get("delay_mins"),
                    }
                    break
        except Exception:
            pass
    return JSONResponse(result)

@app.post("/signals/{signal_id}/skip")
async def skip_signal(request: Request, signal_id: int, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.models.signal import Signal, SignalStatus
    from app.models.audit import AuditLog, AuditAction
    s = db.query(Signal).filter(Signal.id == signal_id, Signal.organization_id == org_id).first()
    if s:
        s.status = SignalStatus.SKIPPED
        db.add(AuditLog(action=AuditAction.MANUAL_OVERRIDE, ticker=s.ticker,
                        actor=request.session.get("email","dashboard"), user_id=request.session.get("user_id"),
                        message="Signal skipped via dashboard", organization_id=org_id))
        db.commit()
        cache.delete_prefix(f"sig_items:{org_id}:")
    return RedirectResponse("/signals", 302)


@app.post("/signals/{signal_id}/unskip")
async def unskip_signal(request: Request, signal_id: int, db: Session = Depends(get_db)):
    """Restore a skipped signal back to PENDING so it can be acted on."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")

    from app.models.signal import Signal, SignalStatus
    from app.models.audit import AuditLog, AuditAction
    s = db.query(Signal).filter(Signal.id == signal_id, Signal.organization_id == org_id).first()
    if s and s.status == SignalStatus.SKIPPED:
        s.status = SignalStatus.PENDING
        db.add(AuditLog(action=AuditAction.MANUAL_OVERRIDE, ticker=s.ticker,
                        actor=request.session.get("email","dashboard"), user_id=request.session.get("user_id"),
                        message="Signal unskipped (restored to PENDING) via dashboard", organization_id=org_id))
        db.commit()
        cache.delete_prefix(f"sig_items:{org_id}:")
    return RedirectResponse("/signals", 302)


@app.post("/signals/{signal_id}/toggle-rule")
async def signal_toggle_rule(
    request: Request,
    signal_id: int,
    rule_id: str = Form(...),
    enabled: str = Form(...),   # "true" or "false"
    db: Session = Depends(get_db),
):
    """Toggle a single rule override for a specific signal. Mandatory rules are protected server-side."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")

    from app.models.signal import Signal, SignalStatus
    from app.models.config import RuleConfig
    from app.models.audit import AuditLog, AuditAction

    s = db.query(Signal).filter(Signal.id == signal_id, Signal.organization_id == org_id).first()
    if not s or s.status not in (SignalStatus.PENDING,):
        return RedirectResponse("/signals", 302)

    # Server-side guard: refuse override of mandatory or globally-disabled rules
    rule = db.query(RuleConfig).filter(
        RuleConfig.rule_id == rule_id,
        RuleConfig.organization_id == org_id,
    ).first()
    if rule and (rule.is_mandatory or not rule.enabled_globally):
        return RedirectResponse("/signals", 302)

    overrides = dict(s.rule_overrides or {})
    new_state = enabled.lower() == "true"
    overrides[rule_id] = new_state
    s.rule_overrides = overrides

    action_label = "enabled" if new_state else "disabled"
    db.add(AuditLog(
        action=AuditAction.RULE_TOGGLED,
        organization_id=org_id,
        ticker=s.ticker,
        actor=request.session.get("email", "dashboard"),
        user_id=request.session.get("user_id"),
        message=f"Rule '{rule_id}' {action_label} for signal {s.ticker} (override — reverts next screen)",
        detail={"signal_id": signal_id, "rule_id": rule_id, "enabled": new_state},
    ))
    db.commit()
    cache.delete_prefix(f"sig_items:{org_id}:")
    return RedirectResponse("/signals", 302)


def _enrich_watchlist_vcp_and_sizing(items: list[dict], db, org_id: int):
    """
    Fills each watchlist item dict with pivot/stop/target/contractions/weeks/size/risk.

    Fast path: most rows already carry VCP geometry persisted by the screener
    (keyed by the last bar date). When that geometry is fresh — its `computed_date`
    matches the latest price bar — it is used directly: no price history is loaded
    and detect_vcp is NOT run; only the cheap position sizing is computed live.
    Rows with missing/stale geometry are computed once here, cached in Redis
    (key = ticker + bar_date) and written back to the row so the next load is fast
    too. RuleEngine is built once per asset_type instead of once per row.

    Each item may carry these internal keys (set by the watchlist route):
      _wl_id, _persisted {pivot,stop,target,contractions,weeks,computed_date},
      _latest_bar_date. They are stripped from the dict before returning.
    """
    from app.models.account import Organization, Account
    from app.models.config import SystemConfig
    from app.screener.rules import RuleEngine
    from app.risk.manager import calculate_position_size
    from loguru import logger
    from datetime import timedelta

    if not items:
        return

    org = db.query(Organization).filter(Organization.id == org_id).first()
    tier = org.tier.value if org else "GOLD"

    account = db.query(Account).filter(
        Account.organization_id == org_id,
        Account.is_active == True
    ).first()
    capital = float(account.capital_aud) if account else 1000.0

    currency_cfg = db.query(SystemConfig).filter(
        SystemConfig.key == "working_capital_currency",
        SystemConfig.organization_id == org_id
    ).first()
    base_currency = currency_cfg.value if currency_cfg else "AUD"

    # One RuleEngine per asset_type (was previously rebuilt for every row).
    _engines: dict = {}
    def _engine_for(asset_type):
        e = _engines.get(asset_type)
        if e is None:
            e = RuleEngine(organization_id=org_id, tier=tier, asset_type=asset_type)
            _engines[asset_type] = e
        return e

    def _currency_for(w):
        c = w.get("currency")
        if c:
            return c
        ex = w.get("exchange_key", "ASX")
        return "AUD" if ex in ("ASX", "CRYPTO_INDEPENDENTRESERVE") else "USD"

    # ── Pass 1: resolve geometry from persisted columns or Redis ──────────────
    _need_compute = []
    for w in items:
        p = w.get("_persisted") or {}
        latest = w.get("_latest_bar_date")
        fresh = (
            p.get("pivot") is not None
            and p.get("computed_date") is not None
            and (latest is None or p.get("computed_date") == latest)
        )
        if fresh:
            w["_geo"] = {
                "pivot": float(p["pivot"]),
                "stop": float(p["stop"]) if p.get("stop") is not None else None,
                "target": float(p["target"]) if p.get("target") is not None else None,
                "contractions": p.get("contractions"),
                "weeks": p.get("weeks"),
            }
        else:
            cached = cache.get(f"wl_vcp:{w['ticker']}:{latest}") if latest is not None else None
            if cached:
                w["_geo"] = cached
            else:
                w["_geo"] = None
                _need_compute.append(w)

    # ── Pass 2: compute geometry ONLY for rows that need it ───────────────────
    if _need_compute:
        from app.models.market import PriceBar
        from app.models.signal import Watchlist
        from app.screener.vcp import detect_vcp, resolve_watchlist_geometry
        import pandas as pd
        import collections
        try:
            from app.utils.time_helper import get_current_date
            _floor = get_current_date() - timedelta(days=420)
        except Exception:
            from datetime import date as _date
            _floor = _date.today() - timedelta(days=420)

        need_tickers = list({w["ticker"] for w in _need_compute})
        rows = (
            db.query(PriceBar)
            .filter(PriceBar.ticker.in_(need_tickers), PriceBar.date >= _floor)
            .order_by(PriceBar.date.asc())
            .all()
        )
        bars_by_ticker = collections.defaultdict(list)
        for b in rows:
            bars_by_ticker[b.ticker].append(b)

        for w in _need_compute:
            ticker = w["ticker"]
            bars = bars_by_ticker.get(ticker, [])
            if not bars:
                w["_geo"] = None
                continue
            df = pd.DataFrame([{
                "high": float(b.high or 0), "low": float(b.low or 0),
                "close": float(b.close or 0), "volume": int(b.volume or 0),
                "avg_vol_50": float(b.avg_vol_50 or 0),
            } for b in bars])
            engine = _engine_for(w["asset_type"])
            avg_vol = float(df["avg_vol_50"].iloc[-1]) if not df.empty else 0.0
            try:
                vcp_result, _ = detect_vcp(ticker, df, engine, avg_vol)
            except Exception as e:
                logger.error(f"VCP detect failed for watchlist {ticker}: {e}")
                vcp_result = None
            last = bars[-1]
            cols = resolve_watchlist_geometry(
                vcp_result, close=float(last.close or 0),
                high_52w=float(last.high_52w or 0), atr_14=float(last.atr_14 or 0),
            )
            geo = {
                "pivot": cols["pivot_price"], "stop": cols["stop_price"],
                "target": cols["target_price"], "contractions": cols["vcp_contractions"],
                "weeks": cols["vcp_base_weeks"],
            }
            w["_geo"] = geo
            cache.set(f"wl_vcp:{ticker}:{last.date}", geo, expire_seconds=86400)
            _wl_id = w.get("_wl_id")
            if _wl_id:
                try:
                    db.query(Watchlist).filter(Watchlist.id == _wl_id).update({
                        "pivot_price": cols["pivot_price"], "stop_price": cols["stop_price"],
                        "target_price": cols["target_price"],
                        "vcp_contractions": cols["vcp_contractions"],
                        "vcp_base_weeks": cols["vcp_base_weeks"],
                        "vcp_computed_date": last.date,
                    }, synchronize_session=False)
                except Exception as e:
                    logger.error(f"Watchlist geometry write-back failed for {ticker}: {e}")

    # ── Pass 3: position sizing (cheap; needs no price history) ───────────────
    for w in items:
        geo = w.get("_geo")
        if not geo or not geo.get("pivot"):
            w.update({"pivot": None, "stop": None, "target": None,
                      "vcp_contractions": None, "vcp_weeks": None,
                      "size": None, "risk_aud": None})
            for _k in ("_persisted", "_latest_bar_date", "_geo", "_wl_id"):
                w.pop(_k, None)
            continue
        pivot_price = geo["pivot"]
        stop_price = geo.get("stop")
        shares = risk_aud = None
        if pivot_price and stop_price and pivot_price > stop_price:
            try:
                sizing = calculate_position_size(
                    capital_aud=capital,
                    entry_price=pivot_price,
                    stop_price=stop_price,
                    engine=_engine_for(w["asset_type"]),
                    currency=_currency_for(w),
                    base_currency=base_currency,
                    is_crypto=(w["asset_type"] == "CRYPTO"),
                )
                shares = sizing.shares
                risk_aud = sizing.risk_aud
            except Exception as e:
                logger.error(f"Sizing failed for watchlist {w['ticker']}: {e}")
        w.update({
            "pivot": pivot_price,
            "stop": stop_price,
            "target": geo.get("target"),
            "vcp_contractions": geo.get("contractions"),
            "vcp_weeks": geo.get("weeks"),
            "size": shares,
            "risk_aud": risk_aud,
        })
        for _k in ("_persisted", "_latest_bar_date", "_geo", "_wl_id"):
            w.pop(_k, None)


@app.get("/watchlist", response_class=HTMLResponse)
async def watchlist(
    request: Request,
    label: int = Query(None),
    exchange: str = Query("ALL"),
    tier: str = Query(None),
    db: Session = Depends(get_db)
):
    """
    Skeleton-first watchlist page.
    Returns instantly with chrome (labels, filters, add form) + an empty card
    container. The browser immediately fetches /watchlist/rows?page=1 via JS,
    which is served from a 5-minute Redis cache on subsequent loads (< 20 ms).
    Live prices are updated separately via /trader/prices polling (15 s).
    """
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")

    from app.models.signal import Watchlist, WatchlistStatus, WatchlistLabel
    from sqlalchemy import func as _sqf, or_ as _or_cnt
    ctx = _global(request, db)

    af = (exchange or "ALL").upper()

    # Labels — served from Redis cache (invalidated on label create/edit)
    all_labels = get_cached_wl_labels(org_id, db)

    def _exchange_labels(labels, exf):
        if exf in ("ASX", "US"):
            return [l for l in labels if not (10 <= l["sort_order"] <= 19)]
        if exf == "CRYPTO":
            return [l for l in labels if not (20 <= l["sort_order"] <= 38) and l["sort_order"] < 100]
        return labels
    filtered_labels = _exchange_labels(all_labels, af)

    # Label counts — fast grouped query (indexed on organization_id + status)
    _label_cnt_q = (
        db.query(Watchlist.label_id, _sqf.count(Watchlist.id))
        .filter(
            Watchlist.organization_id == org_id,
            Watchlist.status == WatchlistStatus.WATCHING,
            Watchlist.label_id.isnot(None),
        )
    )
    if af == "ASX":
        _label_cnt_q = _label_cnt_q.filter(Watchlist.exchange_key == "ASX")
    elif af == "US":
        _label_cnt_q = _label_cnt_q.filter(Watchlist.exchange_key.in_(["NYSE", "NASDAQ"]))
    elif af == "CRYPTO":
        _label_cnt_q = _label_cnt_q.filter(_or_cnt(
            Watchlist.asset_type == "CRYPTO",
            Watchlist.ticker.like("%-AUD"),
            Watchlist.ticker.like("%-USD"),
            Watchlist.ticker.like("%-USDT"),
        ))
    _cnt_rows = _label_cnt_q.group_by(Watchlist.label_id).all()
    ctx["label_counts"] = {row[0]: row[1] for row in _cnt_rows}

    _active_label_ids = set(ctx["label_counts"].keys())
    ctx["labels"] = [l for l in filtered_labels if l["id"] in _active_label_ids]
    ctx["active_label"] = label

    # Total count — used in the "All" label chip. Must always reflect every
    # WATCHING item in the active exchange tab, regardless of which label
    # filter is currently selected — it previously also filtered by `label`,
    # which made the "All" chip show the same count as whichever label was
    # selected (e.g. "All 5" / "Banks 5" both showing 5, even though Biotech
    # alone had 22) instead of the true unfiltered total.
    _total_q = db.query(_sqf.count(Watchlist.id)).filter(
        Watchlist.organization_id == org_id,
        Watchlist.status == WatchlistStatus.WATCHING,
    )
    if af == "ASX":
        _total_q = _total_q.filter(Watchlist.exchange_key == "ASX")
    elif af == "US":
        _total_q = _total_q.filter(Watchlist.exchange_key.in_(["NYSE", "NASDAQ"]))
    elif af == "CRYPTO":
        _total_q = _total_q.filter(_or_cnt(
            Watchlist.asset_type == "CRYPTO",
            Watchlist.ticker.like("%-AUD"),
            Watchlist.ticker.like("%-USD"),
            Watchlist.ticker.like("%-USDT"),
        ))
    total = _total_q.scalar() or 0

    # Crypto tickers — lightweight ticker-only query for the CoinGecko panel
    _crypto_q = db.query(Watchlist.ticker).filter(
        Watchlist.organization_id == org_id,
        Watchlist.status == WatchlistStatus.WATCHING,
        _or_cnt(
            Watchlist.asset_type == "CRYPTO",
            Watchlist.ticker.like("%-AUD"),
            Watchlist.ticker.like("%-USD"),
            Watchlist.ticker.like("%-USDT"),
        )
    ).all()
    crypto_tickers = [r.ticker for r in _crypto_q]
    has_crypto = bool(crypto_tickers)

    # Enabled exchanges for add-manually form
    ef = _get_exchange_filters(org_id, db)
    ee = []
    try:
        from app.models.exchange import ExchangeConfig as _EC
        for e in db.query(_EC).filter(_EC.is_enabled == True).order_by(_EC.sort_order).all():
            ee.append({"key": e.exchange_key, "name": e.display_name, "flag": e.flag_emoji or "", "asset_type": e.asset_type})
    except Exception:
        db.rollback(); ee = [{"key": "ASX", "name": "ASX", "flag": "", "asset_type": "EQUITY"}]

    _active_tier = (tier or "").upper() if tier in ("A", "B", "C") else None
    ctx.update({
        "enabled_exchanges": ee,
        "exchange_filters": ef,
        "active_exchange_filter": af,
        "base_url": "/watchlist",
        "extra_params": f"label={label}" if label is not None else "",
        # Skeleton: no items — JS loads /watchlist/rows?page=1 immediately
        "watchlist": [],
        "total": total,
        "has_crypto": has_crypto,
        "crypto_tickers": crypto_tickers,
        "page": 1,
        "has_more": total > 0,
        "active_tier": _active_tier,
    })
    return templates.TemplateResponse("trading/watchlist.html", ctx)


@app.get("/watchlist/rows", response_class=HTMLResponse)
async def watchlist_rows(
    request: Request,
    label: int = Query(None),
    exchange: str = Query("ALL"),
    tier: str = Query(None),
    page: int = Query(1),
    q: str = Query(None),
    db: Session = Depends(get_db)
):
    """
    Fragment endpoint — returns paginated watchlist card HTML for infinite scroll.

    Results are cached in Redis for 5 minutes per (org, exchange, label, tier, page).
    Cache is invalidated on add/remove/label/promote/screener-run. This means
    the first request after a screener run computes (~800 ms) and all subsequent
    loads for the same filter state are served in < 20 ms.

    `q` — optional search term. When present this bypasses normal pagination
    entirely and searches the ORG'S FULL watchlist (not just whatever pages
    have been scrolled into view), ranking ticker matches (exact > prefix >
    contains) above company-name matches. Not cached — search terms are
    too varied to be worth caching and results need to be live.
    """
    if not _auth(request):
        return HTMLResponse("", status_code=403)
    org_id = request.session.get("organization_id")
    from loguru import logger as _wl_log
    import time as _time
    _t0 = _time.monotonic()

    _tier_filter = (tier or "").upper() if tier in ("A", "B", "C") else None
    _search_term = (q or "").strip().lower()

    # ── Redis HTML cache (raw string, not JSON) — skipped entirely for searches ──
    _af = (exchange or "ALL").upper()
    _lbl_key = str(label) if label is not None else "all"
    _tier_key = _tier_filter or "all"
    _html_ck = f"wl_rows:{org_id}:{_af}:{_lbl_key}:{_tier_key}:{page}"
    if not _search_term:
        _cached_html = cache.get_raw(_html_ck)
        if _cached_html:
            _wl_log.debug(f"watchlist/rows cache hit for org={org_id} exchange={_af} label={_lbl_key} tier={_tier_key} page={page} ({(_time.monotonic()-_t0)*1000:.0f}ms)")
            return HTMLResponse(_cached_html)

    from app.models.signal import Watchlist, WatchlistStatus, WatchlistLabel
    from app.models.market import PriceBar, Stock
    from sqlalchemy.orm import joinedload

    # Labels from Redis cache (card template needs them for label picker)
    labels = get_cached_wl_labels(org_id, db)

    # Same paginated query as the main route
    _WL_PER_PAGE = 20
    af = (exchange or "ALL").upper()
    wl_q = db.query(Watchlist).options(joinedload(Watchlist.label)).filter(
        Watchlist.status == WatchlistStatus.WATCHING,
        Watchlist.organization_id == org_id
    )
    if label is not None:
        wl_q = wl_q.filter(Watchlist.label_id == label)
    if af == "ASX":
        wl_q = wl_q.filter(Watchlist.exchange_key == "ASX")
    elif af == "US":
        wl_q = wl_q.filter(Watchlist.exchange_key.in_(["NYSE", "NASDAQ"]))
    elif af == "CRYPTO":
        from sqlalchemy import or_ as _or_wlr
        wl_q = wl_q.filter(_or_wlr(
            Watchlist.asset_type == "CRYPTO",
            Watchlist.ticker.like("%-AUD"),
            Watchlist.ticker.like("%-USD"),
            Watchlist.ticker.like("%-USDT"),
        ))

    if _search_term:
        # Search the WHOLE filtered set, not just the current page's 20 rows —
        # ticker match (covers both equities like "MIN.AX" and crypto like
        # "BTC-USD") OR company-name match via a Stock subquery. Ranked in
        # Python so ticker hits always outrank company-name hits, THEN paged
        # the same way as the normal feed so a large match set still loads
        # incrementally via infinite scroll instead of one big payload.
        from sqlalchemy import or_ as _or_wlsearch
        _name_match_tickers = db.query(Stock.ticker).filter(Stock.name.ilike(f"%{_search_term}%"))
        wl_q = wl_q.filter(_or_wlsearch(
            Watchlist.ticker.ilike(f"%{_search_term}%"),
            Watchlist.ticker.in_(_name_match_tickers),
        ))
        total = wl_q.count()
        # Rank requires the full match set in hand before paging — cap at a
        # sane ceiling so one search term can't pull thousands of rows.
        _SEARCH_CAP = 500
        _all_matches = wl_q.order_by(desc(Watchlist.created_at)).limit(_SEARCH_CAP).all()

        def _rank(ticker: str) -> int:
            t = ticker.lower()
            if t == _search_term: return 0
            if t.startswith(_search_term): return 1
            if _search_term in t: return 2
            return 3  # matched only via company name
        _ranked_matches = sorted(_all_matches, key=lambda w: _rank(w.ticker))

        total = min(total, _SEARCH_CAP)
        items = _ranked_matches[(page - 1) * _WL_PER_PAGE: page * _WL_PER_PAGE]
        has_more = (page * _WL_PER_PAGE) < total
    elif _tier_filter:
        # setup_tier (A/B/C) is derived from rule_results JSON + RS rating,
        # not a stored column, so it can't be filtered in SQL. Pulling only
        # one DB-side page (20 most-recently-added rows) and THEN filtering
        # by tier — like the normal branch below does — means the filter
        # only ever sees whichever handful of that page happen to match,
        # which is why "★ A — Best" looked like it returned almost nothing.
        # Instead pull the whole exchange/label-filtered set (capped),
        # compute tier for all of it below, then filter + paginate in
        # Python — same approach as the search branch above.
        _TIER_CAP = 1000
        items = wl_q.order_by(desc(Watchlist.created_at)).limit(_TIER_CAP).all()
        total = None    # finalised after tier filter is applied, below
        has_more = None
    else:
        total = wl_q.count()
        items = wl_q.order_by(desc(Watchlist.created_at)).offset((page - 1) * _WL_PER_PAGE).limit(_WL_PER_PAGE).all()
        has_more = (page * _WL_PER_PAGE) < total

    stock_names = get_cached_stock_names(db)

    # Bulk PriceBar prefetch for this page's tickers
    _wl_tickers = list({w.ticker for w in items})
    _bar_lookup: dict[str, object] = {}
    if _wl_tickers:
        from sqlalchemy import func as _func
        _sub = (
            db.query(PriceBar.ticker, _func.max(PriceBar.date).label("max_date"))
            .filter(PriceBar.ticker.in_(_wl_tickers))
            .group_by(PriceBar.ticker)
            .subquery()
        )
        for _bar in db.query(PriceBar).join(_sub, (PriceBar.ticker == _sub.c.ticker) & (PriceBar.date == _sub.c.max_date)).all():
            _bar_lookup[_bar.ticker] = _bar

    _bar_lookup_dict: dict[str, dict] = {}
    for _tk, _bar in _bar_lookup.items():
        _bd = {
            "close":     float(_bar.close or 0),
            "ma_50":     float(_bar.ma_50 or 0),
            "ma_150":    float(_bar.ma_150 or 0),
            "ma_200":    float(_bar.ma_200 or 0),
            "high_52w":  float(_bar.high_52w or 0),
            "low_52w":   float(_bar.low_52w or 0),
            "rs_rating": float(_bar.rs_rating or 0),
            "vol_ratio": float(_bar.vol_ratio) if _bar.vol_ratio is not None else None,
        }
        _bar_lookup_dict[_tk] = _bd
        _ck = f"latest_price_bar:{_tk}"
        if not cache.get(_ck):
            cache.set(_ck, _bd, expire_seconds=300)

    # Build watchlist_data (same logic as main route, minus live crypto fetch for simplicity)
    watchlist_data = []
    for w in items:
        company_name = stock_names.get(w.ticker, "")
        if not company_name:
            _nck = f"missing_name_fetch:{w.ticker}"
            if not cache.get(_nck):
                cache.set(_nck, "attempted", expire_seconds=86400)

        # Build EOD bar dict — same two-step pattern as main route
        is_crypto_row = w.ticker.endswith(("-AUD", "-USD", "-USDT"))
        _eod = cache.get(f"latest_price_bar:{w.ticker}")
        if _eod is None:
            _b = _bar_lookup.get(w.ticker)
            _eod = {
                "close": float(_b.close or 0), "ma_50": float(_b.ma_50 or 0),
                "ma_150": float(_b.ma_150 or 0), "ma_200": float(_b.ma_200 or 0),
                "high_52w": float(_b.high_52w or 0), "low_52w": float(_b.low_52w or 0),
                "rs_rating": float(_b.rs_rating or 0),
                "vol_ratio": float(_b.vol_ratio) if _b.vol_ratio is not None else None,
                "bar_date": _fmt_date(_b.date) if _b.date else None, "live_price": False,
            } if _b else {}
        bar_data = dict(_eod) if _eod else {}
        # Overlay live price for crypto (preserves MA/RS/52W from EOD)
        if is_crypto_row and bar_data:
            _lc = cache.get(f"live_price:{w.ticker}")
            if _lc and not _lc.get("_failed"):
                _lv = float(_lc.get("close") or _lc.get("price") or 0)
                if _lv > 0:
                    bar_data["close"] = _lv
                    bar_data["live_price"] = _lv
        stats_data = bar_data if any(bar_data.values()) else None

        rr = w.rule_results or {}
        passed = sum(1 for v in rr.values() if v.get("passed"))
        lbl = None
        if w.label:
            lbl = {"id": w.label.id, "name": w.label.name, "color": w.label.color}

        # ── Setup quality tier (A/B/C) — Minervini prioritisation ────────────
        _wl_trend_keys = [k for k in rr if k.startswith("trend_")]
        _wl_trend_passed = sum(1 for k in _wl_trend_keys if (rr[k].get("passed") if isinstance(rr[k], dict) else bool(rr[k])))
        _wl_trend_total = len(_wl_trend_keys)
        _wl_rs  = bar_data.get("rs_rating", 0) if bar_data else 0
        _wl_vol = bar_data.get("vol_ratio")     if bar_data else None
        if (_wl_trend_total > 0 and _wl_trend_passed >= _wl_trend_total
                and _wl_rs >= 80
                and (_wl_vol is None or _wl_vol <= 0.6)):
            _wl_tier = "A"
        elif _wl_trend_total > 0 and _wl_trend_passed >= max(_wl_trend_total - 1, 1) and _wl_rs >= 70:
            _wl_tier = "B"
        else:
            _wl_tier = "C"

        watchlist_data.append({
            "id": w.id,
            "ticker": w.ticker,
            "exchange_key": getattr(w, "exchange_key", "ASX") or "ASX",
            "asset_type":   ("CRYPTO" if w.ticker.endswith(("-AUD","-USD","-USDT")) else getattr(w, "asset_type", "EQUITY") or "EQUITY"),
            "company_name": company_name,
            "added": _fmt_date(w.added_date),
            "by": w.added_by,
            "notes": w.notes or "",
            "stats": stats_data,
            "rules_passed": passed,
            "rules_total": len(rr),
            "rule_results": _enrich_rule_results(w.ticker, rr, db, _bar_data=_bar_lookup_dict.get(w.ticker)),
            "label": lbl,
            "setup_tier": _wl_tier,
            "currency": getattr(w, "currency", None),
            # Internal keys consumed (and stripped) by _enrich_watchlist_vcp_and_sizing.
            "_wl_id": w.id,
            "_latest_bar_date": (_bar_lookup[w.ticker].date if w.ticker in _bar_lookup else None),
            "_persisted": {
                "pivot": (float(w.pivot_price) if w.pivot_price is not None else None),
                "stop": (float(w.stop_price) if w.stop_price is not None else None),
                "target": (float(w.target_price) if w.target_price is not None else None),
                "contractions": w.vcp_contractions,
                "weeks": w.vcp_base_weeks,
                "computed_date": w.vcp_computed_date,
            },
        })

    _enrich_watchlist_vcp_and_sizing(watchlist_data, db, org_id)

    # ── Tier filter (post-compute, since setup_tier is derived not stored) ──
    if _tier_filter:
        watchlist_data = [w for w in watchlist_data if w.get("setup_tier") == _tier_filter]
        # Pagination was deferred until after the tier filter (see the
        # _tier_filter branch above) — apply it now against the FILTERED
        # count, so `total`/`has_more` reflect the actual tier subset
        # instead of the unfiltered exchange/label total.
        total = len(watchlist_data)
        has_more = (page * _WL_PER_PAGE) < total
        watchlist_data = watchlist_data[(page - 1) * _WL_PER_PAGE: page * _WL_PER_PAGE]

    # Favourites — same query as _global()
    try:
        _fav_res = db.query(Watchlist.ticker).join(
            WatchlistLabel, Watchlist.label_id == WatchlistLabel.id
        ).filter(
            Watchlist.organization_id == org_id,
            Watchlist.status == WatchlistStatus.WATCHING,
            WatchlistLabel.is_default == True
        ).all()
        favourited_tickers = {r[0] for r in _fav_res}
    except Exception:
        favourited_tickers = set()

    # Render to string, cache it, return
    _html_out = templates.get_template("components/watchlist_cards.html").render({
        "request": request,
        "watchlist": watchlist_data,
        "labels": labels,
        "favourited_tickers": favourited_tickers,
        "path": "/watchlist",
        "has_more": has_more,
        "page": page,
        "total": total,
        "active_label": label,
    })
    # Cache for 5 minutes; invalidated on add/remove/promote/screener-run.
    # Search results are never cached (see _search_term check above).
    if not _search_term:
        cache.set_raw(_html_ck, _html_out, expire_seconds=300)
    _elapsed_ms = (_time.monotonic() - _t0) * 1000
    if _elapsed_ms > 2000:
        _wl_log.warning(f"watchlist/rows SLOW ({_elapsed_ms:.0f}ms) org={org_id} exchange={_af} label={_lbl_key} tier={_tier_key} page={page} n={len(watchlist_data)} need_vcp={len([w for w in watchlist_data if not w.get('pivot')])}")
    else:
        _wl_log.debug(f"watchlist/rows ({_elapsed_ms:.0f}ms) org={org_id} exchange={_af} page={page} n={len(watchlist_data)}")
    return HTMLResponse(_html_out)


@app.post("/watchlist/labels/create")
async def watchlist_label_create(
    request: Request,
    name: str = Form(...),
    color: str = Form("#3b82f6"),
    db: Session = Depends(get_db)
):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")
    from app.models.signal import WatchlistLabel
    name = name.strip()[:64]
    if name:
        existing_count = db.query(WatchlistLabel).filter(
            WatchlistLabel.organization_id == org_id
        ).count()
        db.add(WatchlistLabel(
            organization_id=org_id,
            name=name,
            color=color,
            sort_order=existing_count,
        ))
        db.commit()
        cache.delete(f"wl_labels:{org_id}")
    return RedirectResponse("/watchlist?msg=label_created", 302)


@app.post("/watchlist/{item_id}/set-label")
async def watchlist_set_label(
    request: Request,
    item_id: int,
    label_id: str = Form(""),
    db: Session = Depends(get_db)
):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")
    from app.models.signal import Watchlist, WatchlistStatus
    w = db.query(Watchlist).filter(
        Watchlist.id == item_id,
        Watchlist.organization_id == org_id,
    ).first()
    if w:
        w.label_id = int(label_id) if label_id.isdigit() else None
        db.commit()
        cache.delete_prefix(f"wl_rows:{org_id}:")
        cache.delete(f"tw_data:{org_id}")
    return RedirectResponse("/watchlist", 302)


@app.post("/watchlist/toggle-favourite")
async def toggle_favourite(
    request: Request,
    ticker: str = Form(...),
    redirect_url: str = Form("/watchlist"),
    db: Session = Depends(get_db)
):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")
    from app.models.signal import Watchlist, WatchlistStatus, WatchlistLabel
    from app.models.market import Stock

    t = ticker.strip().upper()
    
    # Query Stock table to see if it's a known non-ASX or normalised ASX stock
    stock = db.query(Stock).filter(Stock.ticker == t).first()
    if not stock:
        # If not found, and it doesn't end with .AX and isn't crypto (no "-"), default to ASX format
        if not t.endswith(".AX") and not "-" in t:
            t = f"{t}.AX"
            stock = db.query(Stock).filter(Stock.ticker == t).first()

    exchange_key = stock.exchange_key if stock else "ASX"
    asset_type = stock.asset_type if stock else "EQUITY"
    currency = stock.currency if stock else "AUD"

    # Find or create Favourites label
    fav_label = db.query(WatchlistLabel).filter(
        WatchlistLabel.organization_id == org_id,
        WatchlistLabel.name == "Favourites"
    ).first()
    if not fav_label:
        fav_label = db.query(WatchlistLabel).filter(
            WatchlistLabel.organization_id == org_id,
            WatchlistLabel.is_default == True
        ).first()
    if not fav_label:
        fav_label = WatchlistLabel(
            organization_id=org_id,
            name="Favourites",
            color="#f59e0b",
            is_default=True,
            sort_order=0
        )
        db.add(fav_label)
        db.flush()

    w = db.query(Watchlist).filter(
        Watchlist.ticker == t,
        Watchlist.organization_id == org_id,
        Watchlist.status == WatchlistStatus.WATCHING
    ).first()

    if w:
        if w.label_id == fav_label.id:
            # Currently favourited -> Unfavourite
            if w.added_by == "admin":
                w.status = WatchlistStatus.REMOVED
            else:
                w.label_id = None
        else:
            # In watchlist but with other label -> Change to Favourites
            w.label_id = fav_label.id
    else:
        # Not in watchlist -> Create new manual watchlist item with Favourites label
        w = Watchlist(
            ticker=t,
            exchange_key=exchange_key,
            asset_type=asset_type,
            currency=currency,
            organization_id=org_id,
            status=WatchlistStatus.WATCHING,
            label_id=fav_label.id,
            added_by="admin"
        )
        db.add(w)

    db.commit()
    cache.delete_prefix(f"wl_rows:{org_id}:")
    cache.delete(f"tw_data:{org_id}")
    return RedirectResponse(redirect_url, 302)


@app.post("/action/positions/{pos_id}/clear-checks")
async def positions_clear_checks(request: Request, pos_id: int, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")
    
    from app.models.audit import AuditLog, AuditAction
    db.query(AuditLog).filter(
        AuditLog.organization_id == org_id,
        AuditLog.action == AuditAction.TASK_RUN,
        AuditLog.entity_type == "Position",
        AuditLog.entity_id == str(pos_id)
    ).delete(synchronize_session=False)
    db.commit()
    
    return RedirectResponse("/positions?msg=checks_cleared", 302)


@app.post("/positions/{pos_id}/close")
async def close_position(
    request: Request,
    pos_id: int,
    exit_reason: str = Form(...),
    exit_price: float = Form(None),
    db: Session = Depends(get_db),
):
    """
    Manually close an open position using AstraTrade exit rules.
    Records a Trade, marks Position CLOSED, writes audit, sends Telegram alert.
    If exit_price is not provided, the last known current_price is used.
    """
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")

    from app.models.trade import Position, Trade, TradeStatus, ExitReason, OrderAction, OrderType, OrderStatus, Order
    from app.models.audit import AuditLog, AuditAction
    from app.models.account import Account

    pos = db.query(Position).filter(
        Position.id == pos_id,
        Position.organization_id == org_id,
        Position.status == TradeStatus.OPEN,
    ).first()
    if not pos:
        return RedirectResponse("/positions?msg=not_found", 302)

    today = get_current_date()
    close_price = exit_price if (exit_price and exit_price > 0) else float(pos.current_price or pos.entry_price)
    entry_price = float(pos.entry_price)
    pnl_aud = (close_price - entry_price) * float(pos.qty or 0)
    pnl_pct = (close_price - entry_price) / entry_price * 100 if entry_price else 0

    # Validate exit reason
    try:
        reason = ExitReason[exit_reason]
    except KeyError:
        reason = ExitReason.MANUAL

    # Mark position closed
    pos.status = TradeStatus.CLOSED

    # Create closed trade record
    trade = Trade(
        ticker=pos.ticker,
        account_id=pos.account_id,
        organization_id=org_id,
        signal_id=pos.signal_id,
        entry_date=pos.entry_date,
        exit_date=today,
        hold_days=(today - pos.entry_date).days,
        entry_price=pos.entry_price,
        exit_price=close_price,
        qty=pos.qty,
        gross_pnl_aud=round(pnl_aud, 2),
        net_pnl_aud=round(pnl_aud - 6.0, 2),   # deduct commission
        pnl_pct=round(pnl_pct / 100, 4),
        initial_stop=pos.initial_stop,
        exit_reason=reason,
        is_paper=pos.is_paper,
        cgt_eligible_discount=(today - pos.entry_date).days > 365,
    )
    db.add(trade)

    db.add(AuditLog(
        action=AuditAction.POSITION_CLOSED,
        organization_id=org_id,
        user_id=request.session.get("user_id"),
        ticker=pos.ticker,
        message=f"Manual close: {reason.value} @ ${close_price:.3f} | P&L ${pnl_aud:+.0f} ({pnl_pct:+.1f}%)",
        detail={"reason": reason.value, "exit_price": close_price, "pnl_aud": round(pnl_aud, 2)},
        actor=request.session.get("email","dashboard"),
    ))
    db.commit()

    # Notification alert in background
    try:
        from app.tasks.reporting import send_notification_message
        send_notification_message.delay(
            org_id,
            "send_exit_alert",
            [pos.ticker, reason.value, pnl_pct, pnl_aud, pos.is_paper]
        )
    except Exception as e:
        from loguru import logger
        logger.error(f"Failed to queue exit alert notification: {e}")

    return RedirectResponse("/positions?msg=closed", 302)


@app.post("/positions/{pos_id}/purge")
async def purge_phantom_position(
    request: Request,
    pos_id: int,
    db: Session = Depends(get_db),
):
    """
    Superadmin-only: hard-delete a Position row that was never a real trade for
    its org — e.g. one imported by the cross-org IBKR account fallback bug
    (see IBKRBroker.connect() and CLAUDE.md). Unlike /positions/{pos_id}/close,
    this does NOT create a Trade record — a phantom position was never actually
    opened or closed by that org, so recording a "closed trade" for it would
    put a fake entry in the org's own P&L/CGT history. Positions has no
    incoming foreign keys, so a hard delete is safe.

    Looked up by position ID alone (not restricted to the session's currently
    active org) — this is the cleanup action for the cross-org phantom-import
    bug, where the whole point is a superadmin can purge affected positions
    across every org from one report (see /superadmin/phantom-positions)
    without switching active org context for each one. Still superadmin-gated,
    so this doesn't widen access — a non-superadmin can never reach this route.
    """
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/positions?msg=forbidden", 302)

    from app.models.trade import Position
    from app.models.audit import AuditLog, AuditAction

    pos = db.query(Position).filter(Position.id == pos_id).first()
    if not pos:
        return RedirectResponse("/positions?msg=not_found", 302)

    detail = {
        "ticker": pos.ticker, "qty": float(pos.qty or 0),
        "entry_price": float(pos.entry_price or 0), "entry_date": str(pos.entry_date),
        "is_paper": pos.is_paper, "status": pos.status.value if pos.status else None,
    }
    db.add(AuditLog(
        action=AuditAction.MANUAL_OVERRIDE,
        organization_id=pos.organization_id,
        user_id=request.session.get("user_id"),
        ticker=pos.ticker,
        message=f"Phantom position purged (not a real trade for this org) — {pos.ticker} qty={detail['qty']}",
        detail=detail,
        actor=request.session.get("email", "superadmin"),
    ))
    db.delete(pos)
    db.commit()

    referer = request.headers.get("referer", "/positions")
    sep = "&" if "?" in referer else "?"
    return RedirectResponse(f"{referer}{sep}msg=purged", 303)


@app.post("/watchlist/add")
async def watchlist_add(
    request: Request,
    ticker: str = Form(...),
    notes: str = Form(""),
    label_id: str = Form(""),
    exchange_key: str = Form("ASX"),
    db: Session = Depends(get_db),
):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")

    t = ticker.strip().upper()
    ek = exchange_key.strip() or "ASX"

    # Normalise ticker for the exchange — do NOT blindly append .AX for non-ASX
    if ek == "ASX":
        if not t.endswith(".AX"):
            t += ".AX"
    # For all other exchanges (US, CRYPTO_*) pass the raw symbol through;
    # screen_single_ticker / normalize_ticker handles the correct suffix.

    # Determine asset_type so the task knows what rules to apply
    is_crypto = ek == "CRYPTO" or ek.startswith("CRYPTO_")
    asset_type = "CRYPTO" if is_crypto else "EQUITY"
    currency   = "AUD" if ek == "ASX" else ("AUD" if ek == "CRYPTO_INDEPENDENTRESERVE" else "USD")

    lbl_id = int(label_id) if label_id.isdigit() else None
    from app.tasks.screening import screen_single_ticker
    screen_single_ticker.delay(
        t, notes,
        organization_id=org_id,
        label_id=lbl_id,
        exchange_key=ek,
        asset_type=asset_type,
        currency=currency,
    )
    from app.models.audit import AuditLog, AuditAction
    db.add(AuditLog(
        action=AuditAction.CONFIG_CHANGED,
        actor=request.session.get("email", "user"),
        user_id=request.session.get("user_id"),
        organization_id=org_id,
        ticker=t,
        message=f"Added {t} to watchlist"
    ))
    db.commit()
    cache.delete_prefix(f"wl_rows:{org_id}:")
    cache.delete(f"tw_data:{org_id}")
    return RedirectResponse("/watchlist?msg=added", 302)


@app.post("/watchlist/{item_id}/promote")
async def watchlist_promote(request: Request, item_id: int, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.models.signal import Watchlist, WatchlistStatus
    
    w = db.query(Watchlist).filter(Watchlist.id == item_id, Watchlist.organization_id == org_id).first()
    if not w:
        return RedirectResponse("/watchlist?msg=not_found", 302)

    # Refuse promotion while an open position exists — the signal could never
    # trigger (entry check skips held tickers) and would just sit PENDING.
    from app.models.trade import Position, TradeStatus
    open_pos = db.query(Position).filter(
        Position.ticker == w.ticker,
        Position.organization_id == org_id,
        Position.status == TradeStatus.OPEN,
    ).first()
    if open_pos:
        from app.models.audit import AuditLog, AuditAction
        db.add(AuditLog(
            action=AuditAction.TASK_ERROR,
            actor=request.session.get("email", "dashboard"),
            user_id=request.session.get("user_id"),
            organization_id=org_id,
            ticker=w.ticker,
            message=f"Promotion of {w.ticker} refused — an open position already exists for this ticker",
        ))
        db.commit()
        return RedirectResponse("/watchlist?msg=position_open", 302)

    # Refuse promotion while a live signal already exists for this ticker —
    # otherwise two Watchlist rows for the same ticker (e.g. re-added after an
    # earlier promotion never triggered/expired) can each be promoted into
    # their own duplicate PENDING signal. Mirrors promote_watchlist_item_task's
    # dedup query exactly (any signal today, or any still-PENDING/TRIGGERED
    # signal regardless of date) so this fast-fail check and the authoritative
    # Celery-task check never disagree.
    from app.models.signal import Signal, SignalStatus
    existing_signal = db.query(Signal).filter(
        Signal.ticker == w.ticker,
        Signal.organization_id == org_id,
        or_(
            Signal.signal_date == get_current_date(),
            Signal.status.in_([SignalStatus.PENDING, SignalStatus.TRIGGERED]),
        ),
    ).first()
    if existing_signal:
        from app.models.audit import AuditLog, AuditAction
        db.add(AuditLog(
            action=AuditAction.TASK_ERROR,
            actor=request.session.get("email", "dashboard"),
            user_id=request.session.get("user_id"),
            organization_id=org_id,
            ticker=w.ticker,
            message=(f"Promotion of {w.ticker} refused — signal #{existing_signal.id} "
                     f"(status={existing_signal.status.value}, from {existing_signal.signal_date}) "
                     f"already exists for this ticker"),
        ))
        db.commit()
        return RedirectResponse("/watchlist?msg=signal_exists", 302)

    # Immediately mark as signalled/processing to prevent double triggers
    w.status = WatchlistStatus.SIGNALLED
    db.commit()

    # Queue the slow parts in Celery.
    # IMPORTANT: if queuing fails (Redis/worker outage, broker error) we MUST revert the
    # status above — otherwise the item silently disappears from "Watching" (the /watchlist
    # view filters status==WATCHING) with no Signal ever created and no feedback to the user.
    # This is exactly what was happening with TRX: the optimistic flip to SIGNALLED was never
    # rolled back when the background task could not run (worker offline — heartbeat stale),
    # so the item vanished from the Watchlist and nothing ever appeared in Signals.
    try:
        from app.tasks.trading import promote_watchlist_item_task
        promote_watchlist_item_task.delay(
            item_id,
            org_id,
            request.session.get("email", "dashboard"),
            request.session.get("user_id")
        )
        from app.models.audit import AuditLog, AuditAction
        db.add(AuditLog(
            action=AuditAction.CONFIG_CHANGED,
            actor=request.session.get("email", "dashboard"),
            user_id=request.session.get("user_id"),
            organization_id=org_id,
            ticker=w.ticker,
            message=f"Manual promotion of {w.ticker} queued successfully"
        ))
        db.commit()
    except Exception as e:
        from loguru import logger
        from app.models.signal import Watchlist as _Watchlist
        from app.models.audit import AuditLog, AuditAction
        logger.error(f"Failed to queue watchlist promotion for item {item_id} ({w.ticker}): {e}")
        # Roll back the optimistic status flip so the item stays visible/actionable
        w2 = db.query(_Watchlist).filter(_Watchlist.id == item_id, _Watchlist.organization_id == org_id).first()
        if w2:
            w2.status = WatchlistStatus.WATCHING
            db.add(AuditLog(
                action=AuditAction.TASK_ERROR,
                ticker=w2.ticker,
                actor=request.session.get("email", "dashboard"),
                user_id=request.session.get("user_id"),
                organization_id=org_id,
                message=f"Manual promotion of {w2.ticker} failed to queue (worker/broker unavailable) — reverted to WATCHING. Error: {e}",
            ))
            db.commit()
        return RedirectResponse("/watchlist?msg=promotion_failed", 302)

    cache.delete_prefix(f"wl_rows:{org_id}:")
    cache.delete_prefix(f"sig_items:{org_id}:")
    cache.delete(f"tw_data:{org_id}")
    return RedirectResponse("/signals?msg=promotion_queued", 302)


@app.post("/watchlist/{item_id}/remove")
async def watchlist_remove(request: Request, item_id: int, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.models.signal import Watchlist, WatchlistStatus
    item = db.query(Watchlist).filter(Watchlist.id == item_id, Watchlist.organization_id == org_id).first()
    if item:
        item.status = WatchlistStatus.REMOVED
        from app.models.audit import AuditLog, AuditAction
        db.add(AuditLog(
            action=AuditAction.CONFIG_CHANGED,
            actor=request.session.get("email", "user"),
            user_id=request.session.get("user_id"),
            organization_id=org_id,
            ticker=item.ticker,
            message=f"Removed {item.ticker} from watchlist"
        ))
        db.commit()
        cache.delete_prefix(f"wl_rows:{org_id}:")
        cache.delete(f"tw_data:{org_id}")
    return RedirectResponse("/watchlist", 302)


# System action endpoints
def _queue_redirect(queue_fn, ok_url: str, fail_url: str = None):
    """
    Queue a Celery task/chain from a dashboard action route and redirect.

    Every action route used to swallow .delay() failures (try/except: pass)
    and still redirect with a SUCCESS banner — with Redis/the broker down,
    buttons silently did nothing. A failed .delay() means the message never
    reached the broker (it will NOT queue itself later), so surface it:
    redirect with ?msg=queue_failed and the templates render an error alert.
    """
    try:
        queue_fn()
        return RedirectResponse(ok_url, 302)
    except Exception as exc:
        logger.error(f"Task queue failed (broker unreachable?): {exc}")
        base = (fail_url or ok_url).split("?")[0]
        return RedirectResponse(f"{base}?msg=queue_failed", 302)


@app.post("/action/pause")
async def action_pause(request: Request, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.models.config import SystemConfig
    from app.models.audit import AuditLog, AuditAction
    cfg = db.query(SystemConfig).filter(SystemConfig.key == "trading_paused", SystemConfig.organization_id == org_id).first()
    if cfg:
        cfg.value = "true"
        cfg.updated_by = "dashboard"
    db.add(AuditLog(action=AuditAction.TRADING_PAUSED, actor=request.session.get("email","dashboard"), user_id=request.session.get("user_id"), organization_id=org_id))
    db.commit()
    return RedirectResponse("/", 302)


@app.post("/action/resume")
async def action_resume(request: Request, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.models.config import SystemConfig
    from app.models.audit import AuditLog, AuditAction
    cfg = db.query(SystemConfig).filter(SystemConfig.key == "trading_paused", SystemConfig.organization_id == org_id).first()
    if cfg:
        cfg.value = "false"
        cfg.updated_by = "dashboard"
    db.add(AuditLog(action=AuditAction.TRADING_RESUMED, actor=request.session.get("email","dashboard"), user_id=request.session.get("user_id"), organization_id=org_id))
    db.commit()
    return RedirectResponse("/", 302)


@app.post("/action/run-screener")
async def action_run_screener(request: Request, exchange: str = Form("ASX")):
    """Queue the screener for the current org only — bypasses trading-day gate."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")
    from app.tasks.screening import _run_screen_force
    cache.delete_prefix(f"wl_rows:{org_id}:")
    cache.delete_prefix(f"sig_items:{org_id}:")
    cache.delete(f"tw_data:{org_id}")
    return _queue_redirect(
        lambda: _run_screen_force.delay(organization_id=org_id, exchange_key=exchange or "ASX"),
        "/signals?msg=screen",
    )


@app.post("/action/send-report")
async def action_send_report(request: Request):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")
    from app.tasks.reporting import send_daily_report
    return _queue_redirect(
        lambda: send_daily_report.delay(organization_id=org_id),
        "/", fail_url="/admin/health",
    )


@app.post("/action/evaluate-regime")
async def action_evaluate_regime(request: Request, exchange: str = Form("ASX")):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    from app.tasks.screening import evaluate_market_regime_task
    return _queue_redirect(
        lambda: evaluate_market_regime_task.delay(exchange_key=exchange or "ASX"),
        "/admin/health?msg=regime",
    )


@app.post("/action/ping-worker")
async def action_ping_worker(request: Request):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    from app.tasks.reporting import health_check
    return _queue_redirect(lambda: health_check.delay(), "/admin/health?msg=ping")


@app.post("/action/refresh-data")
async def action_refresh_data(request: Request, exchange: str = Form(None)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    exchange_key = exchange or None
    is_crypto = exchange_key and (exchange_key == "CRYPTO" or exchange_key.startswith("CRYPTO_"))
    from app.tasks.screening import refresh_price_data, refresh_crypto_universe

    def _queue():
        if is_crypto:
            # For crypto, chain: universe bootstrap → price refresh (bootstrap is fast if already seeded)
            from celery import chain as _chain
            effective_crypto_key = exchange_key if exchange_key != "CRYPTO" else "CRYPTO_INDEPENDENTRESERVE"
            _chain(
                refresh_crypto_universe.si(exchange_key=effective_crypto_key),
                refresh_price_data.si(exchange_key=effective_crypto_key),
            ).delay()
        else:
            refresh_price_data.delay(exchange_key=exchange_key)

    # NOTE: the old fallback re-called refresh_price_data.delay() inside the
    # except block — with the broker down that second .delay() raised too,
    # turning a queue failure into an unhandled 500.
    return _queue_redirect(_queue, "/admin/health?msg=data")


@app.post("/action/refresh-fundamentals")
async def action_refresh_fundamentals(request: Request, exchange: str = Form(None),
                                      force: str = Form(None)):
    """Manually queue a throttled Stock Story (fundamentals) refresh for equities."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    exchange_key = exchange or None
    from app.tasks.screening import refresh_stock_fundamentals
    return _queue_redirect(
        lambda: refresh_stock_fundamentals.delay(
            exchange_key=exchange_key,
            force=(str(force).lower() in ("1", "true", "on", "yes")),
        ),
        "/admin/health?msg=fundamentals",
    )


@app.get("/stock-story/{ticker:path}")
async def stock_story(request: Request, ticker: str, db: Session = Depends(get_db)):
    """
    Return the persisted CommSec-style Stock Story for a ticker as JSON.

    Reads the shared `stock_fundamentals` table. If the row is missing or stale
    it is fetched on-demand (one ticker = one yfinance fetch — cheap and only
    happens the first time a story is opened). Live price-derived figures
    (last price, 1Y sparkline + performance) are merged in from the local
    `price_bars` table so they stay current without re-hitting yfinance.
    """
    if not _auth(request):
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    try:
        from app.models.market import StockFundamentals, PriceBar, Stock
        from app.tasks.screening import upsert_stock_story, STORY_STALE_DAYS_DEFAULT
        from datetime import datetime as _dt, timedelta as _td

        ticker = (ticker or "").strip()
        if not ticker:
            return JSONResponse({"error": "no ticker"}, status_code=400)

        # Resolve stock metadata (for exchange/asset/currency on first fetch)
        stock = db.query(Stock).filter(Stock.ticker == ticker).first()
        exch = stock.exchange_key if stock else "ASX"
        atype = stock.asset_type if stock else "EQUITY"
        cur = stock.currency if stock else "AUD"

        row = db.query(StockFundamentals).filter(StockFundamentals.ticker == ticker).first()

        # On-demand fetch if missing or stale (stale gate prevents repeat fetches)
        need_fetch = row is None or row.fetched_at is None or (
            row.fetched_at < _dt.utcnow() - _td(days=STORY_STALE_DAYS_DEFAULT)
        )
        if need_fetch:
            try:
                upsert_stock_story(ticker, db, exchange_key=exch, asset_type=atype,
                                   currency=cur, force=True)
                db.commit()
                row = db.query(StockFundamentals).filter(
                    StockFundamentals.ticker == ticker).first()
            except Exception as _fx:
                db.rollback()
                logger.warning(f"On-demand stock story fetch failed for {ticker}: {_fx}")

        payload = dict(row.data) if (row and row.data) else {"ticker": ticker, "unavailable": True}
        payload["ticker"] = ticker
        payload["display_code"] = (stock.exchange_code if stock else
                                   ticker.replace(".AX", "").replace("-USD", "")
                                         .replace("-USDT", "").replace("-AUD", ""))
        payload["exchange_key"] = exch
        payload["asset_type"] = atype
        if not payload.get("currency"):
            payload["currency"] = cur
        payload["fetched_at"] = (row.fetched_at.isoformat()
                                 if (row and row.fetched_at) else None)

        # ── Merge live price series from local price_bars (last ~1yr) ─────────
        bars = (db.query(PriceBar.date, PriceBar.close, PriceBar.high_52w,
                         PriceBar.low_52w, PriceBar.ma_50, PriceBar.ma_150,
                         PriceBar.ma_200, PriceBar.rs_rating)
                .filter(PriceBar.ticker == ticker)
                .order_by(PriceBar.date.desc())
                .limit(260).all())
        bars = list(reversed(bars))
        spark = [{"date": str(b[0]), "close": float(b[1])}
                 for b in bars if b[1] is not None]
        last_price = spark[-1]["close"] if spark else None
        perf_1y = None
        if len(spark) >= 2 and spark[0]["close"]:
            perf_1y = (spark[-1]["close"] - spark[0]["close"]) / spark[0]["close"] * 100.0
        latest = bars[-1] if bars else None
        payload["price"] = {
            "last": last_price,
            "perf_1y_pct": perf_1y,
            "sparkline": spark,
            "high_52w": float(latest[2]) if latest and latest[2] is not None else None,
            "low_52w": float(latest[3]) if latest and latest[3] is not None else None,
            "ma_50": float(latest[4]) if latest and latest[4] is not None else None,
            "ma_150": float(latest[5]) if latest and latest[5] is not None else None,
            "ma_200": float(latest[6]) if latest and latest[6] is not None else None,
            "rs_rating": float(latest[7]) if latest and latest[7] is not None else None,
        }

        # ── VCP pattern analysis (recomputed locally) + org-config rule scorecard ─
        org_id = request.session.get("organization_id")
        try:
            payload["vcp"] = _build_vcp_analysis(ticker, exch, atype, org_id, db)
        except Exception as _vex:
            logger.warning(f"VCP analysis failed for {ticker}: {_vex}")
            payload["vcp"] = {"available": False, "reason": "VCP analysis unavailable."}
        try:
            payload["rules"] = _build_rule_breakdown(ticker, org_id, db)
        except Exception as _rex:
            logger.warning(f"Rule breakdown failed for {ticker}: {_rex}")
            payload["rules"] = {"available": False, "reason": "Rule breakdown unavailable."}

        return JSONResponse(payload)
    except Exception as exc:
        import traceback
        try:
            db.rollback()
        except Exception:
            pass
        logger.error(f"stock-story failed: {exc}\n{traceback.format_exc()}")
        from app.config import settings
        body = {"error": str(exc)}
        if settings.app_env == "development":
            body["trace"] = traceback.format_exc()
        return JSONResponse(body, status_code=500)


@app.post("/action/refresh-universe")
async def action_refresh_universe(request: Request, scope: str = Form(None)):
    """Refresh ASX universe with configurable scope (ASX200 / ASX300 / ALL_LISTED)."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    organization_id = request.session.get("organization_id")
    from app.tasks.screening import refresh_universe
    return _queue_redirect(
        lambda: refresh_universe.delay(scope=scope or None, organization_id=organization_id),
        "/admin/health?msg=universe",
    )


@app.post("/action/seed-us-universe")
async def action_seed_us_universe(request: Request, scope: str = Form(None)):
    """Seed or refresh the US equity universe (S&P 500 / NASDAQ-100 / both)."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    organization_id = request.session.get("organization_id")
    from app.tasks.screening import refresh_us_universe
    return _queue_redirect(
        lambda: refresh_us_universe.delay(scope=scope or None, organization_id=organization_id),
        "/admin/health?msg=universe_us",
    )


@app.post("/action/recategorise-labels")
async def action_recategorise_labels(request: Request, force: str = Form("0"),
                                     market: str = Form("ALL")):
    """
    Bulk-assign sector/category labels to watchlist items, scoped to the
    selected market (ALL / ASX / US / CRYPTO).

    When ASX is in scope, chains refresh_asx_sector_data first so ASX stocks
    get a precise GICS industry-group string backfilled from the ASX's own
    export before the keyword/override/crypto classifier in
    recategorise_watchlist_labels runs — without this, most ASX stocks only
    ever carry the coarse Level-1 sector ("Financials") and can't be
    distinguished (Banks vs Insurance vs Fund Managers etc.). For US/CRYPTO
    runs the GICS backfill is skipped entirely — it's ASX-only data and just
    adds a slow network fetch for nothing.
    """
    if not _auth(request):
        return RedirectResponse("/login", 302)
    organization_id = request.session.get("organization_id")
    market = (market or "ALL").strip().upper()
    if market not in ("ALL", "ASX", "US", "CRYPTO"):
        market = "ALL"
    from celery import chain as _chain
    from app.tasks.screening import recategorise_watchlist_labels, refresh_asx_sector_data

    def _queue():
        if market in ("ALL", "ASX"):
            _chain(
                refresh_asx_sector_data.si(organization_id=organization_id),
                recategorise_watchlist_labels.si(organization_id=organization_id,
                                                 force=(force == "1"), market=market),
            ).delay()
        else:
            recategorise_watchlist_labels.delay(organization_id=organization_id,
                                                force=(force == "1"), market=market)

    return _queue_redirect(_queue, "/admin/health?msg=recategorise")


@app.post("/action/seed-crypto")
async def action_seed_crypto(request: Request, exchange: str = Form("CRYPTO_INDEPENDENTRESERVE")):
    """Seed (or refresh) the crypto stock universe for an exchange."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    from app.tasks.screening import refresh_crypto_universe
    return _queue_redirect(
        lambda: refresh_crypto_universe.delay(exchange_key=exchange),
        "/admin/health?msg=crypto_seed",
    )


@app.post("/action/full-setup")
async def action_full_setup(request: Request):
    """First-time setup: universe → price data → regime → screen. Runs as a Celery chain."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    from app.tasks.screening import run_full_setup
    return _queue_redirect(lambda: run_full_setup.delay(), "/admin/tasks?msg=setup")


@app.post("/action/dismiss-onboarding")
async def action_dismiss_onboarding(request: Request, db: Session = Depends(get_db)):
    """Dismiss onboarding guide by setting onboarding_completed to true."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")
    if org_id:
        from app.models.config import SystemConfig
        cfg = db.query(SystemConfig).filter(
            SystemConfig.key == "onboarding_completed",
            SystemConfig.organization_id == org_id
        ).first()
        if cfg:
            cfg.value = "true"
        else:
            cfg = SystemConfig(
                key="onboarding_completed",
                value="true",
                organization_id=org_id,
                value_type="BOOLEAN",
                label="Onboarding Completed",
                description="Whether the organization has completed first-time setup",
                group="general"
            )
            db.add(cfg)
        db.commit()
    return RedirectResponse("/", 302)


@app.post("/action/force-screen")
async def action_force_screen(request: Request, exchange: str = Form("ASX")):
    """Run screener for current org now, bypassing the trading-day gate."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")
    from app.tasks.screening import _run_screen_force
    cache.delete_prefix(f"wl_rows:{org_id}:")
    cache.delete_prefix(f"sig_items:{org_id}:")
    cache.delete(f"tw_data:{org_id}")
    return _queue_redirect(
        lambda: _run_screen_force.delay(organization_id=org_id, exchange_key=exchange or "ASX"),
        "/signals?msg=screen",
    )


@app.post("/action/force-screen-async")
async def action_force_screen_async(
    request: Request, exchange: str = Form("ASX"), db: Session = Depends(get_db)
):
    """AJAX version of force-screen: queues task and returns JSON with last_audit_id
    so the dashboard widget can start polling /admin/tasks/poll?after={last_id}."""
    from fastapi.responses import JSONResponse
    from sqlalchemy import func
    if not _auth(request):
        return JSONResponse({"ok": False, "error": "unauthenticated"}, status_code=401)
    org_id = request.session.get("organization_id")
    # Get highest current audit ID so widget only sees events from this run
    from app.models.audit import AuditLog
    last_id = db.query(func.max(AuditLog.id)).filter(
        AuditLog.organization_id == org_id
    ).scalar() or 0
    try:
        from app.tasks.screening import _run_screen_force
        _run_screen_force.delay(organization_id=org_id, exchange_key=exchange or "ASX")
        cache.delete_prefix(f"wl_rows:{org_id}:")
        cache.delete(f"tw_data:{org_id}")
        cache.delete_prefix(f"sig_items:{org_id}:")
        return JSONResponse({"ok": True, "last_id": last_id, "exchange": exchange})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/action/force-breakout-check")
async def action_force_breakout_check(request: Request, exchange: str = Form("ASX")):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    from app.tasks.trading import check_entry_triggers
    return _queue_redirect(
        lambda: check_entry_triggers.delay(exchange_key=exchange or "ASX"),
        "/admin/tasks?msg=breakout",
    )


@app.post("/action/force-exit-check")
async def action_force_exit_check(request: Request, exchange: str = Form("ASX")):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    from app.tasks.trading import check_exit_rules_task
    return _queue_redirect(
        lambda: check_exit_rules_task.delay(exchange_key=exchange or "ASX"),
        "/admin/tasks?msg=exit_check",
    )


@app.post("/action/force-position-sync")
async def action_force_position_sync(request: Request):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    from app.tasks.trading import sync_ibkr_positions_task
    return _queue_redirect(
        lambda: sync_ibkr_positions_task.delay(),
        "/admin/tasks?msg=positions",
    )


@app.post("/action/force-stop-sync")
async def action_force_stop_sync(request: Request):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    from app.tasks.trading import sync_stop_orders
    return _queue_redirect(
        lambda: sync_stop_orders.delay(),
        "/admin/tasks?msg=stops",
    )



# ===========================================================================
# TRADER TERMINAL
# ===========================================================================

@app.get("/trader", response_class=HTMLResponse)
async def trader_view(request: Request, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login")
    return templates.TemplateResponse("trading/trader.html", {
        "request": request,
        **_global(request, db),
    })


@app.get("/trader/data")
async def trader_data(request: Request, db: Session = Depends(get_db)):
    """Full data payload for the trader terminal — initial load + 30s refresh."""
    if not _auth(request):
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    try:
        return await _trader_data_inner(request, db)
    except Exception as exc:
        import traceback
        trace = traceback.format_exc()
        try:
            db.rollback()  # leave session clean so get_db teardown commit doesn't re-raise
        except Exception:
            pass
        logger.error(f"trader/data failed: {exc}\n{trace}")
        from app.config import settings
        body = {"error": str(exc)}
        if settings.app_env == "development":
            body["trace"] = trace
        return JSONResponse(body, status_code=500)


async def _trader_data_inner(request: Request, db):
    from sqlalchemy import func, desc
    from sqlalchemy.orm import joinedload
    from app.models.signal import Watchlist, WatchlistStatus, Signal, SignalStatus
    from app.models.trade import Position, TradeStatus
    from app.models.market import PriceBar, EntryCheckLog
    from app.models.config import SystemConfig
    from app.models.account import Account
    from app.models.exchange import ExchangeConfig

    org_id = request.session.get("organization_id")
    display_tz = _get_display_tz(org_id, db)

    def _get_next_entry_check(exk: str, tz_name: str) -> str:
        import pytz
        from datetime import datetime as dt_class, timedelta
        try:
            sydney_tz = pytz.timezone("Australia/Sydney")
            now_sydney = dt_class.now(sydney_tz)
            candidate = now_sydney.replace(second=0, microsecond=0)
            limit = 10080
            found = False
            for _ in range(limit):
                candidate += timedelta(minutes=1)
                if candidate.minute % 5 != 0:
                    continue
                if exk == "CRYPTO":
                    found = True
                    break
                elif exk == "NYSE":
                    day = candidate.weekday()
                    hour = candidate.hour
                    if (hour == 23 and day in (0, 1, 2, 3, 4)) or (hour in (0, 1, 2, 3, 4, 5, 6) and day in (1, 2, 3, 4, 5)):
                        found = True
                        break
                else: # ASX
                    day = candidate.weekday()
                    hour = candidate.hour
                    if hour in range(10, 17) and day in (0, 1, 2, 3, 4):
                        found = True
                        break
            if found:
                display_tz_obj = pytz.timezone(tz_name)
                candidate_display = candidate.astimezone(display_tz_obj)
                return _friendly_dt_str(candidate_display)
        except Exception:
            pass
        return "TBD"

    # ── Watchlist ──
    wl_items = (
        db.query(Watchlist)
        .options(joinedload(Watchlist.label))
        .filter(
            Watchlist.organization_id == org_id,
            Watchlist.status == WatchlistStatus.WATCHING,
        )
        .order_by(Watchlist.created_at.desc())
        .limit(1000)
        .all()
    )
    wl_tickers = [w.ticker for w in wl_items]

    # ── Pending signals ──
    signals = (
        db.query(Signal)
        .filter(
            Signal.organization_id == org_id,
            Signal.status == SignalStatus.PENDING,
        )
        .order_by(Signal.signal_date.desc())
        .limit(50)
        .all()
    )

    # ── Open positions (pre-fetch here to use in price_map too) ──
    positions = (
        db.query(Position)
        .filter(Position.organization_id == org_id, Position.status == TradeStatus.OPEN)
        .all()
    )

    # ── Latest price bar — watchlist + signal + position tickers ──
    price_map: dict = {}
    all_tickers = list(set(wl_tickers)
                       | {s.ticker for s in signals}
                       | {p.ticker for p in positions})
    if all_tickers:
        latest_sq = (
            db.query(PriceBar.ticker, func.max(PriceBar.date).label("mx"))
            .filter(PriceBar.ticker.in_(all_tickers))
            .group_by(PriceBar.ticker)
            .subquery()
        )
        bars = (
            db.query(PriceBar)
            .join(latest_sq, (PriceBar.ticker == latest_sq.c.ticker) & (PriceBar.date == latest_sq.c.mx))
            .all()
        )
        price_map = {b.ticker: b for b in bars}

    # ── Latest entry-check per signal ──
    check_map: dict = {}
    if signals:
        sig_ids = [s.id for s in signals]
        chk_sq = (
            db.query(EntryCheckLog.signal_id, func.max(EntryCheckLog.checked_at).label("mx"))
            .filter(EntryCheckLog.organization_id == org_id, EntryCheckLog.signal_id.in_(sig_ids))
            .group_by(EntryCheckLog.signal_id)
            .subquery()
        )
        checks = (
            db.query(EntryCheckLog)
            .join(chk_sq, (EntryCheckLog.signal_id == chk_sq.c.signal_id) &
                  (EntryCheckLog.checked_at == chk_sq.c.mx))
            .all()
        )
        check_map = {c.signal_id: c for c in checks}

    # ── Market regimes ──
    regime_keys = [
        "last_market_regime_ASX", "last_market_regime_NYSE",
        "last_market_regime_NASDAQ", "last_market_regime_CRYPTO_INDEPENDENTRESERVE",
        "last_market_regime_CRYPTO_BINANCE",
    ]
    regime_rows = db.query(SystemConfig).filter(
        SystemConfig.organization_id == org_id,
        SystemConfig.key.in_(regime_keys),
    ).all()
    regimes = {
        row.key.replace("last_market_regime_", ""): (row.value or "UNKNOWN")
        for row in regime_rows if row.value
    }

    # ── Account ──
    account = db.query(Account).filter(Account.organization_id == org_id).first()
    capital = float(account.capital_aud) if account and account.capital_aud else 0.0
    is_paper = account.is_paper if account else True

    # ── Exchange configs (for flags) ──
    ex_cfgs = {e.exchange_key: e for e in db.query(ExchangeConfig).all()}
    def _flag(exk: str) -> str:
        c = ex_cfgs.get(exk)
        return (c.flag_emoji if c and c.flag_emoji else "🌐")

    def _disp(ticker: str) -> str:
        return ticker.replace(".AX", "").replace("-AUD", "").replace("-USD", "")

    stock_names = get_cached_stock_names(db)

    # Query last screener run AuditLog rows once for ASX, NYSE, CRYPTO
    from app.models.audit import AuditLog, AuditAction
    last_screens = {}
    for exk in ["ASX", "NYSE", "CRYPTO"]:
        row = (
            db.query(AuditLog)
            .filter(
                AuditLog.organization_id == org_id,
                AuditLog.action == AuditAction.SCREENER_RUN,
                AuditLog.message.like(f"[{exk}]%")
            )
            .order_by(AuditLog.created_at.desc())
            .first()
        )
        if row:
            last_screens[exk] = row

    def _get_next_scheduled_run(exk: str, tz_name: str) -> str:
        import pytz
        from datetime import datetime as dt_class, timedelta
        try:
            tz = pytz.timezone(tz_name)
            now_local = dt_class.now(tz)
            
            if exk == "CRYPTO":
                # 4-hourly runs: 00:55, 04:55, 08:55, 12:55, 16:55, 20:55
                target_hours = [0, 4, 8, 12, 16, 20]
                for h in target_hours:
                    candidate = now_local.replace(hour=h, minute=55, second=0, microsecond=0)
                    if candidate > now_local:
                        return _friendly_dt_str(candidate)
                tomorrow = now_local + timedelta(days=1)
                return _friendly_dt_str(tomorrow.replace(hour=0, minute=55, second=0, microsecond=0))
                
            elif exk == "NYSE":
                # US: 7:30am Tue-Sat
                for offset in range(8):
                    candidate_day = now_local + timedelta(days=offset)
                    if candidate_day.weekday() in (1, 2, 3, 4, 5):
                        candidate = candidate_day.replace(hour=7, minute=30, second=0, microsecond=0)
                        if candidate > now_local:
                            return _friendly_dt_str(candidate)
                            
            else: # ASX
                # ASX: 5:30pm Mon-Fri
                for offset in range(8):
                    candidate_day = now_local + timedelta(days=offset)
                    if candidate_day.weekday() in (0, 1, 2, 3, 4):
                        candidate = candidate_day.replace(hour=17, minute=30, second=0, microsecond=0)
                        if candidate > now_local:
                            return _friendly_dt_str(candidate)
        except Exception:
            pass
        return "TBD"

    def _pct(a, b):
        if a and b and float(b) > 0:
            return round((float(a) - float(b)) / float(b) * 100, 2)
        return None

    # ── Build watchlist payload ──
    wl_data = []
    pending_signal_tickers = {s.ticker for s in signals}
    for w in wl_items:
        bar = price_map.get(w.ticker)
        has_sig = w.ticker in pending_signal_tickers
        close = float(bar.close) if bar and bar.close else None
        open_ = float(bar.open) if bar and bar.open else None
        
        is_crypto_wl = w.ticker.endswith(("-AUD", "-USD", "-USDT"))
        if is_crypto_wl:
            live_cache_key = f"live_price:{w.ticker}"
            live_cached = cache.get(live_cache_key)
            if live_cached and not live_cached.get("_failed"):
                live_close = float(live_cached.get("close") or live_cached.get("price") or 0)
                if live_close > 0:
                    close = live_close
                    if live_cached.get("change_pct") is not None:
                        open_ = None
        
        chg_pct = _pct(close, open_) if close and open_ and open_ > 0 else 0.0
        
        ma50      = float(bar.ma_50)   if bar and bar.ma_50   else None
        ma150     = float(bar.ma_150)  if bar and bar.ma_150  else None
        ma200     = float(bar.ma_200)  if bar and bar.ma_200  else None
        vol_ratio = float(bar.vol_ratio)  if bar and bar.vol_ratio  else None
        rs_rating = float(bar.rs_rating)  if bar and bar.rs_rating  else None
        high_52w  = float(bar.high_52w)   if bar and bar.high_52w   else None
        low_52w   = float(bar.low_52w)    if bar and bar.low_52w    else None
        
        range_pct = None
        if high_52w and low_52w and high_52w > low_52w and close:
            range_pct = round((close - low_52w) / (high_52w - low_52w) * 100, 1)

        rules = w.rule_results or {}
        trend_keys = [k for k in rules if k.startswith("trend_")]
        trend_passed = sum(
            1 for k in trend_keys
            if (rules[k].get("passed") if isinstance(rules[k], dict) else bool(rules[k]))
        )
        trend_total = len(trend_keys) if trend_keys else 8

        vcp_contractions = None
        if "vcp_contractions" in rules:
            rv = rules["vcp_contractions"]
            vcp_contractions = int(rv.get("value") or 0) if isinstance(rv, dict) and rv.get("value") else None

        # Setup quality tier (A/B/C)
        if (trend_passed >= trend_total > 0
                and rs_rating is not None and rs_rating >= 80
                and (vol_ratio is None or vol_ratio <= 0.6)):
            setup_tier = "A"
        elif (trend_total > 0 and trend_passed >= max(trend_total - 1, 1)
                and rs_rating is not None and rs_rating >= 70):
            setup_tier = "B"
        else:
            setup_tier = "C"

        next_earnings_date = None
        days_to_earnings = None
        cached_ed = cache.get(f"earnings_date:{w.ticker}")
        if cached_ed and isinstance(cached_ed, str):
            try:
                from datetime import date as _date
                _ed = _date.fromisoformat(cached_ed)
                _today = _date.today()
                if _ed >= _today:
                    next_earnings_date = cached_ed
                    days_to_earnings = (_ed - _today).days
            except Exception:
                pass

        is_crypto_item = w.ticker.endswith(("-AUD", "-USD", "-USDT")) or getattr(w, "asset_type", "EQUITY") == "CRYPTO"
        if is_crypto_item:
            scr_ex = "CRYPTO"
        elif (w.exchange_key or "ASX") in ("NYSE", "NASDAQ"):
            scr_ex = "NYSE"
        else:
            scr_ex = "ASX"

        row = last_screens.get(scr_ex)
        last_screen_at_val = _fmt_dt(row.created_at.isoformat(), display_tz) if row else "Never"
        last_screen_summary_val = (row.message.split("] ", 1)[-1] if row and row.message else "No screen run found")
        next_screen_at_val = _get_next_scheduled_run(scr_ex, display_tz)

        wl_data.append({
            "id": w.id,
            "ticker": w.ticker,
            "display_ticker": _disp(w.ticker),
            "name": stock_names.get(w.ticker, _disp(w.ticker)),
            "exchange_key": w.exchange_key or "ASX",
            "asset_type": "CRYPTO" if is_crypto_item else (w.asset_type or "EQUITY"),
            "currency": w.currency or "AUD",
            "flag": _flag(w.exchange_key or "ASX"),
            "label_id": w.label_id,
            "label_name": w.label.name if w.label else None,
            "label_color": w.label.color if w.label else None,
            "close": close,
            "change_pct": chg_pct,
            "ma_50": ma50,
            "ma_150": ma150,
            "ma_200": ma200,
            "ma_50_pct": _pct(close, ma50),
            "ma_150_pct": _pct(close, ma150),
            "ma_200_pct": _pct(close, ma200),
            "vol_ratio": vol_ratio,
            "rs_rating": rs_rating,
            "high_52w": high_52w,
            "low_52w": low_52w,
            "range_pct": range_pct,
            "trend_score": trend_passed,
            "trend_total": trend_total,
            "vcp_contractions": vcp_contractions,
            "rule_results": rules,
            "setup_tier": setup_tier,
            "next_earnings_date": next_earnings_date,
            "days_to_earnings": days_to_earnings,
            "has_signal": has_sig,
            "has_pending_signal": has_sig,
            "added_by": w.added_by or "screener",
            "added_date": w.added_date.isoformat() if w.added_date else None,
            "last_screen_at": last_screen_at_val,
            "last_screen_summary": last_screen_summary_val,
            "next_screen_at": next_screen_at_val,
        })

    # ── Build signals payload ──
    sig_data = []
    for s in signals:
        chk = check_map.get(s.id)
        bar = price_map.get(s.ticker)
        sig_data.append({
            "id": s.id,
            "ticker": s.ticker,
            "display_ticker": _disp(s.ticker),
            "name": stock_names.get(s.ticker, _disp(s.ticker)),
            "exchange_key": s.exchange_key or "ASX",
            "currency": s.currency or "AUD",
            "flag": _flag(s.exchange_key or "ASX"),
            "status": s.status.value if s.status else "PENDING",
            "signal_date": s.signal_date.isoformat() if s.signal_date else None,
            "pivot": float(s.pivot_price) if s.pivot_price else None,
            "stop": float(s.stop_price) if s.stop_price else None,
            "target1": float(s.target_price_1) if s.target_price_1 else None,
            "target2": float(s.target_price_2) if s.target_price_2 else None,
            "close": float(bar.close) if bar and bar.close else None,
            "last_check_price": float(chk.price_current) if chk and chk.price_current else None,
            "last_check_at": chk.checked_at.isoformat() if chk else None,
            "breakout_confirmed": bool(chk.breakout_confirmed) if chk else False,
            "vs_pivot_pct": float(chk.price_vs_pivot) if chk and chk.price_vs_pivot else None,
            "vol_ratio": float(chk.vol_ratio) if chk and chk.vol_ratio else None,
            "rs_rating": float(chk.rs_rating) if chk and chk.rs_rating is not None else (float(bar.rs_rating) if bar and bar.rs_rating is not None else (float(s.rs_rating) if s and s.rs_rating is not None else None)),
            "ma_50": float(chk.ma_50) if chk and chk.ma_50 is not None else (float(bar.ma_50) if bar and bar.ma_50 is not None else None),
            "ma_200": float(chk.ma_200) if chk and chk.ma_200 is not None else (float(bar.ma_200) if bar and bar.ma_200 is not None else None),
            "data_source": chk.data_source if chk else None,
            "data_delay_mins": chk.data_delay_mins if chk else None,
            "rule_results": chk.rule_results if chk else {},
            "next_check_at": _get_next_entry_check(s.exchange_key or "ASX", display_tz),
        })

    # ── Build positions payload ──
    pos_data = []
    total_pnl = 0.0
    for p in positions:
        pnl = float(p.unrealised_pnl) if p.unrealised_pnl else 0.0
        total_pnl += pnl
        pos_data.append({
            "id": p.id,
            "ticker": p.ticker,
            "display_ticker": _disp(p.ticker),
            "name": stock_names.get(p.ticker, _disp(p.ticker)),
            "exchange_key": p.exchange_key or "ASX",
            "currency": p.currency or "AUD",
            "flag": _flag(p.exchange_key or "ASX"),
            "entry_price": float(p.entry_price) if p.entry_price else None,
            "current_price": float(p.current_price) if p.current_price else None,
            "qty": float(p.qty) if p.qty else 0,
            "unrealised_pnl": pnl,
            "unrealised_pct": float(p.unrealised_pct) if p.unrealised_pct else 0.0,
            "stop": float(p.current_stop) if p.current_stop else None,
            "target1": float(p.target_1) if p.target_1 else None,
        })

    # ── Ticker tape: prices for watchlist + signals + positions ──
    tape: dict = {}
    # Collect all tickers that need prices
    tape_currency: dict = {w.ticker: (w.currency or "AUD") for w in wl_items}
    for s in signals:
        if s.ticker not in tape_currency:
            tape_currency[s.ticker] = getattr(s, "currency", "AUD") or "AUD"
    for p in positions:
        if p.ticker not in tape_currency:
            tape_currency[p.ticker] = getattr(p, "currency", "AUD") or "AUD"

    for ticker, currency in tape_currency.items():
        bar = price_map.get(ticker)
        if bar and bar.close:
            close = float(bar.close)
            open_ = float(bar.open) if bar.open and float(bar.open) > 0 else close
            chg = round((close - open_) / open_ * 100, 2)
            tape[ticker] = {
                "display": _disp(ticker),
                "price": close,
                "change_pct": chg,
                "currency": currency,
            }

    return JSONResponse({
        "watchlist": wl_data,
        "signals": sig_data,
        "positions": pos_data,
        "regimes": regimes,
        "total_unrealised_pnl": round(total_pnl, 2),
        "open_positions_count": len(pos_data),
        "capital_aud": capital,
        "account_is_paper": is_paper,
        "tape": tape,
        "exchange_filters": _get_exchange_filters(org_id, db),
    })


@app.get("/trader/chart/{ticker:path}")
async def trader_chart_data(
    ticker: str,
    request: Request,
    tf: str = "1Y",
    db: Session = Depends(get_db),
):
    """OHLCV history for TradingView Lightweight Charts."""
    if not _auth(request):
        return JSONResponse({"error": "unauthenticated"}, status_code=401)

    from app.models.market import PriceBar
    from datetime import timedelta

    tf_days = {"1M": 30, "3M": 90, "6M": 180, "1Y": 365, "2Y": 730}.get(tf.upper(), 365)
    cutoff = datetime.utcnow().date() - timedelta(days=tf_days)

    bars = (
        db.query(PriceBar)
        .filter(PriceBar.ticker == ticker, PriceBar.date >= cutoff)
        .order_by(PriceBar.date.asc())
        .all()
    )

    candles, volumes, ma50, ma150, ma200 = [], [], [], [], []
    high_52w = low_52w = rs_rating = None

    for b in bars:
        if not all([b.open, b.high, b.low, b.close]):
            continue
        ts = b.date.strftime("%Y-%m-%d")
        o, h, l, c = float(b.open), float(b.high), float(b.low), float(b.close)
        candles.append({"time": ts, "open": o, "high": h, "low": l, "close": c})

        # Volume — brighter when breakout volume (vol_ratio ≥ 1.5)
        vr = float(b.vol_ratio) if b.vol_ratio else 1.0
        if c >= o:
            vol_color = "#26a69a" if vr >= 1.5 else "#26a69a55"
        else:
            vol_color = "#ef5350" if vr >= 1.5 else "#ef535055"
        volumes.append({"time": ts, "value": int(b.volume) if b.volume else 0, "color": vol_color})

        # MA lines (skip None values so lines don't plot gaps as zero)
        if b.ma_50:   ma50.append({"time": ts,  "value": float(b.ma_50)})
        if b.ma_150:  ma150.append({"time": ts, "value": float(b.ma_150)})
        if b.ma_200:  ma200.append({"time": ts, "value": float(b.ma_200)})

        # 52-week range from the last bar
        if b.high_52w: high_52w = float(b.high_52w)
        if b.low_52w:  low_52w  = float(b.low_52w)
        if b.rs_rating: rs_rating = float(b.rs_rating)

    return JSONResponse({
        "candles": candles,
        "volumes": volumes,
        "ma50":    ma50,
        "ma150":   ma150,
        "ma200":   ma200,
        "high_52w":  high_52w,
        "low_52w":   low_52w,
        "rs_rating": rs_rating,
        "ticker": ticker,
        "tf": tf,
    })


@app.get("/trader/prices")
async def trader_prices(request: Request, db: Session = Depends(get_db)):
    """Live price poll — latest bar close for all org watchlist tickers."""
    if not _auth(request):
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    try:
        return _trader_prices_inner(request, db)
    except Exception as exc:
        import traceback
        trace = traceback.format_exc()
        try:
            db.rollback()
        except Exception:
            pass
        logger.error(f"trader/prices failed: {exc}\n{trace}")
        from app.config import settings
        body = {"error": str(exc)}
        if settings.app_env == "development":
            body["trace"] = trace
        return JSONResponse(body, status_code=500)


def _trader_prices_inner(request: Request, db):
    from sqlalchemy import func
    from app.models.signal import Watchlist, WatchlistStatus, Signal, SignalStatus
    from app.models.trade import Position, TradeStatus
    from app.models.market import PriceBar, Stock

    org_id = request.session.get("organization_id")

    # Collect tickers from watchlist + open positions + pending signals
    wl_rows = db.query(Watchlist.ticker, Watchlist.currency).filter(
        Watchlist.organization_id == org_id,
        Watchlist.status == WatchlistStatus.WATCHING,
    ).all()
    sig_rows = db.query(Signal.ticker).filter(
        Signal.organization_id == org_id,
        Signal.status == SignalStatus.PENDING,
    ).all()
    pos_rows = db.query(Position.ticker).filter(
        Position.organization_id == org_id,
        Position.status == TradeStatus.OPEN,
    ).all()

    # Build ticker set and currency map
    currency_map: dict = {r[0]: (r[1] or "AUD") for r in wl_rows}
    all_tickers = set(currency_map.keys())
    for r in sig_rows: all_tickers.add(r[0])
    for r in pos_rows: all_tickers.add(r[0])

    if not all_tickers:
        return JSONResponse({})

    # Fill currency for any ticker not in watchlist
    missing = [t for t in all_tickers if t not in currency_map]
    if missing:
        stocks = db.query(Stock.ticker, Stock.currency).filter(Stock.ticker.in_(missing)).all()
        for s in stocks:
            currency_map[s[0]] = s[1] or "AUD"

    # Build asset_type map (CRYPTO vs EQUITY) for routing live-price fetch
    asset_map: dict = {}
    wl_asset_rows = db.query(Watchlist.ticker, Watchlist.asset_type).filter(
        Watchlist.organization_id == org_id,
        Watchlist.status == WatchlistStatus.WATCHING,
    ).all()
    for r in wl_asset_rows:
        # Ticker suffix is authoritative — covers DB rows with NULL/"EQUITY" asset_type
        asset_map[r[0]] = (
            "CRYPTO" if r[0].endswith(("-AUD", "-USD", "-USDT"))
            else r[1] or "EQUITY"
        )
    # For tickers in signals/positions not in watchlist, infer from ticker format
    for t in all_tickers:
        if t not in asset_map:
            asset_map[t] = "CRYPTO" if ("-AUD" in t or "-USD" in t or "-USDT" in t) else "EQUITY"

    tickers = list(all_tickers)

    # ── Pull latest PriceBar as EOD fallback ────────────────────────────────────────────
    latest_sq = (
        db.query(PriceBar.ticker, func.max(PriceBar.date).label("mx"))
        .filter(PriceBar.ticker.in_(tickers))
        .group_by(PriceBar.ticker)
        .subquery()
    )
    bars = (
        db.query(PriceBar)
        .join(latest_sq, (PriceBar.ticker == latest_sq.c.ticker) & (PriceBar.date == latest_sq.c.mx))
        .all()
    )
    eod_map = {b.ticker: b for b in bars}

    def _disp(t: str) -> str:
        return t.replace(".AX", "").replace("-AUD", "").replace("-USD", "").replace("-USDT", "")

    result = {}
    for ticker in tickers:
        currency   = currency_map.get(ticker, "AUD")
        asset_type = asset_map.get(ticker, "EQUITY")

        # ── Try live intraday price (cached 5 min, shared with watchlist page) ──────────
        live_cache_key = f"live_price:{ticker}"
        cached = cache.get(live_cache_key)

        if cached is not None:
            # Cache hit — use it unless it is a failure sentinel
            if not cached.get("_failed"):
                live_close = float(cached.get("close") or cached.get("price") or 0)
                if live_close > 0:
                    eod_bar = eod_map.get(ticker)
                    eod_open = float(eod_bar.open) if eod_bar and eod_bar.open and float(eod_bar.open) > 0 else live_close
                    chg = cached.get("change_pct")
                    if chg is None:
                        chg = round((live_close - eod_open) / eod_open * 100, 2) if eod_open > 0 else 0.0
                    result[ticker] = {
                        "display": _disp(ticker),
                        "price": live_close,
                        "change_pct": chg,
                        "currency": currency,
                        "live": True,
                    }
                    continue
        else:
            # Cache miss — for crypto: inline live fetch (IR/MEXC are 0-delay, ~150ms each).
            # Populates the cache so the next 10s poll is instant. Rarely fires in steady-
            # state because refresh_live_prices_cache_task keeps cache warm every 5 min.
            if asset_type == "CRYPTO":
                try:
                    from app.data.fetcher import get_intraday_price as _gip
                    live = _gip(ticker, asset_type="CRYPTO")
                    if live.get("ok") and live.get("price"):
                        pv = float(live["price"])
                        cache.set(live_cache_key, {
                            "price": pv, "close": pv, "live_price": pv,
                            "data_source": live.get("data_source", "unknown"),
                            "delay_mins": live.get("delay_mins", 0), "_failed": False,
                        }, expire_seconds=360)
                        eod_bar = eod_map.get(ticker)
                        eod_open = (
                            float(eod_bar.open) if eod_bar and eod_bar.open and float(eod_bar.open) > 0
                            else pv
                        )
                        result[ticker] = {
                            "display": _disp(ticker), "price": pv,
                            "change_pct": round((pv - eod_open) / eod_open * 100, 2) if eod_open > 0 else 0.0,
                            "currency": currency, "live": True,
                        }
                        continue
                    else:
                        # Coin unsupported by all sources (e.g. not in IR_SYMBOL_MAP) —
                        # cache a failure sentinel so we don't retry every 10s.
                        # TTL=120s means one retry per 2 min instead of every 10s.
                        cache.set(live_cache_key, {"_failed": True}, expire_seconds=120)
                except Exception:
                    cache.set(live_cache_key, {"_failed": True}, expire_seconds=120)

        # ── EOD fallback (PriceBar last close) ──────────────────────────────────────────
        eod_bar = eod_map.get(ticker)
        if eod_bar and eod_bar.close:
            close = float(eod_bar.close)
            open_ = float(eod_bar.open) if eod_bar.open and float(eod_bar.open) > 0 else close
            chg = round((close - open_) / open_ * 100, 2)
            result[ticker] = {
                "display": _disp(ticker),
                "price": close,
                "change_pct": chg,
                "currency": currency,
                "live": False,
            }

    return JSONResponse(result)


@app.get("/trader/exit-checks")
async def trader_exit_checks(request: Request, db: Session = Depends(get_db)):
    """Latest exit-rule check per open position — polled every 30s by trader terminal."""
    if not _auth(request):
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    try:
        from app.models.trade import Position, TradeStatus
        from app.models.audit import AuditLog, AuditAction
        from sqlalchemy import desc, or_

        org_id = request.session.get("organization_id")
        display_tz = _get_display_tz(org_id, db)
        positions = db.query(Position).filter(
            Position.organization_id == org_id,
            Position.status == TradeStatus.OPEN,
        ).all()

        results = []
        for p in positions:
            entry = float(p.entry_price or 0)
            stop  = float(p.current_stop or 0)

            # Latest exit-check AuditLog row for this ticker/position
            log_entries = db.query(AuditLog).filter(
                AuditLog.organization_id == org_id,
                AuditLog.action == AuditAction.TASK_RUN,
                AuditLog.ticker == p.ticker,
                or_(
                    AuditLog.entity_id == str(p.id),
                    AuditLog.message.ilike("Exit check @ %"),
                ),
            ).order_by(desc(AuditLog.created_at)).limit(5).all()

            checks = []
            for log in log_entries:
                d   = log.detail or {}
                msg = log.message or ""
                result = d.get("result", "")
                if not result:
                    result = "exit_triggered" if "EXIT triggered" in msg else ("holding" if "holding" in msg else "unknown")

                pnl_pct = d.get("pnl_pct")
                if pnl_pct is None and "P&L " in msg:
                    try: pnl_pct = float(msg.split("P&L ")[1].split("%")[0])
                    except Exception: pass

                price = d.get("close")
                if price is None and "Price $" in msg:
                    try: price = float(msg.split("Price $")[1].split(" ")[0].split("|")[0].strip())
                    except Exception: pass

                reason = ""
                if result == "exit_triggered" and "EXIT triggered — " in msg:
                    reason = msg.split("EXIT triggered — ")[1].split(" | ")[0]
                elif result == "holding":
                    reason = "No exit criteria met"
                elif result == "skipped":
                    reason = d.get("reason", "Skipped")
                elif result == "error":
                    reason = d.get("error", "Check error")

                checks.append({
                    "time": _fmt_dt(str(log.created_at), display_tz),
                    "result": result,
                    "price": price,
                    "pnl_pct": pnl_pct,
                    "reason": reason,
                    "message": msg[:120],
                })

            ek = getattr(p, "exchange_key", "ASX") or "ASX"
            curr = float(p.current_price or entry)
            pnl_pct_live = round((curr - entry) / entry * 100, 2) if entry else 0

            results.append({
                "id": p.id,
                "ticker": p.ticker,
                "exchange_key": ek,
                "display_ticker": p.ticker.replace(".AX", "").replace("-AUD", "").replace("-USD", ""),
                "currency": getattr(p, "currency", "AUD") or "AUD",
                "entry": entry,
                "stop": stop,
                "current": curr,
                "pnl_pct": pnl_pct_live,
                "checks": checks,
            })

        return JSONResponse({"positions": results, "display_tz": display_tz})
    except Exception as exc:
        import traceback
        logger.error(f"trader/exit-checks failed: {exc}\n{traceback.format_exc()}")
        from app.config import settings
        body = {"error": str(exc)}
        if settings.app_env == "development":
            body["trace"] = traceback.format_exc()
        return JSONResponse(body, status_code=500)


# ===========================================================================
# TRADER WATCHLIST TERMINAL
# ===========================================================================

@app.get("/trader/watchlist", response_class=HTMLResponse)
async def trader_watchlist_view(request: Request, db: Session = Depends(get_db)):
    """Bloomberg-style fullscreen watchlist terminal — dedicated trader screen."""
    if not _auth(request):
        return RedirectResponse("/login")
    return templates.TemplateResponse("trading/trader_watchlist.html", {
        "request": request,
        **_global(request, db),
    })


@app.get("/trader/watchlist/data")
async def trader_watchlist_data(request: Request, db: Session = Depends(get_db)):
    """Rich watchlist payload — label-grouped, with full rule_results + PriceBar metrics."""
    if not _auth(request):
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    try:
        return _trader_watchlist_data_inner(request, db)
    except Exception as exc:
        import traceback
        try:
            db.rollback()
        except Exception:
            pass
        logger.error(f"trader/watchlist/data failed: {exc}\n{traceback.format_exc()}")
        from app.config import settings
        body = {"error": str(exc)}
        if settings.app_env == "development":
            body["trace"] = traceback.format_exc()
        return JSONResponse(body, status_code=500)


def _trader_watchlist_data_inner(request: Request, db):
    from sqlalchemy import func
    from sqlalchemy.orm import joinedload
    from app.models.signal import Watchlist, WatchlistStatus, WatchlistLabel, Signal, SignalStatus
    from app.models.market import PriceBar, Stock
    from app.models.config import SystemConfig
    from app.models.account import Account
    from app.models.exchange import ExchangeConfig
    import json as _json

    org_id = request.session.get("organization_id")

    # ── 30-second Redis cache (matches the JS poll interval) ──────────────────
    # On cache hit the entire JSON computation is skipped — ~1ms vs 200-800ms.
    # Cache is invalidated on any watchlist mutation or screener run.
    _tw_ck = f"tw_data:{org_id}"
    _cached_json = cache.get_raw(_tw_ck)
    if _cached_json:
        try:
            return JSONResponse(_json.loads(_cached_json))
        except Exception:
            pass  # fall through to recompute on corrupt cache entry

    from app.models.audit import AuditLog, AuditAction
    # Retrieve timezone
    tz_row = db.query(SystemConfig).filter(SystemConfig.key == "org_timezone", SystemConfig.organization_id == org_id).first()
    display_tz = tz_row.value if tz_row else "Australia/Sydney"

    # Query last screener run AuditLog rows once for ASX, NYSE, CRYPTO
    last_screens = {}
    for exk in ["ASX", "NYSE", "CRYPTO"]:
        row = (
            db.query(AuditLog)
            .filter(
                AuditLog.organization_id == org_id,
                AuditLog.action == AuditAction.SCREENER_RUN,
                AuditLog.message.like(f"[{exk}]%")
            )
            .order_by(AuditLog.created_at.desc())
            .first()
        )
        if row:
            last_screens[exk] = row

    def _get_next_scheduled_run(exk: str, tz_name: str) -> str:
        import pytz
        from datetime import datetime as dt_class, timedelta
        try:
            tz = pytz.timezone(tz_name)
            now_local = dt_class.now(tz)
            
            if exk == "CRYPTO":
                # 4-hourly runs: 00:55, 04:55, 08:55, 12:55, 16:55, 20:55
                target_hours = [0, 4, 8, 12, 16, 20]
                for h in target_hours:
                    candidate = now_local.replace(hour=h, minute=55, second=0, microsecond=0)
                    if candidate > now_local:
                        return _friendly_dt_str(candidate)
                tomorrow = now_local + timedelta(days=1)
                return _friendly_dt_str(tomorrow.replace(hour=0, minute=55, second=0, microsecond=0))
                
            elif exk == "NYSE":
                # US: 7:30am Tue-Sat
                for offset in range(8):
                    candidate_day = now_local + timedelta(days=offset)
                    if candidate_day.weekday() in (1, 2, 3, 4, 5):
                        candidate = candidate_day.replace(hour=7, minute=30, second=0, microsecond=0)
                        if candidate > now_local:
                            return _friendly_dt_str(candidate)
                            
            else: # ASX
                # ASX: 5:30pm Mon-Fri
                for offset in range(8):
                    candidate_day = now_local + timedelta(days=offset)
                    if candidate_day.weekday() in (0, 1, 2, 3, 4):
                        candidate = candidate_day.replace(hour=17, minute=30, second=0, microsecond=0)
                        if candidate > now_local:
                            return _friendly_dt_str(candidate)
        except Exception:
            pass
        return "TBD"

    # ── All WATCHING items (with label eager-loaded) ──
    wl_items = (
        db.query(Watchlist)
        .options(joinedload(Watchlist.label))
        .filter(
            Watchlist.organization_id == org_id,
            Watchlist.status == WatchlistStatus.WATCHING,
        )
        .order_by(Watchlist.created_at.desc())
        .limit(1000)
        .all()
    )

    # ── All labels (for ordering — alphabetical, case-insensitive) ──
    from sqlalchemy import func as _twfunc
    labels = (
        db.query(WatchlistLabel)
        .filter(WatchlistLabel.organization_id == org_id)
        .order_by(_twfunc.lower(WatchlistLabel.name))
        .all()
    )

    # ── Latest PriceBar per ticker ──
    all_tickers = list({w.ticker for w in wl_items})
    price_map: dict = {}
    if all_tickers:
        latest_sq = (
            db.query(PriceBar.ticker, func.max(PriceBar.date).label("mx"))
            .filter(PriceBar.ticker.in_(all_tickers))
            .group_by(PriceBar.ticker)
            .subquery()
        )
        bars = (
            db.query(PriceBar)
            .join(latest_sq, (PriceBar.ticker == latest_sq.c.ticker) & (PriceBar.date == latest_sq.c.mx))
            .all()
        )
        price_map = {b.ticker: b for b in bars}

    # ── Active signals (to flag items that already have a pending signal) ──
    pending_signal_tickers = {
        row[0] for row in db.query(Signal.ticker).filter(
            Signal.organization_id == org_id,
            Signal.status == SignalStatus.PENDING,
        ).all()
    }

    # ── Exchange configs ──
    ex_cfgs = {e.exchange_key: e for e in db.query(ExchangeConfig).all()}
    def _flag(exk: str) -> str:
        c = ex_cfgs.get(exk)
        return (c.flag_emoji if c and c.flag_emoji else "")

    def _disp(ticker: str) -> str:
        return ticker.replace(".AX", "").replace("-AUD", "").replace("-USD", "").replace("-USDT", "")

    stock_names = get_cached_stock_names(db)

    def _pct(a, b):
        if a and b and float(b) > 0:
            return round((float(a) - float(b)) / float(b) * 100, 2)
        return None

    def _build_item(w):
        bar = price_map.get(w.ticker)
        close     = float(bar.close)   if bar and bar.close   else None
        open_     = float(bar.open)    if bar and bar.open    else None
        ma50      = float(bar.ma_50)   if bar and bar.ma_50   else None
        ma150     = float(bar.ma_150)  if bar and bar.ma_150  else None
        ma200     = float(bar.ma_200)  if bar and bar.ma_200  else None
        vol_ratio = float(bar.vol_ratio)  if bar and bar.vol_ratio  else None
        rs_rating = float(bar.rs_rating)  if bar and bar.rs_rating  else None
        high_52w  = float(bar.high_52w)   if bar and bar.high_52w   else None
        low_52w   = float(bar.low_52w)    if bar and bar.low_52w    else None

        # ── For crypto: overlay live price from cache (shared 5-min cache key) ──
        # This ensures item.close already reflects the live price on initial page load,
        # before the frontend's /trader/prices poll response arrives.
        # Ticker format is authoritative — covers NULL and wrong asset_type="EQUITY" DB rows
        is_crypto_wl = w.ticker.endswith(("-AUD", "-USD", "-USDT"))
        if is_crypto_wl:
            live_cache_key = f"live_price:{w.ticker}"
            live_cached = cache.get(live_cache_key)
            if live_cached and not live_cached.get("_failed"):
                live_close = float(live_cached.get("close") or live_cached.get("price") or 0)
                if live_close > 0:
                    close = live_close  # override EOD close with live price
                    if live_cached.get("change_pct") is not None:
                        open_ = None  # chg_pct computed from cache below
            else:
                # Cache miss — inline live fetch (IR/MEXC are 0-delay, ~150ms)
                try:
                    from app.data.fetcher import get_intraday_price as _gip
                    live = _gip(w.ticker, asset_type="CRYPTO")
                    if live.get("ok") and live.get("price"):
                        live_close = float(live["price"])
                        cache.set(live_cache_key, {
                            "price": live_close, "close": live_close, "live_price": live_close,
                            "data_source": live.get("data_source", ""), "_failed": False,
                        }, expire_seconds=360)
                        close = live_close
                    else:
                        cache.set(live_cache_key, {"_failed": True}, expire_seconds=120)
                except Exception:
                    cache.set(live_cache_key, {"_failed": True}, expire_seconds=120)

        chg_pct   = _pct(close, open_) if close and open_ and open_ > 0 else 0.0
        range_pct = None
        if high_52w and low_52w and high_52w > low_52w and close:
            range_pct = round((close - low_52w) / (high_52w - low_52w) * 100, 1)

        rules = w.rule_results or {}
        trend_keys = [k for k in rules if k.startswith("trend_")]
        trend_passed = sum(
            1 for k in trend_keys
            if (rules[k].get("passed") if isinstance(rules[k], dict) else bool(rules[k]))
        )
        trend_total = len(trend_keys) if trend_keys else 8

        vcp_contractions = None
        if "vcp_contractions" in rules:
            rv = rules["vcp_contractions"]
            vcp_contractions = int(rv.get("value") or 0) if isinstance(rv, dict) and rv.get("value") else None

        # ── Setup quality tier (A/B/C) — Minervini prioritisation ────────────
        # A: 8/8 trend + RS ≥ 80 + volume dry-up (vol_ratio ≤ 0.6)
        # B: 7+/8 trend + RS ≥ 70
        # C: still forming — below threshold
        if (trend_passed >= trend_total > 0
                and rs_rating is not None and rs_rating >= 80
                and (vol_ratio is None or vol_ratio <= 0.6)):
            setup_tier = "A"
        elif (trend_total > 0 and trend_passed >= max(trend_total - 1, 1)
                and rs_rating is not None and rs_rating >= 70):
            setup_tier = "B"
        else:
            setup_tier = "C"

        # ── Upcoming earnings warning (from Redis — written by get_fundamentals) ─
        next_earnings_date = None
        days_to_earnings = None
        cached_ed = cache.get(f"earnings_date:{w.ticker}")
        if cached_ed and isinstance(cached_ed, str):
            try:
                from datetime import date as _date
                _ed = _date.fromisoformat(cached_ed)
                _today = _date.today()
                if _ed >= _today:
                    next_earnings_date = cached_ed
                    days_to_earnings = (_ed - _today).days
            except Exception:
                pass

        # Determine screener exchange key
        is_crypto_item = w.ticker.endswith(("-AUD", "-USD", "-USDT")) or getattr(w, "asset_type", "EQUITY") == "CRYPTO"
        if is_crypto_item:
            scr_ex = "CRYPTO"
        elif (w.exchange_key or "ASX") in ("NYSE", "NASDAQ"):
            scr_ex = "NYSE"
        else:
            scr_ex = "ASX"

        row = last_screens.get(scr_ex)
        last_screen_at_val = _fmt_dt(row.created_at.isoformat(), display_tz) if row else "Never"
        last_screen_summary_val = (row.message.split("] ", 1)[-1] if row and row.message else "No screen run found")
        next_screen_at_val = _get_next_scheduled_run(scr_ex, display_tz)

        return {
            "id": w.id,
            "ticker": w.ticker,
            "display_ticker": _disp(w.ticker),
            "name": stock_names.get(w.ticker, _disp(w.ticker)),
            "exchange_key": w.exchange_key or "ASX",
            # Ticker format is authoritative for crypto detection — covers DB rows where
            # asset_type was stored as "EQUITY" due to the Jun 2026 screener bug.
            # ASX/US equities never use -AUD/-USD/-USDT suffixes, so this is safe.
            "asset_type": (
                "CRYPTO" if w.ticker.endswith(("-AUD", "-USD", "-USDT"))
                else w.asset_type or "EQUITY"
            ),
            "currency": w.currency or "AUD",
            "flag": _flag(w.exchange_key or "ASX"),
            "label_id": w.label_id,
            "label_name": w.label.name if w.label else None,
            "label_color": w.label.color if w.label else None,
            "close": close,
            "change_pct": chg_pct,
            "ma_50": ma50,
            "ma_150": ma150,
            "ma_200": ma200,
            "ma_50_pct": _pct(close, ma50),
            "ma_150_pct": _pct(close, ma150),
            "ma_200_pct": _pct(close, ma200),
            "vol_ratio": vol_ratio,
            "rs_rating": rs_rating,
            "high_52w": high_52w,
            "low_52w": low_52w,
            "range_pct": range_pct,
            "trend_score": trend_passed,
            "trend_total": trend_total,
            "vcp_contractions": vcp_contractions,
            "rule_results": rules,
            "setup_tier": setup_tier,
            "next_earnings_date": next_earnings_date,
            "days_to_earnings": days_to_earnings,
            "has_pending_signal": w.ticker in pending_signal_tickers,
            "added_by": w.added_by or "screener",
            "added_date": w.added_date.isoformat() if w.added_date else None,
            "last_screen_at": last_screen_at_val,
            "last_screen_summary": last_screen_summary_val,
            "next_screen_at": next_screen_at_val,
        }

    label_map: dict = {}
    unlabelled = []
    for w in wl_items:
        item = _build_item(w)
        if w.label_id:
            label_map.setdefault(w.label_id, []).append(item)
        else:
            unlabelled.append(item)

    groups = []
    for lbl in labels:
        items = label_map.get(lbl.id, [])
        if items:
            groups.append({
                "label_id": lbl.id,
                "label_name": lbl.name,
                "label_color": lbl.color,
                "items": items,
            })
    if unlabelled:
        groups.append({
            "label_id": None,
            "label_name": "Unlabelled",
            "label_color": "#5a5a78",
            "items": unlabelled,
        })

    # Sort items within each group: A-tier first, then B, then C
    _tier_order = {"A": 0, "B": 1, "C": 2}
    for g in groups:
        g["items"].sort(key=lambda x: _tier_order.get(x.get("setup_tier", "C"), 2))

    def _is_crypto(w) -> bool:
        # Ticker format is authoritative — covers DB rows with wrong asset_type="EQUITY"
        return w.ticker.endswith(("-AUD", "-USD", "-USDT"))

    equity_count = sum(1 for w in wl_items if not _is_crypto(w))
    crypto_count = sum(1 for w in wl_items if _is_crypto(w))

    regime_keys = [
        "last_market_regime_ASX", "last_market_regime_NYSE",
        "last_market_regime_NASDAQ", "last_market_regime_CRYPTO_INDEPENDENTRESERVE",
    ]
    regime_rows = db.query(SystemConfig).filter(
        SystemConfig.organization_id == org_id,
        SystemConfig.key.in_(regime_keys),
    ).all()
    regimes = {
        row.key.replace("last_market_regime_", ""): (row.value or "UNKNOWN")
        for row in regime_rows if row.value
    }

    account = db.query(Account).filter(Account.organization_id == org_id).first()
    is_paper = account.is_paper if account else True

    payload = {
        "groups": groups,
        "regimes": regimes,
        "account_is_paper": is_paper,
        "stats": {
            "total": len(wl_items),
            "equity_count": equity_count,
            "crypto_count": crypto_count,
        },
        "exchange_filters": _get_exchange_filters(org_id, db),
    }
    # Cache for 30s — same as JS poll interval, so every poll hits cache
    try:
        cache.set_raw(_tw_ck, _json.dumps(payload), expire_seconds=30)
    except Exception:
        pass
    return JSONResponse(payload)


@app.post("/trader/watchlist/promote/{item_id}")
async def trader_watchlist_promote(request: Request, item_id: int, db: Session = Depends(get_db)):
    """Promote watchlist item to signal — JSON response for in-terminal use."""
    if not _auth(request):
        return JSONResponse({"ok": False, "error": "unauthenticated"}, status_code=401)
    org_id = request.session.get("organization_id")

    from app.models.signal import Watchlist, WatchlistStatus
    from app.models.audit import AuditLog, AuditAction

    w = db.query(Watchlist).filter(Watchlist.id == item_id, Watchlist.organization_id == org_id).first()
    if not w:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)

    # Refuse promotion while an open position exists — the signal could never
    # trigger (entry check skips held tickers) and would just sit PENDING.
    from app.models.trade import Position, TradeStatus
    open_pos = db.query(Position).filter(
        Position.ticker == w.ticker,
        Position.organization_id == org_id,
        Position.status == TradeStatus.OPEN,
    ).first()
    if open_pos:
        return JSONResponse({
            "ok": False,
            "error": (f"{w.ticker} already has an open position — a new signal cannot "
                      f"trigger while the position is held. Close it first."),
        }, status_code=409)

    # Refuse promotion while a live signal already exists for this ticker —
    # otherwise two Watchlist rows for the same ticker can each be promoted
    # into their own duplicate PENDING signal. Mirrors promote_watchlist_item_task's
    # dedup query exactly (any signal today, or any still-PENDING/TRIGGERED
    # signal regardless of date).
    from app.models.signal import Signal, SignalStatus
    existing_signal = db.query(Signal).filter(
        Signal.ticker == w.ticker,
        Signal.organization_id == org_id,
        or_(
            Signal.signal_date == get_current_date(),
            Signal.status.in_([SignalStatus.PENDING, SignalStatus.TRIGGERED]),
        ),
    ).first()
    if existing_signal:
        return JSONResponse({
            "ok": False,
            "error": (f"{w.ticker} already has a {existing_signal.status.value.lower()} signal "
                      f"(#{existing_signal.id}, from {existing_signal.signal_date}) — check the "
                      f"Signals page."),
        }, status_code=409)

    ticker = w.ticker
    w.status = WatchlistStatus.SIGNALLED
    db.commit()

    try:
        from app.tasks.trading import promote_watchlist_item_task
        promote_watchlist_item_task.delay(
            item_id,
            org_id,
            request.session.get("email", "dashboard"),
            request.session.get("user_id"),
        )
        db.add(AuditLog(
            action=AuditAction.CONFIG_CHANGED,
            actor=request.session.get("email", "dashboard"),
            user_id=request.session.get("user_id"),
            organization_id=org_id,
            ticker=ticker,
            message=f"Trader WL terminal: manual promotion of {ticker} queued successfully"
        ))
        db.commit()
        return JSONResponse({"ok": True, "ticker": ticker, "message": f"{ticker} queued for promotion"})
    except Exception as e:
        from loguru import logger
        from app.models.signal import Watchlist as _WL
        logger.error(f"Trader watchlist: promotion queue failed for {ticker}: {e}")
        w2 = db.query(_WL).filter(_WL.id == item_id, _WL.organization_id == org_id).first()
        if w2:
            w2.status = WatchlistStatus.WATCHING
            db.add(AuditLog(
                action=AuditAction.TASK_ERROR,
                ticker=ticker,
                actor=request.session.get("email", "dashboard"),
                user_id=request.session.get("user_id"),
                organization_id=org_id,
                message=f"Trader WL terminal: promotion of {ticker} failed to queue — reverted. Error: {e}",
            ))
            db.commit()
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)



# ===========================================================================
# ADMIN AREA
# ===========================================================================

@app.get("/admin", response_class=HTMLResponse)
async def admin_root():
    return RedirectResponse("/admin/health", 302)


@app.get("/admin/logs")
async def admin_logs_alias(request: Request, tab: str = Query("tasks")):
    """Unified Logs surface alias — the three log views share one tab bar
    (see admin/_logs_tabs.html) and present as a single page; this alias lets
    /admin/logs?tab=audit|tasks|data deep-link to the right one."""
    target = {"audit": "/admin/audit", "data": "/admin/data-log"}.get(tab, "/admin/tasks")
    return RedirectResponse(target, 302)


@app.get("/admin/tasks", response_class=HTMLResponse)
async def admin_tasks(request: Request, db: Session = Depends(get_db)):
    """Live task log — shows audit events with auto-polling for new entries."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.models.audit import AuditLog, AuditAction
    from app.models.market import Stock, PriceBar
    from app.models.signal import Signal, Watchlist, WatchlistStatus
    from sqlalchemy import func

    ctx = _global(request, db)

    # Seed latest 40 rows so the page is not blank on load.
    # Include NULL org rows (global tasks: regime, price refresh, universe, heartbeat)
    # alongside org-scoped rows — same pattern as the health page _lr() helper.
    # Exclude user-activity rows (FEATURE_ACCESS/FEATURE_ACTION) — those are
    # surfaced in the Super Admin User Activity Console, not the operational log.
    try:
        logs = db.query(AuditLog).filter(
            or_(AuditLog.organization_id == org_id, AuditLog.organization_id == None),
            AuditLog.action.notin_([AuditAction.FEATURE_ACCESS, AuditAction.FEATURE_ACTION]),
        ).order_by(desc(AuditLog.id)).limit(40).all()
    except Exception:
        logs = []
    last_id = logs[0].id if logs else 0

    ACTION_COLOURS = {
        "SCREENER_RUN":         "var(--t-accent)",
        "SCREENER_TICKER":      "var(--text-muted)",   # colour set per-message in JS
        "MARKET_REGIME_CHANGE": "var(--warn)",
        "SIGNAL_GENERATED":     "var(--pos)",
        "ORDER_PLACED":         "var(--pos)",
        "ORDER_FILLED":         "var(--pos)",
        "POSITION_CLOSED":      "var(--neg)",
        "TRADING_PAUSED":       "var(--warn)",
        "TRADING_RESUMED":      "var(--pos)",
        "RULE_TOGGLED":         "var(--a-accent)",
        "CONFIG_CHANGED":       "var(--a-accent)",
        "SYSTEM_STARTED":       "var(--text-muted)",
        "HEALTH_CHECK":         "var(--text-subtle)",
        "TASK_RUN":             "var(--t-accent)",
        "TASK_ERROR":           "var(--neg)",
    }

    display_tz = _get_display_tz(org_id, db)
    seed_logs = [{
        "time":    _fmt_dt(str(l.created_at), display_tz),
        "action":  str(l.action).replace("AuditAction.", ""),
        "ticker":  l.ticker or "—",
        "message": (l.message or "")[:120],
        "color":   ACTION_COLOURS.get(str(l.action).replace("AuditAction.", ""), "var(--text-muted)"),
        "detail":  l.detail or None,
    } for l in logs]

    ctx.update({
        "seed_logs":       seed_logs,
        "last_log_id":     last_id,
        "stock_count":     db.query(func.count(Stock.id)).scalar() or 0,
        "price_bar_count": db.query(func.count(PriceBar.id)).scalar() or 0,
        "signal_count_today":    db.query(func.count(Signal.id)).filter(Signal.signal_date == get_current_date(), Signal.organization_id == org_id).scalar() or 0,
        "watchlist_count": db.query(func.count(Watchlist.id)).filter(Watchlist.status == WatchlistStatus.WATCHING, Watchlist.organization_id == org_id).scalar() or 0,
        "exchange_filters": _get_exchange_filters(org_id, db),
    })
    return templates.TemplateResponse("admin/tasks.html", ctx)


@app.get("/admin/tasks/poll")
async def admin_tasks_poll(request: Request, after: int = 0, db: Session = Depends(get_db)):
    """JSON endpoint polled every 3s by the task log page.
    Returns: new audit log rows since `after` id + fresh data counters."""
    if not _auth(request):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "unauthenticated"}, status_code=401)
    org_id = request.session.get("organization_id")

    from fastapi.responses import JSONResponse
    from app.models.audit import AuditLog, AuditAction
    from app.models.market import Stock, PriceBar
    from app.models.signal import Signal, Watchlist, WatchlistStatus
    from sqlalchemy import func

    ACTION_COLOURS = {
        "SCREENER_RUN":         "var(--t-accent)",
        "SCREENER_TICKER":      "var(--text-muted)",   # colour set per-message in JS
        "MARKET_REGIME_CHANGE": "var(--warn)",
        "SIGNAL_GENERATED":     "var(--pos)",
        "ORDER_PLACED":         "var(--pos)",
        "ORDER_FILLED":         "var(--pos)",
        "POSITION_CLOSED":      "var(--neg)",
        "TRADING_PAUSED":       "var(--warn)",
        "TRADING_RESUMED":      "var(--pos)",
        "RULE_TOGGLED":         "var(--a-accent)",
        "CONFIG_CHANGED":       "var(--a-accent)",
        "SYSTEM_STARTED":       "var(--text-muted)",
        "HEALTH_CHECK":         "var(--text-subtle)",
        "TASK_RUN":             "var(--t-accent)",
        "TASK_ERROR":           "var(--neg)",
    }

    display_tz = _get_display_tz(org_id, db)
    try:
        new_logs = db.query(AuditLog).filter(
            AuditLog.id > after,
            or_(AuditLog.organization_id == org_id, AuditLog.organization_id == None),
            AuditLog.action.notin_([AuditAction.FEATURE_ACCESS, AuditAction.FEATURE_ACTION]),
        ).order_by(desc(AuditLog.id)).limit(50).all()
    except Exception:
        new_logs = []
    return JSONResponse({
        "logs": [{
            "id":      l.id,
            "time":    _fmt_dt(str(l.created_at), display_tz),
            "action":  str(l.action).replace("AuditAction.", ""),
            "ticker":  l.ticker or "—",
            "message": (l.message or "")[:120],
            "color":   ACTION_COLOURS.get(str(l.action).replace("AuditAction.", ""), "var(--text-muted)"),
            "detail":  l.detail or None,
        } for l in new_logs],
        "counts": {
            "stocks":    db.query(func.count(Stock.id)).scalar() or 0,
            "bars":      db.query(func.count(PriceBar.id)).scalar() or 0,
            "signals":   db.query(func.count(Signal.id)).filter(Signal.signal_date == get_current_date(), Signal.organization_id == org_id).scalar() or 0,
            "watchlist": db.query(func.count(Watchlist.id)).filter(Watchlist.status == WatchlistStatus.WATCHING, Watchlist.organization_id == org_id).scalar() or 0,
        },
    })


@app.post("/admin/tasks/clear-log")
async def admin_tasks_clear_log(request: Request, db: Session = Depends(get_db)):
    """Delete all TASK_RUN audit entries for this org (entry/exit check noise). Keeps other events."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")
    from app.models.audit import AuditLog, AuditAction
    db.query(AuditLog).filter(
        AuditLog.organization_id == org_id,
        AuditLog.action == AuditAction.TASK_RUN,
    ).delete(synchronize_session=False)
    db.commit()
    return RedirectResponse("/admin/tasks?msg=cleared", 302)



@app.get("/admin/health", response_class=HTMLResponse)
async def admin_health(request: Request, db: Session = Depends(get_db)):
    if not _auth(request): return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")
    from app.models.audit import AuditLog, AuditAction
    from app.models.account import Account
    from app.models.market import Stock, PriceBar
    from app.models.signal import Signal, Watchlist, WatchlistStatus
    from app.models.config import SystemConfig
    from sqlalchemy import func, or_
    ctx = _global(request, db); display_tz = ctx.get("display_tz","UTC")
    # If worker appears offline, check for recent task activity — it may just be running a
    # long task (e.g. price refresh) which blocks the heartbeat task from executing.
    if ctx.get("worker_status") == "offline":
        from datetime import timedelta
        _cutoff = datetime.utcnow() - timedelta(minutes=45)
        _busy = db.query(AuditLog).filter(
            AuditLog.created_at >= _cutoff,
            AuditLog.action.in_([AuditAction.SCREENER_RUN, AuditAction.SCREENER_TICKER, AuditAction.TASK_RUN])
        ).first()
        if _busy:
            ctx["worker_status"] = "busy"
            ctx["trading_active"] = True  # Don't block trading while worker is busy
    account = db.query(Account).filter(Account.is_active==True,Account.organization_id==org_id).first()
    try: logs=db.query(AuditLog).filter(AuditLog.organization_id==org_id).order_by(desc(AuditLog.created_at)).limit(10).all()
    except Exception: logs=[]
    ae_cfg=db.query(SystemConfig).filter(SystemConfig.key=="active_exchanges",SystemConfig.organization_id==org_id).first()
    ae_str=(ae_cfg.value if ae_cfg else "ASX") or "ASX"
    active_exchanges=[e.strip().upper() for e in ae_str.split(",") if e.strip()]
    has_asx="ASX" in active_exchanges; has_us=any(e in active_exchanges for e in ("NYSE","NASDAQ","US")); has_crypto=any(e.startswith("CRYPTO") for e in active_exchanges)
    exchange_regimes={}
    for ek in (["ASX"] if has_asx else [])+(["NYSE"] if has_us else [])+([e for e in active_exchanges if e.startswith("CRYPTO")] if has_crypto else []):
        rc=db.query(SystemConfig).filter(SystemConfig.key==f"last_market_regime_{ek}",SystemConfig.organization_id==org_id).first()
        exchange_regimes[ek]=rc.value if rc else "Not evaluated"
    stock_count=db.query(func.count(Stock.id)).filter(Stock.asset_type!="CRYPTO").scalar() or 0
    crypto_count=db.query(func.count(Stock.id)).filter(Stock.asset_type=="CRYPTO").scalar() or 0
    price_bar_count=db.query(func.count(PriceBar.id)).scalar() or 0
    today_bars=db.query(func.count(PriceBar.id)).filter(PriceBar.date==get_current_date()).scalar() or 0
    esig=db.query(func.count(Signal.id)).filter(Signal.signal_date==get_current_date(),Signal.organization_id==org_id,Signal.asset_type!="CRYPTO").scalar() or 0
    csig=db.query(func.count(Signal.id)).filter(Signal.signal_date==get_current_date(),Signal.organization_id==org_id,Signal.asset_type=="CRYPTO").scalar() or 0
    ewl=db.query(func.count(Watchlist.id)).filter(Watchlist.organization_id==org_id,Watchlist.status==WatchlistStatus.WATCHING,Watchlist.asset_type!="CRYPTO").scalar() or 0
    cwl=db.query(func.count(Watchlist.id)).filter(Watchlist.organization_id==org_id,Watchlist.status==WatchlistStatus.WATCHING,Watchlist.asset_type=="CRYPTO").scalar() or 0
    def _lr(kws, exch=None, actions=None):
        """Look up the most recent audit log entry matching keywords + optional exchange prefix.
        actions: list of AuditAction values to search; defaults to [TASK_RUN].
        """
        try:
            if actions is None:
                actions = [AuditAction.TASK_RUN]
            q = db.query(AuditLog).filter(
                AuditLog.action.in_(actions),
                or_(*[AuditLog.message.ilike(f"%{kw}%") for kw in kws])
            )
            if exch:
                q = q.filter(AuditLog.message.ilike(f"%{exch}%"))
            log = q.order_by(desc(AuditLog.created_at)).first()
            if log:
                return {"time": _fmt_dt(str(log.created_at), display_tz), "raw": log.created_at, "msg": (log.message or "")[:100]}
        except Exception:
            pass
        return None
    task_runs={
        # health_check writes HEALTH_CHECK action with "Heartbeat:" message
        "heartbeat": _lr(["Heartbeat"], actions=[AuditAction.HEALTH_CHECK]),
        # send_daily_report writes HEALTH_CHECK action
        "report":    _lr(["Daily report", "daily report"], actions=[AuditAction.HEALTH_CHECK]),
        # refresh_universe writes SYSTEM_STARTED — no exchange in message so no exch filter
        "universe":    _lr(["Universe refreshed", "universe"], actions=[AuditAction.SYSTEM_STARTED, AuditAction.TASK_RUN]),
        "universe_us": _lr(["[US] Universe", "US] Universe"], actions=[AuditAction.TASK_RUN]) if has_us else None,
        # refresh_price_data writes TASK_RUN with "[ASX] Price data" prefix (fixed in screening.py)
        "price_asx":    _lr(["Price data", "price data"], "ASX"),
        "price_us":     _lr(["Price data", "price data"], "NYSE") if has_us else None,
        "price_crypto": _lr(["Price data", "price data"], "CRYPTO") if has_crypto else None,
        # evaluate_market_regime_task writes MARKET_REGIME_CHANGE with "[ASX] Market regime:" prefix
        "regime_asx":    _lr(["Market regime", "market regime"], "ASX",    actions=[AuditAction.MARKET_REGIME_CHANGE, AuditAction.TASK_RUN]),
        "regime_us":     _lr(["Market regime", "market regime"], "NYSE",   actions=[AuditAction.MARKET_REGIME_CHANGE, AuditAction.TASK_RUN]) if has_us else None,
        "regime_crypto": _lr(["Market regime", "market regime"], "CRYPTO", actions=[AuditAction.MARKET_REGIME_CHANGE, AuditAction.TASK_RUN]) if has_crypto else None,
        # screener writes SCREENER_RUN with "[ASX] Screen" prefix (fixed in screening.py)
        "screen_asx":    _lr(["Screen", "screen"], "ASX",    actions=[AuditAction.SCREENER_RUN, AuditAction.TASK_RUN]),
        "screen_us":     _lr(["Screen", "screen"], "NYSE",   actions=[AuditAction.SCREENER_RUN, AuditAction.TASK_RUN]) if has_us else None,
        "screen_crypto": _lr(["Screen", "screen"], "CRYPTO", actions=[AuditAction.SCREENER_RUN, AuditAction.TASK_RUN]) if has_crypto else None,
        # entry/exit checks write TASK_RUN with "[ASX] Entry check" / "[NYSE] Entry check" etc.
        "entry_asx":    _lr(["Entry check", "entry trigger"], "ASX"),
        "entry_us":     _lr(["Entry check"], "NYSE")   if has_us else None,
        "entry_crypto": _lr(["Entry check"], "CRYPTO") if has_crypto else None,
        "exit_asx":    _lr(["Exit check", "exit rule"], "ASX"),
        "exit_crypto": _lr(["Exit check"], "CRYPTO") if has_crypto else None,
        # Live price cache refresh — every 5 min for crypto watchlist tickers
        "live_prices_crypto": _lr(["Live price cache", "live price cache"], "CRYPTO") if has_crypto else None,
    }
    from app.config import settings as _settings
    dangerous_toggles = []
    if _settings.app_env == "production":
        if _settings.mock_time_enabled:
            dangerous_toggles.append(f"Mock time is ENABLED (mock_current_time={_settings.mock_current_time!r})")
        if _settings.ibkr_simulate_live:
            dangerous_toggles.append("IBKR simulate mode is ENABLED — fills are simulated, not real")

    ctx.update({"capital":float(account.capital_aud) if account else 0,"is_paper_account":account.is_paper if account else True,
        "recent_logs":[{"action":str(l.action).replace("AuditAction.",""),"ticker":l.ticker or "—","message":(l.message or "")[:80],"actor":l.actor,"time":_fmt_dt(str(l.created_at),display_tz)} for l in logs],
        "has_asx":has_asx,"has_us":has_us,"has_crypto":has_crypto,"active_exchanges":active_exchanges,"exchange_regimes":exchange_regimes,
        "stock_count":stock_count,"crypto_count":crypto_count,"price_bar_count":price_bar_count,"today_bars":today_bars,
        "equity_signal_count":esig,"crypto_signal_count":csig,"equity_wl_count":ewl,"crypto_wl_count":cwl,
        "signal_count_today":esig+csig,"watchlist_count":ewl+cwl,"is_first_run":stock_count==0 and crypto_count==0,"task_runs":task_runs,
        "dangerous_toggles":dangerous_toggles})
    return templates.TemplateResponse("admin/health.html", ctx)


@app.get("/admin/rules", response_class=HTMLResponse)
async def admin_rules(request: Request, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.models.config import RuleConfig, RuleCategory
    from app.models.account import Organization

    org = db.query(Organization).filter(Organization.id == org_id).first()
    tier = org.tier.value if org else "GOLD"

    ctx = _global(request, db)
    # Load org-specific rules, fallback to template if none
    rules = db.query(RuleConfig).filter(RuleConfig.organization_id == org_id).order_by(RuleConfig.category, RuleConfig.sort_order).all()
    if not rules:
        rules = db.query(RuleConfig).filter(RuleConfig.organization_id == None).order_by(RuleConfig.category, RuleConfig.sort_order).all()

    CATEGORY_LABELS = {
        "TREND_TEMPLATE": "Trend Template",  "FUNDAMENTAL": "Fundamentals",
        "VCP": "VCP Pattern",                "MARKET_REGIME": "Market Regime",
        "ENTRY": "Entry Rules",              "EXIT_DEFENSIVE": "Defensive Exits",
        "EXIT_OFFENSIVE": "Offensive Exits", "POSITION_SIZING": "Position Sizing",
        "PORTFOLIO": "Portfolio Rules",      "EARNINGS": "Earnings Rules",
        "CRYPTO": "Crypto Rules",
    }
    CATEGORY_ICONS = {
        "TREND_TEMPLATE": "📈", "FUNDAMENTAL": "📊", "VCP": "🔄",
        "MARKET_REGIME": "🌡️",  "ENTRY": "🎯",       "EXIT_DEFENSIVE": "🛑",
        "EXIT_OFFENSIVE": "💰", "POSITION_SIZING": "⚖️", "PORTFOLIO": "🗂️",
        "EARNINGS": "📅",       "CRYPTO": "₿",
    }

    rules_by_cat = {}
    for r in rules:
        cat = r.category.value
        if cat not in rules_by_cat:
            rules_by_cat[cat] = []

        enabled = r.is_enabled_for_tier(tier)
        threshold = r.threshold_for_tier(tier)

        rules_by_cat[cat].append({
            "id": r.id, "rule_id": r.rule_id, "label": r.label, "category": cat,
            "description": r.description or "", "minervini_ref": r.minervini_ref or "",
            "enabled": enabled, "is_mandatory": r.is_mandatory,
            "asset_types": r.asset_types or "BOTH",
            "threshold": float(threshold) if threshold is not None else None,
            "threshold_label": r.threshold_label or "",
            "threshold_min": float(r.threshold_min) if r.threshold_min else 0,
            "threshold_max": float(r.threshold_max) if r.threshold_max else 999,
        })

    ctx.update({
        "rules_by_category": rules_by_cat,
        "category_labels": CATEGORY_LABELS,
        "category_icons": CATEGORY_ICONS,
        "saved": request.query_params.get("saved", ""),
        "read_only": not _has_permission(request, db, "manage_config"),
    })
    return templates.TemplateResponse("admin/rules.html", ctx)


@app.post("/admin/rules/{rule_id}/toggle")
async def toggle_rule(request: Request, rule_id: str, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if not _has_permission(request, db, "manage_config"):
        return RedirectResponse("/admin/rules?error=Unauthorized", 302)

    org_id = request.session.get("organization_id")
    from app.models.config import RuleConfig
    
    rule = db.query(RuleConfig).filter(
        RuleConfig.rule_id == rule_id,
        RuleConfig.organization_id == org_id
    ).first()
    
    # If no scoped rule exists, clone it from template!
    if not rule:
        template = db.query(RuleConfig).filter(
            RuleConfig.rule_id == rule_id,
            RuleConfig.organization_id == None
        ).first()
        if template:
            rule = RuleConfig(
                rule_id=template.rule_id,
                organization_id=org_id,
                category=template.category,
                label=template.label,
                description=template.description,
                minervini_ref=template.minervini_ref,
                enabled_globally=template.enabled_globally,
                threshold=template.threshold,
                threshold_label=template.threshold_label,
                threshold_min=template.threshold_min,
                threshold_max=template.threshold_max,
                tier_overrides=template.tier_overrides.copy() if template.tier_overrides else {},
                is_mandatory=template.is_mandatory,
                sort_order=template.sort_order,
                updated_by="admin"
            )
            db.add(rule)
            db.flush()

    if rule and not rule.is_mandatory:
        rule.enabled_globally = not rule.enabled_globally
        db.commit()
        return RedirectResponse("/admin/rules?saved=1", 302)
    
    return RedirectResponse("/admin/rules?error=Cannot+toggle+mandatory+rule", 302)


@app.post("/admin/rules/{rule_id}/threshold")
async def update_threshold(request: Request, rule_id: str, threshold: float = Form(...), db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if not _has_permission(request, db, "manage_config"):
        return RedirectResponse("/admin/rules?error=Unauthorized", 302)

    org_id = request.session.get("organization_id")
    from app.models.config import RuleConfig
    
    rule = db.query(RuleConfig).filter(
        RuleConfig.rule_id == rule_id,
        RuleConfig.organization_id == org_id
    ).first()
    
    # If no scoped rule exists, clone it from template!
    if not rule:
        template = db.query(RuleConfig).filter(
            RuleConfig.rule_id == rule_id,
            RuleConfig.organization_id == None
        ).first()
        if template:
            rule = RuleConfig(
                rule_id=template.rule_id,
                organization_id=org_id,
                category=template.category,
                label=template.label,
                description=template.description,
                minervini_ref=template.minervini_ref,
                enabled_globally=template.enabled_globally,
                threshold=template.threshold,
                threshold_label=template.threshold_label,
                threshold_min=template.threshold_min,
                threshold_max=template.threshold_max,
                tier_overrides=template.tier_overrides.copy() if template.tier_overrides else {},
                is_mandatory=template.is_mandatory,
                sort_order=template.sort_order,
                updated_by="admin"
            )
            db.add(rule)
            db.flush()

    if rule and rule.threshold is not None:
        if rule.threshold_min is not None and threshold < float(rule.threshold_min):
            return RedirectResponse(f"/admin/rules?error=Value+must+be+at+least+{rule.threshold_min}", 302)
        if rule.threshold_max is not None and threshold > float(rule.threshold_max):
            return RedirectResponse(f"/admin/rules?error=Value+must+be+at+most+{rule.threshold_max}", 302)
            
        rule.threshold = threshold
        db.commit()
        return RedirectResponse("/admin/rules?saved=1", 302)

    return RedirectResponse("/admin/rules?error=Rule+does+not+support+numeric+thresholds", 302)


@app.get("/admin/config", response_class=HTMLResponse)
async def admin_config(request: Request, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.models.config import SystemConfig
    ctx = _global(request, db)

    # ── Group display metadata ────────────────────────────────────────────────
    GROUP_META = {
        "general":     {"icon": "⚙️",  "title": "General"},
        "broker":      {"icon": "🏦",  "title": "Broker — IBKR"},
        "trading":     {"icon": "📈",  "title": "Trading"},
        "notifications": {"icon": "💬",  "title": "Alert & Chat Channels"},
        "data":        {"icon": "📡",  "title": "Data Sources"},
        "risk":        {"icon": "🛡️",  "title": "Risk Management"},
        "crypto":      {"icon": "₿",   "title": "Crypto Exchange"},
        "reporting":   {"icon": "📊",  "title": "Reporting"},
    }

    # ── Per-key UI hints ──────────────────────────────────────────────────────
    FIELD_HINTS = {
        # General
        "org_timezone":          {"control": "timezone_select", "options": [
                                     ("Australia/Sydney","Australia/Sydney (AEST)"),
                                     ("Australia/Melbourne","Australia/Melbourne"),
                                     ("America/New_York","America/New_York (ET)"),
                                     ("America/Chicago","America/Chicago (CT)"),
                                     ("America/Los_Angeles","America/Los_Angeles (PT)"),
                                     ("Europe/London","Europe/London (GMT/BST)"),
                                     ("Asia/Tokyo","Asia/Tokyo (JST)"),
                                     ("UTC","UTC"),
                                 ]},
        "active_exchanges":      {"control": "exchange_multiselect",
                                  "hint_extra": "Select which exchanges to trade on. Crypto requires a configured API key below."},
        "working_capital_currency": {"control": "readonly",
                                     "hint_extra": "Set by Super Admin. Base currency used for position sizing."},
        # Broker
        "ibkr_account":          {"placeholder": "DU1234567",       "hint_extra": "Your IBKR sub-account number (assigned by the platform operator)"},
"ibkr_account_usd":      {"placeholder": "U9876543",       "hint_extra": "USD sub-account (leave blank to use same account)"},
        "fx_audusd_override":    {"control": "number", "placeholder": "0.65", "step": "0.0001",
                                  "hint_extra": "Manual AUD/USD rate override. Leave blank to fetch live."},
        # Alerts & Chat
        "telegram_enabled":      {},   # boolean
        "telegram_bot_token":    {"control": "password", "hint_extra": "Telegram bot token from @BotFather"},
        "telegram_chat_id":      {"placeholder": "123456789,987654321",
                                  "hint_extra": "Comma-separated list of Telegram chat IDs — one per org user, or a single group chat ID"},
        # ASX Universe
        "asx_universe_scope":    {"control": "select", "options": [
                                     ("ASX200",     "ASX200 — Top 200 by market cap (default, fast)"),
                                     ("ASX300",     "ASX300 — Top 300 incl. mid-caps (~300 stocks)"),
                                     ("ALL_LISTED", "All Listed — Full ASX universe ~2,200+ stocks (slow)"),
                                 ],
                                  "hint_extra": "Controls which ASX stocks the screener scans. Larger scope = longer screener runtime (~15–45 min for ALL_LISTED). Run 'Refresh ASX Universe' after changing."},
        # US Universe
        "us_universe_scope":     {"control": "select", "options": [
                                     ("SP500+NASDAQ100", "S&P 500 + NASDAQ-100 — ~600 elite stocks (default, recommended)"),
                                     ("SP500",           "S&P 500 only — ~500 large-cap US stocks"),
                                     ("NASDAQ100",       "NASDAQ-100 only — 100 top NASDAQ tech stocks"),
                                 ],
                                  "hint_extra": "Controls which US stocks are seeded for screening. Refreshed weekly (Sunday 10pm AEST). Run 'Refresh US Universe' on Health page after changing."},
        # Data
        "fmp_api_key":           {"control": "password",
                                  "hint_extra": "Financial Modeling Prep API key (free tier: 250 calls/day)",
                                  "link_url": "https://financialmodelingprep.com/developer/docs/",
                                  "link_text": "Get free key →"},
        # Risk / trading
        "weekly_injection_aud":  {"control": "number", "prefix": "A$", "placeholder": "1000",
                                  "hint_extra": "Weekly capital added to the account for position sizing calculations"},
        "entry_limit_buffer_pct": {"control": "number", "placeholder": "1.0", "step": "0.1",
                                   "hint_extra": "Automated equity breakout entries use a BUY STOP-LIMIT order: the limit "
                                                 "sits this % above the stop trigger (max of the pivot and confirmed "
                                                 "breakout price), capping slippage instead of chasing an extended stock."},
        "trading_kill_switch":   {"hint_extra": "Emergency halt: blocks ALL new entries immediately and cancels every "
                                                 "working entry order. Blunter than Trading Paused above (which only "
                                                 "blocks new entries going forward). Also flippable via Telegram: "
                                                 "KILLSWITCH ON | KILLSWITCH OFF."},
        "max_daily_loss_aud":    {"control": "number", "prefix": "A$", "placeholder": "0 (disabled)",
                                  "hint_extra": "Halts new entries for the rest of the day once today's realised + "
                                                "unrealised P&L breaches -this amount. Leave at 0 to disable."},
        "entry_skip_open_minutes": {"control": "number", "placeholder": "10", "step": "1",
                                    "hint_extra": "Skip ASX entry checks for this many minutes after the 10:00am open — "
                                                  "the staggered opening auction can confirm false breakouts on partial-day volume."},
        # Crypto
        "crypto_exchange_key":   {"control": "crypto_exchange_select",
                                  "hint_extra": "Active crypto exchange. Must also set API key/secret below. "
                                                "Note: MEXC does not support a testnet — paper trading uses simulation mode."},
        "crypto_api_key":        {"control": "password",
                                  "hint_extra": "Exchange API key. For MEXC: create an API key at mexc.com → Account → API Management. "
                                                "Enable 'Trade' permission; restrict to your IP for safety.",
                                  "link_url":  "https://www.mexc.com/user/openapi",
                                  "link_text": "MEXC API Management →"},
        "crypto_api_secret":     {"control": "password",
                                  "hint_extra": "Exchange API secret. Keep this private — never share it."},
        "crypto_testnet":        {"hint_extra": "Enable sandbox/testnet mode. "
                                               "Note: MEXC does not have a testnet — enabling this for MEXC forces simulation mode (no real orders)."},
        "mexc_trading_pairs":    {"control": "text",
                                  "placeholder": "BTC-USD,ETH-USD,SOL-USD,XRP-USD",
                                  "hint_extra": "MEXC only: comma-separated list of up to 30 pairs your API key can trade "
                                                "(e.g. BTC-USD,ETH-USD,SOL-USD). Leave blank to use the default top-300 list. "
                                                "After saving, click 'Re-seed Crypto Universe' on the Health page to apply."},
    }

    # ── Enabled exchanges (for multiselect chip UI) ───────────────────────────
    enabled_exchanges = []
    try:
        from app.models.exchange import ExchangeConfig as _EC
        for e in db.query(_EC).filter(_EC.is_enabled == True).order_by(_EC.sort_order).all():
            enabled_exchanges.append({"key": e.exchange_key, "name": e.display_name,
                                      "flag": e.flag_emoji or "", "asset_type": e.asset_type})
    except Exception:
        db.rollback()
        enabled_exchanges = [{"key": "ASX", "name": "ASX", "flag": "🇦🇺", "asset_type": "EQUITY"}]

    configs = db.query(SystemConfig).filter(SystemConfig.organization_id == org_id).order_by(SystemConfig.group, SystemConfig.key).all()
    by_group: dict = {}
    # "system" holds auto-calculated telemetry (market regime, worker heartbeat) and
    # settings whose real edit surface is elsewhere (noVNC/VNC password live on
    # Super Admin -> Org Detail). Never show it here, even for superadmins.
    HIDDEN_GROUPS = {"system"}
    for c in configs:
        if c.group in HIDDEN_GROUPS:
            continue
        grp = c.group or "general"
        if grp not in by_group:
            by_group[grp] = []
        val = c.value or ""
        by_group[grp].append({
            "id":          c.id,
            "key":         c.key,
            "value":       val if not c.is_secret else "",
            "label":       c.label or c.key,
            "description": c.description or "",
            "is_secret":   c.is_secret,
            "value_type":  c.value_type.value if hasattr(c.value_type, "value") else str(c.value_type or "STRING"),
            "has_value":   bool(val and val.strip()),
            "hint":        FIELD_HINTS.get(c.key, {}),
        })

    ctx.update({
        "configs_by_group":  by_group,
        "group_meta":        GROUP_META,
        "enabled_exchanges": enabled_exchanges,
        "saved":             request.query_params.get("saved", ""),
        "error":             request.query_params.get("error", ""),
    })
    return templates.TemplateResponse("admin/config.html", ctx)


@app.post("/admin/config/{config_id}/update")
async def update_config(request: Request, config_id: int, value: str = Form(...), db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")

    from app.models.config import SystemConfig
    from app.models.audit import AuditLog, AuditAction

    # Try org-scoped row first; fall back to global row (organization_id IS NULL) so that
    # superadmins viewing global configs (mock_clock, regime, etc.) can save them too.
    c = db.query(SystemConfig).filter(SystemConfig.id == config_id).first()
    if c is None:
        return RedirectResponse("/admin/config?error=notfound", 302)

    # Security: regular org users may only edit rows belonging to their own org
    user_role = request.session.get("user_role")
    if user_role != "superadmin" and c.organization_id != org_id:
        return RedirectResponse("/admin/config?error=forbidden", 302)

    # I2 (CLAUDE.md #41): enforce one IBKR account per org. Without this, two
    # orgs could save the same ibkr_account and both submit orders to / read
    # positions from the same real account — double entries, cross-org closes.
    if c.key == "ibkr_account" and value and value.strip():
        from sqlalchemy import func as _cfg_func
        _new_acct = value.strip()
        _dupe = db.query(SystemConfig).filter(
            SystemConfig.key == "ibkr_account",
            SystemConfig.organization_id.isnot(None),
            SystemConfig.organization_id != c.organization_id,
            _cfg_func.lower(SystemConfig.value) == _new_acct.lower(),
        ).first()
        if _dupe:
            from urllib.parse import quote as _urlquote
            _msg = _urlquote(f'IBKR account "{_new_acct}" is already in use by another organization — each org needs its own account')
            return RedirectResponse(f"/admin/config?error={_msg}", 302)

    if value is not None:
        old_val = c.value
        c.value = value
        c.updated_by = request.session.get("email", "dashboard")

        # ── Auto-seed crypto watchlist labels when active_exchanges gains a CRYPTO_ key ──
        if c.key == "active_exchanges" and c.organization_id:
            old_keys = set((old_val or "").replace(" ", "").upper().split(","))
            new_keys = set(value.replace(" ", "").upper().split(","))
            newly_added_crypto = [k for k in new_keys - old_keys if k.startswith("CRYPTO_")]
            if newly_added_crypto:
                try:
                    from app.models.signal import WatchlistLabel
                    _crypto_seed_labels = [
                        ("Crypto Core",  "#06b6d4", False, 10),
                        ("DeFi",         "#10b981", False, 11),
                        ("Altcoins",     "#8b5cf6", False, 12),
                        ("Crypto Watch", "#f97316", False, 13),
                    ]
                    for lname, lcolor, lis_default, lorder in _crypto_seed_labels:
                        exists = db.query(WatchlistLabel).filter(
                            WatchlistLabel.organization_id == c.organization_id,
                            WatchlistLabel.name == lname,
                        ).first()
                        if not exists:
                            db.add(WatchlistLabel(
                                organization_id=c.organization_id,
                                name=lname, color=lcolor,
                                is_default=lis_default, sort_order=lorder,
                            ))
                    db.flush()
                    logger.info(f"Seeded crypto watchlist labels for org {c.organization_id} (added: {newly_added_crypto})")
                except Exception as _lbl_err:
                    logger.warning(f"Could not seed crypto labels: {_lbl_err}")

        # Synchronize working capital configuration with active Account capital
        # NOTE: only working_capital_aud drives account.capital_aud.
        # weekly_injection_aud is stored as a config reference only — it must NOT
        # overwrite capital_aud, which is the position-sizing basis.
        if c.key == "working_capital_aud" and c.organization_id:
            from app.models.account import Account
            account = db.query(Account).filter(Account.is_active == True, Account.organization_id == c.organization_id).first()
            if account:
                try:
                    account.capital_aud = float(value)
                except ValueError:
                    pass

        try:
            db.add(AuditLog(
                action=AuditAction.CONFIG_CHANGED,
                entity_id=c.key,
                before_value=old_val,
                after_value=value,
                actor=request.session.get("email", "dashboard"),
                user_id=request.session.get("user_id"),
                organization_id=c.organization_id,
            ))
            db.commit()
        except Exception as _audit_err:
            logger.warning(f"Config save audit log failed (non-fatal): {_audit_err}")
            try:
                db.rollback()
                c = db.query(SystemConfig).filter(SystemConfig.id == config_id).first()
                if c:
                    c.value = value
                    c.updated_by = request.session.get("email", "dashboard")
                    if c.key == "working_capital_aud" and c.organization_id:
                        from app.models.account import Account
                        account = db.query(Account).filter(Account.is_active == True, Account.organization_id == c.organization_id).first()
                        if account:
                            try:
                                account.capital_aud = float(value)
                            except ValueError:
                                pass
                    db.commit()
            except Exception as _save_err:
                logger.error(f"Config save failed entirely: {_save_err}")
    return RedirectResponse("/admin/config?saved=1", 302)


@app.get("/admin/audit", response_class=HTMLResponse)
async def admin_audit(request: Request, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.models.audit import AuditLog, AuditAction
    ctx = _global(request, db)
    action_f   = request.query_params.get("action", "ALL")
    ticker_f   = request.query_params.get("ticker", "").strip().upper()
    actor_f    = request.query_params.get("actor", "").strip()
    exchange_f = request.query_params.get("exchange", "ALL").upper()

    q = db.query(AuditLog).filter(AuditLog.organization_id == org_id).order_by(desc(AuditLog.created_at))
    # User-activity rows live in the Super Admin Activity Console, not the org
    # operational audit — hide them unless the admin explicitly selects them.
    if action_f == "ALL":
        q = q.filter(AuditLog.action.notin_([AuditAction.FEATURE_ACCESS, AuditAction.FEATURE_ACTION]))
    if action_f != "ALL":
        q = q.filter(AuditLog.action == action_f)
    if ticker_f:
        # Allow bare code (BHP/BTC/AAPL) or full ticker (BHP.AX / BTC-USD / AAPL)
        t_asx    = ticker_f if ticker_f.endswith(".AX")   else ticker_f + ".AX"
        t_crypto = ticker_f if ticker_f.endswith("-USD")  else ticker_f + "-USD"
        q = q.filter(or_(AuditLog.ticker == ticker_f, AuditLog.ticker == t_asx, AuditLog.ticker == t_crypto))
    if actor_f:
        q = q.filter(AuditLog.actor.ilike(f"%{actor_f}%"))
    # Exchange filter.
    # Rows WITH a ticker: filter by suffix (.AX = ASX, -USD/-USDT = CRYPTO, bare = US).
    # Rows WITHOUT a ticker (TASK_RUN, CONFIG_CHANGED, etc.): filter by the [EXCHANGE]
    # prefix that the task log writer prepends to messages (e.g. "[ASX] Entry check …",
    # "[CRYPTO] Entry check …"). Without this second clause every exchange filter would
    # show ALL ticker-less events, which is the bug the user reported.
    if exchange_f not in ("", "ALL"):
        from sqlalchemy import and_ as _and
        if exchange_f == "ASX":
            q = q.filter(or_(
                AuditLog.ticker.like("%.AX"),
                _and(
                    or_(AuditLog.ticker == None, AuditLog.ticker == ""),
                    or_(
                        AuditLog.message.like("[ASX]%"),
                        _and(
                            ~AuditLog.message.like("[CRYPTO%"),
                            ~AuditLog.message.like("[NYSE%"),
                            ~AuditLog.message.like("[NASDAQ%"),
                        )
                    )
                )
            ))
        elif exchange_f == "CRYPTO":
            q = q.filter(or_(
                AuditLog.ticker.like("%-USD"),
                AuditLog.ticker.like("%-USDT"),
                AuditLog.ticker.like("%-AUD"),
                _and(
                    or_(AuditLog.ticker == None, AuditLog.ticker == ""),
                    AuditLog.message.like("[CRYPTO]%")
                )
            ))
        elif exchange_f == "US":
            q = q.filter(or_(
                _and(
                    AuditLog.ticker != None,
                    AuditLog.ticker != "",
                    ~AuditLog.ticker.like("%.AX"),
                    ~AuditLog.ticker.like("%-USD%"),
                ),
                _and(
                    or_(AuditLog.ticker == None, AuditLog.ticker == ""),
                    or_(
                        AuditLog.message.like("[NYSE]%"),
                        AuditLog.message.like("[NASDAQ]%"),
                        AuditLog.message.like("[US]%"),
                    )
                )
            ))

    try:
        logs = q.limit(200).all()
    except Exception:
        logs = []
    audit_tz = _get_display_tz(org_id, db)
    ef = _get_exchange_filters(org_id, db)
    ctx.update({
        "logs": [{"time": _fmt_dt(str(l.created_at), audit_tz), "action": str(l.action).replace("AuditAction.", ""),
                  "actor": l.actor or "system", "ticker": l.ticker or "—",
                  "message": (l.message or "")[:80],
                  "before": (l.before_value or "")[:20], "after": (l.after_value or "")[:20]}
                 for l in logs],
        "actions": ["ALL"] + sorted(set(str(a.value) for a in AuditAction)),
        "filter_action": action_f,
        "filter_ticker": ticker_f,
        "filter_actor": actor_f,
        "exchange_filters": ef,
        "active_exchange_filter": exchange_f,
        "base_url": "/admin/audit",
    })
    return templates.TemplateResponse("admin/audit.html", ctx)


# ===========================================================================
# COMMUNICATIONS HUB — WEBHOOKS + ADMIN CONSOLE
# ===========================================================================

@app.post("/webhook/telegram")
async def webhook_telegram(request: Request, db: Session = Depends(get_db)):
    """
    Handle incoming messages from Telegram Bot API.
    Security: incoming chat_id must match a telegram_chat_id in SystemConfig,
    and (once configured) the X-Telegram-Bot-Api-Secret-Token header must match
    the org's telegram_webhook_secret — see /admin/telegram/set-webhook.
    """
    from fastapi.responses import JSONResponse
    from app.agent.commands import AgentCommandHandler
    from app.notifications.telegram import TelegramNotifier
    from app.models.config import SystemConfig

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    msg = body.get("message")
    if not msg:
        return JSONResponse({"ok": True}) # Ignore edits/other updates

    chat_id = str(msg.get("chat", {}).get("id", ""))
    text    = msg.get("text", "").strip()
    user_id = str(msg.get("from", {}).get("id", ""))

    if not chat_id or not text:
        return JSONResponse({"ok": True})

    # Look up organization by telegram_chat_id — value may be a comma-separated
    # list (one chat per org user, or a single shared group chat).
    org_id = None
    for config in db.query(SystemConfig).filter(SystemConfig.key == "telegram_chat_id").all():
        configured_ids = [c.strip() for c in (config.value or "").split(",") if c.strip()]
        if chat_id in configured_ids:
            org_id = config.organization_id
            break

    if org_id is None:
        logger.warning(f"Telegram message from unknown chat {chat_id} — ignored")
        return JSONResponse({"ok": True})

    # Verify this request genuinely came from Telegram, not a forged POST with
    # a guessed/known chat_id — enforced once the org has re-registered its
    # webhook with a secret_token (see /admin/telegram/set-webhook). Orgs that
    # haven't re-registered since this check shipped have no secret configured
    # yet, so they fail open here until they do (poll_telegram_updates is
    # unaffected either way, as it doesn't go through this endpoint).
    secret_cfg = db.query(SystemConfig).filter(
        SystemConfig.key == "telegram_webhook_secret",
        SystemConfig.organization_id == org_id,
    ).first()
    if secret_cfg and secret_cfg.value:
        import hmac
        incoming_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(incoming_secret.encode(), secret_cfg.value.encode()):
            logger.warning(f"Telegram webhook request for org {org_id} failed secret_token check — rejecting")
            return JSONResponse({"ok": False}, status_code=403)

    # Check if Telegram is enabled
    cfg_enabled = db.query(SystemConfig).filter(
        SystemConfig.key == "telegram_enabled",
        SystemConfig.organization_id == org_id
    ).first()
    if cfg_enabled and cfg_enabled.value.lower() not in ("true", "1", "yes"):
        return JSONResponse({"ok": True})

    handler  = AgentCommandHandler(organization_id=org_id)
    response = handler.handle(text, f"telegram:{chat_id}")

    notifier = TelegramNotifier(organization_id=org_id)
    notifier.send(response, chat_id=chat_id)

    return JSONResponse({"ok": True})


@app.get("/admin/comms", response_class=HTMLResponse)
async def admin_comms(request: Request, db: Session = Depends(get_db)):
    """Communications hub — status for the Telegram integration."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")

    from app.notifications.telegram import TelegramNotifier
    from app.models.audit import AuditLog, AuditAction
    from app.config import settings
    import httpx

    ctx = _global(request, db)

    # ── Telegram Context ──────────────────────────────────────────────────
    tg_status = "NOT_CONFIGURED"
    tg_bot_info = {}
    tg_chat_id = ""
    try:
        tg_notifier = TelegramNotifier(organization_id=org_id)
        if tg_notifier.token:
            # Check bot token validity
            resp = httpx.get(f"https://api.telegram.org/bot{tg_notifier.token}/getMe", timeout=5)
            if resp.status_code == 200:
                tg_status = "CONNECTED"
                tg_bot_info = resp.json().get("result", {})
            else:
                tg_status = "INVALID_TOKEN"
        tg_chat_id = ", ".join(tg_notifier.chat_ids)
    except Exception as _e:
        tg_status = "ERROR"

    # ── Recent Commands ───────────────────────────────────────────────────
    try:
        recent_cmds = db.query(AuditLog).filter(
            AuditLog.action == AuditAction.AGENT_COMMAND,
            AuditLog.organization_id == org_id
        ).order_by(desc(AuditLog.created_at)).limit(20).all()
    except Exception:
        recent_cmds = []

    ctx.update({
        "tg_status":       tg_status,
        "tg_bot_info":     tg_bot_info,
        "tg_chat_id":      tg_chat_id,
        "tg_webhook_url":  f"{str(request.base_url).rstrip('/')}/webhook/telegram",
        "recent_commands": [
            {"time":    _fmt_dt(str(l.created_at), ctx.get("display_tz", "UTC")),
             "message": (l.detail or {}).get("message", ""),
             "sender":  (l.detail or {}).get("sender", "")}
            for l in recent_cmds
        ],
        "msg": request.query_params.get("msg", ""),
    })
    return templates.TemplateResponse("admin/comms.html", ctx)


@app.post("/admin/telegram/set-webhook")
async def telegram_set_webhook(request: Request, db: Session = Depends(get_db)):
    """Register the AstraTrade webhook with Telegram."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")

    from app.notifications.telegram import TelegramNotifier
    import httpx

    notifier = TelegramNotifier(organization_id=org_id)
    if not notifier.token:
        return RedirectResponse("/admin/comms?msg=tg_no_token", 302)

    hook_url = f"{str(request.base_url).rstrip('/')}/webhook/telegram"
    # For safety, ensure it's HTTPS if not localhost
    if "localhost" not in hook_url and "0.0.0.0" not in hook_url and not hook_url.startswith("https"):
        logger.warning(f"Telegram webhook registration: URL is not HTTPS ({hook_url}). Telegram requires HTTPS.")

    secret_token = _get_or_create_telegram_webhook_secret(org_id, db)
    try:
        url = f"https://api.telegram.org/bot{notifier.token}/setWebhook"
        resp = httpx.post(url, data={"url": hook_url, "secret_token": secret_token}, timeout=10)
        ok = resp.status_code == 200 and resp.json().get("ok")
        return RedirectResponse(f"/admin/comms?msg={'tg_hook_ok' if ok else 'tg_hook_fail'}", 302)
    except Exception as e:
        logger.error(f"Telegram setWebhook failed: {e}")
        return RedirectResponse("/admin/comms?msg=tg_hook_fail", 302)


@app.post("/admin/comms/send-test")
async def comms_send_test(request: Request):
    """Send a test message to the active notifier integration."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")

    from app.notifications import get_notifier
    notifier = get_notifier(organization_id=org_id)
    ok = notifier.send("✅ AstraTrade test message — integration is working!")
    return RedirectResponse(f"/admin/comms?msg={'test_ok' if ok else 'test_fail'}", 302)


@app.post("/admin/comms/console-command")
async def comms_console_command(
    request: Request,
    command: str = Form(...),
    db: Session = Depends(get_db)
):
    """Execute a command from the web-based interactive chat console."""
    from fastapi.responses import JSONResponse
    if not _auth(request):
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    org_id = request.session.get("organization_id")
    if not org_id:
        return JSONResponse({"ok": False, "error": "No organization context"}, status_code=400)

    from app.agent.commands import AgentCommandHandler
    # Use a specific sender for console executions
    sender_id = f"console_user_{request.session.get('user_id', 'unknown')}"

    handler = AgentCommandHandler(organization_id=org_id)
    response = handler.handle(command.strip(), sender_id)

    return {"ok": True, "response": response}


# ===========================================================================
# ADMIN DATA LOG — intraday entry check snapshots
# ===========================================================================

@app.get("/admin/data-log", response_class=HTMLResponse)
async def admin_data_log(
    request: Request,
    db: Session = Depends(get_db),
    ticker: str = Query(None),
    window: str = Query("latest"),   # latest | 15 | 30 | 60 | today
    only_confirmed: bool = Query(False),
    exchange: str = Query("ALL"),
):
    """
    Admin Data Log — shows per-signal intraday metric snapshots captured every
    5–15 minutes during market hours, with per-rule pass/fail colouring so users
    can see exactly what metrics are being evaluated against AstraTrade rules.
    Data source badge warns when using delayed yfinance data (≈15-20 min for ASX).
    """
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")
    ctx = _global(request, db)

    from app.models.market import EntryCheckLog
    from app.models.signal import Signal
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    now_utc = _dt.utcnow()
    # Determine time filter
    if window == "today":
        cutoff = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    elif window == "15":
        cutoff = now_utc - _td(minutes=15)
    elif window == "30":
        cutoff = now_utc - _td(minutes=30)
    elif window == "60":
        cutoff = now_utc - _td(minutes=60)
    else:
        # "latest" = most recent snapshot per signal
        cutoff = now_utc - _td(hours=24)

    q = db.query(EntryCheckLog).filter(
        EntryCheckLog.organization_id == org_id,
        EntryCheckLog.checked_at >= cutoff,
    )
    if ticker:
        q = q.filter(EntryCheckLog.ticker == ticker.upper().strip())
    if only_confirmed:
        q = q.filter(EntryCheckLog.breakout_confirmed == True)
    # Exchange filter — use the stored exchange_key (canonical source), not ticker-string guessing
    exchange_f = (exchange or "ALL").upper()
    if exchange_f not in ("", "ALL"):
        if exchange_f == "ASX":
            q = q.filter(EntryCheckLog.exchange_key == "ASX")
        elif exchange_f == "CRYPTO":
            q = q.filter(EntryCheckLog.exchange_key.like("CRYPTO%"))
        elif exchange_f == "US":
            q = q.filter(EntryCheckLog.exchange_key.in_(["NYSE", "NASDAQ"]))
        else:
            q = q.filter(EntryCheckLog.exchange_key == exchange_f)

    logs = q.order_by(desc(EntryCheckLog.checked_at)).limit(200).all()

    # For "latest" mode: deduplicate to most recent per signal
    if window == "latest":
        seen = set()
        deduped = []
        for row in logs:
            key = (row.signal_id, row.ticker)
            if key not in seen:
                seen.add(key)
                deduped.append(row)
        logs = deduped

    # Serialize for template — flatten rule_results JSON into per-rule list
    RULE_LABELS = {
        "vcp_breakout_price":   "Price ≥ Pivot",
        "vcp_breakout_volume":  "Volume Surge",
        "vcp_contractions":     "Contraction Count",
        "vcp_base_weeks":       "Base Length (min weeks)",
        "vcp_max_extension":    "Max Extension",
    }

    rows = []
    for log in logs:
        rules = log.rule_results or {}
        rule_list = [
            {
                "id":        rid,
                "label":     RULE_LABELS.get(rid, rid.replace("_", " ").title()),
                "passed":    rdata.get("passed", False),
                "value":     rdata.get("value"),
                "threshold": rdata.get("threshold"),
                "message":   rdata.get("message", ""),
            }
            for rid, rdata in rules.items()
        ]
        checked_local = _fmt_dt(str(log.checked_at), ctx.get("display_tz", "UTC"))
        source_label  = "IBKR Real-Time" if log.data_source == "ibkr" else (
                         "IR Real-Time"         if log.data_source == "independentreserve" else
                         "EOD Fallback"         if log.data_source == "eod_fallback" else
                         "yfinance (~20 min delayed)"
                        )
        source_color  = "pos"  if log.data_source in ("ibkr", "independentreserve") else (
                         "warn" if log.data_source == "eod_fallback" else
                         "neg"  # yfinance for crypto = stale, flag red
                        )
        rows.append({
            "id":               log.id,
            "ticker":           log.ticker,
            "checked_at":       checked_local,
            "checked_raw":      log.checked_at.isoformat() + "Z",
            "price_current":    float(log.price_current)   if log.price_current   else None,
            "price_pivot":      float(log.price_pivot)     if log.price_pivot     else None,
            "price_stop":       float(log.price_stop)      if log.price_stop      else None,
            "price_vs_pivot":   float(log.price_vs_pivot)  if log.price_vs_pivot  else None,
            "vol_current":      log.vol_current,
            "vol_avg_50":       float(log.vol_avg_50)      if log.vol_avg_50      else None,
            "vol_ratio":        float(log.vol_ratio)       if log.vol_ratio       else None,
            "ma_50":            float(log.ma_50)           if log.ma_50           else None,
            "ma_150":           float(log.ma_150)          if log.ma_150          else None,
            "ma_200":           float(log.ma_200)          if log.ma_200          else None,
            "rs_rating":        float(log.rs_rating)       if log.rs_rating       else None,
            "pct_from_52w_high":float(log.pct_from_52w_high) if log.pct_from_52w_high else None,
            "breakout_confirmed": log.breakout_confirmed,
            "rules":            rule_list,
            "data_source":      log.data_source,
            "source_label":     source_label,
            "source_color":     source_color,
            "delay_mins":       log.data_delay_mins,
        })

    # Distinct tickers for filter dropdown
    all_tickers = sorted(set(r["ticker"] for r in rows))
    has_delayed_data = any(r["data_source"] == "yfinance" for r in rows)
    has_realtime_data = any(r["data_source"] in ("ibkr", "independentreserve") for r in rows)

    ef = _get_exchange_filters(org_id, db)
    ctx.update({
        "rows":                   rows,
        "ticker":                 ticker or "",
        "window":                 window,
        "only_confirmed":         only_confirmed,
        "all_tickers":            all_tickers,
        "total":                  len(rows),
        "has_delayed_data":       has_delayed_data,
        "has_realtime_data":      has_realtime_data,
        "msg":                    request.query_params.get("msg", ""),
        "exchange_filters":       ef,
        "active_exchange_filter": exchange_f,
        "base_url":               "/admin/data-log",
        "extra_params":           f"window={window}" + (f"&ticker={ticker}" if ticker else "") + ("&only_confirmed=true" if only_confirmed else ""),
    })
    return templates.TemplateResponse("admin/data_log.html", ctx)


@app.get("/admin/data-log/poll")
async def admin_data_log_poll(
    request: Request,
    db: Session = Depends(get_db),
    after_id: int = Query(0),
):
    """
    Lightweight JSON poll endpoint — returns rows newer than after_id.
    Used by the auto-refresh on the Data Log page.
    """
    if not _auth(request):
        return {"rows": [], "error": "unauthorized"}
    org_id = request.session.get("organization_id")
    from app.models.market import EntryCheckLog
    from datetime import timedelta as _td, datetime as _dt

    cutoff = _dt.utcnow() - _td(hours=24)
    rows = db.query(EntryCheckLog).filter(
        EntryCheckLog.organization_id == org_id,
        EntryCheckLog.id > after_id,
        EntryCheckLog.checked_at >= cutoff,
    ).order_by(desc(EntryCheckLog.checked_at)).limit(50).all()

    return {
        "rows": [
            {
                "id":               r.id,
                "ticker":           r.ticker,
                "checked_at":       r.checked_at.isoformat() + "Z",
                "price_current":    float(r.price_current)  if r.price_current  else None,
                "price_pivot":      float(r.price_pivot)    if r.price_pivot    else None,
                "price_vs_pivot":   float(r.price_vs_pivot) if r.price_vs_pivot else None,
                "vol_ratio":        float(r.vol_ratio)      if r.vol_ratio      else None,
                "breakout_confirmed": r.breakout_confirmed,
                "data_source":      r.data_source,
                "delay_mins":       r.data_delay_mins,
                "rule_results":     r.rule_results or {},
            }
            for r in rows
        ],
        "count": len(rows),
    }


# ===========================================================================
# SUPER ADMIN AREA — SAAS MANAGEMENT
# ===========================================================================

@app.get("/superadmin/organizations", response_class=HTMLResponse)
async def superadmin_organizations(request: Request, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    from app.models.account import Organization
    ctx = _global(request, db)
    organizations = db.query(Organization).order_by(Organization.name).all()
    ctx.update({"organizations": organizations})
    return templates.TemplateResponse("superadmin/organizations.html", ctx)


@app.post("/superadmin/organizations/create")
async def superadmin_organizations_create(
    request: Request,
    name: str = Form(...),
    tier: str = Form(...),
    admin_name: str = Form(...),
    admin_email: str = Form(...),
    db: Session = Depends(get_db)
):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    from app.config import settings
    from app.models.account import Organization, OrganizationTier, Account, AccountTier
    from app.models.auth import User, Role, hash_password
    from app.models.config import SystemConfig, ConfigValueType

    # Check unique constraints
    existing_org = db.query(Organization).filter(Organization.name == name.strip()).first()
    existing_user = db.query(User).filter(User.email == admin_email.strip().lower()).first()

    ctx = _global(request, db)
    organizations = db.query(Organization).order_by(Organization.name).all()
    ctx.update({"organizations": organizations})

    if existing_org:
        ctx.update({"error": f"Organisation '{name}' already exists. Choose a different organisation name."})
        return templates.TemplateResponse("superadmin/organizations.html", ctx, status_code=400)
    # NOTE: an existing user is NOT an error under the multi-org model — the existing
    # account is simply added as a member/admin of the new organisation (see step 3).

    # Validate tier value before touching the DB so a bad enum doesn't 500.
    if tier not in OrganizationTier.__members__:
        ctx.update({"error": f"Invalid service tier '{tier}'. Choose Bronze, Silver or Gold."})
        return templates.TemplateResponse("superadmin/organizations.html", ctx, status_code=400)

    try:
        # 1. Create Organization
        org = Organization(name=name.strip(), tier=OrganizationTier[tier], is_active=True)
        db.add(org)
        db.flush()

        # 2. Assign Default Account for Org
        # Find AccountTier (we fallback to first tier, e.g. ADMIN or starter if present)
        acc_tier = db.query(AccountTier).first()
        if not acc_tier:
            # Fallback create a dummy account tier if database has none seeded
            from app.models.account import TierLevel
            acc_tier = AccountTier(level=TierLevel.ADMIN, label="Administrator", max_positions=10)
            db.add(acc_tier)
            db.flush()

        account = Account(
            name=f"{name.strip()} Primary Account",
            organization_id=org.id,
            tier_id=acc_tier.id,
            capital_aud=5000.0,
            is_active=True,
            is_paper=True
        )
        db.add(account)

        # 3. Create (or reuse) the Org Admin User
        # Multi-org: if the admin email already belongs to an account, we attach that
        # existing account to the new org as a member instead of failing. A brand-new
        # email creates a new user + a password-setup token (welcome email).
        import secrets
        from datetime import datetime, timedelta
        from app.services.membership import add_user_to_org

        admin_role = db.query(Role).filter(Role.name == "Organisation Admin").first()

        if existing_user:
            user = existing_user
            token = None  # existing accounts already have a password — no setup link
            is_new_user = False
            # New org becomes a membership; keep the user's current home org.
            add_user_to_org(db, user, org.id, role=admin_role, is_default=False)
        else:
            dummy_pass = secrets.token_hex(16)
            hashed_pwd = hash_password(dummy_pass)
            token = secrets.token_urlsafe(32)
            is_new_user = True
            user = User(
                email=admin_email.strip().lower(),
                password_hash=hashed_pwd,
                name=admin_name.strip(),
                organization_id=org.id,
                is_active=True,
                reset_token=token,
                reset_token_expires=datetime.utcnow() + timedelta(hours=24)
            )
            db.add(user)
            db.flush()
            # First org is the user's home org.
            add_user_to_org(db, user, org.id, role=admin_role, is_default=True)

        # Assign Organisation Admin Role globally (permission checks are global for now)
        if admin_role and admin_role not in user.roles:
            user.roles.append(admin_role)

        # 4. Seed Organization System Configurations
        # Telegram is the only remote-control/notification channel. telegram_chat_id
        # supports a comma-separated list so multiple org users can each DM the bot
        # and both receive alerts and issue commands independently.
        configs_to_seed = [
            ("trading_paused", "false", ConfigValueType.BOOLEAN, "Trading Paused", "Toggles automated trade placement"),
            ("telegram_enabled", "true", ConfigValueType.BOOLEAN, "Telegram Alerts Enabled", "Enable or disable Telegram notifications"),
            ("telegram_bot_token", "", ConfigValueType.STRING, "Telegram Bot Token", "The Telegram Bot Token from @BotFather", True),
            ("telegram_chat_id", "", ConfigValueType.STRING, "Telegram Chat ID(s)", "Comma-separated Telegram chat IDs to send alerts to"),
            ("ibkr_account", "", ConfigValueType.STRING, "IBKR Account ID", "Interactive Brokers account number"),
            ("fmp_api_key", "", ConfigValueType.STRING, "FMP API Key", "Financial Modeling Prep API key", True),
            ("working_capital_aud", "5000.0", ConfigValueType.FLOAT, "Working Capital (AUD)", "Working capital used for sizing and risk calculations"),
        ]
        for cfg_item in configs_to_seed:
            key, val, vtype, label, desc = cfg_item[:5]
            is_sec = cfg_item[5] if len(cfg_item) > 5 else False
            db.add(SystemConfig(
                key=key, value=val, value_type=vtype, label=label,
                description=desc, is_secret=is_sec, organization_id=org.id,
                group="broker" if "ibkr" in key else ("notifications" if "telegram" in key else "general")
            ))

        # 5. Clone default templates to RuleConfig for the new organization
        from app.models.config import RuleConfig
        rule_templates = db.query(RuleConfig).filter(RuleConfig.organization_id == None).all()
        for t in rule_templates:
            db.add(RuleConfig(
                rule_id=t.rule_id,
                organization_id=org.id,
                category=t.category,
                label=t.label,
                description=t.description,
                minervini_ref=t.minervini_ref,
                enabled_globally=t.enabled_globally,
                threshold=t.threshold,
                threshold_label=t.threshold_label,
                threshold_min=t.threshold_min,
                threshold_max=t.threshold_max,
                tier_overrides=t.tier_overrides.copy() if t.tier_overrides else {},
                is_mandatory=t.is_mandatory,
                sort_order=t.sort_order,
                updated_by="superadmin"
            ))

        from app.models.audit import AuditLog, AuditAction
        db.add(AuditLog(
            action=AuditAction.CONFIG_CHANGED,
            actor=request.session.get("email", "superadmin"),
            user_id=request.session.get("user_id"),
            organization_id=org.id,
            message=f"Super Admin created organization {org.name} (Tier: {tier})"
        ))
        db.commit()
    except IntegrityError:
        # Race / DB-level uniqueness violation (e.g. org name or email created concurrently).
        db.rollback()
        ctx.update({"error": f"Could not create '{name}' — the organisation name or admin email is already in use."})
        return templates.TemplateResponse("superadmin/organizations.html", ctx, status_code=400)
    except Exception as exc:
        db.rollback()
        logger.error(f"Org creation failed for '{name}': {exc}")
        ctx.update({"error": f"Could not create organisation: {exc}"})
        return templates.TemplateResponse("superadmin/organizations.html", ctx, status_code=400)

    import urllib.parse
    encoded_email = urllib.parse.quote(user.email)

    # Existing accounts are simply added to the new org as admin — no password setup
    # email (they already have credentials). Inform the super admin instead.
    if not is_new_user:
        return RedirectResponse(
            f"/superadmin/organizations?saved=member_added&email={encoded_email}", 302
        )

    # Welcome email sending flow for a brand-new Organization Admin
    from app.utils.email import send_email

    host = request.headers.get("host", "localhost:8501")
    scheme = "https" if request.url.scheme == "https" else "http"
    reset_link = f"{scheme}://{host}/reset-password?token={token}"

    subject = "Welcome to AstraTrade! Set up your Organisation Admin Account"
    html_content = (
        '<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px">'
        '<h2 style="color:#1d4ed8">Welcome to AstraTrade!</h2>'
        f'<p>Hi {user.name},</p>'
        f'<p>Your organization <strong>{org.name}</strong> has been created on AstraTrade. '
        'Click the button below to set up your admin password and log in:</p>'
        f'<div style="text-align:center;margin:30px 0"><a href="{reset_link}" '
        'style="background:#1d4ed8;color:#fff;padding:12px 24px;text-decoration:none;border-radius:6px">Set Up Password & Log In</a></div>'
        f'<p style="font-size:12px;color:#6b7280">Or copy: {reset_link}</p>'
        '<p style="color:#6b7280;font-size:14px">This link expires in 24 hours.</p></div>'
    )
    
    email_sent = send_email(user.email, subject, html_content)
    if email_sent:
        return RedirectResponse(f"/superadmin/organizations?saved=welcome_email&email={encoded_email}", 302)
    else:
        return RedirectResponse(f"/superadmin/organizations?saved=welcome_manual&token={token}&email={encoded_email}", 302)


@app.get("/superadmin/organizations/{org_id}", response_class=HTMLResponse)
async def superadmin_org_detail(org_id: int, request: Request, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    from app.models.account import Organization, Account
    from app.models.auth import User
    from app.models.audit import AuditLog, AuditAction
    from app.models.config import SystemConfig

    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        return RedirectResponse("/superadmin/organizations", 302)

    ctx = _global(request, db)
    from app.models.auth import OrganizationMembership
    from sqlalchemy import or_
    
    users = db.query(User).filter(
        or_(
            User.organization_id == org_id,
            User.id.in_(
                db.query(OrganizationMembership.user_id).filter(OrganizationMembership.organization_id == org_id)
            )
        )
    ).all()
    accounts = db.query(Account).filter(Account.organization_id == org_id).all()
    
    active_users = [u for u in users if u.is_active]
    active_accounts = [a for a in accounts if a.is_active]
    total_capital = sum(float(a.capital_aud or 0.0) for a in active_accounts)

    try:
        # Operational trail only — user-activity rows live in the Activity Console.
        logs = db.query(AuditLog).filter(
            AuditLog.organization_id == org_id,
            AuditLog.action.notin_([AuditAction.FEATURE_ACCESS, AuditAction.FEATURE_ACTION]),
        ).order_by(desc(AuditLog.created_at)).limit(50).all()
    except Exception:
        logs = []

    # MCP credentials for this org
    from app.models.mcp import MCPCredential, MCP_ALL_SCOPES, SCOPE_DESCRIPTIONS
    mcp_credentials = db.query(MCPCredential).filter(
        MCPCredential.organization_id == org_id,
    ).order_by(MCPCredential.created_at.desc()).all()

    from app.utils.cache import cache
    for cred in mcp_credentials:
        cred.plain_secret = cache.get(f"mcp_secret:{cred.client_id}")

    # MCP base URL (global SystemConfig, no org_id)
    mcp_base_url_cfg = db.query(SystemConfig).filter(
        SystemConfig.key == "mcp_base_url",
        SystemConfig.organization_id == None,
    ).first()
    mcp_base_url = (mcp_base_url_cfg.value if mcp_base_url_cfg else "https://vcpilot.astradigital.com.au").rstrip("/")

    # IBKR gateway connectivity + per-org noVNC config
    import socket as _socket, os as _os
    _ibkr_connected = False
    try:
        with _socket.create_connection((_os.getenv("IBKR_HOST", "ibkr"), int(_os.getenv("IBKR_PORT", "4002"))), timeout=1.5):
            _ibkr_connected = True
    except Exception:
        pass

    def _org_cfg(key):
        row = db.query(SystemConfig).filter(
            SystemConfig.key == key,
            SystemConfig.organization_id == org_id,
        ).first()
        return (row.value or "").strip() if row else ""

    ctx.update({
        "organization": org,
        "users": users,
        "accounts": accounts,
        "active_users_count": len(active_users),
        "total_users_count": len(users),
        "active_accounts_count": len(active_accounts),
        "total_accounts_count": len(accounts),
        "total_capital": total_capital,
        "logs": logs,
        "msg": request.query_params.get("msg", ""),
        "mcp_credentials": mcp_credentials,
        "mcp_base_url": mcp_base_url,
        "mcp_all_scopes": MCP_ALL_SCOPES,
        "mcp_scope_descriptions": SCOPE_DESCRIPTIONS,
        "ibkr_connected": _ibkr_connected,
        "ibkr_mode": _os.getenv("IBKR_PAPER_MODE", "paper"),
        "novnc_url": _org_cfg("novnc_url") or _os.getenv("NOVNC_URL", "").rstrip("/"),
        "vnc_password": _org_cfg("vnc_password") or "changeme",
        "org_ibkr_account": _org_cfg("ibkr_account"),
    })
    return templates.TemplateResponse("superadmin/org_detail.html", ctx)


@app.post("/superadmin/organizations/{org_id}/deactivate")
async def superadmin_org_deactivate(org_id: int, request: Request, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    from app.models.account import Organization
    from app.models.audit import AuditLog, AuditAction

    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        return RedirectResponse("/superadmin/organizations?msg=not_found", 302)

    current_org_id = request.session.get("organization_id")
    if org.id == current_org_id:
        return RedirectResponse(f"/superadmin/organizations/{org_id}?msg=cannot_deactivate_self", 302)

    org.is_active = False
    db.add(AuditLog(
        action=AuditAction.CONFIG_CHANGED,
        actor="superadmin",
        organization_id=org_id,
        message=f"Organisation '{org.name}' soft-deleted (is_active=False)",
    ))
    db.commit()
    return RedirectResponse(f"/superadmin/organizations?msg=deactivated&name={org.name}", 302)


@app.post("/superadmin/organizations/{org_id}/sync-positions")
async def superadmin_org_sync_positions(org_id: int, request: Request, db: Session = Depends(get_db)):
    """Queue an IBKR ↔ DB position reconciliation for this org (super admin only)."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    from app.models.account import Organization
    from app.models.audit import AuditLog, AuditAction

    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        return RedirectResponse("/superadmin/organizations?msg=not_found", 302)

    queued = True
    try:
        from app.tasks.trading import sync_ibkr_positions_task
        sync_ibkr_positions_task.delay(organization_id=org_id)
    except Exception as e:
        queued = False
        from loguru import logger
        logger.error(f"Failed to queue IBKR position sync for org {org_id}: {e}")

    db.add(AuditLog(
        action=AuditAction.MANUAL_OVERRIDE,
        actor=request.session.get("email", "superadmin"),
        user_id=request.session.get("user_id"),
        organization_id=org_id,
        message=f"IBKR position sync {'queued' if queued else 'FAILED to queue'} by super admin",
    ))
    db.commit()
    return RedirectResponse(
        f"/superadmin/organizations/{org_id}?msg={'sync_queued' if queued else 'sync_failed'}", 302
    )


@app.post("/superadmin/organizations/{org_id}/reactivate")
async def superadmin_org_reactivate(org_id: int, request: Request, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    from app.models.account import Organization
    from app.models.audit import AuditLog, AuditAction

    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        return RedirectResponse("/superadmin/organizations?msg=not_found", 302)

    org.is_active = True
    db.add(AuditLog(
        action=AuditAction.CONFIG_CHANGED,
        actor="superadmin",
        organization_id=org_id,
        message=f"Organisation '{org.name}' reactivated (is_active=True)",
    ))
    db.commit()
    return RedirectResponse(f"/superadmin/organizations/{org_id}?msg=reactivated", 302)


@app.get("/superadmin/rules", response_class=HTMLResponse)
async def superadmin_rules(request: Request, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    from app.models.config import RuleConfig

    ctx = _global(request, db)
    rules = db.query(RuleConfig).filter(RuleConfig.organization_id == None).order_by(RuleConfig.category, RuleConfig.sort_order).all()

    CATEGORY_LABELS = {
        "TREND_TEMPLATE": "Trend Template",  "FUNDAMENTAL": "Fundamentals",
        "VCP": "VCP Pattern",                "MARKET_REGIME": "Market Regime",
        "ENTRY": "Entry Rules",              "EXIT_DEFENSIVE": "Defensive Exits",
        "EXIT_OFFENSIVE": "Offensive Exits", "POSITION_SIZING": "Position Sizing",
        "PORTFOLIO": "Portfolio Rules",      "EARNINGS": "Earnings Rules",
        "CRYPTO": "Crypto Rules",
    }
    CATEGORY_ICONS = {
        "TREND_TEMPLATE": "📈", "FUNDAMENTAL": "📊", "VCP": "🔄",
        "MARKET_REGIME": "🌡️",  "ENTRY": "🎯",       "EXIT_DEFENSIVE": "🛑",
        "EXIT_OFFENSIVE": "💰", "POSITION_SIZING": "⚖️", "PORTFOLIO": "🗂️",
        "EARNINGS": "📅",       "CRYPTO": "₿",
    }

    rules_by_cat = {}
    for r in rules:
        cat = r.category.value
        if cat not in rules_by_cat:
            rules_by_cat[cat] = []
        rules_by_cat[cat].append({
            "id": r.id, "rule_id": r.rule_id, "label": r.label,
            "description": r.description or "", "minervini_ref": r.minervini_ref or "",
            "enabled": r.enabled_globally, "is_mandatory": r.is_mandatory,
            "threshold": float(r.threshold) if r.threshold is not None else None,
            "threshold_label": r.threshold_label or "",
            "threshold_min": float(r.threshold_min) if r.threshold_min else 0,
            "threshold_max": float(r.threshold_max) if r.threshold_max else 999,
            "tier_overrides": r.tier_overrides or {},
            "asset_types": r.asset_types or "BOTH",
        })

    ctx.update({
        "rules_by_category": rules_by_cat,
        "category_labels": CATEGORY_LABELS,
        "category_icons": CATEGORY_ICONS,
        "saved": request.query_params.get("saved", ""),
        "total_rules": len(rules),
    })
    return templates.TemplateResponse("superadmin/rules.html", ctx)


@app.post("/superadmin/rules/{rule_id}/toggle-global")
async def superadmin_rules_toggle_global(rule_id: str, request: Request, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    from app.models.config import RuleConfig
    r = db.query(RuleConfig).filter(RuleConfig.rule_id == rule_id, RuleConfig.organization_id == None).first()
    if r and not r.is_mandatory:
        r.enabled_globally = not r.enabled_globally
        r.updated_by = "superadmin"
        db.commit()
    return RedirectResponse("/superadmin/rules?saved=1", 302)


@app.post("/superadmin/rules/{rule_id}/toggle-tier")
async def superadmin_rules_toggle_tier(rule_id: str, request: Request, tier: str = Form(...), db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    from app.models.config import RuleConfig
    from sqlalchemy.orm.attributes import flag_modified

    r = db.query(RuleConfig).filter(RuleConfig.rule_id == rule_id, RuleConfig.organization_id == None).first()
    if r and not r.is_mandatory:
        overrides = dict(r.tier_overrides or {})
        tier_override = overrides.get(tier, {})
        tier_override["enabled"] = not tier_override.get("enabled", True)
        overrides[tier] = tier_override
        r.tier_overrides = overrides
        flag_modified(r, "tier_overrides")
        r.updated_by = "superadmin"
        db.commit()
    return RedirectResponse("/superadmin/rules?saved=1", 302)


@app.post("/superadmin/rules/sync-all")
async def superadmin_rules_sync_all(request: Request, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    force = request.query_params.get("force", "0") == "1"
    from app.models.config import RuleConfig
    from app.models.account import Organization
    from sqlalchemy.orm.attributes import flag_modified

    global_rules = db.query(RuleConfig).filter(RuleConfig.organization_id == None).all()
    all_org_ids = [o.id for o in db.query(Organization.id).all()]
    synced = 0
    skipped = 0
    created = 0
    for g in global_rules:
        org_rules = db.query(RuleConfig).filter(
            RuleConfig.rule_id == g.rule_id,
            RuleConfig.organization_id != None,
        ).all()
        org_rules_by_org = {r.organization_id: r for r in org_rules}
        for org_rule in org_rules:
            if not force and org_rule.updated_by not in (None, "superadmin", "migration", "system", "admin"):
                skipped += 1
                continue
            org_rule.enabled_globally = g.enabled_globally
            org_rule.threshold = g.threshold
            org_rule.tier_overrides = dict(g.tier_overrides or {})
            flag_modified(org_rule, "tier_overrides")
            org_rule.updated_by = "superadmin:sync"
            synced += 1

        # Backfill: any org missing a row for this rule_id (e.g. rule added
        # after the org was created) gets a fresh clone of the global template.
        for org_id in all_org_ids:
            if org_id in org_rules_by_org:
                continue
            db.add(RuleConfig(
                rule_id=g.rule_id,
                organization_id=org_id,
                category=g.category,
                label=g.label,
                description=g.description,
                minervini_ref=g.minervini_ref,
                enabled_globally=g.enabled_globally,
                threshold=g.threshold,
                threshold_label=g.threshold_label,
                threshold_min=g.threshold_min,
                threshold_max=g.threshold_max,
                tier_overrides=dict(g.tier_overrides or {}),
                is_mandatory=g.is_mandatory,
                sort_order=g.sort_order,
                asset_types=g.asset_types,
                updated_by="superadmin:sync",
            ))
            created += 1

    db.commit()
    return RedirectResponse(f"/superadmin/rules?saved=1&synced={synced}&skipped={skipped}&created={created}", 302)


@app.post("/superadmin/rules/{rule_id}/threshold")
async def superadmin_rules_threshold(rule_id: str, request: Request, tier: str = Form(...), threshold: float = Form(...), db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    from app.models.config import RuleConfig
    from sqlalchemy.orm.attributes import flag_modified

    r = db.query(RuleConfig).filter(RuleConfig.rule_id == rule_id, RuleConfig.organization_id == None).first()
    if r and r.threshold is not None:
        overrides = dict(r.tier_overrides or {})
        tier_override = overrides.get(tier, {})
        tier_override["threshold"] = float(threshold)
        overrides[tier] = tier_override
        r.tier_overrides = overrides
        flag_modified(r, "tier_overrides")
        r.updated_by = "superadmin"
        db.commit()
    return RedirectResponse("/superadmin/rules?saved=1", 302)


@app.post("/superadmin/config/simulation")
async def superadmin_config_simulation(
    request: Request,
    db: Session = Depends(get_db),
    mock_time_enabled: str = Form("false"),
    mock_current_time: str = Form(""),
    mock_market_regime: str = Form("BULL"),
    ibkr_simulate: str = Form(None),
):
    """
    Save simulation / time-travel controls from the superadmin rules page.
    All keys are stored as global SystemConfig rows (organization_id=NULL).
    """
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    from app.models.config import SystemConfig
    from app.models.audit import AuditLog, AuditAction

    def _upsert(key: str, value: str):
        row = db.query(SystemConfig).filter(
            SystemConfig.key == key,
            SystemConfig.organization_id == None,
        ).first()
        if row:
            row.value = value
            row.updated_by = request.session.get("email", "superadmin")
        else:
            db.add(SystemConfig(
                key=key, value=value,
                value_type="BOOLEAN" if key in ("mock_time_enabled", "ibkr_simulate") else "STRING",
                label=key.replace("_", " ").title(),
                group="system",
                organization_id=None,
                updated_by=request.session.get("email", "superadmin"),
            ))

    _upsert("mock_time_enabled", mock_time_enabled.lower())
    _upsert("mock_current_time", mock_current_time.strip())
    _upsert("mock_market_regime", mock_market_regime.upper())  # store separately — never clobbers last_market_regime

    # ibkr_simulate is optional in the form (checkbox — absent = false)
    _upsert("ibkr_simulate", "true" if ibkr_simulate == "true" else "false")

    db.add(AuditLog(
        action=AuditAction.CONFIG_CHANGED,
        actor=request.session.get("email", "superadmin"),
        organization_id=None,
        message=(
            f"Simulation config updated: mock_clock={mock_time_enabled}, "
            f"regime={mock_market_regime}, ibkr_simulate={ibkr_simulate or 'false'}"
        ),
        detail={
            "mock_time_enabled": mock_time_enabled,
            "mock_current_time": mock_current_time,
            "mock_market_regime": mock_market_regime,
            "ibkr_simulate": ibkr_simulate or "false",
        },
    ))
    db.commit()
    return RedirectResponse("/superadmin/rules?saved=1", 302)


@app.get("/superadmin/users", response_class=HTMLResponse)
async def superadmin_users(request: Request, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    from app.models.auth import User, Role, OrganizationMembership
    from app.models.account import Organization
    from sqlalchemy.orm import joinedload

    search = request.query_params.get("search", "").strip()
    selected_org_id = request.query_params.get("org_id", "")

    query = db.query(User).options(
        joinedload(User.organization),
        joinedload(User.memberships).joinedload(OrganizationMembership.organization)
    )
    if search:
        query = query.filter((User.name.ilike(f"%{search}%")) | (User.email.ilike(f"%{search}%")))
    if selected_org_id:
        query = query.filter(
            (User.organization_id == int(selected_org_id)) |
            User.id.in_(
                db.query(OrganizationMembership.user_id).filter(OrganizationMembership.organization_id == int(selected_org_id))
            )
        )

    users = query.order_by(User.email).all()
    organizations = db.query(Organization).order_by(Organization.name).all()
    roles = db.query(Role).order_by(Role.name).all()

    ctx = _global(request, db)
    ctx.update({
        "users": users, "organizations": organizations, "roles": roles,
        "search": search, "selected_org_id": selected_org_id,
        "saved": request.query_params.get("saved", ""),
        "error": request.query_params.get("error", ""),
    })
    return templates.TemplateResponse("superadmin/users.html", ctx)


@app.post("/superadmin/users/create")
async def superadmin_users_create(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    organization_id: int = Form(...),
    role_id: int = Form(...),
    send_welcome: str = Form(None),
    db: Session = Depends(get_db)
):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    from app.models.auth import User, Role, hash_password
    from app.models.account import Organization
    from app.services.membership import add_user_to_org
    import secrets, urllib.parse
    from app.models.audit import AuditLog, AuditAction

    email_clean = email.strip().lower()
    role = db.query(Role).filter(Role.id == role_id).first()
    existing_user = db.query(User).filter(User.email == email_clean).first()

    # Multi-org: if the user already exists, add them to the chosen org as a member
    # instead of failing. A brand-new email creates the account + password-setup link.
    if existing_user:
        add_user_to_org(db, existing_user, organization_id, role=role, is_default=False)
        if role and role not in existing_user.roles:
            existing_user.roles.append(role)
        db.add(AuditLog(
            action=AuditAction.CONFIG_CHANGED,
            actor=request.session.get("email", "superadmin"),
            user_id=request.session.get("user_id"),
            organization_id=organization_id,
            message=f"Super Admin added existing user {existing_user.email} to organization {organization_id}",
        ))
        db.commit()
        encoded_email = urllib.parse.quote(existing_user.email)
        return RedirectResponse(f"/superadmin/users?saved=member_added&email={encoded_email}", 302)

    dummy_pass = secrets.token_hex(16)
    hashed_pwd = hash_password(dummy_pass)
    from datetime import datetime, timedelta
    token = secrets.token_urlsafe(32)
    user = User(
        email=email_clean, password_hash=hashed_pwd,
        name=name.strip(), organization_id=organization_id, is_active=True,
        reset_token=token,
        reset_token_expires=datetime.utcnow() + timedelta(hours=24)
    )
    db.add(user)
    db.flush()

    if role:
        user.roles.append(role)
    add_user_to_org(db, user, organization_id, role=role, is_default=True)
    db.add(AuditLog(
        action=AuditAction.CONFIG_CHANGED,
        actor=request.session.get("email", "superadmin"),
        user_id=request.session.get("user_id"),
        message=f"Super Admin created user account {user.email} with role {role.name if role else 'None'}"
    ))
    db.commit()

    encoded_email = urllib.parse.quote(user.email)

    if send_welcome == "1":
        from app.utils.email import send_email
        host = request.headers.get("host", "localhost:8501")
        scheme = "https" if request.url.scheme == "https" else "http"
        reset_link = f"{scheme}://{host}/reset-password?token={token}"
        subject = "Welcome to AstraTrade! Set up your account"
        html_content = (
            '<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px">'
            '<h2 style="color:#1d4ed8">Welcome to AstraTrade!</h2>'
            f'<p>Hi {user.name},</p>'
            '<p>An account has been created for you on AstraTrade. Click the button below to set up your password and log in:</p>'
            f'<div style="text-align:center;margin:30px 0"><a href="{reset_link}" '
            'style="background:#1d4ed8;color:#fff;padding:12px 24px;text-decoration:none;border-radius:6px">Set Up Password & Log In</a></div>'
            f'<p style="font-size:12px;color:#6b7280">Or copy: {reset_link}</p>'
            '<p style="color:#6b7280;font-size:14px">This link expires in 24 hours.</p></div>'
        )
        email_sent = send_email(user.email, subject, html_content)
        if email_sent:
            return RedirectResponse(f"/superadmin/users?saved=welcome_email&email={encoded_email}", 302)
        else:
            return RedirectResponse(f"/superadmin/users?saved=welcome_manual&token={token}&email={encoded_email}", 302)
    else:
        return RedirectResponse(f"/superadmin/users?saved=created&email={encoded_email}", 302)


@app.post("/superadmin/users/{user_id}/update-role")
async def superadmin_user_update_role(user_id: int, request: Request, role_id: int = Form(...), db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    from app.models.auth import User, Role
    user = db.query(User).filter(User.id == user_id).first()
    role = db.query(Role).filter(Role.id == role_id).first()
    if user and role:
        user.roles = [role]
        from app.models.audit import AuditLog, AuditAction
        db.add(AuditLog(
            action=AuditAction.CONFIG_CHANGED,
            actor=request.session.get("email", "superadmin"),
            user_id=request.session.get("user_id"),
            message=f"Super Admin updated role of user {user.email} to {role.name}"
        ))
        db.commit()
    return RedirectResponse("/superadmin/users?saved=1", 302)


@app.post("/superadmin/users/{user_id}/reset-password")
async def superadmin_user_reset_password(user_id: int, request: Request, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    import secrets
    from datetime import datetime, timedelta
    from app.models.auth import User
    from app.utils.email import send_email

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse("/superadmin/users?error=User+not+found", 302)

    token = secrets.token_urlsafe(32)
    user.reset_token = token
    user.reset_token_expires = datetime.utcnow() + timedelta(hours=1)
    from app.models.audit import AuditLog, AuditAction
    db.add(AuditLog(
        action=AuditAction.CONFIG_CHANGED,
        actor=request.session.get("email", "superadmin"),
        user_id=request.session.get("user_id"),
        message=f"Super Admin triggered password reset for user {user.email}"
    ))
    db.commit()

    host = request.headers.get("host", "localhost:8501")
    scheme = "https" if request.url.scheme == "https" else "http"
    reset_link = f"{scheme}://{host}/reset-password?token={token}"

    subject = "Reset Your AstraTrade Password"
    html_content = (
        '<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px">'
        '<h2 style="color:#1d4ed8">AstraTrade Password Reset</h2>'
        '<p>Click the button below to set a new password:</p>'
        f'<div style="text-align:center;margin:30px 0"><a href="{reset_link}" '
        'style="background:#1d4ed8;color:#fff;padding:12px 24px;text-decoration:none;border-radius:6px">Reset Password</a></div>'
        f'<p style="font-size:12px;color:#6b7280">Or copy: {reset_link}</p>'
        '<p style="color:#6b7280;font-size:14px">This link expires in 1 hour.</p></div>'
    )
    email_sent = send_email(user.email, subject, html_content)
    if email_sent:
        return RedirectResponse("/superadmin/users?saved=reset_email", 302)
    else:
        import urllib.parse
        encoded_email = urllib.parse.quote(user.email)
        return RedirectResponse(f"/superadmin/users?saved=reset_manual&token={token}&email={encoded_email}", 302)


@app.post("/superadmin/users/{user_id}/delete")
async def superadmin_user_delete(user_id: int, request: Request, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    from app.models.auth import User
    from app.models.audit import AuditLog, AuditAction

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse("/superadmin/users?error=User+not+found", 302)

    # Prevent self-deletion
    current_user_id = request.session.get("user_id")
    current_email = request.session.get("email")
    if user.id == current_user_id or (current_email and user.email.strip().lower() == current_email.strip().lower()):
        return RedirectResponse("/superadmin/users?error=Cannot+delete+currently+logged-in+user", 302)

    email_to_delete = user.email
    db.delete(user)
    
    # Audit log
    db.add(AuditLog(
        action=AuditAction.CONFIG_CHANGED,
        actor=request.session.get("email", "superadmin"),
        organization_id=None,
        message=f"User '{email_to_delete}' was deleted by superadmin.",
    ))
    
    db.commit()
    import urllib.parse
    encoded_email = urllib.parse.quote(email_to_delete)
    return RedirectResponse(f"/superadmin/users?saved=deleted&email={encoded_email}", 302)


@app.get("/superadmin/users/{user_id}/edit", response_class=HTMLResponse)
async def superadmin_user_edit_get(user_id: int, request: Request, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    from app.models.auth import User, Role, OrganizationMembership
    from app.models.account import Organization
    from sqlalchemy.orm import joinedload

    user = db.query(User).options(
        joinedload(User.organization),
        joinedload(User.memberships).joinedload(OrganizationMembership.organization)
    ).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse("/superadmin/users?error=User+not+found", 302)

    organizations = db.query(Organization).order_by(Organization.name).all()
    roles = db.query(Role).order_by(Role.name).all()

    ctx = _global(request, db)
    ctx.update({
        "u": user,
        "organizations": organizations,
        "roles": roles,
        "user_org_ids": user.organization_ids,
        "saved": request.query_params.get("saved", ""),
        "error": request.query_params.get("error", ""),
    })
    return templates.TemplateResponse("superadmin/user_edit.html", ctx)


@app.post("/superadmin/users/{user_id}/edit")
async def superadmin_user_edit_post(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    from app.models.auth import User, Role, OrganizationMembership
    from app.models.audit import AuditLog, AuditAction
    from app.services.membership import add_user_to_org

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse("/superadmin/users?error=User+not+found", 302)

    form_data = await request.form()
    name = form_data.get("name", "").strip()
    email = form_data.get("email", "").strip().lower()
    role_id = int(form_data.get("role_id"))
    organization_id = int(form_data.get("organization_id"))
    org_ids = [int(x) for x in form_data.getlist("org_ids")]
    is_active = form_data.get("is_active") == "on"

    if not name or not email:
        return RedirectResponse(f"/superadmin/users/{user_id}/edit?error=Name+and+Email+are+required", 302)

    # Validate email uniqueness (excluding current user)
    existing_other = db.query(User).filter(User.email == email, User.id != user_id).first()
    if existing_other:
        return RedirectResponse(f"/superadmin/users/{user_id}/edit?error=Email+already+in+use", 302)

    # Update basic details
    user.name = name
    user.email = email
    user.is_active = is_active

    # Update role
    role = db.query(Role).filter(Role.id == role_id).first()
    if role:
        user.roles = [role]

    # Sync memberships
    all_checked_org_ids = set(org_ids)
    all_checked_org_ids.add(organization_id)

    # Add/update memberships for checked orgs
    for oid in all_checked_org_ids:
        is_default = (oid == organization_id)
        add_user_to_org(db, user, oid, role=role, is_default=is_default)

    # Remove memberships for unchecked orgs
    for m in list(user.memberships):
        if m.organization_id not in all_checked_org_ids:
            db.delete(m)
            user.memberships.remove(m)

    # Update default organization_id
    user.organization_id = organization_id

    db.add(AuditLog(
        action=AuditAction.CONFIG_CHANGED,
        actor=request.session.get("email", "superadmin"),
        organization_id=None,
        message=f"Super Admin updated user {user.email}: name={user.name}, active={user.is_active}, role={role.name if role else 'None'}, home_org={organization_id}, org_access={list(all_checked_org_ids)}"
    ))
    db.commit()

    return RedirectResponse(f"/superadmin/users/{user_id}/edit?saved=1", 302)


# ===========================================================================

# SUPER ADMIN DATA VIEW — universe + price data + custom stocks
# ===========================================================================

@app.get("/superadmin/data", response_class=HTMLResponse)
async def superadmin_data(
    request: Request,
    db: Session = Depends(get_db),
    tab: str = Query("universe"),       # universe | us | crypto | custom
    search: str = Query(""),
    sector: str = Query(""),
    exchange: str = Query(""),          # optional exchange_key filter within a tab
    sort_by: str = Query("ticker"),     # ticker | rs_rating | last_price | market_cap | vol_ratio
    sort_dir: str = Query("asc"),
    page: int = Query(1),
):
    """
    Super Admin Data page — exchange-aware tabs:
      Tab 1 (universe): ASX200 stocks with latest PriceBar metrics.
      Tab 2 (us):       US equities (NYSE/NASDAQ) tracked in DB.
      Tab 3 (crypto):   Crypto assets tracked in DB.
      Tab 4 (custom):   ASX equities manually added by orgs that are not in the ASX200 universe.
    """
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    from app.models.market import Stock, PriceBar
    from app.models.signal import Watchlist
    from app.models.account import Organization
    from sqlalchemy import func, and_, text as _text

    ctx = _global(request, db)
    per_page = 50

    def _build_equity_rows(stocks_page, bar_map):
        rows = []
        for s in stocks_page:
            bar = bar_map.get(s.ticker)
            rows.append({
                "ticker":        s.ticker,
                "display_code":  getattr(s, "asx_code", None) or s.ticker.replace(".AX", "").replace("-USD", ""),
                "asx_code":      getattr(s, "asx_code", None) or "",
                "exchange_key":  getattr(s, "exchange_key", "ASX") or "ASX",
                "name":          s.name or "",
                "sector":        s.sector or "",
                "industry":      getattr(s, "industry", None) or "",
                "in_asx200":     getattr(s, "in_asx200", False) or False,
                "asset_type":    getattr(s, "asset_type", "EQUITY") or "EQUITY",
                "currency":      getattr(s, "currency", "AUD") or "AUD",
                "market_cap":    int(s.market_cap) if s.market_cap else None,
                "last_price":    float(s.last_price) if s.last_price else None,
                "last_updated":  str(s.last_updated)[:10] if s.last_updated else "",
                "bar_date":      _fmt_date(bar.date) if bar else "",
                "close":         float(bar.close)          if bar and bar.close          else None,
                "volume":        int(bar.volume)            if bar and bar.volume         else None,
                "ma_50":         float(bar.ma_50)           if bar and bar.ma_50          else None,
                "ma_150":        float(bar.ma_150)          if bar and bar.ma_150         else None,
                "ma_200":        float(bar.ma_200)          if bar and bar.ma_200         else None,
                "vol_ratio":     float(bar.vol_ratio)       if bar and bar.vol_ratio      else None,
                "rs_rating":     float(bar.rs_rating)       if bar and bar.rs_rating      else None,
                "pct_from_52w_high": float(bar.pct_from_52w_high) if bar and bar.pct_from_52w_high else None,
                "atr_14":        float(bar.atr_14)          if bar and bar.atr_14         else None,
                "has_bar":       bar is not None,
            })
        return rows

    def _fetch_bar_map(db, tickers):
        if not tickers:
            return {}
        latest_dates = db.query(
            PriceBar.ticker,
            func.max(PriceBar.date).label("max_date")
        ).filter(PriceBar.ticker.in_(tickers)).group_by(PriceBar.ticker).subquery()
        bars = db.query(PriceBar).join(
            latest_dates,
            and_(PriceBar.ticker == latest_dates.c.ticker,
                 PriceBar.date  == latest_dates.c.max_date)
        ).all()
        return {b.ticker: b for b in bars}

    sort_map = {
        "ticker":     Stock.ticker,
        "name":       Stock.name,
        "sector":     Stock.sector,
        "market_cap": Stock.market_cap,
        "last_price": Stock.last_price,
    }

    if tab == "universe":
        # ── Tab 1: ASX200 universe ─────────────────────────���───────────────────
        q = db.query(Stock).filter(
            Stock.is_active == True,
            Stock.blacklisted == False,
        )
        # Try to filter by exchange_key column (added in migration 002)
        try:
            q = q.filter(Stock.exchange_key == "ASX")
        except Exception:
            pass
        if search:
            q = q.filter(
                (Stock.ticker.ilike(f"%{search.upper()}%")) |
                (Stock.name.ilike(f"%{search}%"))
            )
        if sector:
            q = q.filter(Stock.sector == sector)

        total_stocks = q.count()
        sort_col = sort_map.get(sort_by, Stock.ticker)
        if sort_dir == "desc":
            q = q.order_by(sort_col.desc().nullslast())
        else:
            q = q.order_by(sort_col.asc().nullslast())

        stocks = q.offset((page - 1) * per_page).limit(per_page).all()
        bar_map = _fetch_bar_map(db, [s.ticker for s in stocks])
        rows = _build_equity_rows(stocks, bar_map)

        if sort_by in ("rs_rating", "vol_ratio"):
            rows.sort(key=lambda r: (r[sort_by] is None, r.get(sort_by) or 0), reverse=(sort_dir == "desc"))

        sectors = sorted(set(
            s.sector for s in db.query(Stock.sector).filter(
                Stock.is_active == True, Stock.sector != None
            ).distinct().all() if s.sector
        ))

        ctx.update({
            "tab": "universe", "rows": rows, "custom_rows": [], "crypto_rows": [],
            "search": search, "sector": sector, "sectors": sectors,
            "sort_by": sort_by, "sort_dir": sort_dir, "page": page, "per_page": per_page,
            "total": total_stocks, "total_pages": max(1, (total_stocks + per_page - 1) // per_page),
            "total_with_bars": sum(1 for r in rows if r["has_bar"]),
            "avg_rs": round(sum(r["rs_rating"] for r in rows if r["rs_rating"]) /
                            max(1, sum(1 for r in rows if r["rs_rating"])), 1),
        })

    elif tab == "us":
        # ── Tab 2: US equities (NYSE/NASDAQ) ──────────────────────────────────
        q = db.query(Stock).filter(Stock.is_active == True, Stock.blacklisted == False)
        try:
            from sqlalchemy import or_
            q = q.filter(or_(Stock.exchange_key == "NYSE", Stock.exchange_key == "NASDAQ"))
        except Exception:
            q = q.filter(Stock.ticker.notlike("%.AX")).filter(Stock.ticker.notlike("%-USD"))

        if search:
            q = q.filter(
                (Stock.ticker.ilike(f"%{search.upper()}%")) |
                (Stock.name.ilike(f"%{search}%"))
            )
        if sector:
            q = q.filter(Stock.sector == sector)

        total_stocks = q.count()
        sort_col = sort_map.get(sort_by, Stock.ticker)
        if sort_dir == "desc":
            q = q.order_by(sort_col.desc().nullslast())
        else:
            q = q.order_by(sort_col.asc().nullslast())

        stocks = q.offset((page - 1) * per_page).limit(per_page).all()
        bar_map = _fetch_bar_map(db, [s.ticker for s in stocks])
        rows = _build_equity_rows(stocks, bar_map)

        if sort_by in ("rs_rating", "vol_ratio"):
            rows.sort(key=lambda r: (r[sort_by] is None, r.get(sort_by) or 0), reverse=(sort_dir == "desc"))

        sectors = sorted(set(
            s.sector for s in db.query(Stock.sector).filter(
                Stock.is_active == True, Stock.sector != None
            ).distinct().all() if s.sector
        ))

        ctx.update({
            "tab": "us", "rows": rows, "custom_rows": [], "crypto_rows": [],
            "search": search, "sector": sector, "sectors": sectors,
            "sort_by": sort_by, "sort_dir": sort_dir, "page": page, "per_page": per_page,
            "total": total_stocks, "total_pages": max(1, (total_stocks + per_page - 1) // per_page),
            "total_with_bars": sum(1 for r in rows if r["has_bar"]),
            "avg_rs": round(sum(r["rs_rating"] for r in rows if r["rs_rating"]) /
                            max(1, sum(1 for r in rows if r["rs_rating"])), 1),
        })

    elif tab == "crypto":
        # ── Tab 3: Crypto assets from DB ──────────────────────────────────────
        q = db.query(Stock).filter(Stock.is_active == True)
        try:
            q = q.filter(Stock.asset_type == "CRYPTO")
        except Exception:
            q = q.filter(Stock.ticker.like("%-USD").or_(Stock.ticker.like("%-AUD")))

        if search:
            q = q.filter(
                (Stock.ticker.ilike(f"%{search.upper()}%")) |
                (Stock.name.ilike(f"%{search}%"))
            )

        total_stocks = q.count()
        if sort_dir == "desc":
            q = q.order_by(sort_map.get(sort_by, Stock.ticker).desc().nullslast())
        else:
            q = q.order_by(sort_map.get(sort_by, Stock.ticker).asc().nullslast())

        stocks = q.offset((page - 1) * per_page).limit(per_page).all()
        bar_map = _fetch_bar_map(db, [s.ticker for s in stocks])
        crypto_rows = _build_equity_rows(stocks, bar_map)

        if sort_by in ("rs_rating", "vol_ratio"):
            crypto_rows.sort(key=lambda r: (r[sort_by] is None, r.get(sort_by) or 0), reverse=(sort_dir == "desc"))

        ctx.update({
            "tab": "crypto", "rows": [], "custom_rows": [], "crypto_rows": crypto_rows,
            "search": search, "sector": "", "sectors": [],
            "sort_by": sort_by, "sort_dir": sort_dir, "page": page, "per_page": per_page,
            "total": total_stocks, "total_pages": max(1, (total_stocks + per_page - 1) // per_page),
            "total_with_bars": sum(1 for r in crypto_rows if r["has_bar"]),
            "avg_rs": 0,
        })

    else:
        # ── Tab 4: Custom stocks per org (not in main universe) ──────────────
        custom_rows_raw = db.execute(_text("""
            SELECT
                w.ticker,
                w.organization_id,
                w.exchange_key AS w_exchange_key,
                o.name AS org_name,
                COUNT(*) OVER (PARTITION BY w.ticker) AS org_count,
                s.name AS stock_name,
                s.sector,
                s.in_asx200,
                s.is_active,
                s.exchange_key AS s_exchange_key,
                s.asset_type,
                pb.close,
                pb.date AS bar_date,
                pb.rs_rating,
                pb.vol_ratio,
                pb.ma_50,
                pb.ma_200,
                pb.pct_from_52w_high,
                w.created_at AS added_at
            FROM watchlist w
            JOIN organizations o ON o.id = w.organization_id
            LEFT JOIN stocks s ON s.ticker = w.ticker
            LEFT JOIN LATERAL (
                SELECT close, date, rs_rating, vol_ratio, ma_50, ma_200, pct_from_52w_high
                FROM price_bars
                WHERE ticker = w.ticker
                ORDER BY date DESC
                LIMIT 1
            ) pb ON TRUE
            WHERE (s.in_asx200 IS NULL OR s.in_asx200 = FALSE)
              AND (s.asset_type IS NULL OR s.asset_type = 'EQUITY')
              AND (s.exchange_key IS NULL OR s.exchange_key NOT LIKE 'CRYPTO_%')
              AND (w.exchange_key IS NULL OR w.exchange_key NOT LIKE 'CRYPTO_%')
              AND (w.exchange_key IS NULL OR w.exchange_key NOT IN ('NYSE', 'NASDAQ'))
            ORDER BY org_count DESC, w.ticker, o.name
        """)).fetchall()

        custom_rows = [
            {
                "ticker":       r.ticker,
                "org_id":       r.organization_id,
                "org_name":     r.org_name,
                "org_count":    r.org_count,
                "name":         r.stock_name or "",
                "sector":       r.sector or "",
                "in_asx200":    r.in_asx200 or False,
                "is_active":    r.is_active if r.is_active is not None else True,
                "exchange_key": r.w_exchange_key or r.s_exchange_key or "ASX",
                "asset_type":   r.asset_type or "EQUITY",
                "close":        float(r.close) if r.close else None,
                "bar_date":     _fmt_date(r.bar_date) if r.bar_date else "",
                "rs_rating":    float(r.rs_rating) if r.rs_rating else None,
                "vol_ratio":    float(r.vol_ratio) if r.vol_ratio else None,
                "ma_50":        float(r.ma_50) if r.ma_50 else None,
                "ma_200":       float(r.ma_200) if r.ma_200 else None,
                "pct_from_52w_high": float(r.pct_from_52w_high) if r.pct_from_52w_high else None,
                "added_at":     _fmt_date(r.added_at) if r.added_at else "",
            }
            for r in custom_rows_raw
        ]

        if search:
            su = search.upper()
            custom_rows = [r for r in custom_rows if su in r["ticker"] or search.lower() in r["name"].lower()]

        ctx.update({
            "tab": "custom", "rows": [], "custom_rows": custom_rows, "crypto_rows": [],
            "search": search, "sector": "", "sectors": [],
            "sort_by": sort_by, "sort_dir": sort_dir, "page": 1, "per_page": per_page,
            "total": len(custom_rows), "total_pages": 1,
            "total_with_bars": sum(1 for r in custom_rows if r["close"]),
            "avg_rs": 0,
        })

    # Global summary stats
    total_stocks_db   = db.query(Stock).filter(Stock.is_active == True).count()
    total_bars_db     = db.query(PriceBar).count()
    latest_bar_date_r = db.query(func.max(PriceBar.date)).scalar()

    # Per-exchange counts for tab badges
    try:
        asx_count    = db.query(Stock).filter(Stock.is_active == True, Stock.exchange_key == "ASX").count()
        us_count     = db.query(Stock).filter(Stock.is_active == True, Stock.exchange_key.in_(["NYSE", "NASDAQ"])).count()
        crypto_count = db.query(Stock).filter(Stock.is_active == True, Stock.asset_type == "CRYPTO").count()
    except Exception:
        asx_count = total_stocks_db; us_count = 0; crypto_count = 0

    ctx.update({
        "total_stocks_db":  total_stocks_db,
        "total_bars_db":    total_bars_db,
        "latest_bar_date":  _fmt_date(latest_bar_date_r) if latest_bar_date_r else "—",
        "asx_count":        asx_count,
        "us_count":         us_count,
        "crypto_count":     crypto_count,
        "msg":              request.query_params.get("msg", ""),
    })
    return templates.TemplateResponse("superadmin/data.html", ctx)


@app.get("/reset-password", response_class=HTMLResponse)
async def reset_password_get(request: Request, token: str = Query(...), db: Session = Depends(get_db)):
    from datetime import datetime
    from app.models.auth import User
    user = db.query(User).filter(User.reset_token == token, User.reset_token_expires > datetime.utcnow()).first()
    if not user:
        return templates.TemplateResponse("reset_password.html", {"request": request, "token": token, "error": "Invalid or expired reset token.", "success": False})
    return templates.TemplateResponse("reset_password.html", {"request": request, "token": token, "error": None, "success": False})


@app.post("/reset-password")
async def reset_password_post(request: Request, token: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    from datetime import datetime
    from app.models.auth import User, hash_password as hash_p
    from app.utils.rate_limit import check_ip_throttle

    if not check_ip_throttle(request, "reset_password", max_requests=10, window_seconds=60):
        return templates.TemplateResponse("reset_password.html", {"request": request, "token": token, "error": "Too many attempts. Please wait a minute and try again.", "success": False}, status_code=429)

    user = db.query(User).filter(User.reset_token == token, User.reset_token_expires > datetime.utcnow()).first()
    if not user:
        return templates.TemplateResponse("reset_password.html", {"request": request, "token": token, "error": "Invalid or expired link.", "success": False})

    user.password_hash = hash_p(password)
    user.reset_token = None
    user.reset_token_expires = None
    db.commit()
    return templates.TemplateResponse("reset_password.html", {"request": request, "token": token, "error": None, "success": True})


# ===========================================================================
# SUPER ADMIN EXCHANGE MANAGEMENT
# ===========================================================================

@app.get("/superadmin/exchanges", response_class=HTMLResponse)
async def superadmin_exchanges(request: Request, db: Session = Depends(get_db)):
    if not _auth(request): return RedirectResponse("/login", 302)
    if not _is_superadmin(request): return RedirectResponse("/?error=access_denied", 302)
    exchanges=[]; migration_needed=False
    try:
        from app.models.exchange import ExchangeConfig
        exchanges=db.query(ExchangeConfig).order_by(ExchangeConfig.sort_order).all()
    except Exception: db.rollback(); migration_needed=True
    ctx={**_global(request,db),"exchanges":exchanges,"migration_needed":migration_needed,"msg":request.query_params.get("msg","")}
    return templates.TemplateResponse("superadmin/exchanges.html", ctx)


@app.post("/superadmin/exchanges/{exchange_key}/toggle")
async def superadmin_exchange_toggle(request: Request, exchange_key: str, db: Session = Depends(get_db)):
    if not _auth(request) or not _is_superadmin(request): return RedirectResponse("/login", 302)
    try:
        from app.models.exchange import ExchangeConfig
        exc=db.query(ExchangeConfig).filter(ExchangeConfig.exchange_key==exchange_key).first()
        if exc:
            exc.is_enabled=not exc.is_enabled; db.commit()
            from app.models.audit import AuditLog,AuditAction
            db.add(AuditLog(action=AuditAction.CONFIG_CHANGED,actor=request.session.get("email","superadmin"),message=f"Exchange {exchange_key} enabled={exc.is_enabled}")); db.commit()
            return RedirectResponse(f"/superadmin/exchanges?msg={'enabled' if exc.is_enabled else 'disabled'}", 302)
    except Exception: db.rollback()
    return RedirectResponse("/superadmin/exchanges?msg=error", 302)


@app.post("/superadmin/exchanges/{exchange_key}/update")
async def superadmin_exchange_update(request: Request, exchange_key: str, display_name: str=Form(""), is_enabled: str=Form("false"), ccxt_provider: str=Form(""), ccxt_sandbox: str=Form("false"), db: Session=Depends(get_db)):
    if not _auth(request) or not _is_superadmin(request): return RedirectResponse("/login", 302)
    try:
        from app.models.exchange import ExchangeConfig
        exc=db.query(ExchangeConfig).filter(ExchangeConfig.exchange_key==exchange_key).first()
        if exc:
            if display_name: exc.display_name=display_name
            exc.is_enabled=is_enabled.lower() in ("true","on","1","yes"); exc.ccxt_sandbox=ccxt_sandbox.lower() in ("true","on","1","yes")
            if ccxt_provider: exc.ccxt_provider=ccxt_provider.lower()
            db.commit()
    except Exception: db.rollback()
    return RedirectResponse("/superadmin/exchanges?msg=saved", 302)


# ===========================================================================
# SUPER ADMIN — USER ACTIVITY LOG
# ===========================================================================

@app.get("/superadmin/activity", response_class=HTMLResponse)
async def superadmin_activity(
    request: Request,
    org_id: str = None,
    action: str = None,
    user_id: str = None,
    feature: str = None,
    search: str = None,
    db: Session = Depends(get_db)
):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if not _is_superadmin(request):
        return RedirectResponse("/?error=access_denied", 302)

    from app.models.audit import AuditLog, AuditAction
    from app.models.account import Organization
    from app.models.auth import User
    from sqlalchemy import desc, or_

    q = db.query(AuditLog).order_by(desc(AuditLog.created_at))

    if org_id:
        q = q.filter(AuditLog.organization_id == int(org_id))

    if user_id:
        q = q.filter(AuditLog.user_id == int(user_id))

    if action and action != "ALL":
        q = q.filter(AuditLog.action == action)

    if feature and feature != "ALL":
        q = q.filter(AuditLog.feature == feature)

    if search:
        s_term = f"%{search.strip()}%"
        q = q.filter(
            or_(
                AuditLog.message.ilike(s_term),
                AuditLog.actor.ilike(s_term),
                AuditLog.ticker.ilike(s_term)
            )
        )

    logs = q.limit(300).all()

    organizations = db.query(Organization).order_by(Organization.name).all()
    actions = sorted([a.value for a in AuditAction])

    # Users for the filter dropdown (id, email, org name)
    users = (
        db.query(User, Organization.name)
        .outerjoin(Organization, User.organization_id == Organization.id)
        .order_by(User.email)
        .all()
    )
    user_options = [
        {"id": u.id, "email": u.email, "org_name": oname or "—"}
        for (u, oname) in users
    ]
    # Distinct feature labels seen in the log, for the feature dropdown
    feature_rows = (
        db.query(AuditLog.feature)
        .filter(AuditLog.feature.isnot(None))
        .distinct()
        .all()
    )
    features = sorted({r[0] for r in feature_rows if r[0]})

    display_tz = _get_display_tz(None, db)
    formatted_logs = []
    for l in logs:
        formatted_logs.append({
            "id": l.id,
            "time": _fmt_dt(str(l.created_at), display_tz),
            "action": str(l.action).replace("AuditAction.", ""),
            "actor": l.actor,
            "feature": l.feature or "—",
            "method": l.http_method or "—",
            "ip": l.ip_address or "—",
            "ticker": l.ticker or "—",
            "message": l.message or "",
            "org_name": l.organization.name if l.organization else "System",
            "before": l.before_value or "—",
            "after": l.after_value or "—",
        })

    ctx = _global(request, db)
    ctx.update({
        "logs": formatted_logs,
        "organizations": organizations,
        "actions": actions,
        "user_options": user_options,
        "features": features,
        "selected_org_id": org_id,
        "selected_action": action,
        "selected_user_id": user_id,
        "selected_feature": feature,
        "search": search,
    })
    return templates.TemplateResponse("superadmin/activity.html", ctx)


# SUPER ADMIN — CENTRAL OPERATIONS
# ===========================================================================

@app.get("/superadmin/operations", response_class=HTMLResponse)
async def superadmin_operations(request: Request, db: Session = Depends(get_db)):
    """Central operations hub — global tasks that run on shared price/universe data."""
    if not _auth(request): return RedirectResponse("/login", 302)
    if not _is_superadmin(request): return RedirectResponse("/?error=access_denied", 302)

    from app.models.market import Stock, PriceBar
    from app.models.audit import AuditLog, AuditAction
    from app.models.exchange import MarketRegimeRecord
    from app.models.config import SystemConfig
    from app.models.account import Organization
    from sqlalchemy import func, or_

    ctx = _global(request, db)
    msg = request.query_params.get("msg", "")

    # ── Universe & price stats ────────────────────────────────────────────────
    asx_count    = db.query(func.count(Stock.id)).filter(Stock.exchange_key == "ASX",    Stock.is_active == True).scalar() or 0
    us_count     = db.query(func.count(Stock.id)).filter(Stock.exchange_key.in_(["NYSE","NASDAQ"]), Stock.is_active == True).scalar() or 0
    crypto_count = db.query(func.count(Stock.id)).filter(Stock.asset_type == "CRYPTO",   Stock.is_active == True).scalar() or 0

    # Per-exchange crypto breakdown for the universe table
    crypto_rows = (
        db.query(Stock.exchange_key, func.count(Stock.id).label("cnt"))
        .filter(Stock.asset_type == "CRYPTO", Stock.is_active == True)
        .group_by(Stock.exchange_key)
        .all()
    )
    crypto_by_exchange = [{"key": r.exchange_key or "CRYPTO", "count": r.cnt} for r in crypto_rows]

    # Count crypto stocks that have at least one price bar (active/seeded)
    seeded_crypto_count = (
        db.query(func.count(func.distinct(PriceBar.ticker)))
        .join(Stock, Stock.ticker == PriceBar.ticker)
        .filter(Stock.asset_type == "CRYPTO")
        .scalar() or 0
    )

    total_bars   = db.query(func.count(PriceBar.id)).scalar() or 0
    latest_bar   = db.query(func.max(PriceBar.date)).scalar()
    today_bars   = db.query(func.count(PriceBar.id)).filter(PriceBar.date == get_current_date()).scalar() or 0

    # Custom stocks: any org has added manually and NOT in ASX200/300 index
    custom_count = db.query(func.count(Stock.id)).filter(
        Stock.is_active == True,
        Stock.exchange_key == "ASX",
        Stock.in_index == False,
    ).scalar() or 0

    # Active exchanges across all orgs (union — so we know what exchanges need refreshing)
    all_ae_rows = db.query(SystemConfig).filter(SystemConfig.key == "active_exchanges").all()
    all_exchanges: set[str] = set()
    for row in all_ae_rows:
        for e in (row.value or "").split(","):
            e = e.strip().upper()
            if e:
                all_exchanges.add(e)

    has_asx    = "ASX" in all_exchanges
    has_us     = bool(all_exchanges & {"NYSE", "NASDAQ", "US"})
    has_crypto = any(e.startswith("CRYPTO") for e in all_exchanges)
    crypto_keys = sorted(e for e in all_exchanges if e.startswith("CRYPTO_"))

    # ── Latest regime per exchange (from MarketRegimeRecord) ─────────────────
    regimes: dict[str, dict] = {}
    try:
        for ek in (["ASX"] if has_asx else []) + (["NYSE"] if has_us else []) + crypto_keys:
            rec = db.query(MarketRegimeRecord).filter(
                MarketRegimeRecord.exchange_key == ek
            ).order_by(desc(MarketRegimeRecord.evaluated_at)).first()
            regimes[ek] = {
                "regime": rec.regime if rec else "Not evaluated",
                "time":   _fmt_dt(str(rec.evaluated_at), "Australia/Sydney") if rec else "—",
                "index_close": float(rec.index_close or 0) if rec else 0,
                "breadth_pct": float(rec.breadth_pct or 0) if rec else 0,
            }
    except Exception:
        pass

    # ── Last-run times (global AuditLog — no org filter) ─────────────────────
    def _glr(kws: list[str], exch: str = None, actions=None):
        """Most recent global audit entry matching keywords + optional exchange prefix."""
        try:
            if actions is None:
                actions = [AuditAction.TASK_RUN]
            q = db.query(AuditLog).filter(
                AuditLog.action.in_(actions),
                or_(*[AuditLog.message.ilike(f"%{kw}%") for kw in kws]),
            )
            if exch:
                q = q.filter(AuditLog.message.ilike(f"%{exch}%"))
            log = q.order_by(desc(AuditLog.id)).first()
            if log:
                return {"time": _fmt_dt(str(log.created_at), "Australia/Sydney"), "msg": (log.message or "")[:120]}
        except Exception:
            pass
        return None

    task_runs = {
        "universe":      _glr(["Universe refresh", "universe refresh"], actions=[AuditAction.TASK_RUN, AuditAction.SYSTEM_STARTED]),
        "universe_us":   _glr(["US universe", "us universe", "SP500", "NASDAQ-100", "S&P 500"], actions=[AuditAction.TASK_RUN]) if has_us else None,
        "price_asx":     _glr(["Price data", "price data"], "ASX"),
        "price_us":      _glr(["Price data", "price data"], "NYSE") if has_us else None,
        "price_crypto":  _glr(["Price data", "price data"], "CRYPTO") if has_crypto else None,
        "regime_asx":    _glr(["Market regime"], "ASX",   actions=[AuditAction.MARKET_REGIME_CHANGE, AuditAction.TASK_RUN]) if has_asx else None,
        "regime_us":     _glr(["Market regime"], "NYSE",  actions=[AuditAction.MARKET_REGIME_CHANGE, AuditAction.TASK_RUN]) if has_us else None,
        "regime_crypto": _glr(["Market regime"], "CRYPTO",actions=[AuditAction.MARKET_REGIME_CHANGE, AuditAction.TASK_RUN]) if has_crypto else None,
        "screen_asx":    _glr(["Force screen", "Screen complete"], "ASX",   actions=[AuditAction.SCREENER_RUN]) if has_asx else None,
        "screen_us":     _glr(["Force screen", "Screen complete"], "NYSE",  actions=[AuditAction.SCREENER_RUN]) if has_us else None,
        "screen_crypto": _glr(["Force screen", "Screen complete"], "CRYPTO",actions=[AuditAction.SCREENER_RUN]) if has_crypto else None,
        "heartbeat":     _glr(["Heartbeat"], actions=[AuditAction.HEALTH_CHECK]),
    }

    # Active orgs count (for context banner)
    active_org_count = db.query(func.count(Organization.id)).filter(Organization.is_active == True).scalar() or 0

    ctx.update({
        "msg": msg,
        "asx_count": asx_count, "us_count": us_count, "crypto_count": crypto_count,
        "custom_count": custom_count, "total_bars": total_bars,
        "latest_bar": str(latest_bar) if latest_bar else "—", "today_bars": today_bars,
        "has_asx": has_asx, "has_us": has_us, "has_crypto": has_crypto,
        "crypto_keys": crypto_keys, "all_exchanges": sorted(all_exchanges),
        "regimes": regimes, "task_runs": task_runs,
        "active_org_count": active_org_count,
        "crypto_by_exchange": crypto_by_exchange,
        "seeded_crypto_count": seeded_crypto_count,
    })
    return templates.TemplateResponse("superadmin/operations.html", ctx)


@app.get("/superadmin/phantom-positions", response_class=HTMLResponse)
async def superadmin_phantom_positions(request: Request, db: Session = Depends(get_db)):
    """
    Cross-org report of every OPEN Position that was created by
    sync_ibkr_positions_task's import branch (message "IBKR sync: imported...",
    detail.source == "ibkr_sync") — the only durable signal, since Position
    itself carries no "imported" marker.

    Before the cross-org account fallback was closed (see IBKRBroker.connect()
    and CLAUDE.md), any org with no ibkr_account of its own resolved to the
    shared gateway's default account, so every such org independently imported
    a COPY of that one real account's holdings as if they were its own. This
    is why positions can look "the same regardless of which org you pick" —
    they're not cached or mis-scoped, they're genuinely separate rows with
    identical ticker/qty/price because they all came from the one shared
    account. This report finds every affected org in one pass instead of
    clicking through each org's Positions page individually.

    An org whose CURRENT ibkr_account happens to equal the account these were
    imported from is very likely the real, legitimate owner — flagged as
    "has own account" rather than "likely phantom", but still shown since a
    human should make the final call, not this heuristic.
    """
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if not _is_superadmin(request):
        return RedirectResponse("/?error=access_denied", 302)

    from app.models.trade import Position, TradeStatus
    from app.models.audit import AuditLog, AuditAction
    from app.models.account import Organization
    from app.models.config import SystemConfig

    ctx = _global(request, db)
    msg = request.query_params.get("msg", "")

    # One AuditLog per import — organization_id + ticker identifies the Position.
    import_logs = db.query(AuditLog).filter(
        AuditLog.action == AuditAction.POSITION_OPENED,
        AuditLog.message.ilike("IBKR sync: imported%"),
    ).order_by(desc(AuditLog.created_at)).all()

    org_names = {o.id: o.name for o in db.query(Organization).all()}
    org_has_account = {
        r.organization_id
        for r in db.query(SystemConfig).filter(
            SystemConfig.key == "ibkr_account", SystemConfig.value.isnot(None), SystemConfig.value != "",
        ).all()
    }

    rows = []
    seen = set()
    for log in import_logs:
        key = (log.organization_id, log.ticker)
        if key in seen:
            continue  # keep only the most recent import event per org+ticker
        seen.add(key)

        pos = db.query(Position).filter(
            Position.organization_id == log.organization_id,
            Position.ticker == log.ticker,
            Position.status == TradeStatus.OPEN,
        ).first()
        if not pos:
            continue  # already closed/purged since the import

        rows.append({
            "position_id": pos.id,
            "org_id": log.organization_id,
            "org_name": org_names.get(log.organization_id, f"Org #{log.organization_id}"),
            "ticker": pos.ticker,
            "qty": float(pos.qty or 0),
            "entry_price": float(pos.entry_price or 0),
            "entry_date": _fmt_date(pos.entry_date),
            "imported_at": _fmt_dt(str(log.created_at), "Australia/Sydney"),
            "has_own_account": log.organization_id in org_has_account,
        })

    # Likely-phantom (no ibkr_account today) first.
    rows.sort(key=lambda r: (r["has_own_account"], r["org_name"], r["ticker"]))

    ctx.update({"msg": msg, "rows": rows, "phantom_count": sum(1 for r in rows if not r["has_own_account"])})
    return templates.TemplateResponse("superadmin/phantom_positions.html", ctx)


# ── Super admin global action routes ─────────────────────────────────────────

@app.post("/superadmin/action/refresh-data")
async def sa_action_refresh_data(request: Request, exchange: str = Form(None)):
    if not _auth(request) or not _is_superadmin(request):
        return RedirectResponse("/login", 302)
    from app.tasks.screening import refresh_price_data, refresh_crypto_universe

    def _queue():
        is_crypto = exchange and (exchange == "CRYPTO" or exchange.startswith("CRYPTO_"))
        if is_crypto:
            from celery import chain as _chain
            effective = exchange if exchange != "CRYPTO" else "CRYPTO_INDEPENDENTRESERVE"
            _chain(
                refresh_crypto_universe.si(exchange_key=effective),
                refresh_price_data.si(exchange_key=effective),
            ).delay()
        else:
            refresh_price_data.delay(exchange_key=exchange or None)

    # NOTE: the old except-block fallback re-called .delay() — with the broker
    # down that raised too, turning a queue failure into an unhandled 500.
    return _queue_redirect(_queue, "/superadmin/operations?msg=data")


@app.post("/superadmin/action/refresh-universe")
async def sa_action_refresh_universe(request: Request, scope: str = Form(None)):
    if not _auth(request) or not _is_superadmin(request):
        return RedirectResponse("/login", 302)
    from app.tasks.screening import refresh_universe
    return _queue_redirect(
        lambda: refresh_universe.delay(scope=scope or None, organization_id=None),
        "/superadmin/operations?msg=universe",
    )


@app.post("/superadmin/action/evaluate-regime")
async def sa_action_evaluate_regime(request: Request, exchange: str = Form("ASX")):
    if not _auth(request) or not _is_superadmin(request):
        return RedirectResponse("/login", 302)
    from app.tasks.screening import evaluate_market_regime_task
    return _queue_redirect(
        lambda: evaluate_market_regime_task.delay(exchange_key=exchange or "ASX"),
        "/superadmin/operations?msg=regime",
    )


@app.post("/superadmin/action/seed-crypto")
async def sa_action_seed_crypto(request: Request, exchange: str = Form("CRYPTO_INDEPENDENTRESERVE")):
    if not _auth(request) or not _is_superadmin(request):
        return RedirectResponse("/login", 302)
    from app.tasks.screening import refresh_crypto_universe
    return _queue_redirect(
        lambda: refresh_crypto_universe.delay(exchange_key=exchange),
        "/superadmin/operations?msg=crypto_seed",
    )




@app.post("/superadmin/action/seed-us-universe")
async def sa_action_seed_us_universe(request: Request, scope: str = Form(None)):
    if not _auth(request) or not _is_superadmin(request):
        return RedirectResponse("/login", 302)
    from app.tasks.screening import refresh_us_universe
    return _queue_redirect(
        lambda: refresh_us_universe.delay(scope=scope or None),
        "/superadmin/operations?msg=universe_us",
    )


@app.post("/superadmin/action/run-screener")
async def sa_action_run_screener(request: Request, exchange: str = Form(None)):
    if not _auth(request) or not _is_superadmin(request):
        return RedirectResponse("/login", 302)
    from app.tasks.screening import _run_screen_force
    return _queue_redirect(
        lambda: _run_screen_force.delay(exchange_key=exchange or None, organization_id=None),
        "/superadmin/operations?msg=screener",
    )


@app.post("/superadmin/action/full-setup")
async def sa_action_full_setup(request: Request):
    if not _auth(request) or not _is_superadmin(request):
        return RedirectResponse("/login", 302)
    from app.tasks.screening import run_full_setup
    return _queue_redirect(lambda: run_full_setup.delay(), "/superadmin/operations?msg=setup")


@app.post("/superadmin/action/ping-worker")
async def sa_action_ping_worker(request: Request):
    if not _auth(request) or not _is_superadmin(request):
        return RedirectResponse("/login", 302)
    from app.tasks.reporting import health_check
    return _queue_redirect(lambda: health_check.delay(), "/superadmin/operations?msg=ping")


# ===========================================================================
# MCP — OAuth 2.0 Token Endpoint
# ===========================================================================

from fastapi.responses import JSONResponse as _JSONResponse


@app.get("/authorize", response_class=HTMLResponse)
async def oauth_authorize(
    request: Request,
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    state: str = Query(""),
    code_challenge: str = Query(""),
    code_challenge_method: str = Query(""),
    db: Session = Depends(get_db)
):
    if not _auth(request):
        import urllib.parse
        encoded_url = urllib.parse.quote(str(request.url))
        return RedirectResponse(f"/login?next={encoded_url}", 302)

    if response_type != "code":
        return RedirectResponse(f"{redirect_uri}?error=unsupported_response_type&state={state}", 302)

    from app.models.mcp import MCPCredential, SCOPE_DESCRIPTIONS
    cred = db.query(MCPCredential).filter(MCPCredential.client_id == client_id).first()
    if not cred or not cred.is_valid:
        return HTMLResponse("Invalid or inactive client_id", status_code=400)

    return templates.TemplateResponse("authorize.html", {
        "request": request,
        "client_id": client_id,
        "client_name": cred.name,
        "org_name": cred.organization.name if cred.organization else "AstraTrade Org",
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "scopes": cred.scopes or [],
        "scope_descriptions": SCOPE_DESCRIPTIONS,
    })


@app.post("/authorize/approve")
async def oauth_authorize_approve(
    request: Request,
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    state: str = Form(""),
    code_challenge: str = Form(""),
    code_challenge_method: str = Form(""),
    db: Session = Depends(get_db)
):
    if not _auth(request):
        return RedirectResponse("/login", 302)

    from app.models.mcp import MCPCredential
    cred = db.query(MCPCredential).filter(MCPCredential.client_id == client_id).first()
    if not cred or not cred.is_valid:
        return HTMLResponse("Invalid or inactive client_id", status_code=400)

    import secrets
    auth_code = f"vcp_code_{secrets.token_urlsafe(32)}"

    from app.utils.cache import cache
    code_data = {
        "client_id": client_id,
        "organization_id": cred.organization_id,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "credential_id": cred.id,
    }
    cache.set(f"oauth_code:{auth_code}", code_data, expire_seconds=300)

    import urllib.parse
    redirect_url = f"{redirect_uri}?code={auth_code}"
    if state:
        redirect_url += f"&state={urllib.parse.quote(state)}"
    return RedirectResponse(redirect_url, 302)


@app.post("/mcp/oauth/token")
@app.post("/token")
@app.post("/oauth/token")
async def mcp_oauth_token(request: Request, db: Session = Depends(get_db)):
    """
    OAuth 2.0 Token Endpoint.
    Supports:
      • grant_type=client_credentials (API/CLI clients using client_id + client_secret)
      • grant_type=authorization_code (OAuth flow clients like Claude.ai using PKCE)
    """
    from app.models.mcp import MCPCredential, verify_secret
    from app.mcp.auth import create_access_token
    from app.utils.rate_limit import check_ip_throttle

    if not check_ip_throttle(request, "mcp_oauth_token", max_requests=10, window_seconds=60):
        return _JSONResponse({"error": "slow_down", "error_description": "Too many requests, please wait a minute and try again"}, status_code=429)

    client_id     = None
    client_secret = None
    grant_type    = None
    code          = None
    code_verifier = None
    req_redirect_uri = None

    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        client_id     = form.get("client_id")
        client_secret = form.get("client_secret")
        grant_type    = form.get("grant_type")
        code          = form.get("code")
        code_verifier = form.get("code_verifier")
        req_redirect_uri = form.get("redirect_uri")
    else:
        try:
            body = await request.json()
            client_id     = body.get("client_id")
            client_secret = body.get("client_secret")
            grant_type    = body.get("grant_type")
            code          = body.get("code")
            code_verifier = body.get("code_verifier")
            req_redirect_uri = body.get("redirect_uri")
        except Exception:
            pass

    # Fall back to HTTP Basic Auth for client_credentials
    if not client_id or not client_secret:
        import base64
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                client_id, client_secret = decoded.split(":", 1)
                if not grant_type:
                    grant_type = "client_credentials"
            except Exception:
                pass

    if not grant_type:
        grant_type = "authorization_code" if code else "client_credentials"

    logger.info(
        f"mcp_oauth_token request: content_type={content_type}, grant_type={grant_type}, "
        f"client_id={client_id}, code_present={bool(code)}, code_verifier_present={bool(code_verifier)}, "
        f"redirect_uri={req_redirect_uri}"
    )

    if grant_type not in ("client_credentials", "authorization_code"):
        logger.warning(f"mcp_oauth_token error: unsupported grant type '{grant_type}'")
        return _JSONResponse(
            {"error": "unsupported_grant_type", "error_description": "Only client_credentials and authorization_code are supported"},
            status_code=400,
        )

    if grant_type == "authorization_code":
        if not code:
            logger.warning("mcp_oauth_token error: code is required for authorization_code grant")
            return _JSONResponse(
                {"error": "invalid_request", "error_description": "code is required for authorization_code grant"},
                status_code=400,
            )

        from app.utils.cache import cache
        code_data = cache.get(f"oauth_code:{code}")
        if not code_data:
            logger.warning(f"mcp_oauth_token error: authorization code '{code}' not found or expired in cache")
            return _JSONResponse(
                {"error": "invalid_grant", "error_description": "Authorization code is invalid or expired"},
                status_code=400,
            )

        cache.delete(f"oauth_code:{code}")

        if client_id and client_id != code_data["client_id"]:
            logger.warning(f"mcp_oauth_token error: client_id mismatch. Request={client_id}, Code={code_data['client_id']}")
            return _JSONResponse(
                {"error": "invalid_client", "error_description": "Client ID mismatch"},
                status_code=400,
            )

        client_id = code_data["client_id"]
        challenge = code_data.get("code_challenge")
        method = code_data.get("code_challenge_method", "S256")

        if challenge:
            if not code_verifier:
                logger.warning("mcp_oauth_token error: missing code_verifier (PKCE challenge was set)")
                return _JSONResponse(
                    {"error": "invalid_request", "error_description": "code_verifier is required (PKCE)"},
                    status_code=400,
                )

            import hashlib
            import base64

            def base64url_encode(input_bytes: bytes) -> str:
                return base64.urlsafe_b64encode(input_bytes).decode('utf-8').rstrip('=')

            if method == "S256":
                sha256_hash = hashlib.sha256(code_verifier.encode('utf-8')).digest()
                calculated = base64url_encode(sha256_hash)
            else:
                calculated = code_verifier

            if calculated != challenge:
                logger.warning(f"mcp_oauth_token error: PKCE verification failed. Calculated={calculated}, Challenge={challenge}")
                return _JSONResponse(
                    {"error": "invalid_grant", "error_description": "PKCE verification failed"},
                    status_code=400,
                )

        cred = db.query(MCPCredential).filter(
            MCPCredential.id == code_data["credential_id"],
            MCPCredential.is_active == True,
        ).first()

        if not cred:
            logger.warning(f"mcp_oauth_token error: active credential id {code_data.get('credential_id')} not found")
            return _JSONResponse(
                {"error": "invalid_grant", "error_description": "Associated credential is no longer active"},
                status_code=400,
            )

    else:  # client_credentials
        if not client_id or not client_secret:
            logger.warning("mcp_oauth_token error: client_id and client_secret are required for client_credentials grant")
            return _JSONResponse(
                {"error": "invalid_request", "error_description": "client_id and client_secret are required"},
                status_code=400,
            )

        cred = db.query(MCPCredential).filter(
            MCPCredential.client_id == client_id,
            MCPCredential.is_active == True,
        ).first()

        if not cred or not verify_secret(client_secret, cred.client_secret_hash):
            logger.warning(f"mcp_oauth_token error: invalid client credentials for client_id {client_id}")
            return _JSONResponse(
                {"error": "invalid_client", "error_description": "Invalid client_id or client_secret"},
                status_code=401,
            )

    if cred.is_expired:
        return _JSONResponse(
            {"error": "invalid_client", "error_description": "Credential has expired. Contact your super admin to regenerate."},
            status_code=401,
        )

    token = create_access_token(
        org_id=cred.organization_id,
        scopes=cred.scopes,
        credential_id=cred.id,
        client_id=cred.client_id,
    )
    cred.last_used_at = datetime.utcnow()
    db.commit()

    return _JSONResponse({
        "access_token": token,
        "token_type":   "Bearer",
        "expires_in":   3600,
        "scope":        " ".join(cred.scopes),
    })


# ===========================================================================
# MCP — Super Admin Credential Management
# ===========================================================================

@app.post("/superadmin/mcp/base-url")
async def superadmin_mcp_base_url_save(
    request: Request,
    mcp_base_url: str = Form(...),
    redirect_org_id: str = Form(""),
    db: Session = Depends(get_db),
):
    """Update the global mcp_base_url SystemConfig (no org_id)."""
    if not _auth(request) or not _is_superadmin(request):
        return RedirectResponse("/login", 302)

    from app.models.config import SystemConfig
    row = db.query(SystemConfig).filter(
        SystemConfig.key == "mcp_base_url",
        SystemConfig.organization_id == None,
    ).first()
    url = mcp_base_url.strip().rstrip("/")
    if row:
        row.value = url
        row.updated_by = request.session.get("email", "superadmin")
    else:
        db.add(SystemConfig(
            key="mcp_base_url", value=url, value_type="STRING",
            label="MCP Base URL", group="mcp", organization_id=None,
            updated_by=request.session.get("email", "superadmin"),
        ))
    from app.models.audit import AuditLog, AuditAction
    db.add(AuditLog(
        action=AuditAction.CONFIG_CHANGED,
        actor=request.session.get("email", "superadmin"),
        organization_id=None,
        message=f"MCP base URL updated to: {url}",
    ))
    db.commit()

    if redirect_org_id:
        return RedirectResponse(f"/superadmin/organizations/{redirect_org_id}?msg=mcp_url_saved", 302)
    return RedirectResponse("/superadmin/organizations?msg=mcp_url_saved", 302)


@app.post("/superadmin/organizations/{org_id}/mcp/generate")
async def superadmin_mcp_generate(
    org_id: int,
    request: Request,
    name: str = Form("Default"),
    notes: str = Form(""),
    scopes: list = Form(None),
    db: Session = Depends(get_db),
):
    """
    Generate a new MCP client_id + client_secret pair for an organisation.
    The plain secret is returned once in the redirect URL (shown once in UI).
    """
    if not _auth(request) or not _is_superadmin(request):
        return RedirectResponse("/login", 302)

    from app.models.account import Organization
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        return RedirectResponse("/superadmin/organizations", 302)

    from app.models.mcp import (
        MCPCredential, MCP_ALL_SCOPES,
        MCP_CREDENTIAL_VALIDITY_DAYS,
        generate_client_id, generate_client_secret, hash_secret,
    )
    from datetime import timedelta

    selected_scopes = scopes if scopes else MCP_ALL_SCOPES

    plain_secret = generate_client_secret()
    client_id    = generate_client_id()

    cred = MCPCredential(
        organization_id       = org_id,
        name                  = name,
        client_id             = client_id,
        client_secret_hash    = hash_secret(plain_secret),
        scopes                = selected_scopes,
        expires_at            = datetime.utcnow() + timedelta(days=MCP_CREDENTIAL_VALIDITY_DAYS),
        is_active             = True,
        created_by            = request.session.get("email", "superadmin"),
        notes                 = notes or None,
    )
    db.add(cred)

    from app.utils.cache import cache
    cache.set(f"mcp_secret:{client_id}", plain_secret, expire_seconds=600)

    from app.models.audit import AuditLog, AuditAction
    db.add(AuditLog(
        action=AuditAction.CONFIG_CHANGED,
        actor=request.session.get("email", "superadmin"),
        organization_id=org_id,
        message=f"MCP credential '{name}' generated (client_id={client_id})",
    ))
    db.commit()

    import urllib.parse
    return RedirectResponse(
        f"/superadmin/organizations/{org_id}?msg=mcp_generated"
        f"&mcp_client_id={urllib.parse.quote(client_id)}"
        f"&mcp_secret={urllib.parse.quote(plain_secret)}"
        f"&mcp_name={urllib.parse.quote(name)}",
        302,
    )


@app.post("/superadmin/organizations/{org_id}/mcp/{cred_id}/revoke")
async def superadmin_mcp_revoke(
    org_id: int,
    cred_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Immediately revoke an MCP credential."""
    if not _auth(request) or not _is_superadmin(request):
        return RedirectResponse("/login", 302)

    from app.models.mcp import MCPCredential
    cred = db.query(MCPCredential).filter(
        MCPCredential.id == cred_id,
        MCPCredential.organization_id == org_id,
    ).first()
    if cred:
        cred.is_active  = False
        cred.revoked_at = datetime.utcnow()
        cred.revoked_by = request.session.get("email", "superadmin")
        from app.models.audit import AuditLog, AuditAction
        db.add(AuditLog(
            action=AuditAction.CONFIG_CHANGED,
            actor=request.session.get("email", "superadmin"),
            organization_id=org_id,
            message=f"MCP credential '{cred.name}' (id={cred_id}) revoked",
        ))
        db.commit()

    return RedirectResponse(f"/superadmin/organizations/{org_id}?msg=mcp_revoked", 302)


@app.post("/superadmin/organizations/{org_id}/mcp/{cred_id}/renew")
async def superadmin_mcp_renew(
    org_id: int,
    cred_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Extend an existing credential by another 12 months from today."""
    if not _auth(request) or not _is_superadmin(request):
        return RedirectResponse("/login", 302)

    from app.models.mcp import MCPCredential, MCP_CREDENTIAL_VALIDITY_DAYS
    from datetime import timedelta

    cred = db.query(MCPCredential).filter(
        MCPCredential.id == cred_id,
        MCPCredential.organization_id == org_id,
    ).first()
    if cred:
        cred.expires_at = datetime.utcnow() + timedelta(days=MCP_CREDENTIAL_VALIDITY_DAYS)
        cred.is_active  = True
        cred.revoked_at = None
        cred.revoked_by = None
        from app.models.audit import AuditLog, AuditAction
        db.add(AuditLog(
            action=AuditAction.CONFIG_CHANGED,
            actor=request.session.get("email", "superadmin"),
            organization_id=org_id,
            message=f"MCP credential '{cred.name}' (id={cred_id}) renewed for 12 months",
        ))
        db.commit()

    return RedirectResponse(f"/superadmin/organizations/{org_id}?msg=mcp_renewed", 302)





@app.get("/faq", response_class=HTMLResponse)
async def faq_page(request: Request, db: Session = Depends(get_db)):
    """Public FAQ page — no auth required but needs base.html context."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    ctx = _global(request, db)
    return templates.TemplateResponse("faq.html", ctx)




# ===========================================================================
# MCP -- Mount MCP server ASGI app
# ===========================================================================

try:
    from app.mcp.server import create_mcp_app as _create_mcp_app
    _mcp_asgi = _create_mcp_app()
    app.mount("/mcp", _mcp_asgi)
    logger.info("AstraTrade MCP server mounted at /mcp")
except Exception as _mcp_err:
    logger.exception(f"MCP server not mounted (install mcp[server] package): {_mcp_err}")
