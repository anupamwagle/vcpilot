"""Exchange-truth lifecycle tests for live CCXT orders."""
from datetime import date
from decimal import Decimal

from app.models.signal import Signal, SignalStatus
from app.models.trade import (
    Order, OrderAction, OrderStatus, OrderType, Position, Trade, TradeStatus,
)


class _Broker:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def get_order(self, order_id, ticker):
        return self.snapshot

    def submit_protective_stop(self, ticker, qty, stop_price, order_ref=""):
        return {"status": "submitted", "entry_order_id": f"stop-{order_ref}", "stop_price": stop_price}


def _patch_broker(monkeypatch, snapshot):
    monkeypatch.setattr(
        "app.broker.crypto.get_crypto_broker_for_org",
        lambda *args, **kwargs: _Broker(snapshot),
    )


def test_crypto_buy_position_is_created_only_after_exchange_fill(db_session, org_and_account, monkeypatch):
    from app.tasks.trading import sync_crypto_order_status

    org, account = org_and_account
    signal = Signal(
        ticker="BTC-USD", organization_id=org.id, exchange_key="CRYPTO_MEXC",
        asset_type="CRYPTO", currency="USD", status=SignalStatus.TRIGGERED,
        signal_date=date.today(), pivot_price=Decimal("90000"), stop_price=Decimal("85000"),
        target_price_1=Decimal("100000"), target_price_2=Decimal("110000"),
    )
    db_session.add(signal)
    db_session.flush()
    order = Order(
        ticker=signal.ticker, exchange_key=signal.exchange_key, asset_type="CRYPTO",
        currency="USD", account_id=account.id, organization_id=org.id, signal_id=signal.id,
        action=OrderAction.BUY, order_type=OrderType.BRACKET, status=OrderStatus.SUBMITTED,
        qty_ordered=Decimal("0.1"), external_order_id="buy-1", is_paper=False,
    )
    db_session.add(order)
    db_session.commit()
    _patch_broker(monkeypatch, {"status": "filled", "filled": 0.1, "average": 91000.0, "fee": 1.0})

    sync_crypto_order_status.run(organization_id=org.id)

    db_session.expire_all()
    assert db_session.query(Order).filter_by(id=order.id).one().status == OrderStatus.FILLED
    position = db_session.query(Position).filter_by(organization_id=org.id, ticker="BTC-USD").one()
    assert position.status == TradeStatus.OPEN
    assert float(position.entry_price) == 91000.0
    native_stop = db_session.query(Order).filter(
        Order.action == OrderAction.SELL, Order.order_type == OrderType.STOP,
    ).one()
    assert native_stop.external_order_id.startswith("stop-protect-")
    assert native_stop.raw_ibkr_response["protective_stop"] is True


def test_crypto_sell_closes_only_after_exchange_fill(db_session, org_and_account, monkeypatch):
    from app.tasks.trading import sync_crypto_order_status

    org, account = org_and_account
    position = Position(
        ticker="BTC-USD", exchange_key="CRYPTO_MEXC", asset_type="CRYPTO", currency="USD",
        account_id=account.id, organization_id=org.id, entry_date=date.today(),
        entry_price=Decimal("90000"), qty=Decimal("0.1"), current_price=Decimal("90000"),
        initial_stop=Decimal("85000"), current_stop=Decimal("85000"), status=TradeStatus.OPEN,
    )
    db_session.add(position)
    db_session.flush()
    order = Order(
        ticker=position.ticker, exchange_key=position.exchange_key, asset_type="CRYPTO",
        currency="USD", account_id=account.id, organization_id=org.id,
        action=OrderAction.SELL, order_type=OrderType.MARKET, status=OrderStatus.SUBMITTED,
        qty_ordered=Decimal("0.1"), external_order_id="sell-1", is_paper=False,
        raw_ibkr_response={"position_id": position.id, "exit_reason": "STOP_LOSS"},
    )
    db_session.add(order)
    db_session.commit()
    _patch_broker(monkeypatch, {"status": "filled", "filled": 0.1, "average": 84000.0, "fee": 1.0})
    monkeypatch.setattr("app.data.fetcher.get_fx_rate", lambda *_args: 1.5)

    sync_crypto_order_status.run(organization_id=org.id)

    db_session.expire_all()
    assert db_session.query(Position).filter_by(id=position.id).one().status == TradeStatus.CLOSED
    trade = db_session.query(Trade).filter_by(position_id=position.id).one()
    assert float(trade.gross_pnl_aud) == -900.0


def test_crypto_pyramid_fill_adds_only_the_confirmed_increment(db_session, org_and_account, monkeypatch):
    from app.tasks.trading import sync_crypto_order_status

    org, account = org_and_account
    position = Position(
        ticker="BTC-USD", exchange_key="CRYPTO_MEXC", asset_type="CRYPTO", currency="USD",
        account_id=account.id, organization_id=org.id, entry_date=date.today(),
        entry_price=Decimal("90000"), qty=Decimal("0.1"), current_price=Decimal("92000"),
        initial_stop=Decimal("85000"), current_stop=Decimal("85000"), status=TradeStatus.OPEN,
        is_paper=False, pyramid_count=0,
    )
    db_session.add(position)
    db_session.flush()
    order = Order(
        ticker=position.ticker, exchange_key=position.exchange_key, asset_type="CRYPTO",
        currency="USD", account_id=account.id, organization_id=org.id,
        action=OrderAction.BUY, order_type=OrderType.MARKET, status=OrderStatus.SUBMITTED,
        qty_ordered=Decimal("0.05"), external_order_id="pyramid-1", is_paper=False,
        raw_ibkr_response={"pyramid_position_id": position.id, "pyramid_number": 1},
    )
    db_session.add(order)
    db_session.commit()
    _patch_broker(monkeypatch, {"status": "filled", "filled": 0.05, "average": 93000.0, "fee": 0.0})

    sync_crypto_order_status.run(organization_id=org.id)
    db_session.expire_all()
    assert float(db_session.query(Position).filter_by(id=position.id).one().qty) == 0.15
    assert db_session.query(Position).filter_by(id=position.id).one().pyramid_count == 1
    # The only remaining submitted order is the native stop created for the
    # add-on; it is still working, not a second BUY fill.
    _patch_broker(monkeypatch, {"status": "open", "filled": 0.0, "average": 0.0, "fee": 0.0})
    sync_crypto_order_status.run(organization_id=org.id)
    db_session.expire_all()
    assert float(db_session.query(Position).filter_by(id=position.id).one().qty) == 0.15


def test_crypto_stop_breach_does_not_close_position_when_exchange_sell_fails(
    db_session, org_and_account, open_crypto_position, monkeypatch
):
    from app.tasks.trading import sync_stop_orders

    class _FailingBroker:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def submit_market_sell(self, *args, **kwargs):
            return {"status": "error", "error": "exchange unavailable"}

    monkeypatch.setattr(
        "app.broker.crypto.get_crypto_broker_for_org",
        lambda *args, **kwargs: _FailingBroker(),
    )
    monkeypatch.setattr(
        "app.tasks.trading.get_intraday_price",
        lambda *args, **kwargs: {"ok": True, "price": 0.15, "volume": 1, "data_source": "test"},
    )
    monkeypatch.setattr("app.tasks.trading.get_notifier", lambda *args, **kwargs: type("N", (), {"send_health_alert": lambda *a, **k: None})())

    sync_stop_orders.run()

    db_session.expire_all()
    assert db_session.query(Position).filter_by(id=open_crypto_position.id).one().status == TradeStatus.OPEN
    assert db_session.query(Trade).filter_by(position_id=open_crypto_position.id).count() == 0
