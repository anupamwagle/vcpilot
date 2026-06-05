"""
VCPilot Dashboard — FastAPI + Flowbite/Tailwind
Mobile-first. Split: /trading (client) and /admin (operator).
"""
import os, sys
sys.path.insert(0, "/app")

from datetime import date, datetime, timedelta
from app.utils.time_helper import get_current_date
from fastapi import FastAPI, Request, Form, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import desc
from loguru import logger

app = FastAPI(title="VCPilot", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=os.getenv("APP_SECRET_KEY", "changeme-secret"))

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
            "error_message": "An internal server error occurred. VCPilot has logged the issue and our team has been alerted.",
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
                        json={"webhooks": [{"url": hook, "events": ["message","session.status"]}]},
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
    from app.models.signal import Signal

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

    # Resolve active market regime: use mock_market_regime when simulation clock is on
    mock_time_on = cfg("mock_time_enabled", "false").lower() == "true"
    if mock_time_on:
        mock_regime = cfg("mock_market_regime", "")
        regime_raw  = mock_regime if mock_regime else cfg("last_market_regime", "")
        regime_is_simulated = True
    else:
        regime_raw  = cfg("last_market_regime", "")
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
            Signal.signal_date == get_current_date(),
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
    }


def _auth(request: Request):
    return request.session.get("authenticated", False)


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
async def login_get(request: Request):
    if _auth(request):
        return RedirectResponse("/", 302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
async def login_post(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
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
        return RedirectResponse("/", 302)

    # 2. Check Database Users
    user = db.query(User).filter(User.email == email_clean).first()
    if user and verify_password(password, user.password_hash):
        if not user.is_active:
            return templates.TemplateResponse("login.html", {"request": request, "error": "User account is disabled"}, status_code=401)
        
        # Check if user has "Super Admin" role in DB
        is_super = any(r.name == "Super Admin" for r in user.roles)
        
        request.session["authenticated"] = True
        request.session["user_role"] = "superadmin" if is_super else "user"
        request.session["user_id"] = user.id
        request.session["organization_id"] = user.organization_id
        request.session["organization_name"] = user.organization.name
        request.session["email"] = user.email
        return RedirectResponse("/", 302)

    return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid email or password"}, status_code=401)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 302)


# ---------------------------------------------------------------------------
# OTP Login & Switch Org
# ---------------------------------------------------------------------------
@app.post("/login/request-otp")
async def login_request_otp(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    import secrets
    from datetime import datetime, timedelta
    from app.models.auth import User
    from app.utils.email import send_email
    from app.config import settings

    email_clean = email.strip().lower()
    user = db.query(User).filter(User.email == email_clean).first()
    if not user:
        return templates.TemplateResponse("login.html", {"request": request, "error": "Email address not found"}, status_code=404)
    if not user.is_active:
        return templates.TemplateResponse("login.html", {"request": request, "error": "User account is disabled"}, status_code=401)

    # Generate 6-digit OTP code
    otp = f"{secrets.randbelow(900000) + 100000}"
    user.otp_code = otp
    user.otp_expires_at = datetime.utcnow() + timedelta(minutes=10)
    db.commit()

    # Send email
    subject = "Your VCPilot One-Time Passcode (OTP)"
    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #e5e7eb; border-radius: 8px;">
        <h2 style="color: #1d4ed8; margin-bottom: 20px;">VCPilot OTP Login</h2>
        <p>You requested a one-time passcode to sign in to your VCPilot account.</p>
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
    return RedirectResponse(f"/login/verify-otp?email={user.email}{debug_param}", 302)


@app.get("/login/verify-otp", response_class=HTMLResponse)
async def login_verify_otp_get(request: Request, email: str = Query(...), debug_otp: str = Query(None)):
    return templates.TemplateResponse("verify_otp.html", {"request": request, "email": email, "debug_otp": debug_otp, "error": None})


@app.post("/login/verify-otp")
async def login_verify_otp_post(request: Request, email: str = Form(...), otp_code: str = Form(...), db: Session = Depends(get_db)):
    from datetime import datetime
    from app.models.auth import User

    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if not user:
        return templates.TemplateResponse("verify_otp.html", {"request": request, "email": email, "error": "User session expired. Please request a new OTP."}, status_code=400)
    
    if not user.otp_code or user.otp_code != otp_code.strip() or not user.otp_expires_at or user.otp_expires_at < datetime.utcnow():
        return templates.TemplateResponse("verify_otp.html", {"request": request, "email": email, "error": "Invalid or expired OTP code"}, status_code=400)

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
# CLIENT AREA — TRADING
# ===========================================================================

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, db: Session = Depends(get_db)):
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
        curr  = float(p.current_price or p.entry_price)
        entry = float(p.entry_price)
        stop  = float(p.current_stop)
        pnl   = (curr - entry) * p.qty
        total_risk += (entry - stop) * p.qty
        pos_data.append({
            "ticker": p.ticker,
            "company_name": stock_names.get(p.ticker, ""),
            "qty": p.qty,
            "entry": entry, "current": curr, "stop": stop,
            "pnl_pct": round((curr - entry) / entry * 100, 2),
            "pnl_aud": round(pnl, 2),
            "days": (get_current_date() - p.entry_date).days,
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
        {"name": "Minervini Daily Screener", "frequency": "Daily (Mon-Fri 5:30pm AEST)", "log": latest_screen},
        {"name": "Intraday Breakout Entry Check", "frequency": "Every 5 min (10am-4:12pm AEST)", "log": latest_entry},
        {"name": "Intraday Position Exit Check", "frequency": "Every 5 min (10am-4:12pm AEST)", "log": latest_exit},
    ]

    # ── Watchlist Market Data Table ──
    from app.models.signal import Watchlist, WatchlistStatus, WatchlistLabel
    from app.models.market import Stock, PriceBar
    from sqlalchemy import func, and_
    from sqlalchemy.orm import joinedload

    all_labels = db.query(WatchlistLabel).filter(WatchlistLabel.organization_id == org_id).order_by(WatchlistLabel.sort_order).all()
    wl_labels_data = [{"id": l.id, "name": l.name, "color": l.color} for l in all_labels]

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

    ctx.update({
        "capital": capital,
        "positions": pos_data,
        "signals": sig_data,
        "portfolio_heat": round(total_risk / capital * 100, 1) if capital else 0,
        "today_pnl": round(sum(float(t.net_pnl_aud or 0) for t in today_trades), 2),
        "total_pnl":  round(sum(float(t.net_pnl_aud or 0) for t in all_trades), 2),
        "trade_count": len(all_trades),
        "checks": checks,
        "watchlist_rows": watchlist_rows,
        "wl_labels": wl_labels_data,
        "wl_active_label": active_label_id,
        "wl_only_custom": only_custom,
    })
    return templates.TemplateResponse("trading/home.html", ctx)


@app.get("/positions", response_class=HTMLResponse)
async def positions(request: Request, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.models.trade import Position, Trade, TradeStatus
    from app.models.market import Stock
    from app.models.account import Account

    ctx = _global(request, db)

    stock_names = {s.ticker: (s.name or "") for s in db.query(Stock).all()}

    positions = db.query(Position).filter(Position.status == TradeStatus.OPEN, Position.organization_id == org_id).all()
    pos_data = []
    total_risk = 0.0
    for p in positions:
        curr  = float(p.current_price or p.entry_price)
        entry = float(p.entry_price)
        stop  = float(p.current_stop)
        total_risk += (entry - stop) * p.qty
        
        # Query last 2 exit checks for this position
        exit_checks = []
        try:
            from app.models.audit import AuditLog, AuditAction
            log_entries = db.query(AuditLog).filter(
                AuditLog.organization_id == org_id,
                AuditLog.action == AuditAction.TASK_RUN,
                AuditLog.ticker == p.ticker,
                AuditLog.entity_type == "Position",
                AuditLog.entity_id == str(p.id)
            ).order_by(desc(AuditLog.created_at)).limit(2).all()
            for log_entry in log_entries:
                exit_checks.append({
                    "id": log_entry.id,
                    "time": _fmt_dt(str(log_entry.created_at), ctx.get("display_tz", "UTC")),
                    "message": log_entry.message,
                })
        except Exception:
            pass

        pos_data.append({
            "id": p.id, "ticker": p.ticker,
            "company_name": stock_names.get(p.ticker, ""),
            "qty": p.qty,
            "entry": entry, "current": curr,
            "stop": stop,
            "target_1": float(p.target_1 or 0),
            "pnl_pct": round((curr - entry) / entry * 100, 2),
            "pnl_aud": round((curr - entry) * p.qty, 2),
            "days": (get_current_date() - p.entry_date).days,
            "entry_date": str(p.entry_date),
            "is_paper": p.is_paper,
            "exit_checks": exit_checks,
        })

    trades = db.query(Trade).filter(Trade.organization_id == org_id).order_by(desc(Trade.exit_date)).limit(50).all()
    trade_data = [{
        "ticker": t.ticker,
        "company_name": stock_names.get(t.ticker, ""),
        "entry_date": str(t.entry_date), "exit_date": str(t.exit_date),
        "days": t.hold_days,
        "entry": float(t.entry_price), "exit": float(t.exit_price),
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

    ctx.update({
        "positions": pos_data, "trades": trade_data,
        "win_rate": round(len(wins) / len(trades) * 100) if trades else 0,
        "total_pnl": round(sum(float(t.net_pnl_aud or 0) for t in trades), 2),
        "trade_count": len(trades),
        "portfolio_heat": portfolio_heat,
        "win_loss_ratio": win_loss_ratio,
        "avg_hold_time": avg_hold_time,
        "capital": capital,
    })
    return templates.TemplateResponse("trading/positions.html", ctx)


def _enrich_rule_results(ticker: str, rule_results_dict: dict, db_session, target_date=None, overrides=None) -> list[dict]:
    """
    Enrich rule results with actual values from the price bar on the given date (or latest).
    """
    from app.models.market import PriceBar
    
    # Query price bar for the given date, or latest
    bar = None
    if target_date:
        bar = db_session.query(PriceBar).filter(PriceBar.ticker == ticker).filter(PriceBar.date == target_date).first()
    else:
        cache_key = f"latest_price_bar:{ticker}"
        cached = cache.get(cache_key)
        if cached is not None:
            if cached:
                class DictObject:
                    def __init__(self, data):
                        for k, v in data.items():
                            setattr(self, k, v)
                bar = DictObject(cached)
        else:
            bar_obj = db_session.query(PriceBar).filter(PriceBar.ticker == ticker).order_by(desc(PriceBar.date)).first()
            if bar_obj:
                bar_data = {
                    "close": float(bar_obj.close or 0),
                    "ma_50": float(bar_obj.ma_50 or 0),
                    "ma_150": float(bar_obj.ma_150 or 0),
                    "ma_200": float(bar_obj.ma_200 or 0),
                    "high_52w": float(bar_obj.high_52w or 0),
                    "low_52w": float(bar_obj.low_52w or 0),
                    "rs_rating": float(bar_obj.rs_rating or 0),
                }
                cache.set(cache_key, bar_data, expire_seconds=300)
                class DictObject:
                    def __init__(self, data):
                        for k, v in data.items():
                            setattr(self, k, v)
                bar = DictObject(bar_data)
            else:
                cache.set(cache_key, {}, expire_seconds=3600)
    
    enriched = []
    for rid, robj in rule_results_dict.items():
        passed = robj.get("passed", False) if isinstance(robj, dict) else bool(robj)
        val_str = ""
        
        # Determine labels
        clean_label = rid.replace("trend_", "").replace("fundamental_", "").replace("vcp_", "").replace("_", " ")
        
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
            
        # Fallback to saved value if database bar doesn't exist
        if not val_str and isinstance(robj, dict) and robj.get("value") is not None:
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
async def signals(request: Request, db: Session = Depends(get_db)):
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
        }
        for r in all_org_rules
    }

    from app.models.audit import AuditLog, AuditAction
    sig_tz = _get_display_tz(org_id, db)

    from sqlalchemy import or_
    from app.models.signal import SignalStatus
    sigs = db.query(Signal).filter(
        or_(
            Signal.signal_date == get_current_date(),
            Signal.status == SignalStatus.PENDING
        ),
        Signal.organization_id == org_id
    ).all()
    stock_names = get_cached_stock_names(db)

    sig_data = []
    for s in sigs:
        company_name = stock_names.get(s.ticker, "")

        # Lazy backfill: fetch company name from yfinance if missing and not attempted recently
        if not company_name:
            cache_key = f"missing_name_fetch:{s.ticker}"
            if not cache.get(cache_key):
                try:
                    from app.data.fetcher import get_fundamentals
                    fdata = get_fundamentals(s.ticker)
                    if fdata.get("company_name"):
                        stock_db = db.query(Stock).filter(Stock.ticker == s.ticker).first()
                        if stock_db:
                            stock_db.name     = fdata["company_name"]
                            stock_db.sector   = fdata.get("sector") or stock_db.sector
                            stock_db.industry = fdata.get("industry") or stock_db.industry
                            db.commit()
                            company_name = stock_db.name
                            cache.delete("stock_names_map") # Clear cache
                except Exception:
                    pass
                cache.set(cache_key, "attempted", expire_seconds=86400)  # 24 hours

        rr = s.rule_results or {}
        passed = sum(1 for v in rr.values() if v.get("passed"))
        overrides = s.rule_overrides or {}

        # ── Latest entry check result for this signal ────────────────────
        last_check = None
        try:
            audit_entries = db.query(AuditLog).filter(
                AuditLog.organization_id == org_id,
                AuditLog.action == AuditAction.TASK_RUN,
                AuditLog.ticker == s.ticker,
            ).order_by(desc(AuditLog.created_at)).limit(20).all()
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

        override_rules_failed = []
        override_rules_passed = []
        for rule_id, meta in rules_meta.items():
            if not meta["globally_enabled"]:
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
            "rules_passed": passed,
            "rules_total": len(rr),
            "rule_results": _enrich_rule_results(s.ticker, rr, db, target_date=s.signal_date, overrides=overrides),
            "override_rules_failed": override_rules_failed,
            "override_rules_passed": override_rules_passed,
            "has_overrides": has_overrides,
            "is_promoted_manual": bool(is_promoted_manual),
            "is_promoted_vcp": bool(is_promoted_vcp),
            "last_check": last_check,
        })
    ctx.update({"signals": sig_data, "signal_date": str(get_current_date())})
    return templates.TemplateResponse("trading/signals.html", ctx)


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
    label: int = Query(None),       # filter by label id; None = show all
    db: Session = Depends(get_db)
):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")

    from app.models.signal import Watchlist, WatchlistStatus, WatchlistLabel
    from app.models.market import PriceBar, Stock
    ctx = _global(request, db)

    # Load all labels for this org (for filter chips)
    all_labels = db.query(WatchlistLabel).filter(
        WatchlistLabel.organization_id == org_id
    ).order_by(WatchlistLabel.sort_order).all()
    ctx["labels"] = [{"id": l.id, "name": l.name, "color": l.color, "is_default": l.is_default} for l in all_labels]
    ctx["active_label"] = label  # currently selected filter (None = all)

    from sqlalchemy.orm import joinedload
    q = db.query(Watchlist).options(joinedload(Watchlist.label)).filter(
        Watchlist.status == WatchlistStatus.WATCHING,
        Watchlist.organization_id == org_id
    )
    if label is not None:
        q = q.filter(Watchlist.label_id == label)
    items = q.order_by(desc(Watchlist.created_at)).all()

    stock_names = get_cached_stock_names(db)

    watchlist_data = []
    for w in items:
        company_name = stock_names.get(w.ticker, "")
        if not company_name:
            cache_key = f"missing_name_fetch:{w.ticker}"
            if not cache.get(cache_key):
                try:
                    from app.data.fetcher import get_fundamentals
                    fdata = get_fundamentals(w.ticker)
                    if fdata.get("company_name"):
                        stock_db = db.query(Stock).filter(Stock.ticker == w.ticker).first()
                        if stock_db:
                            stock_db.name     = fdata["company_name"]
                            stock_db.sector   = fdata.get("sector") or stock_db.sector
                            stock_db.industry = fdata.get("industry") or stock_db.industry
                            db.commit()
                            company_name = stock_db.name
                            cache.delete("stock_names_map") # Clear cache
                except Exception:
                    pass
                cache.set(cache_key, "attempted", expire_seconds=86400)  # 24 hours

        # Check cache for PriceBar details
        cache_key = f"latest_price_bar:{w.ticker}"
        bar_data = cache.get(cache_key)
        if bar_data is None:
            bar = db.query(PriceBar).filter(PriceBar.ticker == w.ticker).order_by(desc(PriceBar.date)).first()
            if bar:
                bar_data = {
                    "close": float(bar.close or 0),
                    "ma_50": float(bar.ma_50 or 0),
                    "ma_150": float(bar.ma_150 or 0),
                    "ma_200": float(bar.ma_200 or 0),
                    "high_52w": float(bar.high_52w or 0),
                    "low_52w": float(bar.low_52w or 0),
                    "rs_rating": float(bar.rs_rating or 0),
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
            "company_name": company_name,
            "added": str(w.added_date),
            "by": w.added_by,
            "notes": w.notes or "",
            "stats": stats_data,
            "rules_passed": passed,
            "rules_total": len(rr),
            "rule_results": _enrich_rule_results(w.ticker, rr, db),
            "label": lbl,
        })

    ctx["watchlist"] = watchlist_data
    return templates.TemplateResponse("trading/watchlist.html", ctx)


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

    t = ticker.strip().upper()
    if not t.endswith(".AX"):
        t += ".AX"

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
    Manually close an open position the Minervini way.
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
    pnl_aud = (close_price - entry_price) * pos.qty
    pnl_pct = (close_price - entry_price) / entry_price * 100

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

    # WhatsApp alert
    try:
        from app.notifications.whatsapp import WhatsAppNotifier
        WhatsAppNotifier(organization_id=org_id).send_exit_alert(
            pos.ticker, reason.value, pnl_pct, pnl_aud, pos.is_paper
        )
    except Exception:
        pass

    return RedirectResponse("/positions?msg=closed", 302)


@app.post("/watchlist/add")
async def watchlist_add(request: Request, ticker: str = Form(...), notes: str = Form(""), label_id: str = Form(""), db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    t = ticker.strip().upper()
    if not t.endswith(".AX"):
        t += ".AX"
    lbl_id = int(label_id) if label_id.isdigit() else None
    from app.tasks.screening import screen_single_ticker
    screen_single_ticker.delay(t, notes, organization_id=org_id, label_id=lbl_id)
    return RedirectResponse("/watchlist?msg=added", 302)


@app.post("/watchlist/{item_id}/promote")
async def watchlist_promote(request: Request, item_id: int, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.models.signal import Watchlist, WatchlistStatus, Signal, SignalStatus
    from app.models.market import PriceBar, Stock
    from app.models.audit import AuditLog, AuditAction
    
    w = db.query(Watchlist).filter(Watchlist.id == item_id, Watchlist.organization_id == org_id).first()
    if not w:
        return RedirectResponse("/watchlist?msg=not_found", 302)

    # ── Ensure the Stock row has a company name (may be empty for manually-added tickers) ──
    stock_row = db.query(Stock).filter(Stock.ticker == w.ticker).first()
    if stock_row and not stock_row.name:
        try:
            from app.data.fetcher import get_fundamentals
            fdata = get_fundamentals(w.ticker)
            if fdata.get("company_name"):
                stock_row.name     = fdata["company_name"]
                stock_row.sector   = fdata.get("sector") or stock_row.sector
                stock_row.industry = fdata.get("industry") or stock_row.industry
        except Exception:
            pass  # non-critical — name will just stay blank

    bar = db.query(PriceBar).filter(PriceBar.ticker == w.ticker).order_by(desc(PriceBar.date)).first()
    close_price = float(bar.close) if bar and bar.close else 1.0
    
    pivot = close_price
    stop = close_price * 0.92
    
    today = get_current_date()
    existing = db.query(Signal).filter(Signal.ticker == w.ticker, Signal.signal_date == today, Signal.organization_id == org_id).first()
    if not existing:
        sig = Signal(
            ticker=w.ticker,
            signal_date=today,
            status=SignalStatus.PENDING,
            close_price=close_price,
            pivot_price=pivot,
            stop_price=stop,
            target_price_1=pivot * 1.20,
            target_price_2=pivot * 1.40,
            rs_rating=float(bar.rs_rating or 0) if bar else 0,
            trend_score=w.rules_passed if hasattr(w, 'rules_passed') else 6,
            rule_results=w.rule_results or {},
            notes=f"[Manual Promotion] {request.session.get('email', 'dashboard')} | {w.notes or ''}".strip().rstrip('|').strip(),
            organization_id=org_id,
        )
        db.add(sig)
        
    w.status = WatchlistStatus.SIGNALLED
    db.add(AuditLog(
        action=AuditAction.MANUAL_OVERRIDE,
        ticker=w.ticker,
        actor=request.session.get("email","dashboard"),
        user_id=request.session.get("user_id"),
        message="Watchlist item manually promoted to Signal",
        organization_id=org_id
    ))
    db.commit()
    
    try:
        from app.notifications.whatsapp import WhatsAppNotifier
        WhatsAppNotifier(organization_id=org_id).send(f"🚀 *Manual Promotion*: {w.ticker} has been manually promoted from Watchlist to Signals for entry!")
    except Exception:
        pass
        
    return RedirectResponse("/signals?msg=promoted", 302)


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
async def action_run_screener(request: Request):
    """Queue the screener for the current org only — bypasses trading-day gate."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")
    try:
        from app.tasks.screening import _run_screen_force
        _run_screen_force.delay(organization_id=org_id)
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
async def action_evaluate_regime(request: Request):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    from app.tasks.screening import evaluate_market_regime_task
    evaluate_market_regime_task.delay()
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
async def action_refresh_data(request: Request):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    from app.tasks.screening import refresh_price_data
    refresh_price_data.delay()
    return RedirectResponse("/admin/health?msg=data", 302)


@app.post("/action/full-setup")
async def action_full_setup(request: Request):
    """First-time setup: universe → price data → regime → screen. Runs as a Celery chain."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    from app.tasks.screening import run_full_setup
    run_full_setup.delay()
    return RedirectResponse("/admin/health?msg=setup", 302)


@app.post("/action/force-screen")
async def action_force_screen(request: Request):
    """Run screener for current org now, bypassing the trading-day gate."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")
    try:
        from app.tasks.screening import _run_screen_force
        _run_screen_force.delay(organization_id=org_id)
    except Exception:
        pass
    return RedirectResponse("/signals?msg=screen", 302)


@app.post("/action/force-breakout-check")
async def action_force_breakout_check(request: Request):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    from app.tasks.trading import check_entry_triggers
    check_entry_triggers.delay()
    return RedirectResponse("/admin/tasks?msg=breakout", 302)


@app.post("/action/force-exit-check")
async def action_force_exit_check(request: Request):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    from app.tasks.trading import check_exit_rules_task
    check_exit_rules_task.delay()
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
        "seed_logs":      seed_logs,
        "last_log_id":    last_id,
        "stock_count":    db.query(func.count(Stock.id)).scalar() or 0,
        "price_bar_count": db.query(func.count(PriceBar.id)).scalar() or 0,
        "signal_count":   db.query(func.count(Signal.id)).filter(Signal.signal_date == get_current_date(), Signal.organization_id == org_id).scalar() or 0,
        "watchlist_count": db.query(func.count(Watchlist.id)).filter(Watchlist.status == WatchlistStatus.WATCHING, Watchlist.organization_id == org_id).scalar() or 0,
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
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.models.audit import AuditLog
    from app.models.account import Account
    from app.models.market import Stock, PriceBar
    from app.models.signal import Signal, Watchlist, WatchlistStatus
    from sqlalchemy import func

    ctx = _global(request, db)
    account = db.query(Account).filter(Account.is_active == True, Account.organization_id == org_id).first()
    try:
        logs = db.query(AuditLog).filter(AuditLog.organization_id == org_id).order_by(desc(AuditLog.created_at)).limit(8).all()
    except Exception:
        logs = []

    stock_count     = db.query(func.count(Stock.id)).scalar() or 0
    price_bar_count = db.query(func.count(PriceBar.id)).scalar() or 0
    today_bars      = db.query(func.count(PriceBar.id)).filter(PriceBar.date == get_current_date()).scalar() or 0
    signal_count    = db.query(func.count(Signal.id)).filter(Signal.signal_date == get_current_date(), Signal.organization_id == org_id).scalar() or 0
    watchlist_count = db.query(func.count(Watchlist.id)).filter(Watchlist.status == WatchlistStatus.WATCHING, Watchlist.organization_id == org_id).scalar() or 0
    is_first_run    = stock_count == 0

    ctx.update({
        "capital": float(account.capital_aud) if account else 0,
        "is_paper_account": account.is_paper if account else True,
        "recent_logs": [{
            "action": str(l.action).replace("AuditAction.", ""),
            "ticker": l.ticker or "—",
            "message": (l.message or "")[:60],
            "actor": l.actor,
            "time": _fmt_dt(str(l.created_at), ctx.get("display_tz", "UTC")),
        } for l in logs],
        # Diagnostics
        "stock_count": stock_count,
        "price_bar_count": price_bar_count,
        "today_bars": today_bars,
        "signal_count_today": signal_count,
        "watchlist_count": watchlist_count,
        "is_first_run": is_first_run,
    })
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
    configs = db.query(SystemConfig).filter(SystemConfig.organization_id == org_id).order_by(SystemConfig.group, SystemConfig.key).all()
    by_group = {}
    HIDDEN_GROUPS = {"system"} if request.session.get("user_role") != "superadmin" else set()
    for c in configs:
        if c.group in HIDDEN_GROUPS:
            continue
        grp = c.group or "general"
        if grp not in by_group:
            by_group[grp] = []
        by_group[grp].append({
            "id": c.id, "key": c.key,
            "value": c.value if not c.is_secret else "",
            "label": c.label or c.key,
            "description": c.description or "",
            "is_secret": c.is_secret,
            "value_type": c.value_type.value if hasattr(c.value_type, "value") else str(c.value_type),
        })
    ctx.update({"configs_by_group": by_group, "saved": request.query_params.get("saved", "")})
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

        # Synchronize working capital configuration with active Account capital
        if c.key in ("working_capital_aud", "weekly_injection_aud") and c.organization_id:
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
                    if c.key in ("working_capital_aud", "weekly_injection_aud") and c.organization_id:
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
    action_f = request.query_params.get("action", "ALL")
    ticker_f = request.query_params.get("ticker", "").strip().upper()

    actor_f = request.query_params.get("actor", "").strip()

    q = db.query(AuditLog).filter(AuditLog.organization_id == org_id).order_by(desc(AuditLog.created_at))
    if action_f != "ALL":
        q = q.filter(AuditLog.action == action_f)
    if ticker_f:
        t = ticker_f if ticker_f.endswith(".AX") else ticker_f + ".AX"
        q = q.filter(AuditLog.ticker == t)
    if actor_f:
        q = q.filter(AuditLog.actor.ilike(f"%{actor_f}%"))

    try:
        logs = q.limit(200).all()
    except Exception:
        logs = []
    audit_tz = _get_display_tz(org_id, db)
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
    })
    return templates.TemplateResponse("admin/audit.html", ctx)


# ===========================================================================
# WHATSAPP WEBHOOK + STATUS PAGE
# ===========================================================================

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
        configs = db.query(SystemConfig).filter(SystemConfig.key == "whatsapp_admin_number").all()
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

    if event != "message":
        return JSONResponse({"ok": True})

    if is_from_me or not text:
        return JSONResponse({"ok": True})

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


@app.get("/admin/whatsapp", response_class=HTMLResponse)
async def admin_whatsapp(request: Request, db: Session = Depends(get_db)):
    """WhatsApp integration status — shows session state, QR code, test button."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.notifications.whatsapp import WhatsAppNotifier
    from app.models.audit import AuditLog, AuditAction
    from app.config import settings

    ctx = _global(request, db)

    # WAHA calls are all wrapped — if WAHA container is down, page still loads
    try:
        notifier     = WhatsAppNotifier(organization_id=org_id)
        session_info = notifier.get_session_status()
        status       = session_info.get("status", "UNKNOWN")
        qr_b64       = notifier.get_qr() if status in ("SCAN_QR_CODE", "STARTING") else None
        wa_session   = notifier.session
        admin_jid    = notifier.admin_jid or ""
        admin_number = admin_jid.split("@")[0] if admin_jid else ""
    except Exception as _e:
        logger.warning(f"WhatsApp page: WAHA unreachable — {_e}")
        status = "UNKNOWN"
        qr_b64 = None
        wa_session = "—"
        admin_jid = ""
        admin_number = ""

    try:
        recent_cmds = db.query(AuditLog).filter(
            AuditLog.action == AuditAction.AGENT_COMMAND,
            AuditLog.organization_id == org_id
        ).order_by(desc(AuditLog.created_at)).limit(20).all()
    except Exception:
        recent_cmds = []

    ctx.update({
        "wa_status":       status,
        "wa_session":      wa_session,
        "wa_url":          settings.waha_api_url,
        "admin_jid":       admin_jid,
        "admin_number":    admin_number,
        "hook_url":        settings.waha_hook_url,
        "qr_b64":          qr_b64,
        "recent_commands": [
            {"time":    _fmt_dt(str(l.created_at), ctx.get("display_tz", "UTC")),
             "message": (l.detail or {}).get("message", ""),
             "sender":  (l.detail or {}).get("sender", "")}
            for l in recent_cmds
        ],
        "msg": request.query_params.get("msg", ""),
    })
    return templates.TemplateResponse("admin/whatsapp.html", ctx)


@app.post("/admin/whatsapp/start-session")
async def whatsapp_start_session(request: Request):
    """
    Force-restart the WAHA session.
    Stops any existing session and starts fresh → puts it into SCAN_QR_CODE state.
    """
    if not _auth(request):
        return RedirectResponse("/login", 302)
    org_id = request.session.get("organization_id")

    from app.notifications.whatsapp import WhatsAppNotifier
    new_status = WhatsAppNotifier(organization_id=org_id).restart_session()
    # Redirect with a flash that tells the page what happened
    if new_status == "SCAN_QR_CODE":
        return RedirectResponse("/admin/whatsapp?msg=scan_qr", 302)
    elif new_status == "WORKING":
        return RedirectResponse("/admin/whatsapp?msg=already_working", 302)
    else:
        return RedirectResponse(f"/admin/whatsapp?msg=started", 302)


@app.post("/admin/whatsapp/send-test")
async def whatsapp_send_test(request: Request):
    """Send a test message to the admin number to verify the integration."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.notifications.whatsapp import WhatsAppNotifier
    ok = WhatsAppNotifier(organization_id=org_id).send("✅ VCPilot test message — WhatsApp integration is working!")
    return RedirectResponse(f"/admin/whatsapp?msg={'test_ok' if ok else 'test_fail'}", 302)


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
):
    """
    Admin Data Log — shows per-signal intraday metric snapshots captured every
    5–15 minutes during market hours, with per-rule pass/fail colouring so users
    can see exactly what metrics are being evaluated against Minervini rules.
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
                         "EOD Fallback"   if log.data_source == "eod_fallback" else
                         "yfinance ~20 min delay"
                        )
        source_color  = "pos" if log.data_source == "ibkr" else (
                         "warn" if log.data_source == "eod_fallback" else "warn"
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

    ctx.update({
        "rows":           rows,
        "ticker":         ticker or "",
        "window":         window,
        "only_confirmed": only_confirmed,
        "all_tickers":    all_tickers,
        "total":          len(rows),
        "msg":            request.query_params.get("msg", ""),
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
            group="broker" if "ibkr" in key else ("whatsapp" if "whatsapp" in key else "general")
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

    subject = "Welcome to VCPilot! Set up your Organisation Admin Account"
    html_content = (
        '<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px">'
        '<h2 style="color:#1d4ed8">Welcome to VCPilot!</h2>'
        f'<p>Hi {user.name},</p>'
        f'<p>Your organization <strong>{org.name}</strong> has been created on VCPilot. '
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

    ctx.update({
        "organization": org,
        "users": users,
        "accounts": accounts,
        "logs": logs,
        "msg": request.query_params.get("msg", ""),
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
        rules_by_cat[cat].append({
            "id": r.id, "rule_id": r.rule_id, "label": r.label,
            "description": r.description or "", "minervini_ref": r.minervini_ref or "",
            "enabled": r.enabled_globally, "is_mandatory": r.is_mandatory,
            "threshold": float(r.threshold) if r.threshold is not None else None,
            "threshold_label": r.threshold_label or "",
            "threshold_min": float(r.threshold_min) if r.threshold_min else 0,
            "threshold_max": float(r.threshold_max) if r.threshold_max else 999,
            "tier_overrides": r.tier_overrides or {},
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

    # Welcome email sending flow for User
    from app.utils.email import send_email
    import urllib.parse
    
    host = request.headers.get("host", "localhost:8501")
    scheme = "https" if request.url.scheme == "https" else "http"
    reset_link = f"{scheme}://{host}/reset-password?token={token}"

    subject = "Welcome to VCPilot! Set up your account"
    html_content = (
        '<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px">'
        '<h2 style="color:#1d4ed8">Welcome to VCPilot!</h2>'
        f'<p>Hi {user.name},</p>'
        '<p>An account has been created for you on VCPilot. Click the button below to set up your password and log in:</p>'
        f'<div style="text-align:center;margin:30px 0"><a href="{reset_link}" '
        'style="background:#1d4ed8;color:#fff;padding:12px 24px;text-decoration:none;border-radius:6px">Set Up Password & Log In</a></div>'
        f'<p style="font-size:12px;color:#6b7280">Or copy: {reset_link}</p>'
        '<p style="color:#6b7280;font-size:14px">This link expires in 24 hours.</p></div>'
    )
    
    email_sent = send_email(user.email, subject, html_content)
    encoded_email = urllib.parse.quote(user.email)
    if email_sent:
        return RedirectResponse(f"/superadmin/users?saved=welcome_email&email={encoded_email}", 302)
    else:
        return RedirectResponse(f"/superadmin/users?saved=welcome_manual&token={token}&email={encoded_email}", 302)


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

    subject = "Reset Your VCPilot Password"
    html_content = (
        '<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px">'
        '<h2 style="color:#1d4ed8">VCPilot Password Reset</h2>'
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


# ===========================================================================
# SUPER ADMIN DATA VIEW — universe + price data + custom stocks
# ===========================================================================

@app.get("/superadmin/data", response_class=HTMLResponse)
async def superadmin_data(
    request: Request,
    db: Session = Depends(get_db),
    tab: str = Query("universe"),       # universe | custom
    search: str = Query(""),
    sector: str = Query(""),
    sort_by: str = Query("ticker"),     # ticker | rs_rating | last_price | market_cap | vol_ratio
    sort_dir: str = Query("asc"),
    page: int = Query(1),
):
    """
    Super Admin Data page — two-tab view:
      Tab 1 (universe): All ASX200/active stocks with their latest PriceBar metrics.
      Tab 2 (custom):   Custom/non-universe stocks added per-org via watchlist.
    Interactive: sortable columns, search, sector filter, pagination.
    """
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    from app.models.market import Stock, PriceBar
    from app.models.signal import Watchlist
    from app.models.account import Organization
    from sqlalchemy import func, and_

    ctx = _global(request, db)
    per_page = 50

    if tab == "universe":
        # ── Tab 1: Centralized universe stocks with latest price bar ──────────
        q = db.query(Stock).filter(Stock.is_active == True, Stock.blacklisted == False)
        if search:
            q = q.filter(
                (Stock.ticker.ilike(f"%{search.upper()}%")) |
                (Stock.name.ilike(f"%{search}%"))
            )
        if sector:
            q = q.filter(Stock.sector == sector)

        total_stocks = q.count()

        # Sort
        sort_map = {
            "ticker":     Stock.ticker,
            "name":       Stock.name,
            "sector":     Stock.sector,
            "market_cap": Stock.market_cap,
            "last_price": Stock.last_price,
        }
        sort_col = sort_map.get(sort_by, Stock.ticker)
        if sort_dir == "desc":
            q = q.order_by(sort_col.desc().nullslast())
        else:
            q = q.order_by(sort_col.asc().nullslast())

        offset = (page - 1) * per_page
        stocks = q.offset(offset).limit(per_page).all()

        # Fetch latest price bars for these stocks
        tickers = [s.ticker for s in stocks]
        if tickers:
            # Subquery: max date per ticker
            latest_dates = db.query(
                PriceBar.ticker,
                func.max(PriceBar.date).label("max_date")
            ).filter(PriceBar.ticker.in_(tickers)).group_by(PriceBar.ticker).subquery()

            bars = db.query(PriceBar).join(
                latest_dates,
                and_(
                    PriceBar.ticker == latest_dates.c.ticker,
                    PriceBar.date == latest_dates.c.max_date,
                )
            ).all()
            bar_map = {b.ticker: b for b in bars}
        else:
            bar_map = {}

        # Serialize
        rows = []
        for s in stocks:
            bar = bar_map.get(s.ticker)
            rows.append({
                "ticker":         s.ticker,
                "asx_code":       s.asx_code,
                "name":           s.name or "",
                "sector":         s.sector or "",
                "industry":       s.industry or "",
                "in_asx200":      s.in_asx200,
                "market_cap":     int(s.market_cap) if s.market_cap else None,
                "last_price":     float(s.last_price) if s.last_price else None,
                "last_updated":   str(s.last_updated)[:10] if s.last_updated else "",
                # Price bar fields
                "bar_date":       str(bar.date) if bar else "",
                "close":          float(bar.close)         if bar and bar.close         else None,
                "volume":         int(bar.volume)          if bar and bar.volume        else None,
                "ma_50":          float(bar.ma_50)         if bar and bar.ma_50         else None,
                "ma_150":         float(bar.ma_150)        if bar and bar.ma_150        else None,
                "ma_200":         float(bar.ma_200)        if bar and bar.ma_200        else None,
                "vol_ratio":      float(bar.vol_ratio)     if bar and bar.vol_ratio     else None,
                "rs_rating":      float(bar.rs_rating)     if bar and bar.rs_rating     else None,
                "pct_from_52w_high": float(bar.pct_from_52w_high) if bar and bar.pct_from_52w_high else None,
                "atr_14":         float(bar.atr_14)        if bar and bar.atr_14        else None,
                "has_bar":        bar is not None,
            })

        # Sort by price bar fields (not available as SQL columns — sort in Python)
        if sort_by in ("rs_rating", "vol_ratio"):
            reverse = sort_dir == "desc"
            rows.sort(key=lambda r: (r[sort_by] is None, r.get(sort_by) or 0), reverse=reverse)

        # Distinct sectors for filter
        sectors = sorted(set(
            s.sector for s in db.query(Stock.sector).filter(
                Stock.is_active == True, Stock.sector != None
            ).distinct().all()
            if s.sector
        ))

        # Summary stats
        total_with_bars = sum(1 for r in rows if r["has_bar"])
        avg_rs = round(sum(r["rs_rating"] for r in rows if r["rs_rating"]) /
                       max(1, sum(1 for r in rows if r["rs_rating"])), 1)

        ctx.update({
            "tab":            "universe",
            "rows":           rows,
            "search":         search,
            "sector":         sector,
            "sectors":        sectors,
            "sort_by":        sort_by,
            "sort_dir":       sort_dir,
            "page":           page,
            "per_page":       per_page,
            "total":          total_stocks,
            "total_pages":    max(1, (total_stocks + per_page - 1) // per_page),
            "total_with_bars": total_with_bars,
            "avg_rs":         avg_rs,
            "custom_rows":    [],
        })

    else:
        # ── Tab 2: Custom stocks per org (in watchlist but not in ASX200 universe) ──
        # Find tickers in watchlist that are NOT in the stocks table as ASX200
        from sqlalchemy import text as _text

        custom_rows_raw = db.execute(_text("""
            SELECT
                w.ticker,
                w.organization_id,
                o.name AS org_name,
                COUNT(*) OVER (PARTITION BY w.ticker) AS org_count,
                s.name AS stock_name,
                s.sector,
                s.in_asx200,
                s.is_active,
                pb.close,
                pb.date AS bar_date,
                pb.rs_rating,
                pb.vol_ratio,
                pb.ma_50,
                pb.ma_200,
                pb.pct_from_52w_high,
                w.added_at
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
            ORDER BY org_count DESC, w.ticker, o.name
        """)).fetchall()

        custom_rows = [
            {
                "ticker":     r.ticker,
                "org_id":     r.organization_id,
                "org_name":   r.org_name,
                "org_count":  r.org_count,
                "name":       r.stock_name or "",
                "sector":     r.sector or "",
                "in_asx200":  r.in_asx200 or False,
                "is_active":  r.is_active if r.is_active is not None else True,
                "close":      float(r.close) if r.close else None,
                "bar_date":   str(r.bar_date) if r.bar_date else "",
                "rs_rating":  float(r.rs_rating) if r.rs_rating else None,
                "vol_ratio":  float(r.vol_ratio) if r.vol_ratio else None,
                "ma_50":      float(r.ma_50) if r.ma_50 else None,
                "ma_200":     float(r.ma_200) if r.ma_200 else None,
                "pct_from_52w_high": float(r.pct_from_52w_high) if r.pct_from_52w_high else None,
                "added_at":   str(r.added_at)[:10] if r.added_at else "",
            }
            for r in custom_rows_raw
        ]

        # Apply search filter
        if search:
            su = search.upper()
            custom_rows = [r for r in custom_rows if su in r["ticker"] or search.lower() in r["name"].lower()]

        ctx.update({
            "tab":        "custom",
            "rows":       [],
            "custom_rows": custom_rows,
            "search":     search,
            "sector":     "",
            "sectors":    [],
            "sort_by":    sort_by,
            "sort_dir":   sort_dir,
            "page":       1,
            "per_page":   per_page,
            "total":      len(custom_rows),
            "total_pages": 1,
            "total_with_bars": sum(1 for r in custom_rows if r["close"]),
            "avg_rs": 0,
        })

    # Global data stats for summary cards
    total_stocks_db    = db.query(Stock).filter(Stock.is_active == True).count()
    total_bars_db      = db.query(PriceBar).count()
    latest_bar_date_r  = db.query(func.max(PriceBar.date)).scalar()
    ctx.update({
        "total_stocks_db":   total_stocks_db,
        "total_bars_db":     total_bars_db,
        "latest_bar_date":   str(latest_bar_date_r) if latest_bar_date_r else "—",
        "msg":               request.query_params.get("msg", ""),
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
