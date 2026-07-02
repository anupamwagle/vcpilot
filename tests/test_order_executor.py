"""Tests for app/trading/order_executor.py — execute_signal_order()."""
import pytest
from decimal import Decimal
from unittest.mock import patch, MagicMock


def _make_pending_signal(db, org_id, ticker="BHP.AX", exchange_key="ASX", asset_type="EQUITY"):
    from app.models.signal import Signal, SignalStatus
    sig = Signal(
        organization_id=org_id,
        ticker=ticker,
        exchange_key=exchange_key,
        asset_type=asset_type,
        currency="AUD" if asset_type == "EQUITY" else "AUD",
        signal_date=__import__("datetime").date.today(),
        status=SignalStatus.PENDING,
        pivot_price=45.0,
        stop_price=42.0,
        target_price_1=54.0,
        close_price=45.0,
        rs_rating=80,
        trend_score=7,
    )
    db.add(sig)
    db.commit()
    return sig


def _patch_ibkr_simulate(monkeypatch):
    from app.broker.ibkr import IBKRBroker
    monkeypatch.setattr(IBKRBroker, "connect", lambda self: False)
    monkeypatch.setattr(
        IBKRBroker, "submit_bracket_order",
        lambda self, **kw: {"simulated": True, "order_id": "SIM-1", "broker": "simulation"},
    )


# --- Signal not found ---

def test_execute_signal_order_signal_not_found(db_session, org_and_account):
    from app.trading.order_executor import execute_signal_order
    org, _ = org_and_account
    result = execute_signal_order(signal_id=99999, organization_id=org.id)
    assert result["ok"] is False
    assert "not found" in result["error"]


# --- Signal not PENDING ---

def test_execute_signal_order_signal_not_pending(db_session, org_and_account):
    from app.trading.order_executor import execute_signal_order
    from app.models.signal import Signal, SignalStatus
    org, _ = org_and_account
    sig = _make_pending_signal(db_session, org.id)
    sig.status = SignalStatus.TRIGGERED
    db_session.commit()
    result = execute_signal_order(signal_id=sig.id, organization_id=org.id)
    assert result["ok"] is False
    assert "PENDING" in result["error"]


# --- Missing pivot/stop ---

def test_execute_signal_order_missing_pivot(db_session, org_and_account):
    from app.trading.order_executor import execute_signal_order
    from app.models.signal import Signal, SignalStatus
    org, _ = org_and_account
    sig = Signal(
        organization_id=org.id, ticker="BHP.AX", exchange_key="ASX",
        signal_date=__import__("datetime").date.today(), status=SignalStatus.PENDING,
        pivot_price=None, stop_price=42.0, close_price=44.0, rs_rating=80, trend_score=7,
    )
    db_session.add(sig)
    db_session.commit()
    result = execute_signal_order(signal_id=sig.id, organization_id=org.id)
    assert result["ok"] is False
    assert "pivot" in result["error"].lower() or "stop" in result["error"].lower()


# --- Over-extended price ---

def test_execute_signal_order_over_extended(db_session, org_and_account):
    from app.trading.order_executor import execute_signal_order
    org, _ = org_and_account
    sig = _make_pending_signal(db_session, org.id)
    # Force entry price 15% above pivot (45.0 * 1.15 = 51.75)
    result = execute_signal_order(
        signal_id=sig.id, organization_id=org.id, force_entry_price=52.0
    )
    assert result["ok"] is False
    assert "extended" in result["error"].lower() or "above pivot" in result["error"].lower()


# --- Happy path (equity, simulated broker) ---

def test_execute_signal_order_happy_path_equity(db_session, org_and_account, monkeypatch):
    from app.trading.order_executor import execute_signal_order
    from app.models.signal import Signal, SignalStatus
    from app.models.trade import Position, TradeStatus
    from app.risk.manager import SizingResult
    org, _ = org_and_account
    sig = _make_pending_signal(db_session, org.id)

    _patch_ibkr_simulate(monkeypatch)
    mock_notifier = MagicMock()
    monkeypatch.setattr("app.notifications.get_notifier", lambda organization_id=None: mock_notifier)
    # Ensure sizing returns a non-zero result (test DB has no rule configs)
    sizing = SizingResult(10, 10, 455.0, 420.0, 35.0, 350.0, 42.0, 45.5, "AUD", 1.0, "OK")
    monkeypatch.setattr("app.risk.manager.calculate_position_size", lambda **kw: sizing)

    result = execute_signal_order(
        signal_id=sig.id, organization_id=org.id,
        force_entry_price=45.5,
    )
    assert result["ok"] is True
    assert result["ticker"] == "BHP.AX"
    assert result["entry_price"] == 45.5

    # Signal should be TRIGGERED
    db_session.expire_all()
    refreshed = db_session.query(Signal).filter(Signal.id == sig.id).first()
    assert refreshed.status == SignalStatus.TRIGGERED

    # Position should be created
    pos = db_session.query(Position).filter(
        Position.organization_id == org.id, Position.ticker == "BHP.AX"
    ).first()
    assert pos is not None
    assert pos.status == TradeStatus.OPEN


# --- Happy path (crypto, simulated broker) ---

def test_execute_signal_order_happy_path_crypto(db_session, org_and_account, monkeypatch):
    from app.trading.order_executor import execute_signal_order
    from app.models.signal import Signal, SignalStatus
    from app.risk.manager import SizingResult
    org, _ = org_and_account
    sig = _make_pending_signal(db_session, org.id, ticker="BTC-AUD",
                               exchange_key="CRYPTO_INDEPENDENTRESERVE", asset_type="CRYPTO")
    sizing = SizingResult(0.1, 0.1, 4.5, 4.2, 0.3, 3.0, 42.0, 44.0, "AUD", 1.0, "OK")
    monkeypatch.setattr("app.risk.manager.calculate_position_size", lambda **kw: sizing)

    # Mock crypto broker
    mock_broker = MagicMock()
    mock_broker.__enter__ = lambda self: mock_broker
    mock_broker.__exit__ = MagicMock(return_value=False)
    mock_broker.submit_bracket_order.return_value = {
        "simulated": True, "order_id": "CCXT-1", "broker": "ccxt"
    }
    monkeypatch.setattr(
        "app.broker.crypto.get_crypto_broker_for_org",
        lambda org_id: mock_broker,
    )
    mock_notifier = MagicMock()
    monkeypatch.setattr("app.notifications.get_notifier", lambda organization_id=None: mock_notifier)

    result = execute_signal_order(
        signal_id=sig.id, organization_id=org.id, force_entry_price=44.0,
    )
    assert result["ok"] is True
    assert result["broker"] == "ccxt"


# --- Position size zero → error ---

def test_execute_signal_order_zero_size(db_session, org_and_account, monkeypatch):
    from app.trading.order_executor import execute_signal_order
    from app.risk.manager import SizingResult
    org, _ = org_and_account
    sig = _make_pending_signal(db_session, org.id)

    # Patch sizing to return 0 shares
    zero_result = SizingResult(0, 0, 0, 0, 0, 0, 42.0, 45.0, "AUD", 1.0, "Blocked")
    monkeypatch.setattr("app.risk.manager.calculate_position_size", lambda **kw: zero_result)

    result = execute_signal_order(
        signal_id=sig.id, organization_id=org.id, force_entry_price=45.0,
    )
    assert result["ok"] is False
    assert "zero" in result["error"].lower()


# --- Price fetch fallback path ---

def test_execute_signal_order_price_fetch_fallback(db_session, org_and_account, monkeypatch):
    """When get_intraday_price fails, fall back to close_price."""
    from app.trading.order_executor import execute_signal_order
    from app.risk.manager import SizingResult
    org, _ = org_and_account
    sig = _make_pending_signal(db_session, org.id)

    # Return no price from intraday
    monkeypatch.setattr("app.data.fetcher.get_intraday_price",
                        lambda *a, **kw: {"ok": False})
    sizing = SizingResult(10, 10, 450.0, 420.0, 30.0, 300.0, 42.0, 45.0, "AUD", 1.0, "OK")
    monkeypatch.setattr("app.risk.manager.calculate_position_size", lambda **kw: sizing)
    _patch_ibkr_simulate(monkeypatch)
    mock_notifier = MagicMock()
    monkeypatch.setattr("app.notifications.get_notifier", lambda organization_id=None: mock_notifier)

    result = execute_signal_order(signal_id=sig.id, organization_id=org.id)
    # Should succeed using close_price as entry
    assert result["ok"] is True


# --- Sizing exception path ---

def test_execute_signal_order_sizing_exception(db_session, org_and_account, monkeypatch):
    """When calculate_position_size raises, returns error."""
    from app.trading.order_executor import execute_signal_order
    org, _ = org_and_account
    sig = _make_pending_signal(db_session, org.id)

    monkeypatch.setattr("app.risk.manager.calculate_position_size",
                        lambda **kw: (_ for _ in ()).throw(Exception("Config missing")))

    result = execute_signal_order(
        signal_id=sig.id, organization_id=org.id, force_entry_price=45.0
    )
    assert result["ok"] is False
    assert "sizing" in result["error"].lower() or "failed" in result["error"].lower()


# --- Broker exception path ---

def test_execute_signal_order_broker_exception(db_session, org_and_account, monkeypatch):
    """When broker.submit_bracket_order raises, returns error."""
    from app.trading.order_executor import execute_signal_order
    from app.risk.manager import SizingResult
    org, _ = org_and_account
    sig = _make_pending_signal(db_session, org.id)

    sizing = SizingResult(10, 10, 450.0, 420.0, 30.0, 300.0, 42.0, 45.0, "AUD", 1.0, "OK")
    monkeypatch.setattr("app.risk.manager.calculate_position_size", lambda **kw: sizing)

    from app.broker.ibkr import IBKRBroker
    monkeypatch.setattr(IBKRBroker, "connect", lambda self: False)
    monkeypatch.setattr(IBKRBroker, "submit_bracket_order",
                        lambda self, **kw: (_ for _ in ()).throw(Exception("Connection reset")))

    result = execute_signal_order(
        signal_id=sig.id, organization_id=org.id, force_entry_price=45.0
    )
    assert result["ok"] is False
    assert "broker" in result["error"].lower() or "failed" in result["error"].lower()


# --- IBKRBroker.connect() must actually be called for equity orders ---

def test_execute_signal_order_equity_calls_connect(db_session, org_and_account, monkeypatch):
    """
    Regression test: the equity branch previously built a bare IBKRBroker()
    and called submit_bracket_order() directly with no connect() call, so
    is_connected was always False and every equity order silently simulated
    regardless of whether IBKR Gateway was actually up. Assert connect() is
    now actually invoked (via the `with IBKRBroker(...) as ibkr:` context
    manager, matching the crypto branch's pattern).
    """
    from app.trading.order_executor import execute_signal_order
    from app.risk.manager import SizingResult
    org, _ = org_and_account
    sig = _make_pending_signal(db_session, org.id)

    sizing = SizingResult(10, 10, 455.0, 420.0, 35.0, 350.0, 42.0, 45.5, "AUD", 1.0, "OK")
    monkeypatch.setattr("app.risk.manager.calculate_position_size", lambda **kw: sizing)

    from app.broker.ibkr import IBKRBroker
    connect_calls = []
    monkeypatch.setattr(IBKRBroker, "connect", lambda self: (connect_calls.append(1), False)[1])
    monkeypatch.setattr(
        IBKRBroker, "submit_bracket_order",
        lambda self, **kw: {"status": "simulated", "broker": "simulation", "ibkr_parent_id": None, "raw": []},
    )
    monkeypatch.setattr("app.notifications.get_notifier", lambda organization_id=None: MagicMock())

    result = execute_signal_order(
        signal_id=sig.id, organization_id=org.id, force_entry_price=45.5,
    )
    assert result["ok"] is True
    assert len(connect_calls) == 1


# --- Broker rejection (status=error) must NOT create a Position ---

def test_execute_signal_order_broker_rejection_no_position_created(db_session, org_and_account, monkeypatch):
    """
    Regression test: submit_bracket_order() catches its own exceptions and
    returns {"status": "error", ...} on rejection instead of raising — this
    does NOT hit the `except Exception` around the broker call, so previously
    execution fell straight through to Position creation and a Telegram
    "Order Placed" confirmation for an order the broker never actually
    accepted. Assert this now short-circuits with ok=False and no Position.
    """
    from app.trading.order_executor import execute_signal_order
    from app.models.trade import Position
    from app.risk.manager import SizingResult
    org, _ = org_and_account
    sig = _make_pending_signal(db_session, org.id)

    sizing = SizingResult(10, 10, 450.0, 420.0, 30.0, 300.0, 42.0, 45.0, "AUD", 1.0, "OK")
    monkeypatch.setattr("app.risk.manager.calculate_position_size", lambda **kw: sizing)

    from app.broker.ibkr import IBKRBroker
    monkeypatch.setattr(IBKRBroker, "connect", lambda self: True)
    monkeypatch.setattr(IBKRBroker, "is_connected", property(lambda self: True))
    monkeypatch.setattr(
        IBKRBroker, "submit_bracket_order",
        lambda self, **kw: {"status": "error", "error": "contract not qualified", "ticker": kw.get("ticker")},
    )

    result = execute_signal_order(
        signal_id=sig.id, organization_id=org.id, force_entry_price=45.0
    )
    assert result["ok"] is False
    assert "rejected" in result["error"].lower()

    pos = db_session.query(Position).filter(
        Position.organization_id == org.id, Position.ticker == "BHP.AX"
    ).first()
    assert pos is None
