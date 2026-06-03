"""
VCPilot Dashboard — FastAPI + Flowbite/Tailwind
Mobile-first. Split: /trading (client) and /admin (operator).
"""
import os, sys
sys.path.insert(0, "/app")

from datetime import date, datetime, timedelta
from fastapi import FastAPI, Request, Form, Depends, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import desc
from loguru import logger

app = FastAPI(title="VCPilot", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=os.getenv("APP_SECRET_KEY", "changeme-secret"))

templates = Jinja2Templates(directory="/app/dashboard/templates")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "changeme")


@app.on_event("startup")
async def _startup():
    """Auto-start WAHA session + configure webhook on every dashboard boot."""
    import asyncio, httpx
    from loguru import logger
    from app.config import settings

    async def _init_waha():
        await asyncio.sleep(3)   # give WAHA a moment to be reachable
        api   = settings.waha_api_url.rstrip("/")
        key   = settings.waha_api_key
        sess  = settings.waha_session
        hook  = settings.waha_hook_url
        hdrs  = {"X-Api-Key": key, "Content-Type": "application/json"}

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Start session if not already running
                r = await client.post(f"{api}/api/sessions/start",
                                      json={"name": sess}, headers=hdrs)
                status = r.json().get("status", "?")
                logger.info(f"WAHA session '{sess}' → {status}")

                # Register webhook if URL is configured
                if hook:
                    await client.put(f"{api}/api/sessions/{sess}",
                        json={"webhooks": [{"url": hook, "events": ["message", "session.status"]}]},
                        headers=hdrs)
                    logger.info(f"WAHA webhook registered: {hook}")
        except Exception as e:
            logger.warning(f"WAHA startup init failed (non-fatal): {e}")

    asyncio.create_task(_init_waha())


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


# ---------------------------------------------------------------------------
# Global context (navbar + every template)
# ---------------------------------------------------------------------------
def _global(request: Request, db: Session) -> dict:
    from app.models.config import SystemConfig
    from app.models.trade import Position, TradeStatus
    from app.models.signal import Signal

    org_id = request.session.get("organization_id")

    def cfg(key, default=""):
        if key in ("last_market_regime", "last_regime_check", "last_heartbeat"):
            c = db.query(SystemConfig).filter(SystemConfig.key == key, SystemConfig.organization_id == None).first()
        else:
            c = db.query(SystemConfig).filter(SystemConfig.key == key, SystemConfig.organization_id == org_id).first()
        return c.value if c else default

    raw_hb       = cfg("last_heartbeat", "")
    hb_display   = raw_hb[:16] if raw_hb else ""
    wstatus      = _worker_status(raw_hb)
    trading_paused = cfg("trading_paused", "false").lower() == "true"
    whatsapp_enabled = cfg("whatsapp_enabled", "true").lower() == "true"
    regime_raw   = cfg("last_market_regime", "")

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
            Signal.signal_date == date.today(),
            Signal.organization_id == org_id
        ).count()

    all_orgs = []
    user_role = request.session.get("user_role")
    if user_role == "superadmin":
        from app.models.account import Organization
        all_orgs = db.query(Organization).filter(Organization.is_active == True).order_by(Organization.name).all()

    return {
        "request": request,
        "path": str(request.url.path),
        # Regime: show empty string when never evaluated (templates handle display)
        "regime": regime_raw,
        "regime_set": bool(regime_raw and regime_raw not in ("UNKNOWN", "")),
        "trading_paused": trading_paused,
        "whatsapp_enabled": whatsapp_enabled,
        "is_paper": is_paper,
        "heartbeat": hb_display or "Never",
        "worker_status": wstatus,                          # online | starting | offline
        # Trading is only truly active if: not paused AND worker online
        "trading_active": (not trading_paused) and (wstatus == "online"),
        "open_count": open_count,
        "signal_count": signal_count,
        "user_role": user_role,
        "org_name": request.session.get("organization_name", ""),
        "all_orgs": all_orgs,
        "current_org_id": org_id,
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

    ctx = _global(request, db)

    account = db.query(Account).filter(Account.is_active == True, Account.organization_id == org_id).first()
    capital = float(account.capital_aud) if account else 1000.0

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
            "ticker": p.ticker, "qty": p.qty,
            "entry": entry, "current": curr, "stop": stop,
            "pnl_pct": round((curr - entry) / entry * 100, 2),
            "pnl_aud": round(pnl, 2),
            "days": (date.today() - p.entry_date).days,
            "is_paper": p.is_paper,
        })

    # Signals
    signals = db.query(Signal).filter(Signal.signal_date == date.today(), Signal.organization_id == org_id).all()
    sig_data = [{
        "id": s.id, "ticker": s.ticker,
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
    today_trades = db.query(Trade).filter(Trade.exit_date == date.today(), Trade.organization_id == org_id).all()
    all_trades   = db.query(Trade).filter(Trade.organization_id == org_id).all()

    ctx.update({
        "capital": capital,
        "positions": pos_data,
        "signals": sig_data,
        "portfolio_heat": round(total_risk / capital * 100, 1) if capital else 0,
        "today_pnl": round(sum(float(t.net_pnl_aud or 0) for t in today_trades), 2),
        "total_pnl":  round(sum(float(t.net_pnl_aud or 0) for t in all_trades), 2),
        "trade_count": len(all_trades),
    })
    return templates.TemplateResponse("trading/home.html", ctx)


@app.get("/positions", response_class=HTMLResponse)
async def positions(request: Request, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.models.trade import Position, Trade, TradeStatus

    ctx = _global(request, db)
    positions = db.query(Position).filter(Position.status == TradeStatus.OPEN, Position.organization_id == org_id).all()
    pos_data = []
    for p in positions:
        curr  = float(p.current_price or p.entry_price)
        entry = float(p.entry_price)
        pos_data.append({
            "id": p.id, "ticker": p.ticker, "qty": p.qty,
            "entry": entry, "current": curr,
            "stop": float(p.current_stop),
            "target_1": float(p.target_1 or 0),
            "pnl_pct": round((curr - entry) / entry * 100, 2),
            "pnl_aud": round((curr - entry) * p.qty, 2),
            "days": (date.today() - p.entry_date).days,
            "entry_date": str(p.entry_date),
            "is_paper": p.is_paper,
        })

    trades = db.query(Trade).filter(Trade.organization_id == org_id).order_by(desc(Trade.exit_date)).limit(50).all()
    trade_data = [{
        "ticker": t.ticker,
        "entry_date": str(t.entry_date), "exit_date": str(t.exit_date),
        "days": t.hold_days,
        "entry": float(t.entry_price), "exit": float(t.exit_price),
        "pnl_pct": round(float(t.pnl_pct or 0) * 100, 2),
        "pnl_aud": round(float(t.net_pnl_aud or 0), 2),
        "reason": str(t.exit_reason).replace("ExitReason.", "").replace("_", " "),
        "cgt": t.cgt_eligible_discount,
        "is_paper": t.is_paper,
    } for t in trades]

    wins = [t for t in trades if float(t.net_pnl_aud or 0) > 0]
    ctx.update({
        "positions": pos_data, "trades": trade_data,
        "win_rate": round(len(wins) / len(trades) * 100) if trades else 0,
        "total_pnl": round(sum(float(t.net_pnl_aud or 0) for t in trades), 2),
        "trade_count": len(trades),
    })
    return templates.TemplateResponse("trading/positions.html", ctx)


def _enrich_rule_results(ticker: str, rule_results_dict: dict, db_session, target_date=None) -> list[dict]:
    """
    Enrich rule results with actual values from the price bar on the given date (or latest).
    """
    from app.models.market import PriceBar
    
    # Query price bar for the given date, or latest
    q = db_session.query(PriceBar).filter(PriceBar.ticker == ticker)
    if target_date:
        q = q.filter(PriceBar.date == target_date)
    else:
        q = q.order_by(desc(PriceBar.date))
    bar = q.first()
    
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

    ctx = _global(request, db)
    sigs = db.query(Signal).filter(Signal.signal_date == date.today(), Signal.organization_id == org_id).all()
    sig_data = []
    for s in sigs:
        # Fetch company name
        stock_db = db.query(Stock).filter(Stock.ticker == s.ticker).first()
        company_name = stock_db.name if stock_db else ""

        rr = s.rule_results or {}
        passed = sum(1 for v in rr.values() if v.get("passed"))
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
            "rule_results": _enrich_rule_results(s.ticker, rr, db, target_date=s.signal_date),
        })
    ctx.update({"signals": sig_data, "signal_date": str(date.today())})
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
                        actor="dashboard", message="Signal skipped via dashboard",
                        organization_id=org_id))
        db.commit()
    return RedirectResponse("/signals", 302)


@app.get("/watchlist", response_class=HTMLResponse)
async def watchlist(request: Request, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.models.signal import Watchlist, WatchlistStatus
    from app.models.market import PriceBar, Stock
    ctx = _global(request, db)
    items = db.query(Watchlist).filter(
        Watchlist.status == WatchlistStatus.WATCHING,
        Watchlist.organization_id == org_id
    ).order_by(desc(Watchlist.created_at)).all()
    
    watchlist_data = []
    for w in items:
        # Fetch company name
        stock_db = db.query(Stock).filter(Stock.ticker == w.ticker).first()
        company_name = stock_db.name if stock_db else ""

        # Fetch the latest price bar for the ticker to show stats
        bar = db.query(PriceBar).filter(PriceBar.ticker == w.ticker).order_by(desc(PriceBar.date)).first()
        bar_data = None
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
            
        rr = w.rule_results or {}
        passed = sum(1 for v in rr.values() if v.get("passed"))
        
        watchlist_data.append({
            "id": w.id,
            "ticker": w.ticker,
            "company_name": company_name,
            "added": str(w.added_date),
            "by": w.added_by,
            "notes": w.notes or "",
            "stats": bar_data,
            "rules_passed": passed,
            "rules_total": len(rr),
            "rule_results": _enrich_rule_results(w.ticker, rr, db),
        })
        
    ctx["watchlist"] = watchlist_data
    return templates.TemplateResponse("trading/watchlist.html", ctx)


@app.post("/watchlist/add")
async def watchlist_add(request: Request, ticker: str = Form(...), notes: str = Form(""), db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    t = ticker.strip().upper()
    if not t.endswith(".AX"):
        t += ".AX"
    from app.tasks.screening import screen_single_ticker
    screen_single_ticker.delay(t, notes, organization_id=org_id)
    return RedirectResponse("/watchlist?msg=added", 302)


@app.post("/watchlist/{item_id}/promote")
async def watchlist_promote(request: Request, item_id: int, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.models.signal import Watchlist, WatchlistStatus, Signal, SignalStatus
    from app.models.market import PriceBar
    from app.models.audit import AuditLog, AuditAction
    
    w = db.query(Watchlist).filter(Watchlist.id == item_id, Watchlist.organization_id == org_id).first()
    if not w:
        return RedirectResponse("/watchlist?msg=not_found", 302)
        
    bar = db.query(PriceBar).filter(PriceBar.ticker == w.ticker).order_by(desc(PriceBar.date)).first()
    close_price = float(bar.close) if bar and bar.close else 1.0
    
    pivot = close_price
    stop = close_price * 0.92
    
    today = date.today()
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
            notes=f"Manually promoted from Watchlist. Original notes: {w.notes or ''}",
            organization_id=org_id,
        )
        db.add(sig)
        
    w.status = WatchlistStatus.SIGNALLED
    db.add(AuditLog(
        action=AuditAction.MANUAL_OVERRIDE,
        ticker=w.ticker,
        actor="dashboard",
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
    db.add(AuditLog(action=AuditAction.TRADING_PAUSED, actor="dashboard", organization_id=org_id))
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
    db.add(AuditLog(action=AuditAction.TRADING_RESUMED, actor="dashboard", organization_id=org_id))
    db.commit()
    return RedirectResponse("/", 302)


@app.post("/action/run-screener")
async def action_run_screener(request: Request):
    """Queue the screener — bypasses trading-day gate so it works any day/time."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    try:
        from app.tasks.screening import _run_screen_force
        _run_screen_force.delay()
    except Exception:
        pass  # Worker may not be available; task will queue when it comes online
    return RedirectResponse("/signals?msg=screen", 302)


@app.post("/action/send-report")
async def action_send_report(request: Request):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    from app.tasks.reporting import send_daily_report
    send_daily_report.delay()
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
    """Run screener now, bypassing the trading-day gate."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    try:
        from app.tasks.screening import _run_screen_force
        _run_screen_force.delay()
    except Exception:
        pass
    return RedirectResponse("/signals?msg=screen", 302)


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
    logs = db.query(AuditLog).filter(AuditLog.organization_id == org_id).order_by(desc(AuditLog.id)).limit(40).all()
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
    }

    seed_logs = [{
        "time":    str(l.created_at)[11:19],
        "action":  str(l.action).replace("AuditAction.", ""),
        "ticker":  l.ticker or "—",
        "message": (l.message or "")[:80],
        "color":   ACTION_COLOURS.get(str(l.action).replace("AuditAction.", ""), "var(--text-muted)"),
    } for l in logs]

    ctx.update({
        "seed_logs":      seed_logs,
        "last_log_id":    last_id,
        "stock_count":    db.query(func.count(Stock.id)).scalar() or 0,
        "price_bar_count": db.query(func.count(PriceBar.id)).scalar() or 0,
        "signal_count":   db.query(func.count(Signal.id)).filter(Signal.signal_date == date.today(), Signal.organization_id == org_id).scalar() or 0,
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
    if request.session.get("user_role") == "superadmin":
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": "unauthorized"}, status_code=403)
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
    }

    new_logs = db.query(AuditLog).filter(AuditLog.id > after, AuditLog.organization_id == org_id).order_by(desc(AuditLog.id)).limit(50).all()
    return JSONResponse({
        "logs": [{
            "id":      l.id,
            "time":    str(l.created_at)[11:19],
            "action":  str(l.action).replace("AuditAction.", ""),
            "ticker":  l.ticker or "—",
            "message": (l.message or "")[:80],
            "color":   ACTION_COLOURS.get(str(l.action).replace("AuditAction.", ""), "var(--text-muted)"),
        } for l in new_logs],
        "counts": {
            "stocks":    db.query(func.count(Stock.id)).scalar() or 0,
            "bars":      db.query(func.count(PriceBar.id)).scalar() or 0,
            "signals":   db.query(func.count(Signal.id)).filter(Signal.signal_date == date.today(), Signal.organization_id == org_id).scalar() or 0,
            "watchlist": db.query(func.count(Watchlist.id)).filter(Watchlist.status == WatchlistStatus.WATCHING, Watchlist.organization_id == org_id).scalar() or 0,
        },
    })



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
    logs = db.query(AuditLog).filter(AuditLog.organization_id == org_id).order_by(desc(AuditLog.created_at)).limit(8).all()

    stock_count     = db.query(func.count(Stock.id)).scalar() or 0
    price_bar_count = db.query(func.count(PriceBar.id)).scalar() or 0
    today_bars      = db.query(func.count(PriceBar.id)).filter(PriceBar.date == date.today()).scalar() or 0
    signal_count    = db.query(func.count(Signal.id)).filter(Signal.signal_date == date.today(), Signal.organization_id == org_id).scalar() or 0
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
            "time": str(l.created_at)[11:19],
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
    for c in configs:
        if c.group == "system":
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
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.models.config import SystemConfig
    from app.models.audit import AuditLog, AuditAction
    c = db.query(SystemConfig).filter(SystemConfig.id == config_id, SystemConfig.organization_id == org_id).first()
    if c and value:
        old = c.value
        c.value = value
        c.updated_by = "dashboard"
        db.add(AuditLog(action=AuditAction.CONFIG_CHANGED, entity_id=c.key,
                        before_value=old, after_value=value, actor="dashboard",
                        organization_id=org_id))
        db.commit()
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

    q = db.query(AuditLog).filter(AuditLog.organization_id == org_id).order_by(desc(AuditLog.created_at))
    if action_f != "ALL":
        q = q.filter(AuditLog.action == action_f)
    if ticker_f:
        t = ticker_f if ticker_f.endswith(".AX") else ticker_f + ".AX"
        q = q.filter(AuditLog.ticker == t)

    logs = q.limit(100).all()
    ctx.update({
        "logs": [{"time": str(l.created_at)[5:19], "action": str(l.action).replace("AuditAction.", ""),
                  "actor": l.actor, "ticker": l.ticker or "—",
                  "message": (l.message or "")[:70],
                  "before": (l.before_value or "")[:20], "after": (l.after_value or "")[:20]}
                 for l in logs],
        "actions": ["ALL"] + sorted(set(str(a.value) for a in AuditAction)),
        "filter_action": action_f,
        "filter_ticker": ticker_f,
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
    notifier     = WhatsAppNotifier(organization_id=org_id)
    session_info = notifier.get_session_status()
    status       = session_info.get("status", "UNKNOWN")
    qr_b64       = notifier.get_qr() if status == "SCAN_QR_CODE" else None

    recent_cmds = db.query(AuditLog).filter(
        AuditLog.action == AuditAction.AGENT_COMMAND,
        AuditLog.organization_id == org_id
    ).order_by(desc(AuditLog.created_at)).limit(20).all()

    ctx.update({
        "wa_status":       status,
        "wa_session":      notifier.session,
        "wa_url":          settings.waha_api_url,
        "admin_jid":       notifier.admin_jid,
        "admin_number":    notifier.admin_jid.split("@")[0] if notifier.admin_jid else "",
        "hook_url":        settings.waha_hook_url,
        "qr_b64":          qr_b64,
        "recent_commands": [
            {"time":    str(l.created_at)[11:19],
             "message": (l.detail or {}).get("message", ""),
             "sender":  (l.detail or {}).get("sender", "")}
            for l in recent_cmds
        ],
        "msg": request.query_params.get("msg", ""),
    })
    return templates.TemplateResponse("admin/whatsapp.html", ctx)


@app.post("/admin/whatsapp/start-session")
async def whatsapp_start_session(request: Request):
    """Manually start / restart the WAHA session."""
    if not _auth(request):
        return RedirectResponse("/login", 302)
    # Super admins allowed in standard views under active organization context
    org_id = request.session.get("organization_id")

    from app.notifications.whatsapp import WhatsAppNotifier
    WhatsAppNotifier(organization_id=org_id).ensure_session()
    return RedirectResponse("/admin/whatsapp?msg=started", 302)


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
        capital_aud=1000.0,
        is_active=True,
        is_paper=True
    )
    db.add(account)

    # 3. Create Org Admin User
    import secrets
    dummy_pass = secrets.token_hex(16)
    hashed_pwd = hash_password(dummy_pass)
    user = User(
        email=admin_email.strip().lower(),
        password_hash=hashed_pwd,
        name=admin_name.strip(),
        organization_id=org.id,
        is_active=True
    )
    db.add(user)
    db.flush()

    # Assign Organisation Admin Role
    admin_role = db.query(Role).filter(Role.name == "Organisation Admin").first()
    if admin_role:
        user.roles.append(admin_role)

    # 4. Seed Organization System Configurations
    configs_to_seed = [
        ("trading_paused", "false", ConfigValueType.BOOLEAN, "Trading Paused", "Toggles automated trade placement"),
        ("whatsapp_enabled", "true", ConfigValueType.BOOLEAN, "WhatsApp Alerts", "Enables real-time notifications"),
        ("whatsapp_admin_number", "", ConfigValueType.STRING, "WhatsApp Admin Number", "Number to send alerts and receive commands JID format"),
        ("whatsapp_api_key", settings.waha_api_key, ConfigValueType.STRING, "WhatsApp API Key", "API key for the WhatsApp (WAHA) service", True),
        ("whatsapp_session_name", "default", ConfigValueType.STRING, "WhatsApp Session Name", "The session name registered in WAHA (e.g. 'default' for WAHA Core, or org-specific for WAHA Plus)"),
        ("ibkr_account", "", ConfigValueType.STRING, "IBKR Account ID", "Interactive Brokers account number"),
        ("ibkr_username", "", ConfigValueType.STRING, "IBKR Username", "Interactive Brokers login username"),
        ("ibkr_password", "", ConfigValueType.STRING, "IBKR Password", "Interactive Brokers login password", True),
        ("ibkr_paper_mode", "true", ConfigValueType.BOOLEAN, "IBKR Paper Mode", "Use paper trading environment"),
        ("fmp_api_key", "", ConfigValueType.STRING, "FMP API Key", "Financial Modeling Prep API key", True),
        ("weekly_injection_aud", "1000.0", ConfigValueType.FLOAT, "Weekly Injection (AUD)", "Capital added weekly for sizing"),
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
    templates = db.query(RuleConfig).filter(RuleConfig.organization_id == None).all()
    for t in templates:
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
    return RedirectResponse("/superadmin/organizations?saved=1", 302)


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
    logs = db.query(AuditLog).filter(AuditLog.organization_id == org_id).order_by(desc(AuditLog.created_at)).limit(50).all()

    ctx.update({
        "organization": org,
        "users": users,
        "accounts": accounts,
        "logs": logs
    })
    return templates.TemplateResponse("superadmin/org_detail.html", ctx)


@app.get("/superadmin/rules", response_class=HTMLResponse)
async def superadmin_rules(request: Request, db: Session = Depends(get_db)):
    if not _auth(request):
        return RedirectResponse("/login", 302)
    if request.session.get("user_role") != "superadmin":
        return RedirectResponse("/", 302)

    from app.models.config import RuleConfig

    ctx = _global(request, db)
    # Only load global template rules
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
        current_enabled = tier_override.get("enabled", True)
        tier_override["enabled"] = not current_enabled
        overrides[tier] = tier_override
        r.tier_overrides = overrides
        flag_modified(r, "tier_overrides")
        r.updated_by = "superadmin"
        db.commit()

    return RedirectResponse("/superadmin/rules?saved=1", 302)


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
        "users": users,
        "organizations": organizations,
        "roles": roles,
        "search": search,
        "selected_org_id": selected_org_id,
        "saved": request.query_params.get("saved", ""),
        "error": request.query_params.get("error", "")
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

    # Check unique email
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
            "search": "", "selected_org_id": ""
        })
        return templates.TemplateResponse("superadmin/users.html", ctx, status_code=400)

    # Generate a random secure dummy password initially
    dummy_pass = secrets.token_hex(16)
    hashed_pwd = hash_password(dummy_pass)

    user = User(
        email=email_clean,
        password_hash=hashed_pwd,
        name=name.strip(),
        organization_id=organization_id,
        is_active=True
    )
    db.add(user)
    db.flush()

    role = db.query(Role).filter(Role.id == role_id).first()
    if role:
        user.roles.append(role)

    db.commit()
    return RedirectResponse("/superadmin/users?saved=1", 302)


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

    # Generate token
    token = secrets.token_urlsafe(32)
    user.reset_token = token
    user.reset_token_expires = datetime.utcnow() + timedelta(hours=1)
    db.commit()

    # Generate link
    host = request.headers.get("host", "localhost:8501")
    scheme = "https" if request.url.scheme == "https" else "http"
    reset_link = f"{scheme}://{host}/reset-password?token={token}"

    subject = "Reset Your VCPilot Password"
    html_content = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #e5e7eb; border-radius: 8px;">
        <h2 style="color: #1d4ed8; margin-bottom: 20px;">VCPilot Password Reset</h2>
        <p>A password reset has been initiated for your account.</p>
        <p>Please click the button below to choose a new password:</p>
        <div style="text-align: center; margin: 30px 0;">
            <a href="{reset_link}" style="background-color: #1d4ed8; color: white; padding: 12px 24px; text-decoration: none; font-weight: bold; border-radius: 6px; display: inline-block;">Reset Password</a>
        </div>
        <p>Or copy and paste this link into your browser:</p>
        <p style="font-size: 12px; word-break: break-all; color: #6b7280;">{reset_link}</p>
        <p style="color: #6b7280; font-size: 14px;">This link will expire in 1 hour.</p>
    </div>
    """

    email_sent = send_email(user.email, subject, html_content)
    if email_sent:
        return RedirectResponse("/superadmin/users?saved=reset_email", 302)
    else:
        import urllib.parse
        return RedirectResponse(f"/superadmin/users?saved=reset_manual&token={token}&email={urllib.parse.quote(user.email)}", 302)


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
    from app.models.auth import User, hash_password

    user = db.query(User).filter(User.reset_token == token, User.reset_token_expires > datetime.utcnow()).first()
    if not user:
        return templates.TemplateResponse("reset_password.html", {"request": request, "token": token, "error": "Invalid or expired reset token.", "success": False})

    user.password_hash = hash_password(password)
    user.reset_token = None
    user.reset_token_expires = None
    db.commit()

    return templates.TemplateResponse("reset_password.html", {"request": request, "token": token, "error": None, "success": True})
