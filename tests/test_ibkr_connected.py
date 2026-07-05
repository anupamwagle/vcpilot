"""Tests for app/broker/ibkr.py — connected broker paths (lines 177-214, 294-317)."""
import pytest
from unittest.mock import MagicMock, patch


def _make_connected_ibkr():
    from app.broker.ibkr import IBKRBroker
    b = IBKRBroker.__new__(IBKRBroker)
    b._ib = MagicMock()
    b._connected = True
    b.organization_id = None
    b.paper_mode = True
    b.port = 4002
    b.host = "127.0.0.1"
    b.client_id = 1
    b.account = ""
    return b, b._ib


# ────────────────────────────────────────────────────────────
# submit_bracket_order — connected success path
# ────────────────────────────────────────────────────────────

def test_submit_bracket_order_connected_success(monkeypatch):
    b, mock_ib = _make_connected_ibkr()

    mock_contract = MagicMock()
    monkeypatch.setattr("app.broker.ibkr.IB", MagicMock(), raising=False)

    # _build_contract returns a mock
    b._build_contract = lambda ticker, exchange_key="ASX": mock_contract
    mock_ib.qualifyContracts = MagicMock()
    mock_ib.bracketOrder.return_value = [MagicMock(), MagicMock(), MagicMock()]

    mock_trade = MagicMock()
    mock_trade.order.orderId = 42
    mock_ib.placeOrder.return_value = mock_trade
    mock_ib.sleep = MagicMock()

    result = b.submit_bracket_order(
        ticker="BHP",
        action="BUY",
        qty=100,
        entry_price=40.0,
        stop_price=38.0,
        target_price=48.0,
        exchange_key="ASX",
        order_ref="test-ref",
    )

    assert result["status"] == "submitted"
    assert result["ticker"] == "BHP"
    assert result["ibkr_parent_id"] == 42


# ────────────────────────────────────────────────────────────
# submit_bracket_order — BUY STOP-LIMIT entry (CLAUDE.md #39)
# ────────────────────────────────────────────────────────────

def test_submit_bracket_order_stop_limit_entry_for_buy_with_pivot(monkeypatch):
    b, mock_ib = _make_connected_ibkr()
    b._build_contract = lambda ticker, exchange_key="ASX": MagicMock()
    mock_ib.qualifyContracts = MagicMock()
    mock_ib.client.getReqId.side_effect = [101, 102, 103]

    mock_trade = MagicMock()
    mock_trade.order.orderId = 101
    mock_ib.placeOrder.return_value = mock_trade
    mock_ib.sleep = MagicMock()

    result = b.submit_bracket_order(
        ticker="BHP", action="BUY", qty=100,
        entry_price=40.15,   # confirmed breakout price, already above pivot
        stop_price=37.00, target_price=48.00,
        exchange_key="ASX", order_ref="astratrade-1",
        pivot_price=40.00, limit_buffer_pct=1.0,
    )

    assert result["status"] == "submitted"
    assert result["entry_order_type"] == "STP LMT"
    assert result["trigger_price"] == pytest.approx(40.15)
    assert result["limit_price"] == pytest.approx(40.55)   # 40.15 * 1.01, tick-rounded
    mock_ib.bracketOrder.assert_not_called()   # must NOT fall back to the plain-LMT helper

    placed_orders = [call.args[1] for call in mock_ib.placeOrder.call_args_list]
    parent = placed_orders[0]
    assert parent.orderType == "STP LMT"
    assert parent.action == "BUY"
    assert float(parent.auxPrice) == pytest.approx(40.15)   # stop trigger
    assert float(parent.lmtPrice) == pytest.approx(40.55)   # limit
    assert parent.transmit is False

    take_profit = placed_orders[1]
    assert take_profit.action == "SELL"
    assert float(take_profit.lmtPrice) == pytest.approx(48.00)
    assert take_profit.transmit is False

    stop_loss = placed_orders[2]
    assert stop_loss.action == "SELL"
    assert float(stop_loss.auxPrice) == pytest.approx(37.00)
    assert stop_loss.transmit is True


def test_submit_bracket_order_trigger_uses_pivot_when_higher_than_confirm_price(monkeypatch):
    """Stop trigger = max(pivot, confirm price) — a confirm price that reads
    below the pivot (e.g. a brief intrabar dip) must not pull the trigger
    below the pivot itself."""
    b, mock_ib = _make_connected_ibkr()
    b._build_contract = lambda ticker, exchange_key="ASX": MagicMock()
    mock_ib.qualifyContracts = MagicMock()
    mock_ib.client.getReqId.side_effect = [201, 202, 203]
    mock_trade = MagicMock()
    mock_trade.order.orderId = 201
    mock_ib.placeOrder.return_value = mock_trade
    mock_ib.sleep = MagicMock()

    result = b.submit_bracket_order(
        ticker="BHP", action="BUY", qty=100,
        entry_price=39.98,   # below pivot
        stop_price=37.00, target_price=48.00,
        exchange_key="ASX", order_ref="astratrade-2",
        pivot_price=40.00, limit_buffer_pct=1.0,
    )
    assert result["trigger_price"] == pytest.approx(40.00)


def test_submit_bracket_order_sell_action_never_uses_stop_limit(monkeypatch):
    """pivot_price must be ignored for SELL — exits always keep the plain LIMIT entry leg."""
    b, mock_ib = _make_connected_ibkr()
    b._build_contract = lambda ticker, exchange_key="ASX": MagicMock()
    mock_ib.qualifyContracts = MagicMock()
    mock_ib.bracketOrder.return_value = [MagicMock(), MagicMock(), MagicMock()]
    mock_trade = MagicMock()
    mock_trade.order.orderId = 55
    mock_ib.placeOrder.return_value = mock_trade
    mock_ib.sleep = MagicMock()

    result = b.submit_bracket_order(
        ticker="BHP", action="SELL", qty=100,
        entry_price=40.0, stop_price=0, target_price=0,
        exchange_key="ASX", order_ref="exit-1",
        pivot_price=40.00,   # even if passed, must be ignored for SELL
    )
    assert result["entry_order_type"] == "LMT"
    mock_ib.bracketOrder.assert_called_once()


def test_submit_bracket_order_connected_exception(monkeypatch):
    b, mock_ib = _make_connected_ibkr()
    b._build_contract = lambda ticker, exchange_key="ASX": MagicMock()
    mock_ib.qualifyContracts.side_effect = Exception("Connection reset")

    result = b.submit_bracket_order(
        ticker="BHP",
        action="BUY",
        qty=100,
        entry_price=40.0,
        stop_price=38.0,
        target_price=48.0,
        exchange_key="ASX",
        order_ref="test-ref",
    )

    assert result["status"] == "error"
    assert "Connection reset" in result["error"]


# ────────────────────────────────────────────────────────────
# get_market_snapshot — connected path
# ────────────────────────────────────────────────────────────

def test_get_market_snapshot_connected_returns_data(monkeypatch):
    b, mock_ib = _make_connected_ibkr()
    b._build_contract = lambda ticker, exchange_key="ASX": MagicMock()
    mock_ib.qualifyContracts = MagicMock()

    mock_ticker = MagicMock()
    mock_ticker.last = 42.5
    mock_ticker.close = 42.0
    mock_ticker.bid = 42.4
    mock_ticker.ask = 42.6
    mock_ticker.volume = 500000
    mock_ib.reqMktData.return_value = mock_ticker
    mock_ib.sleep = MagicMock()

    result = b.get_market_snapshot("BHP", "ASX")

    assert result is not None
    assert result["last"] == 42.5
    assert result["bid"] == 42.4


def test_get_market_snapshot_no_last_price(monkeypatch):
    b, mock_ib = _make_connected_ibkr()
    b._build_contract = lambda ticker, exchange_key="ASX": MagicMock()
    mock_ib.qualifyContracts = MagicMock()

    # No last/close AND no bid/ask (must be explicit — MagicMock's default
    # numeric magic methods return 1.0 for an unconfigured attribute, which
    # would otherwise be picked up by the bid/ask midpoint fallback below).
    mock_ticker = MagicMock()
    mock_ticker.last = None
    mock_ticker.close = None
    mock_ticker.bid = None
    mock_ticker.ask = None
    mock_ib.reqMktData.return_value = mock_ticker
    mock_ib.sleep = MagicMock()

    result = b.get_market_snapshot("BHP", "ASX")
    assert result is None


def test_get_market_snapshot_falls_back_to_bid_ask_midpoint(monkeypatch):
    """Thin ASX names can have live bid/ask with no last trade — use the midpoint."""
    b, mock_ib = _make_connected_ibkr()
    b._build_contract = lambda ticker, exchange_key="ASX": MagicMock()
    mock_ib.qualifyContracts = MagicMock()

    mock_ticker = MagicMock()
    mock_ticker.last = None
    mock_ticker.close = None
    mock_ticker.bid = 10.0
    mock_ticker.ask = 10.20
    mock_ticker.volume = 0
    mock_ib.reqMktData.return_value = mock_ticker
    mock_ib.sleep = MagicMock()

    result = b.get_market_snapshot("BHP", "ASX")
    assert result is not None
    assert result["last"] == pytest.approx(10.10)


def test_get_market_snapshot_retries_delayed_when_live_unavailable(monkeypatch):
    """No live data at all -> retry with delayed (reqMarketDataType 3) before giving up."""
    b, mock_ib = _make_connected_ibkr()
    b._build_contract = lambda ticker, exchange_key="ASX": MagicMock()
    mock_ib.qualifyContracts = MagicMock()
    mock_ib.sleep = MagicMock()

    live_ticker = MagicMock()
    live_ticker.last = None
    live_ticker.close = None
    live_ticker.bid = None
    live_ticker.ask = None

    delayed_ticker = MagicMock()
    delayed_ticker.last = 42.5
    delayed_ticker.close = 42.0
    delayed_ticker.bid = 42.4
    delayed_ticker.ask = 42.6
    delayed_ticker.volume = 1000

    data_types_requested = []
    mock_ib.reqMarketDataType.side_effect = lambda t: data_types_requested.append(t)
    mock_ib.reqMktData.side_effect = [live_ticker, delayed_ticker]

    result = b.get_market_snapshot("BHP", "ASX")

    assert result is not None
    assert result["last"] == 42.5
    assert result["delayed"] is True
    assert data_types_requested == [1, 3], "Must try live (1) before falling back to delayed (3)"


def test_get_market_snapshot_exception_returns_none():
    b, mock_ib = _make_connected_ibkr()
    b._build_contract = lambda ticker, exchange_key="ASX": MagicMock()
    mock_ib.qualifyContracts.side_effect = Exception("Lost connection")

    result = b.get_market_snapshot("BHP", "ASX")
    assert result is None


# ────────────────────────────────────────────────────────────
# get_account_summary — connected path
# ────────────────────────────────────────────────────────────

def test_get_account_summary_connected():
    b, mock_ib = _make_connected_ibkr()
    mock_value = MagicMock()
    mock_value.tag = "NetLiquidation"
    mock_value.value = "50000.0"
    mock_value.currency = "AUD"
    mock_ib.accountSummary.return_value = [mock_value]

    result = b.get_account_summary()
    assert "NetLiquidation" in result


# ────────────────────────────────────────────────────────────
# get_open_positions — connected path
# ────────────────────────────────────────────────────────────

def test_get_open_positions_connected():
    b, mock_ib = _make_connected_ibkr()

    mock_pos = MagicMock()
    mock_pos.contract.symbol = "BHP"
    mock_pos.contract.secType = "STK"
    mock_pos.contract.currency = "AUD"
    mock_pos.position = 100
    mock_pos.avgCost = 40.0
    mock_ib.positions.return_value = [mock_pos]

    result = b.get_open_positions()
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["ticker"] == "BHP"


# ────────────────────────────────────────────────────────────
# cancel_order — connected path
# ────────────────────────────────────────────────────────────

def test_cancel_order_connected_found():
    b, mock_ib = _make_connected_ibkr()

    mock_trade = MagicMock()
    mock_trade.order.orderId = 99
    mock_ib.openTrades.return_value = [mock_trade]
    mock_ib.cancelOrder = MagicMock()

    result = b.cancel_order(99)
    assert result is True


def test_cancel_order_connected_not_found():
    b, mock_ib = _make_connected_ibkr()
    mock_ib.openTrades.return_value = []

    result = b.cancel_order(99)
    assert result is False
