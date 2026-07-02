"""
Regression tests for the cross-org phantom-position cleanup tools:
  - POST /positions/{id}/purge — now looks up by position ID alone (not
    restricted to the caller's currently-active org session), since a
    superadmin needs to purge affected positions across every org without
    switching active org context for each one.
  - GET /superadmin/phantom-positions — cross-org report of every OPEN
    position created by sync_ibkr_positions_task's import branch, flagging
    which orgs still have no ibkr_account configured (very likely phantom)
    vs. which do (possibly the legitimate owner).

See app/broker/ibkr.py / app/tasks/trading.py for the underlying cross-org
IBKR account fallback bug these tools clean up after.
"""
import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from app.models.trade import Position, TradeStatus
from app.models.audit import AuditLog, AuditAction
from app.models.config import SystemConfig


def _fake_request(org_id, role="superadmin", email="admin@astradigital.com.au", user_id=1):
    return SimpleNamespace(
        session={
            "authenticated": True,
            "organization_id": org_id,
            "user_role": role,
            "email": email,
            "user_id": user_id,
        },
        headers={},
        query_params={},
        url=SimpleNamespace(path="/positions"),
    )


def _make_position(db, org, account, ticker="EDU", qty=1196, exchange_key="ASX"):
    pos = Position(
        ticker=ticker, exchange_key=exchange_key, asset_type="EQUITY", currency="AUD",
        account_id=account.id, organization_id=org.id,
        entry_date=date(2026, 7, 2), entry_price=1.05, qty=qty,
        current_price=1.10, initial_stop=0.946, current_stop=0.946,
        status=TradeStatus.OPEN, is_paper=True,
    )
    db.add(pos)
    db.commit()
    db.refresh(pos)
    return pos


# ──────────────────────────────────────────────────────────────────────────
# POST /positions/{id}/purge
# ──────────────────────────────────────────────────────────────────────────

def test_purge_allows_superadmin_to_purge_position_in_different_org(db_session, org_and_account):
    """
    The whole point of the cross-org report is purging affected positions
    without switching active org context for each one — the route must not
    restrict the lookup to request.session["organization_id"].
    """
    from web.main import purge_phantom_position

    org, account = org_and_account
    pos = _make_position(db_session, org, account)

    # Superadmin's session is on a DIFFERENT org (id=999) than the position.
    response = asyncio.run(
        purge_phantom_position(_fake_request(org_id=999), pos.id, db=db_session)
    )
    assert response.status_code == 303

    assert db_session.query(Position).filter(Position.id == pos.id).first() is None

    log = db_session.query(AuditLog).filter(
        AuditLog.action == AuditAction.MANUAL_OVERRIDE,
        AuditLog.organization_id == org.id,
        AuditLog.ticker == "EDU",
    ).order_by(AuditLog.id.desc()).first()
    assert log is not None, "Audit entry must be attributed to the position's OWN org, not the superadmin's active org"
    assert "phantom" in log.message.lower()


def test_purge_forbidden_for_non_superadmin(db_session, org_and_account):
    from web.main import purge_phantom_position

    org, account = org_and_account
    pos = _make_position(db_session, org, account)

    response = asyncio.run(
        purge_phantom_position(_fake_request(org.id, role="org_admin"), pos.id, db=db_session)
    )
    assert response.status_code == 302
    assert "forbidden" in response.headers["location"]

    # Position must still exist — nothing was deleted.
    assert db_session.query(Position).filter(Position.id == pos.id).first() is not None


def test_purge_creates_no_trade_record(db_session, org_and_account):
    """A phantom position was never a real trade — purge must not create a Trade row."""
    from web.main import purge_phantom_position
    from app.models.trade import Trade

    org, account = org_and_account
    pos = _make_position(db_session, org, account)

    asyncio.run(purge_phantom_position(_fake_request(org.id), pos.id, db=db_session))

    assert db_session.query(Trade).filter(Trade.ticker == "EDU").count() == 0


# ──────────────────────────────────────────────────────────────────────────
# GET /superadmin/phantom-positions
# ──────────────────────────────────────────────────────────────────────────

def _patch_template_passthrough(monkeypatch):
    """
    templates.TemplateResponse renders real Jinja2 files from a hardcoded
    /app/web/templates path that doesn't exist outside the Docker container.
    Intercept it to return the context dict itself so route logic can be
    asserted on without needing real template rendering.
    """
    import web.main as main_module
    monkeypatch.setattr(main_module.templates, "TemplateResponse", lambda name, context: context)


def _seed_import_audit_log(db, org, ticker="EDU", qty=1196):
    db.add(AuditLog(
        action=AuditAction.POSITION_OPENED, organization_id=org.id, ticker=ticker,
        message=f"IBKR sync: imported {qty}x{ticker} @ 1.0500 (avg cost from IBKR); stop defaulted to -10% — review",
        detail={"source": "ibkr_sync", "avg_cost": 1.05, "qty": qty},
    ))
    db.commit()


def test_phantom_report_flags_org_without_ibkr_account(db_session, org_and_account, monkeypatch):
    _patch_template_passthrough(monkeypatch)
    from web.main import superadmin_phantom_positions

    org, account = org_and_account
    pos = _make_position(db_session, org, account)
    _seed_import_audit_log(db_session, org)
    # No ibkr_account SystemConfig row for this org.

    ctx = asyncio.run(superadmin_phantom_positions(_fake_request(org.id), db=db_session))

    assert ctx["phantom_count"] == 1
    row = next(r for r in ctx["rows"] if r["position_id"] == pos.id)
    assert row["has_own_account"] is False
    assert row["ticker"] == "EDU"
    assert row["org_id"] == org.id


def test_phantom_report_flags_org_with_ibkr_account_as_not_phantom(db_session, org_and_account, monkeypatch):
    _patch_template_passthrough(monkeypatch)
    from web.main import superadmin_phantom_positions

    org, account = org_and_account
    pos = _make_position(db_session, org, account)
    _seed_import_audit_log(db_session, org)
    db_session.add(SystemConfig(key="ibkr_account", organization_id=org.id, value="DU123"))
    db_session.commit()

    ctx = asyncio.run(superadmin_phantom_positions(_fake_request(org.id), db=db_session))

    assert ctx["phantom_count"] == 0
    row = next(r for r in ctx["rows"] if r["position_id"] == pos.id)
    assert row["has_own_account"] is True


def test_phantom_report_excludes_positions_without_import_audit_trail(db_session, org_and_account, monkeypatch):
    """A position with no matching 'IBKR sync: imported' audit entry is a real
    trade, not a phantom import — must not appear in the report at all."""
    _patch_template_passthrough(monkeypatch)
    from web.main import superadmin_phantom_positions

    org, account = org_and_account
    _make_position(db_session, org, account, ticker="BHP")
    # No audit log seeded for BHP.

    ctx = asyncio.run(superadmin_phantom_positions(_fake_request(org.id), db=db_session))

    assert ctx["rows"] == []
    assert ctx["phantom_count"] == 0


def test_phantom_report_excludes_already_closed_positions(db_session, org_and_account, monkeypatch):
    """If a position was already purged/closed since the import, it must not
    reappear in the report just because the audit trail still exists."""
    _patch_template_passthrough(monkeypatch)
    from web.main import superadmin_phantom_positions

    org, account = org_and_account
    pos = _make_position(db_session, org, account)
    _seed_import_audit_log(db_session, org)
    pos.status = TradeStatus.CLOSED
    db_session.commit()

    ctx = asyncio.run(superadmin_phantom_positions(_fake_request(org.id), db=db_session))

    assert ctx["rows"] == []


def test_phantom_report_forbidden_for_non_superadmin(db_session, org_and_account, monkeypatch):
    _patch_template_passthrough(monkeypatch)
    from web.main import superadmin_phantom_positions

    org, _account = org_and_account
    response = asyncio.run(
        superadmin_phantom_positions(_fake_request(org.id, role="org_admin"), db=db_session)
    )
    assert response.status_code == 302
