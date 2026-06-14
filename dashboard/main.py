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
from loguru import logger

app = FastAPI(title="AstraTrade", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=os.getenv("APP_SECRET_KEY", "changeme-secret"))

# ---------------------------------------------------------------------------
# Mobile REST API (JWT-authenticated, consumed by React Native app)
# ---------------------------------------------------------------------------
from app.api.mobile import router as mobile_router
app.include_router(mobile_router)

from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exceptions import RequestValidationError
from app.utils.cache import cache

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
    cache_key = f"wl_labels:{org_id}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    labels = db.query(WatchlistLabel).filter(
        WatchlistLabel.organization_id == org_id
    ).order_by(WatchlistLabel.sort_order).all()
    result = [
        {"id": l.id, "name": l.name, "color": l.color, "is_default": l.is_default, "sort_order": l.sort_order}
        for l in labels
    ]
    cache.set(cache_key, result, expire_seconds=300)
    return result

templates = Jinja2Templates(directory="/app/dashboard/templates")
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


@app.on_event("startup")
async def _startup():
    """
    On startup, verify WAHA is reachable and log active sessions.
    We do NOT auto-start any org session — each org admin triggers their own
    QR scan via /admin/whatsapp → 'Start Session'.
    Each org uses session name 'org_{id}' (requires WAHA Plus for multi-session).
    """
    import asyncio, httpx
    from loguru import logger
    from app.config import settings

    async def _check_waha():
        await asyncio.sleep(4)   # give WAHA a moment to be ready
        api  = settings.waha_api_url.rstrip("/")
        key  = settings.waha_api_key
        sess = settings.waha_session   # "default" for WAHA Core
        hook = settings.waha_hook_url
        hdrs = {"X-Api-Key": key, "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Start/ensure default session
                r = await client.post(f"{api}/api/sessions/start",
                                      json={"name": sess}, headers=hdrs)
                status = r.json().get("status", "?") if r.status_code in (200,201) else r.status_code
                logger.info(f"WAHA session '{sess}' → {status}")
                # Register webhook
                if hook:
                    await client.put(f"{api}/api/sessions/{sess}",
                        json={"webhooks": [{"url": hook, "events": ["message","message.any","session.status"]}]},
                        headers=hdrs)
                    logger.info(f"WAHA webhook registered: {hook}")
        except Exception as e:
            logger.warning(f"WAHA startup init failed (non-fatal): {e}")

    asyncio.create_task(_check_waha())


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


def _fmt_dt(utc_iso: str, tz_name: str = "UTC") -> str:
    """
    Convert a stored UTC ISO timestamp string to a human-readable string
    in the given IANA timezone (e.g. 'Australia/Sydney').
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
        abbr = local_dt.strftime("%Z")   # e.g. AEST, UTC
        return local_dt.strftime(f"%Y-%m-%d %H:%M {abbr}")
    except Exception:
        return utc_iso[:16]              # fall back to raw truncated string


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
    whatsapp_enabled = cfg("whatsapp_enabled", "true").lower() == "true"
    notification_channel = cfg("notification_channel", "whatsapp") or "whatsapp"

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
    user_role = request.session.get("user_role")
    if user_role == "superadmin":
        from app.models.account import Organization
        all_orgs = db.query(Organization).filter(Organization.is_active == True).order_by(Organization.name).all()

    # User display info for sidebar footer
    user_email = request.session.get("email", "")
    user_name  = ""
    if user_role != "superadmin" and request.session.get("user_id"):
        from app.models.auth import User
        u = db.query(User).filter(User.id == request.session.get("user_id")).first()
        if u:
            user_name = u.name or ""
    if not user_name:
        user_name = "Super Admin" if user_role == "superadmin" else (user_email.split("@")[0] if user_email else "User")

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
        "whatsapp_enabled": whatsapp_enabled,
        "notification_channel": notification_channel,
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
        "current_org_id": org_id,
        "ibkr_simulate": cfg("ibkr_simulate", "false").lower() == "true",
        "mock_time_enabled": mock_time_on,
        "mock_current_time": cfg("mock_current_time", ""),
        "mock_market_regime": cfg("mock_market_regime", "BULL"),
        "onboarding_completed": cfg("onboarding_completed", "false").lower() == "true",
    }


def _auth(request: Request):
    return request.session.get("authenticated", False)


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
            return RedirectResponse(next, 302)
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
    from app.config import settings
    from app.models.auth import User, verify_password
    from app.models.account import Organization

    email_clean = email.strip().lower()

    # 1. Check Super Admin from .env
    if email_clean == settings.superadmin_email.strip().lower() and password == settings.superadmin_password:
        default_org = db.query(Organization).order_by(Organization.id).first()
        request.session["authenticated"] = True
        request.session["user_role"] = "superadmin"
        request.session["organization_id"] = default_org.id if default_org else 1
        request.session["organization_name"] = default_org.name if default_org else "Default Org"
        request.session["email"] = settings.superadmin_email
        if next:
            return RedirectResponse(next, 302)
        return RedirectResponse("/", 302)

    # 2. Check Database Users
    user = db.query(User).filter(User.email == email_clean).first()
    if user and verify_password(password, user.password_hash):
        if not user.is_active:
            return templates.TemplateResponse("login.html", {"request": request, "error": "User account is disabled", "next": next}, status_code=401)
        
        # Check if user has "Super Admin" role in DB
        is_super = any(r.name == "Super Admin" for r in user.roles)
        
        request.session["authenticated"] = True
        request.session["user_role"] = "superadmin" if is_super else "user"
        request.session["user_id"] = user.id
        request.session["organization_id"] = user.organization_id
        request.session["organization_name"] = user.organization.name
        request.session["email"] = user.email
        if next:
            return RedirectResponse(next, 302)
        return RedirectResponse("/", 302)

    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid email or password", "next": next}, status_code=401)


@app.get("/logout")
async def logout(request: Request):
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
    
    # In development mode, append debug otp to url if email delivery is offline/disabled
    debug_param = f"&debug_otp={otp}" if (settings.smtp_host == "smtp.gmail.com" and not email_sent) or (not settings.smtp_username) else ""
    
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

    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if not user:
        return templates.TemplateResponse("verify_otp.html", {"request": request, "email": email, "error": "User session expired. Please request a new OTP.", "next": next}, status_code=400)
    
    if not user.otp_code or user.otp_code != otp_code.strip() or not user.otp_expires_at or user.otp_expires_at < datetime.utcnow():
        return templates.TemplateResponse("verify_otp.html", {"request": request, "email": email, "error": "Invalid or expired OTP code", "next": next}, status_code=400)

    # Clear OTP
    user.otp_code = None
    user.otp_expires_at = None
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
        return RedirectResponse(next, 302)
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

    # Single query for all company names — used across positions + signals
    stock_names = {s.ticker: (s.name or "") for s in db.query(Stock).all()}

    # Open positions
    positions = db.query(Position).filter(Position.status == TradeStatus.OPEN, Position.organization_id == org_id).all()
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

    # Signals (Show today's signals OR active pending signals)
    from sqlalchemy import or_
    signals = db.query(Signal).filter(
        or_(
            Signal.signal_date == get_current_date(),
            Signal.status == SignalStatus.PENDING
        ),
        Signal.organization_id == org_id
    ).all()
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
    today_trades = db.query(Trade).filter(Trade.exit_date == get_current_date(), Trade.organization_id == org_id).all()
    all_trades   = db.query(Trade).filter(Trade.organization_id == org_id).all()

    # ── Automated System Checks ──
    from app.models.audit import AuditLog, AuditAction
    def get_latest_check(action, message_like=None):
        q = db.query(AuditLog).filter(
            or_(AuditLog.organization_id == org_id, AuditLog.organization_id == None),
            AuditLog.action == action
        )
        if message_like:
            q = q.filter(AuditLog.message.ilike(f"%{message_like}%"))
        return q.order_by(desc(AuditLog.created_at)).first()

    latest_universe = get_latest_check(AuditAction.SYSTEM_STARTED, "Universe")
    latest_price = get_latest_check(AuditAction.TASK_RUN, "Price data")
    latest_regime = get_latest_check(AuditAction.MARKET_REGIME_CHANGE)
    latest_screen = get_latest_check(AuditAction.SCREENER_RUN)
    latest_entry = get_latest_check(AuditAction.TASK_RUN, "Entry check")
    latest_exit = get_latest_check(AuditAction.TASK_RUN, "Exit check")

    from app.models.config import SystemConfig
    cfg_last = db.query(SystemConfig).filter(SystemConfig.key == "last_market_regime", SystemConfig.organization_id == None).first()
    last_eval = cfg_last.value if cfg_last else ""

    checks = [
        {"name": "Universe Constituents Sync", "frequency": "Weekly (Sun 8pm AEST)", "log": latest_universe},
        {"name": "EOD Price Data Ingestion", "frequency": "Daily (Mon-Fri 5pm AEST)", "log": latest_price},
        {"name": "Market Regime Evaluation", "frequency": "Daily (Mon-Fri 5:15pm AEST)", "log": latest_regime,
         "active_regime": last_eval, "regime_is_simulated": False},
        {"name": "AstraTrade Daily Screener", "frequency": "Daily (Mon-Fri 5:30pm AEST)", "log": latest_screen},
        {"name": "Intraday Breakout Entry Check", "frequency": "Every 5 min (10am-4:12pm AEST)", "log": latest_entry},
        {"name": "Intraday Position Exit Check", "frequency": "Every 5 min (10am-4:12pm AEST)", "log": latest_exit},
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

    wq = db.query(Watchlist).options(joinedload(Watchlist.label)).filter(
        Watchlist.status == WatchlistStatus.WATCHING,
        Watchlist.organization_id == org_id
    )
    if active_label_id is not None:
        wq = wq.filter(Watchlist.label_id == active_label_id)

    wl_items = wq.all()
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

        watchlist_rows.append({
            "ticker": w.ticker,
            "company_name": s.name if s else "",
            "sector": s.sector if s else "",
            "in_asx200": s.in_asx200 if s else False,
            "is_custom": is_custom,
            "label": {"id": w.label.id, "name": w.label.name, "color": w.label.color} if w.label else None,
            "exchange_key": getattr(w, "exchange_key", "ASX") or "ASX",
            "asset_type": getattr(w, "asset_type", "EQUITY") or "EQUITY",
            "close": float(bar.close) if bar and bar.close else None,
            "volume": int(bar.volume) if bar and bar.volume else None,
            "ma_50": float(bar.ma_50) if bar and bar.ma_50 else None,
            "ma_150": float(bar.ma_150) if bar and bar.ma_150 else None,
            "ma_200": float(bar.ma_200) if bar and bar.ma_200 else None,
            "vol_ratio": float(bar.vol_ratio) if bar and bar.vol_ratio else None,
            "rs_rating": float(bar.rs_rating) if bar and bar.rs_rating else None,
            "pct_from_52w_high": float(bar.pct_from_52w_high) if bar and bar.pct_from_52w_high else None,
            "atr_14": float(bar.atr_14) if bar and bar.atr_14 else None,
            "bar_date": str(bar.date) if bar else "",
        })

    # Filter by exchange
    active_exchange = (exchange or "ALL").upper()
    if active_exchange not in ("", "ALL"):
        watchlist_rows = [row for row in watchlist_rows if
            (active_exchange == "ASX" and row.get("exchange_key") == "ASX") or
            (active_exchange == "US"  and row.get("exchange_key") in ("NYSE", "NASDAQ")) or
            (active_exchange == "CRYPTO" and row.get("asset_type") == "CRYPTO")]

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
        regimes_list.append({"flag": _flag, "label": _label, "val": _val})

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
        "today_pnl": round(sum(float(t.net_pnl_aud or 0) for t in today_trades), 2),
        "total_pnl":  round(sum(float(t.net_pnl_aud or 0) for t in all_trades), 2),
        "trade_count": len(all_trades),
        "checks": checks,
        "watchlist_rows": watchlist_rows,
        "wl_labels": wl_labels_data,
        "wl_label_counts": wl_label_counts,
        "wl_total_watching": wl_total_watching,
        "wl_active_label": active_label_id,
        "wl_only_custom": only_custom,
        "wl_exchange_filters": _get_exchange_filters(org_id, db),
        "wl_active_exchange": active_exchange,
    })
    return templates.TemplateResponse("trading/home.html", ctx)


@app.get("/positions", response_class=HTMLResponse)
async def positions(request: Request, db: Session = Depends(get_db),
                    exchange: str = Query("ALL")):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.models.trade import Position, Trade, TradeStatus
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
            pos_q = pos_q.filter(Position.exchange_key == "ASX")
        elif af == "CRYPTO":
            pos_q = pos_q.filter(Position.asset_type == "CRYPTO")
        elif af == "US":
            pos_q = pos_q.filter(Position.exchange_key.in_(["NYSE", "NASDAQ"]))
    except Exception:
        pass
    positions = pos_q.all()
    pos_data = []
    total_risk = 0.0
    for p in positions:
        entry = float(p.entry_price or 0)
        curr  = float(p.current_price or entry)
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
            "entry_date": str(p.entry_date),
            "is_paper": p.is_paper,
            "exit_checks": exit_checks,
        })

    # Closed trades — also filter by exchange if selected
    trade_q = db.query(Trade).filter(Trade.organization_id == org_id).order_by(desc(Trade.exit_date))
    try:
        if af == "ASX":
            trade_q = trade_q.filter(Trade.exchange_key == "ASX")
        elif af == "CRYPTO":
            trade_q = trade_q.filter(Trade.asset_type == "CRYPTO")
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
        "entry_date": str(t.entry_date or ""), "exit_date": str(t.exit_date or ""),
        "days": t.hold_days or 0,
        "entry": float(t.entry_price or 0), "exit": float(t.exit_price or 0),
        "pnl_pct": round(float(t.pnl_pct or 0) * 100, 2),
        "pnl_aud": round(float(t.net_pnl_aud or 0), 2),
        "reason": str(t.exit_reason).replace("ExitReason.", "").replace("_", " "),
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

    ef = _get_exchange_filters(org_id, db)
    ctx.update({
        "positions": pos_data, "trades": trade_data,
        "win_rate": round(len(wins) / len(trades) * 100) if trades else 0,
        "total_pnl": round(sum(float(t.net_pnl_aud or 0) for t in trades), 2),
        "trade_count": len(trades),
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


@app.get("/signals", response_class=HTMLResponse)
async def signals(request: Request, db: Session = Depends(get_db),
                  exchange: str = Query("ALL")):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.models.signal import Signal
    from app.models.market import Stock
    from app.models.config import RuleConfig
    from app.screener.rules import RuleEngine

    ctx = _global(request, db)

    # Load org rule metadata once for the override UI
    from app.models.account import Organization as _Org
    org_obj = db.query(_Org).filter(_Org.id == org_id).first()
    tier = org_obj.tier.value if org_obj else "GOLD"
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

    from app.models.audit import AuditLog, AuditAction
    sig_tz = _get_display_tz(org_id, db)

    # Build flag emoji lookup from ExchangeConfig
    flag_map: dict[str, str] = {}
    try:
        from app.models.exchange import ExchangeConfig as _EC
        for ec in db.query(_EC).all():
            flag_map[ec.exchange_key] = ec.flag_emoji or ""
    except Exception:
        pass

    from sqlalchemy import or_
    from app.models.signal import SignalStatus
    q = db.query(Signal).filter(
        or_(
            Signal.signal_date == get_current_date(),
            Signal.status == SignalStatus.PENDING,
        ),
        Signal.status != SignalStatus.TRIGGERED,
        Signal.organization_id == org_id
    )
    # Apply exchange filter at DB level
    af = (exchange or "ALL").upper()
    try:
        if af == "ASX":
            q = q.filter(Signal.exchange_key == "ASX")
        elif af == "CRYPTO":
            q = q.filter(Signal.asset_type == "CRYPTO")
        elif af == "US":
            q = q.filter(Signal.exchange_key.in_(["NYSE", "NASDAQ"]))
    except Exception:
        pass
    sigs = q.all()
    stock_names = get_cached_stock_names(db)

    # ── Pre-fetch audit entries for all tickers in one query (avoids N+1) ──
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

    # ── Pre-fetch all regime configs in one query (avoids N+1 per signal) ──
    from app.models.config import SystemConfig as _SC
    _regime_rows = db.query(_SC).filter(
        or_(
            (_SC.key.like("last_market_regime_%") & (_SC.organization_id == org_id)),
            (_SC.key == "last_market_regime") & (_SC.organization_id == None),
        )
    ).all()
    _regime_map: dict[str, str] = {}  # key → value
    for _rc in _regime_rows:
        _regime_map[_rc.key] = _rc.value or "UNKNOWN"

    # ── Bulk-fetch latest PriceBars for all signal tickers in one query ──────
    # Eliminates per-signal DB query inside _enrich_rule_results (N+1 fix).
    # We also seed the Redis cache so subsequent calls (e.g. repeated page loads)
    # are served from cache rather than hitting the DB at all.
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
            }
            _sig_bar_lookup[_sb.ticker] = _bd
            # Seed cache so future calls (within same request or repeated loads) skip DB
            _ck = f"latest_price_bar:{_sb.ticker}"
            if not cache.get(_ck):
                cache.set(_ck, _bd, expire_seconds=300)

    sig_data = []
    for s in sigs:
        company_name = stock_names.get(s.ticker, "")

        # NOTE: Inline yfinance name backfill removed — it blocks the request for 2-5s per
        # unknown ticker. Missing names are filled in by the background screener task.
        # The 24-hr marker below prevents re-attempting on every page load.
        if not company_name:
            _nck = f"missing_name_fetch:{s.ticker}"
            if not cache.get(_nck):
                cache.set(_nck, "attempted", expire_seconds=86400)

        rr = s.rule_results or {}
        passed = sum(1 for v in rr.values() if v.get("passed"))
        overrides = s.rule_overrides or {}

        # ── Latest entry check result for this signal ────────────────────
        last_check = None
        try:
            audit_entries = _audit_by_ticker.get(s.ticker, [])
            for entry in audit_entries:
                d = entry.detail or {}
                if d.get("signal_id") == s.id:
                    raw_overrides = d.get("overrides_applied", {})
                    # Only count rules that were actually disabled (value == False)
                    active_overrides = {k: v for k, v in raw_overrides.items() if v is False}
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

        # ── Build override rule list: only rules that make sense to toggle ──
        # Use screener rule_results to know pass/fail; merge with breakout check
        screener_pass_fail = {rid: bool(v.get("passed")) for rid, v in rr.items()}
        # Also include breakout rules from last entry check
        if last_check and last_check.get("rules"):
            for br in last_check["rules"]:
                screener_pass_fail[br["rule_id"]] = br["passed"]

        # Inject BEAR regime block as a failed rule when market is in BEAR
        # so the org admin can override it per-signal from this page
        sig_is_crypto = getattr(s, "asset_type", "EQUITY") == "CRYPTO"
        bear_rule_id  = "regime_bear_block_crypto" if sig_is_crypto else "regime_bear_block_equities"
        _exc_key = getattr(s, "exchange_key", "ASX") or "ASX"
        _is_crypto_exc = _exc_key == "CRYPTO" or _exc_key.startswith("CRYPTO_")
        if _is_crypto_exc:
            _eff_exc = _exc_key if _exc_key != "CRYPTO" else "CRYPTO_INDEPENDENTRESERVE"
            _current_regime = _regime_map.get(f"last_market_regime_{_eff_exc}", "BULL")
        else:
            _current_regime = _regime_map.get("last_market_regime", "UNKNOWN")
        _bear_rule_meta = rules_meta.get(bear_rule_id, {})
        _bear_rule_globally_on = _bear_rule_meta.get("globally_enabled", True)
        if _current_regime == "BEAR" and _bear_rule_globally_on:
            # True if user already overrode this rule for the signal, False if still blocking
            screener_pass_fail[bear_rule_id] = overrides.get(bear_rule_id) is False

        ek = getattr(s, "exchange_key", "ASX") or "ASX"
        at = getattr(s, "asset_type",   "EQUITY") or "EQUITY"

        override_rules_failed = []
        override_rules_passed = []
        for rule_id, meta in rules_meta.items():
            if not meta["globally_enabled"]:
                continue
            # Skip rules that don't apply to this signal's asset type
            rule_asset = meta.get("asset_types", "BOTH")
            if rule_asset == "CRYPTO" and at != "CRYPTO":
                continue
            if rule_asset == "EQUITY" and at == "CRYPTO":
                continue
            rule_passed = screener_pass_fail.get(rule_id)   # True / False / None (unknown)
            current_override = overrides.get(rule_id, None)  # True / False / None
            entry = {
                "rule_id":    rule_id,
                "label":      meta["label"],
                "category":   meta["category"],
                "is_mandatory": meta["is_mandatory"],
                "rule_passed": rule_passed,   # None = not evaluated yet
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

        sig_data.append({
            "id": s.id, "ticker": s.ticker,
            "exchange_key":  ek,
            "asset_type":    at,
            "currency":      getattr(s, "currency", "AUD") or "AUD",
            "flag_emoji":    flag_map.get(ek, ""),
            "company_name": company_name,
            "close": float(s.close_price or 0),
            "pivot": float(s.pivot_price or 0),
            "stop": float(s.stop_price or 0),
            "target": float(s.target_price_1 or 0),
            "rs": float(s.rs_rating or 0),
            "trend_score": s.trend_score or 0,
            "fund_score": s.fundamental_score or 0,
            "vcp_contractions": s.vcp_contractions or 0,
            "vcp_weeks": s.vcp_weeks or 0,
            "size": s.suggested_size_shares or 0,
            "risk_aud": float(s.risk_per_trade_aud or 0),
            "status": s.status.value,
            "sig_date": str(s.signal_date) if s.signal_date else "",
            "rules_passed": passed,
            "rules_total": len(rr),
            "rule_results": _enrich_rule_results(s.ticker, rr, db, target_date=s.signal_date, overrides=overrides, _bar_data=_sig_bar_lookup.get(s.ticker)),
            "override_rules_failed": override_rules_failed,
            "override_rules_passed": override_rules_passed,
            "has_overrides": has_overrides,
            "is_promoted_manual": bool(is_promoted_manual),
            "is_promoted_vcp": bool(is_promoted_vcp),
            "last_check": last_check,
        })
    # Sort: PENDING first (actionable), then TRIGGERED, then SKIPPED
    _status_order = {"PENDING": 0, "TRIGGERED": 1, "SKIPPED": 2}
    sig_data.sort(key=lambda x: _status_order.get(x["status"], 9))

    pending_count   = sum(1 for s in sig_data if s["status"] == "PENDING")
    triggered_count = sum(1 for s in sig_data if s["status"] == "TRIGGERED")
    skipped_count   = sum(1 for s in sig_data if s["status"] == "SKIPPED")

    ef = _get_exchange_filters(org_id, db)
    ctx.update({
        "signals": sig_data,
        "signal_date": str(get_current_date()),
        "pending_count":   pending_count,
        "triggered_count": triggered_count,
        "skipped_count":   skipped_count,
        "exchange_filters":       ef,
        "active_exchange_filter": af,
        "base_url":               "/signals",
        "extra_params":           "",
    })
    return templates.TemplateResponse("trading/signals.html", ctx)


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
    return RedirectResponse("/signals", 302)


@app.get("/watchlist", response_class=HTMLResponse)
async def watchlist(
    request: Request,
    label: int = Query(None),
    exchange: str = Query("ALL"),
    page: int = Query(1),
    db: Session = Depends(get_db)
):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")

    from app.models.signal import Watchlist, WatchlistStatus, WatchlistLabel
    from app.models.market import PriceBar, Stock
    ctx = _global(request, db)

    af = (exchange or "ALL").upper()

    # Load labels from Redis cache — invalidated by label create/edit routes
    all_labels = get_cached_wl_labels(org_id, db)
    # Filter labels based on active exchange so irrelevant groups are hidden
    def _exchange_labels(labels, exf):
        if exf in ("ASX", "US"):
            return [l for l in labels if not (10 <= l["sort_order"] <= 19)]
        if exf == "CRYPTO":
            return [l for l in labels if not (20 <= l["sort_order"] <= 38) and l["sort_order"] < 100]
        return labels
    ctx["labels"] = _exchange_labels(all_labels, af)
    ctx["active_label"] = label  # currently selected filter (None = all)

    # Label counts: one GROUP BY query → {label_id: count} for badge display on chips
    from sqlalchemy import func as _sqf
    _cnt_rows = (
        db.query(Watchlist.label_id, _sqf.count(Watchlist.id))
        .filter(
            Watchlist.organization_id == org_id,
            Watchlist.status == WatchlistStatus.WATCHING,
            Watchlist.label_id.isnot(None),
        )
        .group_by(Watchlist.label_id)
        .all()
    )
    ctx["label_counts"] = {row[0]: row[1] for row in _cnt_rows}
    ctx["total_watching"] = (
        db.query(_sqf.count(Watchlist.id))
        .filter(Watchlist.organization_id == org_id, Watchlist.status == WatchlistStatus.WATCHING)
        .scalar() or 0
    )

    from sqlalchemy.orm import joinedload
    q = db.query(Watchlist).options(joinedload(Watchlist.label)).filter(
        Watchlist.status == WatchlistStatus.WATCHING,
        Watchlist.organization_id == org_id
    )
    if label is not None:
        q = q.filter(Watchlist.label_id == label)
    # Apply exchange filter at DB level — avoids fetching + processing all items then discarding
    if af == "ASX":
        q = q.filter(Watchlist.exchange_key == "ASX")
    elif af == "US":
        q = q.filter(Watchlist.exchange_key.in_(["NYSE", "NASDAQ"]))
    elif af == "CRYPTO":
        q = q.filter(Watchlist.asset_type == "CRYPTO")
    _WL_PER_PAGE = 20
    total = q.count()
    items = q.order_by(desc(Watchlist.created_at)).offset((page - 1) * _WL_PER_PAGE).limit(_WL_PER_PAGE).all()
    has_more = (page * _WL_PER_PAGE) < total

    stock_names = get_cached_stock_names(db)

    # ── Bulk-fetch latest PriceBar for all watchlist tickers in one query ──
    # Avoids N individual DB hits inside the loop below (major perf win for
    # large watchlists). Uses a DISTINCT ON subquery via raw SQL-friendly approach:
    # select the max date per ticker, then join back to get full rows.
    _wl_tickers = list({w.ticker for w in items})
    _bar_lookup: dict[str, object] = {}  # ticker → latest PriceBar row
    if _wl_tickers:
        from sqlalchemy import func as _func
        _sub = (
            db.query(PriceBar.ticker, _func.max(PriceBar.date).label("max_date"))
            .filter(PriceBar.ticker.in_(_wl_tickers))
            .group_by(PriceBar.ticker)
            .subquery()
        )
        _latest_bars = (
            db.query(PriceBar)
            .join(_sub, (PriceBar.ticker == _sub.c.ticker) & (PriceBar.date == _sub.c.max_date))
            .all()
        )
        for _bar in _latest_bars:
            _bar_lookup[_bar.ticker] = _bar

    # ── Pre-seed Redis cache from bulk lookup so _enrich_rule_results never hits DB ──
    # This covers both equity and crypto tickers. Without this, crypto tickers miss
    # latest_price_bar: (they set live_price: instead) and cause per-item DB queries.
    # Also build _bar_lookup_dict (plain dicts) for passing as _bar_data to _enrich_rule_results.
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
        }
        _bar_lookup_dict[_tk] = _bd
        _ck = f"latest_price_bar:{_tk}"
        if not cache.get(_ck):
            cache.set(_ck, _bd, expire_seconds=300)

    # ── Batch live-price warm-up for crypto tickers with cold cache ─────────────────────
    # Runs in parallel (ThreadPoolExecutor) so page-load latency is ~200ms, not 2–3s.
    # After first load the cache stays warm for 6 min; background task then takes over.
    _crypto_miss: list[str] = [
        w.ticker for w in items
        if (
            (getattr(w, "asset_type", None) == "CRYPTO")
            or w.ticker.endswith(("-AUD", "-USD", "-USDT"))
        )
        and not cache.get(f"live_price:{w.ticker}")
    ]
    if _crypto_miss:
        from concurrent.futures import ThreadPoolExecutor
        from app.data.fetcher import get_intraday_price as _gip_warm
        def _warm_one(t: str) -> None:
            try:
                r = _gip_warm(t, asset_type="CRYPTO")
                if r.get("ok") and r.get("price"):
                    pv = float(r["price"])
                    cache.set(f"live_price:{t}", {
                        "price": pv, "close": pv, "live_price": pv,
                        "data_source": r.get("data_source", ""), "_failed": False,
                    }, expire_seconds=360)
            except Exception:
                pass
        with ThreadPoolExecutor(max_workers=min(8, len(_crypto_miss))) as _wex:
            list(_wex.map(_warm_one, _crypto_miss))

    watchlist_data = []
    for w in items:
        company_name = stock_names.get(w.ticker, "")
        # NOTE: Inline yfinance name backfill removed — it blocks for 2-5s per unknown ticker.
        # Missing names are filled in by the background screener. The marker below prevents
        # re-attempting on every page load.
        if not company_name:
            _nck = f"missing_name_fetch:{w.ticker}"
            if not cache.get(_nck):
                cache.set(_nck, "attempted", expire_seconds=86400)

        # ── Price data ───────────────────────────────────────────────────────
        # For crypto: try live IR API first (cached 5 min), fall back to PriceBar.
        # For equities: use PriceBar EOD close (refreshed daily at 5pm AEST).
        # IMPORTANT: reset bar_data each iteration — without this, a crypto item's
        # IR data bleeds into the next equity item in the loop.
        bar_data = None
        # Infer CRYPTO from ticker format as well — covers rows where asset_type is NULL
        is_crypto_item = (
            (getattr(w, "asset_type", None) == "CRYPTO")
            or w.ticker.endswith(("-AUD", "-USD", "-USDT"))
        )
        live_price_used = False

        if is_crypto_item:
            live_cache_key = f"live_price:{w.ticker}"
            cached = cache.get(live_cache_key)
            if cached is not None:
                # Cache hit — could be real data or a failure sentinel
                bar_data = None if cached.get("_failed") else cached
            else:
                # Cache miss — batch pre-fetch above should have populated it;
                # fall back to EOD via PriceBar below
                bar_data = None

            if bar_data and bar_data.get("live_price"):
                live_price_used = True

        # Fall back to PriceBar (EOD) for equities or when live fetch fails
        if not bar_data:
            cache_key = f"latest_price_bar:{w.ticker}"
            bar_data = cache.get(cache_key)
            if bar_data is None:
                # Use pre-fetched bar from bulk lookup — no per-item DB hit
                bar = _bar_lookup.get(w.ticker)
                if bar:
                    bar_data = {
                        "close":     float(bar.close or 0),
                        "ma_50":     float(bar.ma_50 or 0),
                        "ma_150":    float(bar.ma_150 or 0),
                        "ma_200":    float(bar.ma_200 or 0),
                        "high_52w":  float(bar.high_52w or 0),
                        "low_52w":   float(bar.low_52w  or 0),
                        "rs_rating": float(bar.rs_rating or 0),
                        "bar_date":  str(bar.date) if bar.date else None,
                        "live_price": False,
                    }
                    cache.set(cache_key, bar_data, expire_seconds=300)
                else:
                    bar_data = {}
                    cache.set(cache_key, {}, expire_seconds=3600)

        stats_data = bar_data if bar_data else None

        rr = w.rule_results or {}
        passed = sum(1 for v in rr.values() if v.get("passed"))

        # Label info for card display
        lbl = None
        if w.label:
            lbl = {"id": w.label.id, "name": w.label.name, "color": w.label.color}

        watchlist_data.append({
            "id": w.id,
            "ticker": w.ticker,
            "exchange_key": getattr(w,"exchange_key","ASX") or "ASX",
            "asset_type":   getattr(w,"asset_type","EQUITY") or "EQUITY",
            "company_name": company_name,
            "added": str(w.added_date),
            "by": w.added_by,
            "notes": w.notes or "",
            "stats": stats_data,
            "rules_passed": passed,
            "rules_total": len(rr),
            "rule_results": _enrich_rule_results(w.ticker, rr, db, _bar_data=_bar_lookup_dict.get(w.ticker)),
            "label": lbl,
        })

    # Exchange filter already applied at DB level above — no Python-level re-filter needed.
    ef = _get_exchange_filters(org_id, db)
    ee = []
    try:
        from app.models.exchange import ExchangeConfig as _EC
        for e in db.query(_EC).filter(_EC.is_enabled==True).order_by(_EC.sort_order).all():
            ee.append({"key":e.exchange_key,"name":e.display_name,"flag":e.flag_emoji or "","asset_type":e.asset_type})
    except Exception:
        db.rollback(); ee=[{"key":"ASX","name":"ASX","flag":"","asset_type":"EQUITY"}]
    ctx.update({"enabled_exchanges":ee,"exchange_filters":ef,"active_exchange_filter":af,
                "base_url":"/watchlist","extra_params":f"label={label}" if label is not None else "",
                "watchlist":watchlist_data, "total":total, "page":page, "has_more":has_more})
    return templates.TemplateResponse("trading/watchlist.html", ctx)


@app.get("/watchlist/rows", response_class=HTMLResponse)
async def watchlist_rows(
    request: Request,
    label: int = Query(None),
    exchange: str = Query("ALL"),
    page: int = Query(1),
    db: Session = Depends(get_db)
):
    """Fragment endpoint — returns paginated watchlist card HTML for infinite scroll."""
    if not _auth(request):
        return HTMLResponse("", status_code=403)
    org_id = request.session.get("organization_id")

    from app.models.signal import Watchlist, WatchlistStatus, WatchlistLabel
    from app.models.market import PriceBar, Stock
    from sqlalchemy.orm import joinedload

    # Labels from Redis cache (card template needs them for label picker)
    labels = get_cached_wl_labels(org_id, db)

    # Same paginated query as the main route
    _WL_PER_PAGE = 20
    af = (exchange or "ALL").upper()
    q = db.query(Watchlist).options(joinedload(Watchlist.label)).filter(
        Watchlist.status == WatchlistStatus.WATCHING,
        Watchlist.organization_id == org_id
    )
    if label is not None:
        q = q.filter(Watchlist.label_id == label)
    if af == "ASX":
        q = q.filter(Watchlist.exchange_key == "ASX")
    elif af == "US":
        q = q.filter(Watchlist.exchange_key.in_(["NYSE", "NASDAQ"]))
    elif af == "CRYPTO":
        q = q.filter(Watchlist.asset_type == "CRYPTO")

    total = q.count()
    items = q.order_by(desc(Watchlist.created_at)).offset((page - 1) * _WL_PER_PAGE).limit(_WL_PER_PAGE).all()
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

        bar_data = cache.get(f"latest_price_bar:{w.ticker}") or {}
        stats_data = bar_data if bar_data else None

        rr = w.rule_results or {}
        passed = sum(1 for v in rr.values() if v.get("passed"))
        lbl = None
        if w.label:
            lbl = {"id": w.label.id, "name": w.label.name, "color": w.label.color}

        watchlist_data.append({
            "id": w.id,
            "ticker": w.ticker,
            "exchange_key": getattr(w, "exchange_key", "ASX") or "ASX",
            "asset_type":   getattr(w, "asset_type", "EQUITY") or "EQUITY",
            "company_name": company_name,
            "added": str(w.added_date),
            "by": w.added_by,
            "notes": w.notes or "",
            "stats": stats_data,
            "rules_passed": passed,
            "rules_total": len(rr),
            "rule_results": _enrich_rule_results(w.ticker, rr, db, _bar_data=_bar_lookup_dict.get(w.ticker)),
            "label": lbl,
        })

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

    return templates.TemplateResponse("components/watchlist_cards.html", {
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
    Records a Trade, marks Position CLOSED, writes audit, sends WhatsApp alert.
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

    # WhatsApp alert in background
    try:
        from app.tasks.reporting import send_whatsapp_message
        send_whatsapp_message.delay(
            org_id,
            "send_exit_alert",
            [pos.ticker, reason.value, pnl_pct, pnl_aud, pos.is_paper]
        )
    except Exception as e:
        from loguru import logger
        logger.error(f"Failed to queue exit alert WhatsApp message: {e}")

    return RedirectResponse("/positions?msg=closed", 302)


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
        db.commit()
    return RedirectResponse("/watchlist", 302)


# System action endpoints
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
    try:
        from app.tasks.screening import _run_screen_force
        _run_screen_force.delay(organization_id=org_id, exchange_key=exchange or "ASX")
    except Exception:
        pass
    return RedirectResponse("/signals?msg=screen", 302)


@app.post("/action/send-report")
async def action_send_report(request: Request):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")
    from app.tasks.reporting import send_daily_report
    send_daily_report.delay(organization_id=org_id)
    return RedirectResponse("/", 302)


@app.post("/action/evaluate-regime")
async def action_evaluate_regime(request: Request, exchange: str = Form("ASX")):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    from app.tasks.screening import evaluate_market_regime_task
    evaluate_market_regime_task.delay(exchange_key=exchange or "ASX")
    return RedirectResponse("/admin/health?msg=regime", 302)


@app.post("/action/ping-worker")
async def action_ping_worker(request: Request):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    from app.tasks.reporting import health_check
    health_check.delay()
    return RedirectResponse("/admin/health?msg=ping", 302)


@app.post("/action/refresh-data")
async def action_refresh_data(request: Request, exchange: str = Form(None)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    exchange_key = exchange or None
    is_crypto = exchange_key and (exchange_key == "CRYPTO" or exchange_key.startswith("CRYPTO_"))
    from app.tasks.screening import refresh_price_data, refresh_crypto_universe
    try:
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
    except Exception:
        refresh_price_data.delay(exchange_key=exchange_key)
    return RedirectResponse("/admin/health?msg=data", 302)


@app.post("/action/refresh-universe")
async def action_refresh_universe(request: Request, scope: str = Form(None)):
    """Refresh ASX universe with configurable scope (ASX200 / ASX300 / ALL_LISTED)."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    organization_id = request.session.get("organization_id")
    from app.tasks.screening import refresh_universe
    try:
        refresh_universe.delay(scope=scope or None, organization_id=organization_id)
    except Exception:
        pass
    return RedirectResponse("/admin/health?msg=universe", 302)


@app.post("/action/recategorise-labels")
async def action_recategorise_labels(request: Request, force: str = Form("0")):
    """Bulk-assign sector labels to all unlabelled watchlist items."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    organization_id = request.session.get("organization_id")
    from app.tasks.screening import recategorise_watchlist_labels
    try:
        recategorise_watchlist_labels.delay(organization_id=organization_id, force=(force == "1"))
    except Exception:
        pass
    return RedirectResponse("/admin/health?msg=recategorise", 302)


@app.post("/action/seed-crypto")
async def action_seed_crypto(request: Request, exchange: str = Form("CRYPTO_INDEPENDENTRESERVE")):
    """Seed (or refresh) the crypto stock universe for an exchange."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    from app.tasks.screening import refresh_crypto_universe
    try:
        refresh_crypto_universe.delay(exchange_key=exchange)
    except Exception:
        pass
    return RedirectResponse("/admin/health?msg=crypto_seed", 302)


@app.post("/action/full-setup")
async def action_full_setup(request: Request):
    """First-time setup: universe → price data → regime → screen. Runs as a Celery chain."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    from app.tasks.screening import run_full_setup
    run_full_setup.delay()
    return RedirectResponse("/admin/tasks?msg=setup", 302)


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
    try:
        from app.tasks.screening import _run_screen_force
        _run_screen_force.delay(organization_id=org_id, exchange_key=exchange or "ASX")
    except Exception:
        pass
    return RedirectResponse("/signals?msg=screen", 302)


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
        return JSONResponse({"ok": True, "last_id": last_id, "exchange": exchange})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/action/force-breakout-check")
async def action_force_breakout_check(request: Request, exchange: str = Form("ASX")):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    from app.tasks.trading import check_entry_triggers
    check_entry_triggers.delay(exchange_key=exchange or "ASX")
    return RedirectResponse("/admin/tasks?msg=breakout", 302)


@app.post("/action/force-exit-check")
async def action_force_exit_check(request: Request, exchange: str = Form("ASX")):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    from app.tasks.trading import check_exit_rules_task
    check_exit_rules_task.delay(exchange_key=exchange or "ASX")
    return RedirectResponse("/admin/tasks?msg=exit_check", 302)


@app.post("/action/force-position-sync")
async def action_force_position_sync(request: Request):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    from app.tasks.trading import sync_ibkr_positions_task
    sync_ibkr_positions_task.delay()
    return RedirectResponse("/admin/tasks?msg=positions", 302)


@app.post("/action/force-stop-sync")
async def action_force_stop_sync(request: Request):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    from app.tasks.trading import sync_stop_orders
    sync_stop_orders.delay()
    return RedirectResponse("/admin/tasks?msg=stops", 302)



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
        return JSONResponse({"error": str(exc), "trace": trace}, status_code=500)


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

    # ── Watchlist ──
    wl_items = (
        db.query(Watchlist)
        .options(joinedload(Watchlist.label))
        .filter(
            Watchlist.organization_id == org_id,
            Watchlist.status == WatchlistStatus.WATCHING,
        )
        .order_by(Watchlist.created_at.desc())
        .limit(150)
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

    # ── Build watchlist payload ──
    wl_data = []
    for w in wl_items:
        bar = price_map.get(w.ticker)
        has_sig = any(s.ticker == w.ticker for s in signals)
        close = float(bar.close) if bar and bar.close else None
        open_ = float(bar.open) if bar and bar.open else None
        chg = round((close - open_) / open_ * 100, 2) if close and open_ and open_ > 0 else 0.0
        wl_data.append({
            "ticker": w.ticker,
            "display_ticker": _disp(w.ticker),
            "name": stock_names.get(w.ticker, _disp(w.ticker)),
            "exchange_key": w.exchange_key or "ASX",
            "asset_type": w.asset_type or "EQUITY",
            "currency": w.currency or "AUD",
            "flag": _flag(w.exchange_key or "ASX"),
            "close": close,
            "change_pct": chg,
            "volume": int(bar.volume) if bar and bar.volume else 0,
            "label_name": w.label.name if w.label else None,
            "label_color": w.label.color if w.label else None,
            "has_signal": has_sig,
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
            "rs_rating": float(chk.rs_rating) if chk and chk.rs_rating else None,
            "ma_50": float(chk.ma_50) if chk and chk.ma_50 else None,
            "ma_200": float(chk.ma_200) if chk and chk.ma_200 else None,
            "data_source": chk.data_source if chk else None,
            "data_delay_mins": chk.data_delay_mins if chk else None,
            "rule_results": chk.rule_results if chk else {},
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
        return JSONResponse({"error": str(exc), "trace": trace}, status_code=500)


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
        asset_map[r[0]] = r[1] or "EQUITY"
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
                except Exception:
                    pass

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
        return JSONResponse({"error": str(exc), "trace": traceback.format_exc()}, status_code=500)


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
        return JSONResponse({"error": str(exc), "trace": traceback.format_exc()}, status_code=500)


def _trader_watchlist_data_inner(request: Request, db):
    from sqlalchemy import func
    from sqlalchemy.orm import joinedload
    from app.models.signal import Watchlist, WatchlistStatus, WatchlistLabel, Signal, SignalStatus
    from app.models.market import PriceBar, Stock
    from app.models.config import SystemConfig
    from app.models.account import Account
    from app.models.exchange import ExchangeConfig

    org_id = request.session.get("organization_id")

    # ── All WATCHING items (with label eager-loaded) ──
    wl_items = (
        db.query(Watchlist)
        .options(joinedload(Watchlist.label))
        .filter(
            Watchlist.organization_id == org_id,
            Watchlist.status == WatchlistStatus.WATCHING,
        )
        .order_by(Watchlist.created_at.desc())
        .limit(300)
        .all()
    )

    # ── All labels (for ordering) ──
    labels = (
        db.query(WatchlistLabel)
        .filter(WatchlistLabel.organization_id == org_id)
        .order_by(WatchlistLabel.sort_order, WatchlistLabel.id)
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
        return ticker.replace(".AX", "").replace("-AUD", "").replace("-USD", "")

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
                except Exception:
                    pass

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
            "has_pending_signal": w.ticker in pending_signal_tickers,
            "added_by": w.added_by or "screener",
            "added_date": w.added_date.isoformat() if w.added_date else None,
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

    return JSONResponse({
        "groups": groups,
        "regimes": regimes,
        "account_is_paper": is_paper,
        "stats": {
            "total": len(wl_items),
            "equity_count": equity_count,
            "crypto_count": crypto_count,
        },
    })


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


@app.get("/admin/tasks", response_class=HTMLResponse)
async def admin_tasks(request: Request, db: Session = Depends(get_db)):
    """Live task log — shows audit events with auto-polling for new entries."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.models.audit import AuditLog
    from app.models.market import Stock, PriceBar
    from app.models.signal import Signal, Watchlist, WatchlistStatus
    from sqlalchemy import func

    ctx = _global(request, db)

    # Seed latest 40 rows so the page is not blank on load
    try:
        logs = db.query(AuditLog).filter(AuditLog.organization_id == org_id).order_by(desc(AuditLog.id)).limit(40).all()
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
        "signal_count":    db.query(func.count(Signal.id)).filter(Signal.signal_date == get_current_date(), Signal.organization_id == org_id).scalar() or 0,
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
    from app.models.audit import AuditLog
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
        new_logs = db.query(AuditLog).filter(AuditLog.id > after, AuditLog.organization_id == org_id).order_by(desc(AuditLog.id)).limit(50).all()
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
        "universe":  _lr(["Universe refreshed", "universe"], actions=[AuditAction.SYSTEM_STARTED, AuditAction.TASK_RUN]),
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
    }
    ctx.update({"capital":float(account.capital_aud) if account else 0,"is_paper_account":account.is_paper if account else True,
        "recent_logs":[{"action":str(l.action).replace("AuditAction.",""),"ticker":l.ticker or "—","message":(l.message or "")[:80],"actor":l.actor,"time":_fmt_dt(str(l.created_at),display_tz)} for l in logs],
        "has_asx":has_asx,"has_us":has_us,"has_crypto":has_crypto,"active_exchanges":active_exchanges,"exchange_regimes":exchange_regimes,
        "stock_count":stock_count,"crypto_count":crypto_count,"price_bar_count":price_bar_count,"today_bars":today_bars,
        "equity_signal_count":esig,"crypto_signal_count":csig,"equity_wl_count":ewl,"crypto_wl_count":cwl,
        "signal_count_today":esig+csig,"watchlist_count":ewl+cwl,"is_first_run":stock_count==0 and crypto_count==0,"task_runs":task_runs})
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
    }
    CATEGORY_ICONS = {
        "TREND_TEMPLATE": "📈", "FUNDAMENTAL": "📊", "VCP": "🔄",
        "MARKET_REGIME": "🌡️",  "ENTRY": "🎯",       "EXIT_DEFENSIVE": "🛑",
        "EXIT_OFFENSIVE": "💰", "POSITION_SIZING": "⚖️", "PORTFOLIO": "🗂️",
        "EARNINGS": "📅",
    }

    rules_by_cat = {}
    for r in rules:
        cat = r.category.value
        if cat not in rules_by_cat:
            rules_by_cat[cat] = []
        
        enabled = r.is_enabled_for_tier(tier)
        threshold = r.threshold_for_tier(tier)

        rules_by_cat[cat].append({
            "id": r.id, "rule_id": r.rule_id, "label": r.label,
            "description": r.description or "", "minervini_ref": r.minervini_ref or "",
            "enabled": enabled, "is_mandatory": r.is_mandatory,
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
        "whatsapp":    {"icon": "💬",  "title": "Alert & Chat Channels"},
        "data":        {"icon": "📡",  "title": "Data Sources"},
        "risk":        {"icon": "🛡️",  "title": "Risk Management"},
        "crypto":      {"icon": "₿",   "title": "Crypto Exchange"},
        "system":      {"icon": "🔧",  "title": "System (Super Admin)"},
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
        "ibkr_account":          {"placeholder": "U1234567",       "hint_extra": "Your IBKR account number"},
        "ibkr_username":         {"placeholder": "ibkr_username"},
        "ibkr_password":         {"control": "password",
                                  "hint_extra": "IBKR Gateway login password — stored encrypted"},
        "ibkr_paper_mode":       {},   # boolean — auto-rendered by value_type
        "ibkr_account_usd":      {"placeholder": "U9876543",       "hint_extra": "USD sub-account (leave blank to use same account)"},
        "fx_audusd_override":    {"control": "number", "placeholder": "0.65", "step": "0.0001",
                                  "hint_extra": "Manual AUD/USD rate override. Leave blank to fetch live."},
        # Alerts & Chat
        "notification_channel":  {"control": "select", "options": [
                                     ("whatsapp", "WhatsApp"),
                                     ("telegram", "Telegram"),
                                 ],
                                  "hint_extra": "Select the active alert and command communication channel"},
        "telegram_enabled":      {},   # boolean
        "telegram_bot_token":    {"control": "password", "hint_extra": "Telegram bot token from @BotFather"},
        "telegram_chat_id":      {"placeholder": "123456789", "hint_extra": "Your Telegram user or group chat ID"},
        "whatsapp_admin_number": {"placeholder": "61450325233",
                                  "hint_extra": "Digits only, no + or spaces. E.g. 61450325233 for AU +61 450 325 233"},
        "whatsapp_api_key":      {"control": "password", "hint_extra": "WAHA API key from your .env"},
        "whatsapp_session_name": {"placeholder": "default",
                                  "hint_extra": "WAHA session name. Use 'default' for shared WAHA Core instance."},
        "whatsapp_enabled":      {},   # boolean
        # ASX Universe
        "asx_universe_scope":    {"control": "select", "options": [
                                     ("ASX200",     "ASX200 — Top 200 by market cap (default, fast)"),
                                     ("ASX300",     "ASX300 — Top 300 incl. mid-caps (~300 stocks)"),
                                     ("ALL_LISTED", "All Listed — Full ASX universe ~2,200+ stocks (slow)"),
                                 ],
                                  "hint_extra": "Controls which ASX stocks the screener scans. Larger scope = longer screener runtime (~15–45 min for ALL_LISTED). Run 'Refresh ASX Universe' after changing."},
        # Data
        "fmp_api_key":           {"control": "password",
                                  "hint_extra": "Financial Modeling Prep API key (free tier: 250 calls/day)",
                                  "link_url": "https://financialmodelingprep.com/developer/docs/",
                                  "link_text": "Get free key →"},
        # Risk / trading
        "weekly_injection_aud":  {"control": "number", "prefix": "A$", "placeholder": "1000",
                                  "hint_extra": "Weekly capital added to the account for position sizing calculations"},
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
    HIDDEN_GROUPS = {"system"} if request.session.get("user_role") != "superadmin" else set()
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
    Security: incoming chat_id must match a telegram_chat_id in SystemConfig.
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

    # Look up organization by telegram_chat_id
    org_id = None
    config = db.query(SystemConfig).filter(
        SystemConfig.key == "telegram_chat_id",
        SystemConfig.value == chat_id
    ).first()
    
    if config:
        org_id = config.organization_id
    else:
        # Fallback: check if the notification_channel is telegram and this matches a global one
        # Or just log warning
        logger.warning(f"Telegram message from unknown chat {chat_id} — ignored")
        return JSONResponse({"ok": True})

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


@app.post("/webhook/whatsapp")
async def webhook_whatsapp(request: Request, db: Session = Depends(get_db)):
    """
    WAHA calls this for every incoming WhatsApp message / session event.
    No login cookie required — security via sender JID matching admin number.
    """
    from fastapi.responses import JSONResponse
    from app.notifications.whatsapp import WhatsAppNotifier
    from app.agent.commands import AgentCommandHandler
    from app.config import settings

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

    session_name = body.get("session", "default")
    event = body.get("event", "")
    payload    = body.get("payload", {})
    from_jid   = payload.get("from", "")
    text       = payload.get("body", "").strip()
    is_from_me = payload.get("fromMe", False)

    org_id = None
    if session_name.startswith("org_"):
        try:
            org_id = int(session_name.split("_")[1])
        except (ValueError, IndexError):
            pass

    # If it's a shared session name like "default", look up by sender phone number (from_jid)
    if org_id is None and from_jid:
        sender_number = from_jid.split("@")[0] if "@" in from_jid else from_jid
        sender_number = sender_number.lstrip("+").replace(" ", "")
        
        from app.models.config import SystemConfig
        # Order by organization_id DESC to prioritize custom organizations (e.g. Org 10) over Default Org (Org 1)
        configs = db.query(SystemConfig).filter(SystemConfig.key == "whatsapp_admin_number").order_by(SystemConfig.organization_id.desc()).all()
        for cfg in configs:
            if cfg.value:
                val_clean = cfg.value.lstrip("+").replace(" ", "")
                if val_clean == sender_number:
                    org_id = cfg.organization_id
                    break

    # If still not resolved, fall back to default organization
    if org_id is None:
        from app.models.account import Organization
        default_org = db.query(Organization).order_by(Organization.id).first()
        if default_org:
            org_id = default_org.id

    if org_id is None:
        logger.error(f"Incoming WhatsApp message ignored — no organization context found for session {session_name}")
        return JSONResponse({"ok": True, "ignored": "no_org"})

    from app.models.config import SystemConfig
    def get_org_cfg(key: str) -> str | None:
        c = db.query(SystemConfig).filter(
            SystemConfig.key == key,
            SystemConfig.organization_id == org_id
        ).first()
        return c.value if c else None

    # Check if WhatsApp is enabled for this organization
    org_enabled = get_org_cfg("whatsapp_enabled")
    whatsapp_enabled = org_enabled.lower() in ("true", "1", "yes") if org_enabled is not None else settings.whatsapp_enabled

    if not whatsapp_enabled:
        logger.info(f"Incoming WhatsApp message ignored — WhatsApp disabled for Org {org_id}")
        return JSONResponse({"ok": True, "ignored": "disabled_for_org"})

    logger.debug(f"WAHA webhook: event={event}, session={session_name}, org_id={org_id}")

    # Session status notification — just log it
    if event == "session.status":
        status = body.get("payload", {}).get("status", "")
        logger.info(f"WAHA session status for {session_name} → {status}")
        return JSONResponse({"ok": True})

    if event not in ("message", "message.any"):
        return JSONResponse({"ok": True})

    if not text:
        return JSONResponse({"ok": True})

    # If the message is from me, only allow it to proceed if it is a valid AstraTrade command.
    # This prevents infinite message loops when the bot replies to itself.
    if is_from_me:
        cmd_word = text.split()[0].upper() if text.split() else ""
        valid_commands = {
            "STATUS", "POSITIONS", "SIGNALS", "WATCHLIST", "MARKET", 
            "PAUSE", "RESUME", "REPORT", "SKIP", "UNSKIP", "EXIT", 
            "STOP", "RULE", "CONFIG", "HELP"
        }
        if cmd_word not in valid_commands:
            return JSONResponse({"ok": True})
        logger.debug(f"WAHA webhook: allowing self-message command '{cmd_word}'")

    # Security: only respond to the configured admin number for this organization
    admin_number = get_org_cfg("whatsapp_admin_number")
    if not admin_number:
        admin_jid = settings.admin_jid
    else:
        num = admin_number.lstrip("+").replace(" ", "")
        admin_jid = f"{num}@c.us"

    if admin_jid and from_jid.split("@")[0] != admin_jid.split("@")[0]:
        logger.warning(f"WhatsApp from unknown sender {from_jid} for Org {org_id} (expected {admin_jid}) — ignored")
        return JSONResponse({"ok": True})

    handler  = AgentCommandHandler(organization_id=org_id)
    response = handler.handle(text, from_jid)

    notifier = WhatsAppNotifier(organization_id=org_id)
    notifier.send(response, chat_id=from_jid)

    return JSONResponse({"ok": True, "replied": response[:80]})


@app.get("/admin/whatsapp")
async def admin_whatsapp_redirect():
    return RedirectResponse("/admin/comms", 302)


@app.get("/admin/comms", response_class=HTMLResponse)
async def admin_comms(request: Request, db: Session = Depends(get_db)):
    """Communications hub — status for Telegram and WhatsApp integration."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")

    from app.notifications.whatsapp import WhatsAppNotifier
    from app.notifications.telegram import TelegramNotifier
    from app.models.audit import AuditLog, AuditAction
    from app.config import settings
    import httpx

    ctx = _global(request, db)
    
    # ── WhatsApp Context ──────────────────────────────────────────────────
    try:
        wa_notifier  = WhatsAppNotifier(organization_id=org_id)
        wa_session_info = wa_notifier.get_session_status()
        wa_status    = wa_session_info.get("status", "UNKNOWN")
        wa_qr_b64    = wa_notifier.get_qr() if wa_status in ("SCAN_QR_CODE", "STARTING") else None
        wa_session_name = wa_notifier.session
        wa_admin_jid = wa_notifier.admin_jid or ""
        wa_admin_number = wa_admin_jid.split("@")[0] if wa_admin_jid else ""
    except Exception as _e:
        wa_status = "UNKNOWN"
        wa_qr_b64 = None
        wa_session_name = "—"
        wa_admin_jid = ""
        wa_admin_number = ""

    # ── Telegram Context ──────────────────────────────────────────────────
    tg_status = "NOT_CONFIGURED"
    tg_bot_info = {}
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
        tg_chat_id = tg_notifier.chat_id
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
        "wa_status":       wa_status,
        "wa_session":      wa_session_name,
        "wa_admin_number": wa_admin_number,
        "wa_qr_b64":       wa_qr_b64,
        "tg_status":       tg_status,
        "tg_bot_info":     tg_bot_info,
        "tg_chat_id":      tg_chat_id,
        "tg_webhook_url":  f"{str(request.base_url).rstrip('/')}/webhook/telegram",
        "wa_webhook_url":  f"{str(request.base_url).rstrip('/')}/webhook/whatsapp",
        "recent_commands": [
            {"time":    _fmt_dt(str(l.created_at), ctx.get("display_tz", "UTC")),
             "message": (l.detail or {}).get("message", ""),
             "sender":  (l.detail or {}).get("sender", "")}
            for l in recent_cmds
        ],
        "msg": request.query_params.get("msg", ""),
    })
    return templates.TemplateResponse("admin/comms.html", ctx)


@app.post("/admin/whatsapp/start-session")
async def whatsapp_start_session(request: Request):
    """Force-restart the WAHA session."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")
    from app.notifications.whatsapp import WhatsAppNotifier
    new_status = WhatsAppNotifier(organization_id=org_id).restart_session()
    if new_status == "SCAN_QR_CODE":
        return RedirectResponse("/admin/comms?msg=scan_qr", 302)
    elif new_status == "WORKING":
        return RedirectResponse("/admin/comms?msg=already_working", 302)
    else:
        return RedirectResponse(f"/admin/comms?msg=started", 302)


@app.post("/admin/telegram/set-webhook")
async def telegram_set_webhook(request: Request):
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
        
    try:
        url = f"https://api.telegram.org/bot{notifier.token}/setWebhook"
        resp = httpx.post(url, data={"url": hook_url}, timeout=10)
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
        ctx.update({"error": f"Organisation '{name}' already exists"})
        return templates.TemplateResponse("superadmin/organizations.html", ctx, status_code=400)
    if existing_user:
        ctx.update({"error": f"User with email '{admin_email}' already exists"})
        return templates.TemplateResponse("superadmin/organizations.html", ctx, status_code=400)

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

    # 3. Create Org Admin User
    import secrets
    from datetime import datetime, timedelta
    dummy_pass = secrets.token_hex(16)
    hashed_pwd = hash_password(dummy_pass)
    token = secrets.token_urlsafe(32)
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

    # Assign Organisation Admin Role
    admin_role = db.query(Role).filter(Role.name == "Organisation Admin").first()
    if admin_role:
        user.roles.append(admin_role)

    # 4. Seed Organization System Configurations
    # WAHA Core (free) only supports one "default" session shared by all orgs.
    # The webhook routes messages to the correct org by matching the sender phone number
    # against each org's whatsapp_admin_number setting.
    # For per-org sessions, use WAHA Plus (paid): devlikeapro/waha-plus:latest
    configs_to_seed = [
        ("trading_paused", "false", ConfigValueType.BOOLEAN, "Trading Paused", "Toggles automated trade placement"),
        ("whatsapp_enabled", "true", ConfigValueType.BOOLEAN, "WhatsApp Alerts", "Enables real-time notifications"),
        ("whatsapp_admin_number", "", ConfigValueType.STRING, "WhatsApp Admin Number", "Number to send alerts and receive commands JID format"),
        ("whatsapp_api_key", settings.waha_api_key, ConfigValueType.STRING, "WhatsApp API Key", "API key for the WhatsApp (WAHA) service", True),
        ("whatsapp_session_name", "default", ConfigValueType.STRING, "WhatsApp Session Name", "WAHA session name (always 'default' for WAHA Core; use WAHA Plus for per-org sessions)"),
        ("notification_channel", "telegram", ConfigValueType.STRING, "Notification Channel", "Active communication channel ('whatsapp' or 'telegram')"),
        ("telegram_enabled", "true", ConfigValueType.BOOLEAN, "Telegram Alerts Enabled", "Enable or disable Telegram notifications"),
        ("telegram_bot_token", "", ConfigValueType.STRING, "Telegram Bot Token", "The Telegram Bot Token from @BotFather", True),
        ("telegram_chat_id", "", ConfigValueType.STRING, "Telegram Chat ID", "The Telegram Chat ID to send alerts to"),
        ("ibkr_account", "", ConfigValueType.STRING, "IBKR Account ID", "Interactive Brokers account number"),
        ("ibkr_username", "", ConfigValueType.STRING, "IBKR Username", "Interactive Brokers login username"),
        ("ibkr_password", "", ConfigValueType.STRING, "IBKR Password", "Interactive Brokers login password", True),
        ("ibkr_paper_mode", "true", ConfigValueType.BOOLEAN, "IBKR Paper Mode", "Use paper trading environment"),
        ("fmp_api_key", "", ConfigValueType.STRING, "FMP API Key", "Financial Modeling Prep API key", True),
        ("working_capital_aud", "5000.0", ConfigValueType.FLOAT, "Working Capital (AUD)", "Working capital used for sizing and risk calculations"),
    ]
    for cfg_item in configs_to_seed:
        key, val, vtype, label, desc = cfg_item[:5]
        is_sec = cfg_item[5] if len(cfg_item) > 5 else False
        db.add(SystemConfig(
            key=key, value=val, value_type=vtype, label=label,
            description=desc, is_secret=is_sec, organization_id=org.id,
            group="broker" if "ibkr" in key else ("whatsapp" if ("whatsapp" in key or "telegram" in key or "notification" in key) else "general")
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

    db.commit()

    # Welcome email sending flow for Organization Admin
    from app.utils.email import send_email
    import urllib.parse
    
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
    encoded_email = urllib.parse.quote(user.email)
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
    from app.models.audit import AuditLog
    from app.models.config import SystemConfig

    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        return RedirectResponse("/superadmin/organizations", 302)

    ctx = _global(request, db)
    users = db.query(User).filter(User.organization_id == org_id).all()
    accounts = db.query(Account).filter(Account.organization_id == org_id).all()
    try:
        logs = db.query(AuditLog).filter(AuditLog.organization_id == org_id).order_by(desc(AuditLog.created_at)).limit(50).all()
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

    ctx.update({
        "organization": org,
        "users": users,
        "accounts": accounts,
        "logs": logs,
        "msg": request.query_params.get("msg", ""),
        "mcp_credentials": mcp_credentials,
        "mcp_base_url": mcp_base_url,
        "mcp_all_scopes": MCP_ALL_SCOPES,
        "mcp_scope_descriptions": SCOPE_DESCRIPTIONS,
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
    from sqlalchemy.orm.attributes import flag_modified

    global_rules = db.query(RuleConfig).filter(RuleConfig.organization_id == None).all()
    synced = 0
    skipped = 0
    for g in global_rules:
        org_rules = db.query(RuleConfig).filter(
            RuleConfig.rule_id == g.rule_id,
            RuleConfig.organization_id != None,
        ).all()
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

    db.commit()
    return RedirectResponse(f"/superadmin/rules?saved=1&synced={synced}&skipped={skipped}", 302)


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

    from app.models.auth import User, Role
    from app.models.account import Organization

    search = request.query_params.get("search", "").strip()
    selected_org_id = request.query_params.get("org_id", "")

    query = db.query(User)
    if search:
        query = query.filter((User.name.ilike(f"%{search}%")) | (User.email.ilike(f"%{search}%")))
    if selected_org_id:
        query = query.filter(User.organization_id == int(selected_org_id))

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
    import secrets

    email_clean = email.strip().lower()
    existing_user = db.query(User).filter(User.email == email_clean).first()
    if existing_user:
        ctx = _global(request, db)
        users = db.query(User).order_by(User.email).all()
        organizations = db.query(Organization).order_by(Organization.name).all()
        roles = db.query(Role).order_by(Role.name).all()
        ctx.update({
            "users": users, "organizations": organizations, "roles": roles,
            "error": f"User with email '{email}' already exists",
            "search": "", "selected_org_id": "",
        })
        return templates.TemplateResponse("superadmin/users.html", ctx, status_code=400)

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

    role = db.query(Role).filter(Role.id == role_id).first()
    if role:
        user.roles.append(role)
    db.commit()

    import urllib.parse
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
                "bar_date":      str(bar.date) if bar else "",
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
                "bar_date":     str(r.bar_date) if r.bar_date else "",
                "rs_rating":    float(r.rs_rating) if r.rs_rating else None,
                "vol_ratio":    float(r.vol_ratio) if r.vol_ratio else None,
                "ma_50":        float(r.ma_50) if r.ma_50 else None,
                "ma_200":       float(r.ma_200) if r.ma_200 else None,
                "pct_from_52w_high": float(r.pct_from_52w_high) if r.pct_from_52w_high else None,
                "added_at":     str(r.added_at)[:10] if r.added_at else "",
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
        "latest_bar_date":  str(latest_bar_date_r) if latest_bar_date_r else "—",
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
        "price_asx":     _glr(["Price data", "price data"], "ASX"),
        "price_us":      _glr(["Price data", "price data"], "NYSE") if has_us else None,
        "price_crypto":  _glr(["Price data", "price data"], "CRYPTO") if has_crypto else None,
        "regime_asx":    _glr(["Market regime"], "ASX",   actions=[AuditAction.MARKET_REGIME_CHANGE, AuditAction.TASK_RUN]) if has_asx else None,
        "regime_us":     _glr(["Market regime"], "NYSE",  actions=[AuditAction.MARKET_REGIME_CHANGE, AuditAction.TASK_RUN]) if has_us else None,
        "regime_crypto": _glr(["Market regime"], "CRYPTO",actions=[AuditAction.MARKET_REGIME_CHANGE, AuditAction.TASK_RUN]) if has_crypto else None,
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


# ── Super admin global action routes ─────────────────────────────────────────

@app.post("/superadmin/action/refresh-data")
async def sa_action_refresh_data(request: Request, exchange: str = Form(None)):
    if not _auth(request) or not _is_superadmin(request):
        return RedirectResponse("/login", 302)
    from app.tasks.screening import refresh_price_data, refresh_crypto_universe
    try:
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
    except Exception:
        refresh_price_data.delay(exchange_key=exchange or None)
    return RedirectResponse("/superadmin/operations?msg=data", 302)


@app.post("/superadmin/action/refresh-universe")
async def sa_action_refresh_universe(request: Request, scope: str = Form(None)):
    if not _auth(request) or not _is_superadmin(request):
        return RedirectResponse("/login", 302)
    from app.tasks.screening import refresh_universe
    try:
        refresh_universe.delay(scope=scope or None, organization_id=None)
    except Exception:
        pass
    return RedirectResponse("/superadmin/operations?msg=universe", 302)


@app.post("/superadmin/action/evaluate-regime")
async def sa_action_evaluate_regime(request: Request, exchange: str = Form("ASX")):
    if not _auth(request) or not _is_superadmin(request):
        return RedirectResponse("/login", 302)
    from app.tasks.screening import evaluate_market_regime_task
    evaluate_market_regime_task.delay(exchange_key=exchange or "ASX")
    return RedirectResponse("/superadmin/operations?msg=regime", 302)


@app.post("/superadmin/action/seed-crypto")
async def sa_action_seed_crypto(request: Request, exchange: str = Form("CRYPTO_INDEPENDENTRESERVE")):
    if not _auth(request) or not _is_superadmin(request):
        return RedirectResponse("/login", 302)
    from app.tasks.screening import refresh_crypto_universe
    try:
        refresh_crypto_universe.delay(exchange_key=exchange)
    except Exception:
        pass
    return RedirectResponse("/superadmin/operations?msg=crypto_seed", 302)


@app.post("/superadmin/action/full-setup")
async def sa_action_full_setup(request: Request):
    if not _auth(request) or not _is_superadmin(request):
        return RedirectResponse("/login", 302)
    from app.tasks.screening import run_full_setup
    run_full_setup.delay()
    return RedirectResponse("/superadmin/operations?msg=setup", 302)


@app.post("/superadmin/action/ping-worker")
async def sa_action_ping_worker(request: Request):
    if not _auth(request) or not _is_superadmin(request):
        return RedirectResponse("/login", 302)
    from app.tasks.reporting import health_check
    health_check.delay()
    return RedirectResponse("/superadmin/operations?msg=ping", 302)


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
        f"client_id={client_id}, code={code}, code_verifier={code_verifier}, redirect_uri={req_redirect_uri}"
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
