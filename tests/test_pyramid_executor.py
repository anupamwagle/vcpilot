from datetime import date
from decimal import Decimal


def _position(db, org, account):
    from app.models.trade import Position, TradeStatus
    pos = Position(
        ticker="BHP.AX", exchange_key="ASX", asset_type="EQUITY", currency="AUD",
        account_id=account.id, organization_id=org.id, entry_date=date.today(),
        entry_price=Decimal("45"), current_price=Decimal("47"), qty=Decimal("100"),
        initial_stop=Decimal("42"), current_stop=Decimal("42"), status=TradeStatus.OPEN,
        is_paper=True, pyramid_count=0,
    )
    db.add(pos)
    db.commit()
    return pos


def test_pyramid_requires_explicit_account_opt_in(db_session, org_and_account):
    from app.trading.pyramid_executor import request_pyramid_add_on
    org, account = org_and_account
    pos = _position(db_session, org, account)

    result = request_pyramid_add_on(pos.id, org.id, price=47.0)

    assert result["ok"] is False
    assert "disabled" in result["error"].lower()


def test_paper_pyramid_updates_qty_only_after_simulated_fill(db_session, org_and_account):
    from app.trading.pyramid_executor import request_pyramid_add_on
    from app.models.trade import Order, OrderAction, OrderStatus, Position
    org, account = org_and_account
    account.tier.allow_pyramid = True
    db_session.commit()
    pos = _position(db_session, org, account)

    result = request_pyramid_add_on(pos.id, org.id, price=47.0)

    assert result["ok"] is True
    assert result["status"] == "filled"
    db_session.expire_all()
    updated = db_session.query(Position).get(pos.id)
    assert float(updated.qty) == 150.0
    assert updated.pyramid_count == 1
    order = db_session.query(Order).filter_by(action=OrderAction.BUY, status=OrderStatus.FILLED).one()
    assert order.raw_ibkr_response["pyramid_position_id"] == pos.id
