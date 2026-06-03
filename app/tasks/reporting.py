"""
Reporting tasks — daily P&L report, health checks.
"""
from __future__ import annotations
from datetime import date
from loguru import logger

from app.tasks.celery_app import app
from app.database import get_db
from app.models.signal import Signal
from app.models.trade import Position, Trade, TradeStatus
from app.models.audit import AuditLog, AuditAction
from app.models.config import SystemConfig
from app.notifications.whatsapp import WhatsAppNotifier


def generate_daily_report() -> dict:
    """Generate daily report dict. Called by task and by agent."""
    today = date.today()
    with get_db() as db:
        signals_today = db.query(Signal).filter(Signal.signal_date == today).count()
        open_positions = db.query(Position).filter(Position.status == TradeStatus.OPEN).count()

        # Today's closed trades P&L
        today_trades = db.query(Trade).filter(Trade.exit_date == today).all()
        pnl_today = sum(float(t.net_pnl_aud or 0) for t in today_trades)

        # All-time P&L
        all_trades = db.query(Trade).all()
        pnl_total = sum(float(t.net_pnl_aud or 0) for t in all_trades)

        regime_cfg = db.query(SystemConfig).filter(
            SystemConfig.key == "last_market_regime"
        ).first()
        regime = regime_cfg.value if regime_cfg else "UNKNOWN"

    return {
        "date": str(today),
        "signals_count": signals_today,
        "open_positions": open_positions,
        "pnl_today_aud": round(pnl_today, 2),
        "pnl_total_aud": round(pnl_total, 2),
        "market_regime": regime,
    }


@app.task(name="app.tasks.reporting.send_daily_report", bind=True)
def send_daily_report(self):
    """Send daily P&L report via WhatsApp."""
    logger.info("Generating daily report...")
    try:
        report = generate_daily_report()
        notifier = WhatsAppNotifier()
        notifier.send_daily_report(report)

        with get_db() as db:
            db.add(AuditLog(
                action=AuditAction.HEALTH_CHECK,
                message="Daily report sent",
                detail=report,
            ))
        logger.info(f"Daily report sent: {report}")
    except Exception as e:
        logger.error(f"Daily report failed: {e}")


@app.task(name="app.tasks.reporting.health_check", bind=True)
def health_check(self):
    """
    Heartbeat task. If this stops running, the worker is dead.
    Stores last heartbeat timestamp in SystemConfig.
    """
    from datetime import datetime
    now_str = datetime.utcnow().isoformat()
    try:
        with get_db() as db:
            cfg = db.query(SystemConfig).filter(
                SystemConfig.key == "last_heartbeat"
            ).first()
            if cfg:
                cfg.value = now_str
            else:
                db.add(SystemConfig(
                    key="last_heartbeat",
                    value=now_str,
                    label="Last Worker Heartbeat",
                    group="system",
                ))
            db.add(AuditLog(
                action=AuditAction.HEALTH_CHECK,
                message=f"Heartbeat: {now_str}",
            ))
        logger.debug(f"Health check OK: {now_str}")
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        notifier = WhatsAppNotifier()
        notifier.send_health_alert("Celery Worker", str(e))
