"""
Tests for sync_order_status (app/tasks/trading.py) — the order fill/expiry
reconciliation task (CLAUDE.md #36). Before this task existed, a real IBKR
Order was stamped SUBMITTED and nothing ever updated it: a fill only became a
Position via the position-sync safety net (defaulted -10% stop, no signal
linkage), and an unfilled DAY order silently evaporated with the Order stuck
SUBMITTED and the Signal stuck TRIGGERED forever.

A fake broker stands in for IBKRBroker so no live gateway is needed.
"""
from datetime import date

import pytest

import app.tasks.trading as trading
from app.models.signal import Signal, SignalStatus
from app.models.trade import Order, OrderAction, OrderType, OrderStatus, Position, Trade, TradeStatus, ExitReason
from app.models.audit import AuditLog, AuditAction


class _FakeBroker:
    """Stand-in for IBKRBroker driven by class-level fixtures."""
    ACCOUNT = "DU123"
    OPEN_ORDERS: list[dict] = []
    EXECUTIONS: list[dict] = []

    def __init__(self, organization_id=None):
        self.organization_id = organization_id
        self.account = _FakeBroker.ACCOUNT

    def connect(self):
        return True

    @property
    def is_connected(self):
        return True

    def get_open_orders(self):
        return list(_FakeBroker.OPEN_ORDERS)

    def get_executions(self, days=2):
        return list(_FakeBroker.EXECUTIONS)

    def disconnect(self):
        pass


@pytest.fixture()
def fake_broker(monkeypatch):
    monkeypatch.setattr(trading, "IBKRBroker", _FakeBroker)
    _FakeBroker.OPEN_ORDERS = []
    _FakeBroker.EXECUTIONS = []
    return _FakeBroker


def _set_ibkr_account(db, org, value="DU123"):
    from app.models.config import SystemConfig
    db.add(SystemConfig(key="ibkr_account", organization_id=org.id, value=value))
    db.commit()


def _make_signal(db, org_id, ticker="WOW.AX", pivot=37.0, stop=34.0, t1=44.0, t2=51.0,
                 status=SignalStatus.TRIGGERED):
    sig = Signal(
        ticker=ticker, organization_id=org_id, exchange_key="ASX", asset_type="EQUITY",
        currency="AUD", status=status, pivot_price=pivot, stop_price=stop,
        target_price_1=t1, target_price_2=t2, signal_date=date(2026, 7, 1),
    )
    db.add(sig)
    db.commit()
    db.refresh(sig)
    return sig


def _make_buy_order(db, org, account, signal, ticker="WOW.AX", qty=50, ibkr_order_id=1001,
                    status=OrderStatus.SUBMITTED):
    order = Order(
        ticker=ticker, exchange_key="ASX", asset_type="EQUITY", currency="AUD",
        account_id=account.id, organization_id=org.id, signal_id=signal.id,
        action=OrderAction.BUY, order_type=OrderType.BRACKET, status=status,
        qty_ordered=qty, qty_filled=0, limit_price=37.0, stop_price=float(signal.stop_price),
        ibkr_order_id=ibkr_order_id, is_paper=True,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def _exec(order_id, signal_id, side, qty, avg_price, commission=6.0, ticker="WOW", perm_id=None):
    return {
        "perm_id": perm_id, "order_id": order_id, "order_ref": f"astratrade-{signal_id}",
        "ticker": ticker, "side": side, "qty": qty, "avg_price": avg_price,
        "commission": commission, "time": None,
    }


def test_buy_fill_creates_position_with_signal_stop_and_targets(db_session, org_and_account, fake_broker):
    org, account = org_and_account
    _set_ibkr_account(db_session, org)
    signal = _make_signal(db_session, org.id, stop=34.0, t1=44.0, t2=51.0)
    order = _make_buy_order(db_session, org, account, signal, qty=50, ibkr_order_id=1001)

    fake_broker.OPEN_ORDERS = []  # no longer working — it filled
    fake_broker.EXECUTIONS = [_exec(1001, signal.id, "BOT", 50, 37.55, commission=6.0, perm_id=555)]

    trading.sync_order_status.run(organization_id=org.id)
    db_session.expire_all()

    order = db_session.query(Order).filter(Order.id == order.id).first()
    assert order.status == OrderStatus.FILLED
    assert float(order.qty_filled) == 50
    assert float(order.avg_fill_price) == pytest.approx(37.55)
    assert order.perm_id == 555

    pos = db_session.query(Position).filter(Position.ticker == "WOW.AX").first()
    assert pos is not None
    assert pos.status == TradeStatus.OPEN
    assert pos.signal_id == signal.id
    assert float(pos.entry_price) == pytest.approx(37.55)
    assert float(pos.qty) == 50
    assert float(pos.initial_stop) == 34.0
    assert float(pos.current_stop) == 34.0
    assert float(pos.target_1) == 44.0
    assert float(pos.target_2) == 51.0

    sig = db_session.query(Signal).filter(Signal.id == signal.id).first()
    assert sig.status == SignalStatus.TRIGGERED  # unchanged — it filled, no reason to revert

    assert db_session.query(AuditLog).filter(
        AuditLog.action == AuditAction.ORDER_FILLED, AuditLog.ticker == "WOW.AX",
    ).first() is not None
    assert db_session.query(AuditLog).filter(
        AuditLog.action == AuditAction.POSITION_OPENED, AuditLog.ticker == "WOW.AX",
    ).first() is not None


def test_day_order_expiry_reverts_signal_to_pending(db_session, org_and_account, fake_broker):
    org, account = org_and_account
    _set_ibkr_account(db_session, org)
    signal = _make_signal(db_session, org.id, ticker="CSL.AX", status=SignalStatus.TRIGGERED)
    order = _make_buy_order(db_session, org, account, signal, ticker="CSL.AX", qty=10, ibkr_order_id=2002)

    fake_broker.OPEN_ORDERS = []   # gone from the broker...
    fake_broker.EXECUTIONS = []    # ...and no fill was ever recorded for it

    trading.sync_order_status.run(organization_id=org.id)
    db_session.expire_all()

    order = db_session.query(Order).filter(Order.id == order.id).first()
    assert order.status == OrderStatus.CANCELLED
    assert order.cancelled_at is not None

    sig = db_session.query(Signal).filter(Signal.id == signal.id).first()
    assert sig.status == SignalStatus.PENDING

    log = db_session.query(AuditLog).filter(
        AuditLog.action == AuditAction.ORDER_CANCELLED, AuditLog.ticker == "CSL.AX",
    ).first()
    assert log is not None
    assert "expired unfilled" in log.message.lower()

    # No phantom position was created for an order that never filled.
    assert db_session.query(Position).filter(Position.ticker == "CSL.AX").first() is None


def test_stop_child_fill_closes_position_with_real_price(db_session, org_and_account, fake_broker):
    org, account = org_and_account
    _set_ibkr_account(db_session, org)
    signal = _make_signal(db_session, org.id, ticker="CBA.AX", stop=92.0, t1=120.0)

    pos = Position(
        ticker="CBA.AX", exchange_key="ASX", asset_type="EQUITY", currency="AUD",
        account_id=account.id, organization_id=org.id, signal_id=signal.id,
        entry_date=date(2026, 6, 15), entry_price=100.0, qty=10,
        current_price=95.0, initial_stop=92.0, current_stop=92.0, target_1=120.0,
        status=TradeStatus.OPEN, is_paper=True,
    )
    db_session.add(pos)
    db_session.commit()
    db_session.refresh(pos)

    # Fill price close to the stop, not the target -> STOP_LOSS.
    fake_broker.EXECUTIONS = [_exec(3003, signal.id, "SLD", 10, 91.80, commission=6.0, ticker="CBA")]

    trading.sync_order_status.run(organization_id=org.id)
    db_session.expire_all()

    pos = db_session.query(Position).filter(Position.id == pos.id).first()
    assert pos.status == TradeStatus.CLOSED

    trade = db_session.query(Trade).filter(Trade.ticker == "CBA.AX").first()
    assert trade is not None
    assert trade.exit_reason == ExitReason.STOP_LOSS
    assert float(trade.exit_price) == pytest.approx(91.80)
    assert float(trade.qty) == 10
    assert float(trade.gross_pnl_aud) == pytest.approx((91.80 - 100.0) * 10)

    assert db_session.query(AuditLog).filter(
        AuditLog.action == AuditAction.POSITION_CLOSED, AuditLog.ticker == "CBA.AX",
    ).first() is not None


def test_target_child_fill_closes_position_as_profit_target(db_session, org_and_account, fake_broker):
    org, account = org_and_account
    _set_ibkr_account(db_session, org)
    signal = _make_signal(db_session, org.id, ticker="NAB.AX", stop=27.0, t1=36.0)

    pos = Position(
        ticker="NAB.AX", exchange_key="ASX", asset_type="EQUITY", currency="AUD",
        account_id=account.id, organization_id=org.id, signal_id=signal.id,
        entry_date=date(2026, 6, 1), entry_price=30.0, qty=20,
        current_price=35.5, initial_stop=27.0, current_stop=27.0, target_1=36.0,
        status=TradeStatus.OPEN, is_paper=True,
    )
    db_session.add(pos)
    db_session.commit()
    db_session.refresh(pos)

    # Fill price close to the target, not the stop -> PROFIT_TARGET_1.
    fake_broker.EXECUTIONS = [_exec(4004, signal.id, "SLD", 20, 36.10, commission=6.0, ticker="NAB")]

    trading.sync_order_status.run(organization_id=org.id)
    db_session.expire_all()

    trade = db_session.query(Trade).filter(Trade.ticker == "NAB.AX").first()
    assert trade is not None
    assert trade.exit_reason == ExitReason.PROFIT_TARGET_1
    assert float(trade.exit_price) == pytest.approx(36.10)


def test_partial_fill_then_completion_is_idempotent_and_updates_qty(db_session, org_and_account, fake_broker):
    org, account = org_and_account
    _set_ibkr_account(db_session, org)
    signal = _make_signal(db_session, org.id, ticker="WOW.AX", stop=34.0, t1=44.0)
    order = _make_buy_order(db_session, org, account, signal, ticker="WOW.AX", qty=100, ibkr_order_id=5005)

    # First run: 40 of 100 filled, order still working at the broker.
    fake_broker.OPEN_ORDERS = [{"ibkr_order_id": 5005, "ticker": "WOW", "status": "Submitted",
                                "filled": 40, "remaining": 60}]
    fake_broker.EXECUTIONS = [_exec(5005, signal.id, "BOT", 40, 37.50, commission=3.0, perm_id=900)]

    trading.sync_order_status.run(organization_id=org.id)
    db_session.expire_all()

    order_row = db_session.query(Order).filter(Order.id == order.id).first()
    assert order_row.status == OrderStatus.PARTIAL
    assert float(order_row.qty_filled) == 40

    pos = db_session.query(Position).filter(Position.ticker == "WOW.AX").first()
    assert pos is not None
    assert float(pos.qty) == 40
    assert float(pos.entry_price) == pytest.approx(37.50)

    # Second run: the rest fills (60 more @ a different price), order now fully filled and gone.
    fake_broker.OPEN_ORDERS = []
    fake_broker.EXECUTIONS = [
        _exec(5005, signal.id, "BOT", 40, 37.50, commission=3.0, perm_id=900),
        _exec(5005, signal.id, "BOT", 60, 37.60, commission=4.0, perm_id=900),
    ]

    trading.sync_order_status.run(organization_id=org.id)
    db_session.expire_all()

    order_row = db_session.query(Order).filter(Order.id == order.id).first()
    assert order_row.status == OrderStatus.FILLED
    assert float(order_row.qty_filled) == 100

    # Same Position row updated in place — not a second Position created.
    positions = db_session.query(Position).filter(Position.ticker == "WOW.AX").all()
    assert len(positions) == 1
    assert float(positions[0].qty) == 100
    assert float(positions[0].entry_price) == pytest.approx((40 * 37.50 + 60 * 37.60) / 100)

    # Re-running once more with identical state must not change anything further
    # (Order is now FILLED, so it's excluded from the next run's query entirely).
    trading.sync_order_status.run(organization_id=org.id)
    db_session.expire_all()
    positions = db_session.query(Position).filter(Position.ticker == "WOW.AX").all()
    assert len(positions) == 1
    assert float(positions[0].qty) == 100


def test_orphan_position_repaired_with_real_signal_stop_and_targets(db_session, org_and_account, fake_broker):
    """
    If the position-sync safety net races this task and imports the fill as an
    "orphan" first (defaulted -10% stop, no signal linkage), this task must
    repair it with the real signal-derived stop/targets rather than duplicate it.
    """
    org, account = org_and_account
    _set_ibkr_account(db_session, org)
    signal = _make_signal(db_session, org.id, ticker="BHP.AX", stop=36.0, t1=48.0, t2=56.0)
    order = _make_buy_order(db_session, org, account, signal, ticker="BHP.AX", qty=50, ibkr_order_id=6006)

    # Orphan-imported position: no signal_id, defaulted -10% stop.
    orphan = Position(
        ticker="BHP.AX", exchange_key="ASX", asset_type="EQUITY", currency="AUD",
        account_id=account.id, organization_id=org.id, signal_id=None,
        entry_date=date(2026, 7, 1), entry_price=40.0, qty=50,
        current_price=40.0, initial_stop=36.0, current_stop=36.0,
        status=TradeStatus.OPEN, is_paper=True,
    )
    db_session.add(orphan)
    db_session.commit()

    fake_broker.OPEN_ORDERS = []
    fake_broker.EXECUTIONS = [_exec(6006, signal.id, "BOT", 50, 40.05, commission=6.0, ticker="BHP")]

    trading.sync_order_status.run(organization_id=org.id)
    db_session.expire_all()

    positions = db_session.query(Position).filter(Position.ticker == "BHP.AX").all()
    assert len(positions) == 1, "Must repair the existing orphan, not create a duplicate"
    pos = positions[0]
    assert pos.signal_id == signal.id
    assert float(pos.initial_stop) == 36.0
    assert float(pos.target_1) == 48.0
    assert float(pos.target_2) == 56.0

    assert db_session.query(AuditLog).filter(
        AuditLog.action == AuditAction.POSITION_UPDATED, AuditLog.ticker == "BHP.AX",
    ).first() is not None
