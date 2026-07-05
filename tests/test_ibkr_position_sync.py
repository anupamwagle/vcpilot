"""
Tests for sync_ibkr_positions_task — the IBKR ↔ DB position reconciliation
queued from the super admin org detail page.

Covers the four reconciliation outcomes:
  1. IBKR holding missing from DB      → imported as OPEN Position
  2. DB position missing from IBKR     → auto-closed as BROKER_SYNC (+ Trade row)
  3. Quantity drift                    → DB qty reconciled to IBKR qty
  4. Crypto positions                  → NEVER touched (IBKR can't hold them)

A fake broker stands in for IBKRBroker so no live gateway is needed.
"""
from datetime import date

import pytest

import app.tasks.trading as trading
from app.models.trade import Position, Trade, TradeStatus, ExitReason


def _make_equity_pos(db, org, account, ticker, qty, exchange_key="ASX"):
    pos = Position(
        ticker=ticker, exchange_key=exchange_key, asset_type="EQUITY", currency="AUD",
        account_id=account.id, organization_id=org.id,
        entry_date=date(2026, 6, 1), entry_price=10.0, qty=qty,
        current_price=11.0, initial_stop=9.0, current_stop=9.0,
        status=TradeStatus.OPEN, is_paper=True,
    )
    db.add(pos)
    db.commit()
    db.refresh(pos)
    return pos


class _FakeBroker:
    """Stand-in for IBKRBroker driven by class-level fixtures."""
    POSITIONS: list[dict] = []
    ACCOUNT = "DU123"
    DETECTED_PAPER_MODE = None   # I1 / CLAUDE.md #41 — gateway-derived paper/live

    def __init__(self, organization_id=None):
        self.organization_id = organization_id
        self.account = _FakeBroker.ACCOUNT
        self.detected_paper_mode = _FakeBroker.DETECTED_PAPER_MODE

    def connect(self):
        return True

    @property
    def is_connected(self):
        return True

    def get_open_positions(self, exchange_key=None):
        return list(_FakeBroker.POSITIONS)

    def disconnect(self):
        pass


@pytest.fixture()
def fake_broker(monkeypatch):
    monkeypatch.setattr(trading, "IBKRBroker", _FakeBroker)
    _FakeBroker.DETECTED_PAPER_MODE = None
    return _FakeBroker


def _set_ibkr_account(db, org, value="DU123"):
    """
    sync_ibkr_positions_task refuses to reconcile any org without its own
    explicit ibkr_account SystemConfig row (see the cross-org account fallback
    guard in app/tasks/trading.py and app/broker/ibkr.py) — otherwise it would
    silently resolve to whichever account the shared gateway happens to be
    logged into, which is exactly the bug that guard exists to prevent. Tests
    exercising the reconciliation logic itself must set this explicitly.
    """
    from app.models.config import SystemConfig
    db.add(SystemConfig(key="ibkr_account", organization_id=org.id, value=value))
    db.commit()


def test_sync_imports_closes_updates_and_skips_crypto(
    db_session, org_and_account, open_crypto_position, fake_broker
):
    org, account = org_and_account
    _set_ibkr_account(db_session, org)

    # DB pre-state: an orphan equity, a drifting equity, plus the crypto position.
    orphan = _make_equity_pos(db_session, org, account, "CSL.AX", qty=20)
    drift = _make_equity_pos(db_session, org, account, "WBC.AX", qty=100)
    crypto = open_crypto_position  # TRX-AUD, OPEN

    # IBKR live state: a new holding (BHP), the drifting one at a different qty.
    fake_broker.ACCOUNT = "DU123"
    fake_broker.POSITIONS = [
        {"ticker": "BHP", "exchange": "ASX", "currency": "AUD", "qty": 50,
         "avg_cost": 40.0, "account": "DU123"},
        {"ticker": "WBC", "exchange": "ASX", "currency": "AUD", "qty": 150,
         "avg_cost": 30.0, "account": "DU123"},
    ]

    trading.sync_ibkr_positions_task.run(organization_id=org.id)
    db_session.expire_all()

    # 1. BHP imported as a new OPEN position
    bhp = db_session.query(Position).filter(Position.ticker == "BHP.AX").first()
    assert bhp is not None
    assert bhp.status == TradeStatus.OPEN
    assert float(bhp.qty) == 50
    assert float(bhp.entry_price) == 40.0
    # stop defaulted to -10%
    assert float(bhp.initial_stop) == pytest.approx(36.0)

    # 2. CSL orphan auto-closed as BROKER_SYNC + Trade row created
    # (NOT ExitReason.MANUAL — that reads as "closed manually by you" in the UI,
    # which is misleading for an automated reconciliation close)
    db_session.refresh(orphan)
    assert orphan.status == TradeStatus.CLOSED
    csl_trade = db_session.query(Trade).filter(Trade.ticker == "CSL.AX").first()
    assert csl_trade is not None
    assert csl_trade.exit_reason == ExitReason.BROKER_SYNC

    # 3. WBC qty reconciled 100 -> 150, still one OPEN row (no duplicate)
    db_session.refresh(drift)
    assert float(drift.qty) == 150
    assert drift.status == TradeStatus.OPEN
    wbc_open = db_session.query(Position).filter(
        Position.ticker == "WBC.AX", Position.status == TradeStatus.OPEN
    ).count()
    assert wbc_open == 1

    # 4. Crypto position untouched (NOT closed, no spurious Trade)
    db_session.refresh(crypto)
    assert crypto.status == TradeStatus.OPEN
    assert db_session.query(Trade).filter(Trade.ticker == "TRX-AUD").count() == 0


def test_sync_skips_when_gateway_not_connected(db_session, org_and_account, monkeypatch):
    org, account = org_and_account
    # Must set this explicitly (see _set_ibkr_account) so this test actually
    # exercises the "gateway unreachable" skip path, not the (also-skipping,
    # but different) "no ibkr_account configured" guard from earlier in the task.
    _set_ibkr_account(db_session, org)
    orphan = _make_equity_pos(db_session, org, account, "CSL.AX", qty=20)

    class _Offline(_FakeBroker):
        @property
        def is_connected(self):
            return False

    monkeypatch.setattr(trading, "IBKRBroker", _Offline)
    trading.sync_ibkr_positions_task.run(organization_id=org.id)
    db_session.expire_all()

    # Nothing reconciled — orphan stays OPEN when the gateway is unreachable.
    db_session.refresh(orphan)
    assert orphan.status == TradeStatus.OPEN
    assert db_session.query(Trade).count() == 0


def test_sync_skips_org_with_no_ibkr_account_configured(db_session, org_and_account, fake_broker):
    """
    An org with no ibkr_account SystemConfig row must never reconcile at all —
    not even a call to broker.connect() — because IBKRBroker would otherwise
    resolve to the shared gateway's default account, which may belong to a
    different org entirely (this bit AW org id=10 in production).
    """
    org, account = org_and_account
    orphan = _make_equity_pos(db_session, org, account, "CSL.AX", qty=20)

    fake_broker.ACCOUNT = "DU123"
    fake_broker.POSITIONS = [
        {"ticker": "BHP", "exchange": "ASX", "currency": "AUD", "qty": 50,
         "avg_cost": 40.0, "account": "DU123"},
    ]

    trading.sync_ibkr_positions_task.run(organization_id=org.id)
    db_session.expire_all()

    # No import, no auto-close — the org was skipped before ever calling connect().
    assert db_session.query(Position).filter(Position.ticker == "BHP.AX").first() is None
    db_session.refresh(orphan)
    assert orphan.status == TradeStatus.OPEN
    assert db_session.query(Trade).count() == 0

    from app.models.audit import AuditLog
    skip_log = db_session.query(AuditLog).filter(
        AuditLog.organization_id == org.id,
        AuditLog.message.ilike("%no ibkr_account configured%"),
    ).first()
    assert skip_log is not None


# ─────────────────────────────────────────────────────────────────────────
# I1 (CLAUDE.md #41) — paper/live derived from the gateway login itself
# ─────────────────────────────────────────────────────────────────────────

def test_paper_live_mismatch_auto_corrects_and_alerts(db_session, org_and_account, fake_broker, monkeypatch):
    """Account.is_paper says PAPER (fixture default), but the gateway login is
    actually LIVE (U* account) -> auto-correct the label + alert once."""
    from unittest.mock import MagicMock
    from app.models.account import Account

    org, account = org_and_account
    assert account.is_paper is True  # fixture default
    _set_ibkr_account(db_session, org, value="U987654")
    fake_broker.ACCOUNT = "U987654"
    fake_broker.DETECTED_PAPER_MODE = False   # gateway login is actually LIVE
    fake_broker.POSITIONS = []
    mock_notifier = MagicMock()
    monkeypatch.setattr(trading, "get_notifier", lambda organization_id=None: mock_notifier)

    trading.sync_ibkr_positions_task.run(organization_id=org.id)
    db_session.expire_all()

    acct = db_session.query(Account).filter(Account.id == account.id).first()
    assert acct.is_paper is False, "Must auto-correct the label to match the real gateway login"

    from app.models.audit import AuditLog
    log = db_session.query(AuditLog).filter(
        AuditLog.organization_id == org.id, AuditLog.message.like("%paper/live MISMATCH%"),
    ).first()
    assert log is not None
    mock_notifier.send.assert_called_once()


def test_paper_live_no_mismatch_no_op(db_session, org_and_account, fake_broker, monkeypatch):
    """Detected mode matches the existing label -> no correction, no alert."""
    from unittest.mock import MagicMock
    from app.models.account import Account

    org, account = org_and_account
    assert account.is_paper is True  # fixture default
    _set_ibkr_account(db_session, org, value="DU123")
    fake_broker.ACCOUNT = "DU123"
    fake_broker.DETECTED_PAPER_MODE = True   # matches Account.is_paper
    fake_broker.POSITIONS = []
    mock_notifier = MagicMock()
    monkeypatch.setattr(trading, "get_notifier", lambda organization_id=None: mock_notifier)

    trading.sync_ibkr_positions_task.run(organization_id=org.id)
    db_session.expire_all()

    acct = db_session.query(Account).filter(Account.id == account.id).first()
    assert acct.is_paper is True

    from app.models.audit import AuditLog
    log = db_session.query(AuditLog).filter(
        AuditLog.organization_id == org.id, AuditLog.message.like("%paper/live MISMATCH%"),
    ).first()
    assert log is None
    mock_notifier.send.assert_not_called()


def test_paper_live_mismatch_skipped_when_undetermined(db_session, org_and_account, fake_broker, monkeypatch):
    """detected_paper_mode is None (couldn't determine) -> never touch the label."""
    from unittest.mock import MagicMock
    from app.models.account import Account

    org, account = org_and_account
    _set_ibkr_account(db_session, org, value="DU123")
    fake_broker.ACCOUNT = "DU123"
    fake_broker.DETECTED_PAPER_MODE = None
    fake_broker.POSITIONS = []
    mock_notifier = MagicMock()
    monkeypatch.setattr(trading, "get_notifier", lambda organization_id=None: mock_notifier)

    trading.sync_ibkr_positions_task.run(organization_id=org.id)
    db_session.expire_all()

    acct = db_session.query(Account).filter(Account.id == account.id).first()
    assert acct.is_paper is True  # unchanged
    mock_notifier.send.assert_not_called()
