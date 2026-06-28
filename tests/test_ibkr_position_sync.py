"""
Tests for sync_ibkr_positions_task — the IBKR ↔ DB position reconciliation
queued from the super admin org detail page.

Covers the four reconciliation outcomes:
  1. IBKR holding missing from DB      → imported as OPEN Position
  2. DB position missing from IBKR     → auto-closed as MANUAL (+ Trade row)
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

    def __init__(self, organization_id=None):
        self.organization_id = organization_id
        self.account = _FakeBroker.ACCOUNT

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
    return _FakeBroker


def test_sync_imports_closes_updates_and_skips_crypto(
    db_session, org_and_account, open_crypto_position, fake_broker
):
    org, account = org_and_account

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

    # 2. CSL orphan auto-closed as MANUAL + Trade row created
    db_session.refresh(orphan)
    assert orphan.status == TradeStatus.CLOSED
    csl_trade = db_session.query(Trade).filter(Trade.ticker == "CSL.AX").first()
    assert csl_trade is not None
    assert csl_trade.exit_reason == ExitReason.MANUAL

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
