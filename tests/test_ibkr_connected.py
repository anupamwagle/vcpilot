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

    mock_ticker = MagicMock()
    mock_ticker.last = None
    mock_ticker.close = None
    mock_ib.reqMktData.return_value = mock_ticker
    mock_ib.sleep = MagicMock()

    result = b.get_market_snapshot("BHP", "ASX")
    assert result is None


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
