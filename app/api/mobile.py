"""
AstraTrade Mobile API — JWT-authenticated JSON endpoints for the React Native app.
All routes are prefixed /api/mobile (registered in dashboard/main.py).

Auth flow:
  POST /api/mobile/auth/login  → { access_token, token_type, org_id, email }
  All other routes:            Authorization: Bearer <access_token>
"""
import os
from datetime import datetime, timedelta, date
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.orm import Session
from loguru import logger

from app.database import SessionLocal
from app.models.auth import User, verify_password
from app.models.trade import Position, Trade, TradeStatus, ExitReason
from app.models.signal import Signal, SignalStatus, Watchlist, WatchlistStatus
from app.models.config import SystemConfig
from app.models.account import Organization

router = APIRouter(prefix="/api/mobile", tags=["mobile"])

JWT_SECRET = os.getenv("APP_SECRET_KEY", "changeme-secret")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = 168  # 7 days

_bearer = HTTPBearer()


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: str
    password: str


class ClosePositionRequest(BaseModel):
    exit_reason: str
    exit_price: Optional[float] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _create_token(user_id: int, org_id: int, email: str) -> str:
    payload = {
        "sub": str(user_id),
        "org": org_id,
        "email": email,
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def _current_user(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
    db: Session = Depends(get_db),
) -> tuple[User, int]:
    """Dependency: validates JWT and returns (user, org_id)."""
    payload = _decode_token(creds.credentials)
    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user, int(payload["org"])


def _cfg(db: Session, key: str, org_id: int, default: str = "") -> str:
    c = db.query(SystemConfig).filter(
        SystemConfig.key == key,
        SystemConfig.organization_id == org_id,
    ).first()
    return (c.value or default) if c else default


def _float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _ticker_display(ticker: str) -> str:
    """Strip exchange suffix for display: BHP.AX → BHP, BTC-USD → BTC."""
    if ticker.endswith(".AX"):
        return ticker[:-3]
    if ticker.endswith("-USD"):
        return ticker[:-4]
    return ticker


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@router.post("/auth/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    """Authenticate with email + password, receive JWT."""
    user = db.query(User).filter(User.email == req.email.strip().lower()).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")

    token = _create_token(user.id, user.organization_id, user.email)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user_id": user.id,
        "org_id": user.organization_id,
        "email": user.email,
        "name": user.name or user.email.split("@")[0],
    }


@router.get("/auth/me")
def me(auth=Depends(_current_user), db: Session = Depends(get_db)):
    user, org_id = auth
    org = db.query(Organization).filter(Organization.id == org_id).first()
    return {
        "user_id": user.id,
        "email": user.email,
        "name": user.name or user.email.split("@")[0],
        "org_id": org_id,
        "org_name": org.name if org else "",
    }


# ---------------------------------------------------------------------------
# Dashboard summary
# ---------------------------------------------------------------------------

@router.get("/dashboard")
def dashboard(auth=Depends(_current_user), db: Session = Depends(get_db)):
    """Home screen stats: P&L, positions, signals, regime, worker status."""
    user, org_id = auth
    from app.models.account import Account
    from app.models.trade import Order

    # Open positions
    open_positions = db.query(Position).filter(
        Position.organization_id == org_id,
        Position.status == TradeStatus.OPEN,
    ).all()

    total_unrealised = sum(_float(p.unrealised_pnl) or 0 for p in open_positions)
    total_unrealised_pct = (
        sum(_float(p.unrealised_pct) or 0 for p in open_positions) / len(open_positions)
        if open_positions else 0
    )

    # Closed trades today
    today = date.today()
    todays_trades = db.query(Trade).filter(
        Trade.organization_id == org_id,
        Trade.exit_date == today,
    ).all()
    todays_pnl = sum(_float(t.net_pnl_aud) or 0 for t in todays_trades)

    # Signals today (pending)
    pending_signals = db.query(Signal).filter(
        Signal.organization_id == org_id,
        Signal.status == SignalStatus.PENDING,
        Signal.signal_date == today,
    ).count()

    # Market regime
    regime_asx = _cfg(db, "last_market_regime_ASX", org_id, "Unknown")
    regime_crypto = _cfg(db, "last_market_regime_CRYPTO_INDEPENDENTRESERVE", org_id, "Unknown")

    # Worker status
    hb_str = _cfg(db, "last_heartbeat", org_id, "")
    if not hb_str:
        c = db.query(SystemConfig).filter(
            SystemConfig.key == "last_heartbeat",
            SystemConfig.organization_id == None,
        ).first()
        hb_str = c.value if c else ""

    worker = "starting"
    if hb_str:
        try:
            last = datetime.fromisoformat(hb_str.strip()[:19])
            age = datetime.utcnow() - last
            worker = "online" if age <= timedelta(minutes=15) else "offline"
        except Exception:
            pass

    # Trading paused?
    paused = _cfg(db, "trading_paused", org_id, "False").lower() == "true"

    # Account capital
    acct = db.query(Account).filter(Account.organization_id == org_id).first()
    capital_aud = _float(acct.capital_aud) if acct else 0

    # Active exchanges
    active_exchanges = _cfg(db, "active_exchanges", org_id, "ASX")

    return {
        "open_positions_count": len(open_positions),
        "pending_signals_count": pending_signals,
        "total_unrealised_pnl": round(total_unrealised, 2),
        "total_unrealised_pct": round(total_unrealised_pct, 2),
        "todays_realised_pnl": round(todays_pnl, 2),
        "todays_trades_count": len(todays_trades),
        "regime_asx": regime_asx,
        "regime_crypto": regime_crypto,
        "worker_status": worker,
        "trading_paused": paused,
        "capital_aud": round(capital_aud or 0, 2),
        "active_exchanges": active_exchanges,
        "timestamp": datetime.utcnow().isoformat(),
    }


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

@router.get("/positions")
def get_positions(auth=Depends(_current_user), db: Session = Depends(get_db)):
    user, org_id = auth
    positions = db.query(Position).filter(
        Position.organization_id == org_id,
        Position.status == TradeStatus.OPEN,
    ).order_by(Position.entry_date.desc()).all()

    result = []
    for p in positions:
        pnl = _float(p.unrealised_pnl) or 0
        pct = _float(p.unrealised_pct) or 0
        result.append({
            "id": p.id,
            "ticker": _ticker_display(p.ticker),
            "ticker_raw": p.ticker,
            "exchange_key": p.exchange_key,
            "asset_type": p.asset_type,
            "currency": p.currency,
            "entry_date": str(p.entry_date),
            "entry_price": _float(p.entry_price),
            "current_price": _float(p.current_price),
            "current_stop": _float(p.current_stop),
            "qty": _float(p.qty),
            "unrealised_pnl": round(pnl, 2),
            "unrealised_pct": round(pct, 2),
            "target_1": _float(p.target_1),
            "target_2": _float(p.target_2),
            "target_1_hit": p.target_1_hit,
            "is_paper": p.is_paper,
            "last_updated": p.last_updated.isoformat() if p.last_updated else None,
        })

    return {"positions": result, "count": len(result)}


@router.post("/positions/{position_id}/close")
def close_position(
    position_id: int,
    req: ClosePositionRequest,
    auth=Depends(_current_user),
    db: Session = Depends(get_db),
):
    user, org_id = auth
    pos = db.query(Position).filter(
        Position.id == position_id,
        Position.organization_id == org_id,
        Position.status == TradeStatus.OPEN,
    ).first()
    if not pos:
        raise HTTPException(status_code=404, detail="Position not found")

    try:
        exit_reason = ExitReason[req.exit_reason.upper()]
    except KeyError:
        raise HTTPException(status_code=400, detail=f"Invalid exit reason: {req.exit_reason}")

    exit_price = req.exit_price or _float(pos.current_price) or _float(pos.entry_price)
    entry_price = _float(pos.entry_price) or 0
    qty = _float(pos.qty) or 0
    gross_pnl = (exit_price - entry_price) * qty
    net_pnl = gross_pnl  # no commission for paper
    pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price else 0

    today = date.today()
    entry_d = pos.entry_date if isinstance(pos.entry_date, date) else date.today()
    hold_days = (today - entry_d).days

    trade = Trade(
        ticker=pos.ticker,
        exchange_key=pos.exchange_key,
        asset_type=pos.asset_type,
        currency=pos.currency,
        account_id=pos.account_id,
        organization_id=org_id,
        signal_id=pos.signal_id,
        entry_date=entry_d,
        exit_date=today,
        hold_days=hold_days,
        entry_price=entry_price,
        exit_price=exit_price,
        qty=qty,
        gross_pnl_aud=round(gross_pnl, 2),
        net_pnl_aud=round(net_pnl, 2),
        pnl_pct=round(pnl_pct, 4),
        initial_stop=pos.initial_stop,
        exit_reason=exit_reason,
        is_paper=pos.is_paper,
        cgt_eligible_discount=hold_days >= 365,
    )
    db.add(trade)
    pos.status = TradeStatus.CLOSED

    from app.models.audit import AuditLog
    db.add(AuditLog(
        organization_id=org_id,
        user_id=user.id,
        actor=user.email,
        action="POSITION_CLOSED",
        entity_type="position",
        entity_id=pos.id,
        detail=f"Mobile: {pos.ticker} closed @ {exit_price:.4f} | {exit_reason.value} | P&L {pnl_pct:.1f}%",
    ))

    db.commit()
    return {
        "success": True,
        "ticker": _ticker_display(pos.ticker),
        "exit_price": exit_price,
        "net_pnl_aud": round(net_pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
    }


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

@router.get("/signals")
def get_signals(
    days: int = 7,
    auth=Depends(_current_user),
    db: Session = Depends(get_db),
):
    user, org_id = auth
    cutoff = date.today() - timedelta(days=days)
    signals = db.query(Signal).filter(
        Signal.organization_id == org_id,
        Signal.signal_date >= cutoff,
    ).order_by(Signal.signal_date.desc(), Signal.id.desc()).all()

    result = []
    for s in signals:
        rule_results = s.rule_results or {}
        passed = sum(1 for v in rule_results.values() if v is True)
        total = len(rule_results)
        result.append({
            "id": s.id,
            "ticker": _ticker_display(s.ticker),
            "ticker_raw": s.ticker,
            "exchange_key": s.exchange_key,
            "asset_type": s.asset_type,
            "currency": s.currency,
            "signal_date": str(s.signal_date),
            "status": s.status.value,
            "close_price": _float(s.close_price),
            "pivot_price": _float(s.pivot_price),
            "stop_price": _float(s.stop_price),
            "target_1": _float(s.target_price_1),
            "target_2": _float(s.target_price_2),
            "rs_rating": _float(s.rs_rating),
            "trend_score": s.trend_score,
            "fundamental_score": s.fundamental_score,
            "vcp_contractions": s.vcp_contractions,
            "vcp_weeks": s.vcp_weeks,
            "rules_passed": passed,
            "rules_total": total,
            "suggested_size_aud": _float(s.suggested_size_aud),
            "risk_per_trade_aud": _float(s.risk_per_trade_aud),
            "created_at": s.created_at.isoformat() if s.created_at else None,
        })

    return {"signals": result, "count": len(result)}


@router.post("/signals/{signal_id}/skip")
def skip_signal(signal_id: int, auth=Depends(_current_user), db: Session = Depends(get_db)):
    user, org_id = auth
    sig = db.query(Signal).filter(
        Signal.id == signal_id,
        Signal.organization_id == org_id,
    ).first()
    if not sig:
        raise HTTPException(status_code=404, detail="Signal not found")
    sig.status = SignalStatus.SKIPPED
    from app.models.audit import AuditLog
    db.add(AuditLog(
        organization_id=org_id, user_id=user.id, actor=user.email,
        action="SIGNAL_SKIPPED", entity_type="signal", entity_id=sig.id,
        detail=f"Mobile: {sig.ticker} skipped",
    ))
    db.commit()
    return {"success": True, "signal_id": signal_id, "status": "SKIPPED"}


@router.post("/signals/{signal_id}/unskip")
def unskip_signal(signal_id: int, auth=Depends(_current_user), db: Session = Depends(get_db)):
    user, org_id = auth
    sig = db.query(Signal).filter(
        Signal.id == signal_id,
        Signal.organization_id == org_id,
    ).first()
    if not sig:
        raise HTTPException(status_code=404, detail="Signal not found")
    sig.status = SignalStatus.PENDING
    db.commit()
    return {"success": True, "signal_id": signal_id, "status": "PENDING"}


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

@router.get("/watchlist")
def get_watchlist(auth=Depends(_current_user), db: Session = Depends(get_db)):
    user, org_id = auth
    items = db.query(Watchlist).filter(
        Watchlist.organization_id == org_id,
        Watchlist.status == WatchlistStatus.WATCHING,
    ).order_by(Watchlist.created_at.desc()).all()

    result = []
    for w in items:
        result.append({
            "id": w.id,
            "ticker": _ticker_display(w.ticker),
            "ticker_raw": w.ticker,
            "exchange_key": w.exchange_key,
            "asset_type": w.asset_type,
            "currency": w.currency,
            "added_date": str(w.added_date),
            "added_by": w.added_by,
            "label": w.label.name if w.label else None,
            "label_color": w.label.color if w.label else None,
            "notes": w.notes,
            "rule_results": w.rule_results or {},
        })

    return {"watchlist": result, "count": len(result)}


# ---------------------------------------------------------------------------
# Quick actions
# ---------------------------------------------------------------------------

def _queue_task(task_name: str, org_id: int, *args, **kwargs) -> bool:
    """Queue a Celery task. Returns True on success, False if Redis unavailable."""
    try:
        from app.tasks.celery_app import celery_app
        celery_app.send_task(task_name, args=args, kwargs={"organization_id": org_id, **kwargs})
        return True
    except Exception as e:
        logger.warning(f"Could not queue {task_name}: {e}")
        return False


@router.post("/actions/pause")
def pause_trading(auth=Depends(_current_user), db: Session = Depends(get_db)):
    user, org_id = auth
    cfg = db.query(SystemConfig).filter(
        SystemConfig.key == "trading_paused",
        SystemConfig.organization_id == org_id,
    ).first()
    if cfg:
        cfg.value = "True"
    else:
        db.add(SystemConfig(key="trading_paused", value="True", organization_id=org_id))
    from app.models.audit import AuditLog
    db.add(AuditLog(
        organization_id=org_id, user_id=user.id, actor=user.email,
        action="TRADING_PAUSED", entity_type="system", entity_id=None,
        detail="Mobile: trading paused",
    ))
    db.commit()
    return {"success": True, "trading_paused": True}


@router.post("/actions/resume")
def resume_trading(auth=Depends(_current_user), db: Session = Depends(get_db)):
    user, org_id = auth
    cfg = db.query(SystemConfig).filter(
        SystemConfig.key == "trading_paused",
        SystemConfig.organization_id == org_id,
    ).first()
    if cfg:
        cfg.value = "False"
    from app.models.audit import AuditLog
    db.add(AuditLog(
        organization_id=org_id, user_id=user.id, actor=user.email,
        action="TRADING_RESUMED", entity_type="system", entity_id=None,
        detail="Mobile: trading resumed",
    ))
    db.commit()
    return {"success": True, "trading_paused": False}


@router.post("/actions/force-screen")
def force_screen(auth=Depends(_current_user), db: Session = Depends(get_db)):
    user, org_id = auth
    queued = _queue_task("app.tasks.screening._run_screen_force", org_id)
    from app.models.audit import AuditLog
    db.add(AuditLog(
        organization_id=org_id, user_id=user.id, actor=user.email,
        action="FORCE_SCREEN", entity_type="system", entity_id=None,
        detail=f"Mobile: force screen queued={queued}",
    ))
    db.commit()
    return {"success": queued, "message": "Screener queued" if queued else "Worker offline — will run when available"}


@router.post("/actions/refresh-data")
def refresh_data(
    exchange_key: str = "ASX",
    auth=Depends(_current_user),
    db: Session = Depends(get_db),
):
    user, org_id = auth
    queued = _queue_task("app.tasks.screening.refresh_price_data", org_id,
                         exchange_key=exchange_key)
    return {"success": queued, "message": f"Refresh data queued for {exchange_key}"}


@router.post("/actions/ping-worker")
def ping_worker(auth=Depends(_current_user), db: Session = Depends(get_db)):
    user, org_id = auth
    queued = _queue_task("app.tasks.reporting.health_check", org_id)
    return {"success": queued, "message": "Ping sent" if queued else "Redis unavailable"}


@router.post("/actions/evaluate-regime")
def evaluate_regime(
    exchange_key: str = "ASX",
    auth=Depends(_current_user),
    db: Session = Depends(get_db),
):
    user, org_id = auth
    queued = _queue_task("app.tasks.screening.evaluate_market_regime_task", org_id,
                         exchange_key=exchange_key)
    return {"success": queued, "message": f"Regime evaluation queued for {exchange_key}"}


@router.post("/actions/send-report")
def send_report(auth=Depends(_current_user), db: Session = Depends(get_db)):
    user, org_id = auth
    queued = _queue_task("app.tasks.reporting.send_daily_report", org_id)
    return {"success": queued, "message": "Daily report queued" if queued else "Worker offline"}


# ---------------------------------------------------------------------------
# Trades history
# ---------------------------------------------------------------------------

@router.get("/trades")
def get_trades(
    limit: int = 30,
    auth=Depends(_current_user),
    db: Session = Depends(get_db),
):
    user, org_id = auth
    cutoff = date.today() - timedelta(days=90)
    trades = db.query(Trade).filter(
        Trade.organization_id == org_id,
        Trade.exit_date >= cutoff,
    ).order_by(Trade.exit_date.desc()).limit(limit).all()

    result = []
    for t in trades:
        result.append({
            "id": t.id,
            "ticker": _ticker_display(t.ticker),
            "ticker_raw": t.ticker,
            "exchange_key": t.exchange_key,
            "asset_type": t.asset_type,
            "entry_date": str(t.entry_date),
            "exit_date": str(t.exit_date),
            "hold_days": t.hold_days,
            "entry_price": _float(t.entry_price),
            "exit_price": _float(t.exit_price),
            "qty": _float(t.qty),
            "net_pnl_aud": _float(t.net_pnl_aud),
            "pnl_pct": _float(t.pnl_pct),
            "exit_reason": t.exit_reason.value if t.exit_reason else None,
            "is_paper": t.is_paper,
        })

    # Quick stats
    pnl_values = [_float(t.net_pnl_aud) or 0 for t in trades]
    winners = [v for v in pnl_values if v > 0]
    losers = [v for v in pnl_values if v < 0]

    return {
        "trades": result,
        "count": len(result),
        "stats": {
            "total_pnl": round(sum(pnl_values), 2),
            "win_rate": round(len(winners) / len(pnl_values) * 100, 1) if pnl_values else 0,
            "avg_winner": round(sum(winners) / len(winners), 2) if winners else 0,
            "avg_loser": round(sum(losers) / len(losers), 2) if losers else 0,
        },
    }
