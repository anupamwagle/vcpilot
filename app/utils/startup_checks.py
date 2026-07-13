"""
Startup-time safety checks — run once per process (web app, Celery worker) to
surface dangerous global toggles left on in production. Does not disable
anything: mock_time_enabled / ibkr_simulate are legitimate superadmin tools
for testing, this only makes sure a leftover toggle from a test session
doesn't silently fake the clock or simulate fills without anyone noticing.
"""
from loguru import logger


def warn_if_dangerous_toggles_enabled(source: str) -> None:
    """Log CRITICAL + write an AuditLog row if app_env == production and any
    of mock_time_enabled / ibkr_simulate are truthy. `source` identifies the
    calling process (e.g. "web", "worker") in the log/audit message."""
    from app.config import settings
    if settings.app_env != "production":
        return

    flags = []
    try:
        if settings.mock_time_enabled:
            flags.append(f"mock_time_enabled=True (mock_current_time={settings.mock_current_time!r})")
    except Exception as e:
        logger.debug(f"startup toggle check ({source}): mock_time_enabled read failed: {e}")
    try:
        if settings.ibkr_simulate_live:
            flags.append("ibkr_simulate=True")
    except Exception as e:
        logger.debug(f"startup toggle check ({source}): ibkr_simulate read failed: {e}")

    if not flags:
        return

    message = f"[{source}] DANGEROUS TOGGLE(S) ENABLED IN PRODUCTION: " + "; ".join(flags)
    logger.critical(message)
    try:
        from app.database import get_db
        from app.models.audit import AuditLog, AuditAction
        with get_db() as db:
            AuditLog.safe(db, action=AuditAction.SYSTEM_STARTED, actor="system", message=message)
    except Exception as e:
        logger.warning(f"startup toggle check ({source}): failed to write AuditLog: {e}")
