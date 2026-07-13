"""Tests for app/utils/startup_checks.py — dangerous-toggle production warning."""
from app.models.audit import AuditLog, AuditAction


def _set_config(db, key, value, org_id=None):
    from app.models.config import SystemConfig
    db.add(SystemConfig(key=key, value=value, organization_id=org_id))
    db.commit()


def test_no_warning_when_not_production(db_session, monkeypatch):
    from app.config import settings
    from app.utils.startup_checks import warn_if_dangerous_toggles_enabled

    monkeypatch.setattr(settings, "app_env", "development")
    _set_config(db_session, "ibkr_simulate", "true")

    warn_if_dangerous_toggles_enabled("test")

    assert db_session.query(AuditLog).filter(AuditLog.action == AuditAction.SYSTEM_STARTED).count() == 0


def test_no_warning_when_toggles_off_in_production(db_session, monkeypatch):
    from app.config import settings
    from app.utils.startup_checks import warn_if_dangerous_toggles_enabled

    monkeypatch.setattr(settings, "app_env", "production")

    warn_if_dangerous_toggles_enabled("test")

    assert db_session.query(AuditLog).filter(AuditLog.action == AuditAction.SYSTEM_STARTED).count() == 0


def test_warns_and_audits_when_ibkr_simulate_on_in_production(db_session, monkeypatch):
    from app.config import settings
    from app.utils.startup_checks import warn_if_dangerous_toggles_enabled

    monkeypatch.setattr(settings, "app_env", "production")
    _set_config(db_session, "ibkr_simulate", "true")

    warn_if_dangerous_toggles_enabled("test")

    log = db_session.query(AuditLog).filter(AuditLog.action == AuditAction.SYSTEM_STARTED).first()
    assert log is not None
    assert "ibkr_simulate=True" in log.message
    assert "[test]" in log.message


def test_warns_and_audits_when_mock_time_on_in_production(db_session, monkeypatch):
    from app.config import settings
    from app.utils.startup_checks import warn_if_dangerous_toggles_enabled

    monkeypatch.setattr(settings, "app_env", "production")
    _set_config(db_session, "mock_time_enabled", "true")
    _set_config(db_session, "mock_current_time", "2020-01-01 00:00:00")

    warn_if_dangerous_toggles_enabled("test")

    log = db_session.query(AuditLog).filter(AuditLog.action == AuditAction.SYSTEM_STARTED).first()
    assert log is not None
    assert "mock_time_enabled=True" in log.message
    assert "2020-01-01" in log.message


def test_does_not_raise_if_db_unavailable(monkeypatch):
    """Startup must never crash the process over a config-check failure."""
    from app.config import settings
    from app.utils.startup_checks import warn_if_dangerous_toggles_enabled

    monkeypatch.setattr(settings, "app_env", "production")

    def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(type(settings), "mock_time_enabled", property(lambda self: _boom()))

    warn_if_dangerous_toggles_enabled("test")  # must not raise
