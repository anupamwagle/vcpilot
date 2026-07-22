from datetime import date
from decimal import Decimal


def _open_position(db, org, account, *, paper):
    from app.models.trade import Position, TradeStatus
    pos = Position(
        ticker="BHP.AX", exchange_key="ASX", asset_type="EQUITY", currency="AUD",
        account_id=account.id, organization_id=org.id, entry_date=date.today(),
        entry_price=Decimal("45"), qty=Decimal("10"), current_price=Decimal("44"),
        initial_stop=Decimal("42"), current_stop=Decimal("42"), status=TradeStatus.OPEN,
        is_paper=paper,
    )
    db.add(pos)
    db.commit()
    return pos


def test_live_manual_exit_submits_order_and_keeps_position_open(db_session, org_and_account, monkeypatch):
    from app.trading.exit_executor import request_position_exit
    from app.models.trade import Order, OrderAction, OrderStatus, Position, TradeStatus
    from app.broker.ibkr import IBKRBroker

    org, account = org_and_account
    pos = _open_position(db_session, org, account, paper=False)
    monkeypatch.setattr(IBKRBroker, "connect", lambda self: True)
    monkeypatch.setattr(IBKRBroker, "submit_market_sell", lambda self, **_kw: {
        "status": "submitted", "ibkr_order_id": 123, "ibkr_perm_id": 456,
    })

    result = request_position_exit(pos.id, org.id, "MANUAL", actor="test")

    assert result["ok"] is True
    db_session.expire_all()
    assert db_session.query(Position).get(pos.id).status == TradeStatus.OPEN
    sell = db_session.query(Order).filter_by(action=OrderAction.SELL, status=OrderStatus.SUBMITTED).one()
    assert sell.ibkr_order_id == 123
    assert sell.raw_ibkr_response["position_id"] == pos.id


def test_paper_manual_exit_is_immediately_recorded(db_session, org_and_account):
    from app.trading.exit_executor import request_position_exit
    from app.models.trade import Position, Trade, TradeStatus

    org, account = org_and_account
    pos = _open_position(db_session, org, account, paper=True)

    result = request_position_exit(pos.id, org.id, "MANUAL", requested_price=44.0)

    assert result["status"] == "filled"
    db_session.expire_all()
    assert db_session.query(Position).get(pos.id).status == TradeStatus.CLOSED
    assert db_session.query(Trade).filter_by(position_id=pos.id).count() == 1
