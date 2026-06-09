"""
Reporting tasks — daily P&L report, health checks.
"""
from __future__ import annotations
from datetime import date
from loguru import logger
from app.utils.time_helper import get_current_date

from app.tasks.celery_app import app
from app.database import get_db
from app.models.signal import Signal
from app.models.trade import Position, Trade, TradeStatus
from app.models.audit import AuditLog, AuditAction
from app.models.config import SystemConfig
from app.notifications import get_notifier


def generate_daily_report(organization_id: int = None) -> dict:
    """Generate daily report dict. Scoped by organization if provided."""
    today = get_current_date()
    with get_db() as db:
        if organization_id:
            signals_today = db.query(Signal).filter(
                Signal.signal_date == today,
                Signal.organization_id == organization_id
            ).count()
            open_positions = db.query(Position).filter(
                Position.status == TradeStatus.OPEN,
                Position.organization_id == organization_id
            ).count()
            today_trades = db.query(Trade).filter(
                Trade.exit_date == today,
                Trade.organization_id == organization_id
            ).all()
            pnl_today = sum(float(t.net_pnl_aud or 0) for t in today_trades)
            all_trades = db.query(Trade).filter(
                Trade.organization_id == organization_id
            ).all()
            pnl_total = sum(float(t.net_pnl_aud or 0) for t in all_trades)
        else:
            signals_today = db.query(Signal).filter(Signal.signal_date == today).count()
            open_positions = db.query(Position).filter(Position.status == TradeStatus.OPEN).count()
            today_trades = db.query(Trade).filter(Trade.exit_date == today).all()
            pnl_today = sum(float(t.net_pnl_aud or 0) for t in today_trades)
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
def send_daily_report(self, organization_id: int = None):
    """Send daily P&L report via WhatsApp.
    When organization_id is provided, sends only to that org (manual trigger).
    When None (scheduled), sends to all active orgs.
    """
    scope = f"org {organization_id}" if organization_id else "all organizations"
    logger.info(f"Generating daily report for {scope}...")
    from app.models.account import Organization
    try:
        with get_db() as db:
            org_query = db.query(Organization).filter(Organization.is_active == True)
            if organization_id:
                org_query = org_query.filter(Organization.id == organization_id)
            orgs = org_query.all()

        for org in orgs:
            try:
                report = generate_daily_report(organization_id=org.id)
                notifier = get_notifier(organization_id=org.id)
                notifier.send_daily_report(report)
                with get_db() as db:
                    db.add(AuditLog(
                        action=AuditAction.HEALTH_CHECK,
                        message=f"Daily report sent to Org {org.name}",
                        detail=report,
                        organization_id=org.id
                    ))
                logger.info(f"Daily report sent to Org {org.name} (ID: {org.id}): {report}")
            except Exception as org_err:
                logger.error(f"Failed sending daily report for Org {org.name} (ID: {org.id}): {org_err}")
    except Exception as e:
        logger.error(f"Daily report loop failed: {e}")


@app.task(name="app.tasks.reporting.health_check", bind=True)
def health_check(self):
    """
    Heartbeat task. If this stops running, the worker is dead.
    Writes a global heartbeat AND per-org heartbeat so each org's
    health page shows the correct worker online/offline status.
    """
    from datetime import datetime
    now_str = datetime.utcnow().isoformat()
    try:
        with get_db() as db:
            from app.models.account import Organization

            # ── Global (system-level) heartbeat ──────────────────────────
            cfg = db.query(SystemConfig).filter(
                SystemConfig.key == "last_heartbeat",
                SystemConfig.organization_id == None,
            ).first()
            if cfg:
                cfg.value = now_str
            else:
                db.add(SystemConfig(
                    key="last_heartbeat",
                    value=now_str,
                    label="Last Worker Heartbeat",
                    group="system",
                    organization_id=None,
                ))

            # ── Per-org heartbeat — keeps each org's status widget green ─
            orgs = db.query(Organization).filter(Organization.is_active == True).all()
            for org in orgs:
                cfg_org = db.query(SystemConfig).filter(
                    SystemConfig.key == "last_heartbeat",
                    SystemConfig.organization_id == org.id,
                ).first()
                if cfg_org:
                    cfg_org.value = now_str
                else:
                    db.add(SystemConfig(
                        key="last_heartbeat",
                        value=now_str,
                        label="Last Worker Heartbeat",
                        group="system",
                        organization_id=org.id,
                    ))

            db.add(AuditLog(
                action=AuditAction.HEALTH_CHECK,
                message=f"Heartbeat: {now_str}",
            ))

        logger.debug(f"Health check OK: {now_str}")
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        notifier = get_notifier()
        notifier.send_health_alert("Celery Worker", str(e))


@app.task(name="app.tasks.reporting.send_whatsapp_message", bind=True)
def send_whatsapp_message(self, organization_id: int, method_name: str, args: list = None, kwargs: dict = None):
    """
    Asynchronously send a WhatsApp notification via the background worker.
    """
    args = args or []
    kwargs = kwargs or {}
    logger.info(f"Sending WhatsApp notification asynchronously for Org {organization_id} calling {method_name}...")
    try:
        from app.notifications import get_notifier
        notifier = get_notifier(organization_id=organization_id)
        func = getattr(notifier, method_name, None)
        if func:
            func(*args, **kwargs)
            logger.info(f"Successfully sent alert message calling {method_name}")
        else:
            logger.error(f"Notifier does not have method {method_name}")
    except Exception as e:
        logger.error(f"Failed to send WhatsApp message: {e}")

