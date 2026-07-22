from datetime import date
from decimal import Decimal


def _paper_position(db, org, account):
    from app.models.trade import Position, TradeStatus
    pos = Position(
        ticker="BHP.AX", exchange_key="ASX", asset_type="EQUITY", currency="AUD",
        account_id=account.id, organization_id=org.id, entry_date=date.today(),
        entry_price=Decimal("45"), current_price=Decimal("50"), qty=Decimal("10"),
        initial_stop=Decimal("42"), current_stop=Decimal("42"), status=TradeStatus.OPEN,
        is_paper=True,
    )
    db.add(pos)
    db.commit()
    return pos


def test_paper_stop_update_is_protective_only(db_session, org_and_account):
    from app.trading.stop_executor import request_stop_update
    from app.models.trade import Position
    org, account = org_and_account
    pos = _paper_position(db_session, org, account)

    rejected = request_stop_update(pos.id, org.id, 40.0)
    applied = request_stop_update(pos.id, org.id, 46.0)

    assert rejected["ok"] is False
    assert applied["ok"] is True
    db_session.expire_all()
    assert float(db_session.query(Position).filter_by(id=pos.id).one().current_stop) == 46.0
