"""Tests for app/models/audit.py — AuditAction enum + AuditLog model."""
from app.models.audit import AuditLog, AuditAction


def test_login_and_login_failed_enum_members_exist():
    """B13 — Postgres enum migration (migrate_saas Migration 013) adds these
    values ahead of any call site using them; this just guards that the
    Python-side enum members exist with the expected string values."""
    assert AuditAction.LOGIN.value == "LOGIN"
    assert AuditAction.LOGIN_FAILED.value == "LOGIN_FAILED"


def test_auditlog_can_be_created_with_login_action(db_session, org_and_account):
    org, _ = org_and_account
    log = AuditLog(action=AuditAction.LOGIN, actor="test@astradigital.com.au",
                    organization_id=org.id, message="Test login")
    db_session.add(log)
    db_session.commit()

    row = db_session.query(AuditLog).filter(AuditLog.action == AuditAction.LOGIN).first()
    assert row is not None
    assert row.message == "Test login"


def test_auditlog_can_be_created_with_login_failed_action(db_session, org_and_account):
    org, _ = org_and_account
    log = AuditLog(action=AuditAction.LOGIN_FAILED, actor="test@astradigital.com.au",
                    organization_id=org.id, message="Test failed login")
    db_session.add(log)
    db_session.commit()

    row = db_session.query(AuditLog).filter(AuditLog.action == AuditAction.LOGIN_FAILED).first()
    assert row is not None
