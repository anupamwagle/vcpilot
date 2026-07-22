"""Tests for app/broker/crypto.py — CryptoBroker and helpers."""
import pytest
from unittest.mock import MagicMock, patch


# --- _yfinance_to_ccxt ---

def test_yfinance_to_ccxt_binance_btc():
    from app.broker.crypto import _yfinance_to_ccxt
    assert _yfinance_to_ccxt("BTC-USD", "binance") == "BTC/USDT"


def test_yfinance_to_ccxt_ir_btc():
    from app.broker.crypto import _yfinance_to_ccxt
    # IR maps BTC → XBT
    assert _yfinance_to_ccxt("BTC-AUD", "independentreserve") == "XBT/AUD"


def test_yfinance_to_ccxt_ir_eth():
    from app.broker.crypto import _yfinance_to_ccxt
    assert _yfinance_to_ccxt("ETH-AUD", "independentreserve") == "ETH/AUD"


def test_yfinance_to_ccxt_eth_usd_binance():
    from app.broker.crypto import _yfinance_to_ccxt
    assert _yfinance_to_ccxt("ETH-USD", "binance") == "ETH/USDT"


def test_yfinance_to_ccxt_passthrough():
    from app.broker.crypto import _yfinance_to_ccxt
    assert _yfinance_to_ccxt("SOMETHING", "") == "SOMETHING"


# --- _simulate_crypto_order ---

def test_simulate_crypto_order_returns_simulated_status():
    from app.broker.crypto import _simulate_crypto_order
    result = _simulate_crypto_order("BTC-AUD", "BUY", 0.01, 90000.0, 85000.0, "test_ref")
    assert result["status"] == "simulated"
    assert result["ticker"] == "BTC-AUD"
    assert result["qty"] == 0.01
    assert result["broker"] == "simulation"
    assert "entry_order_id" in result
    assert result["entry_order_id"].startswith("SIM_")


# --- CryptoBroker instantiation ---

def test_crypto_broker_no_credentials_not_connected():
    from app.broker.crypto import CryptoBroker
    b = CryptoBroker(ccxt_provider="binance", api_key="", api_secret="")
    connected = b.connect()
    assert connected is False
    assert b.is_connected is False


def test_crypto_broker_connect_no_ccxt():
    """When ccxt is unavailable, connect returns False."""
    from app.broker import crypto as crypto_mod
    orig = crypto_mod.CCXT_AVAILABLE
    try:
        crypto_mod.CCXT_AVAILABLE = False
        from app.broker.crypto import CryptoBroker
        b = CryptoBroker.__new__(CryptoBroker)
        b.ccxt_provider = "binance"
        b.api_key = "key"
        b.api_secret = "secret"
        b.testnet = True
        b.organization_id = None
        b._exchange = None
        b._connected = False
        assert b.connect() is False
    finally:
        crypto_mod.CCXT_AVAILABLE = orig


def test_crypto_broker_context_manager_calls_connect_and_disconnect():
    from app.broker.crypto import CryptoBroker
    b = CryptoBroker(ccxt_provider="binance", api_key="", api_secret="")
    with b as broker:
        assert broker is b
    assert b._connected is False


def test_crypto_broker_submit_bracket_order_simulation():
    """With no credentials, submit_bracket_order returns simulated result."""
    from app.broker.crypto import CryptoBroker
    b = CryptoBroker(ccxt_provider="binance", api_key="", api_secret="")
    result = b.submit_bracket_order("BTC-USD", "BUY", 0.1, 90000, 85000, 95000)
    assert result["status"] == "simulated"
    assert result["ticker"] == "BTC-USD"


def test_crypto_broker_get_balance_not_connected():
    from app.broker.crypto import CryptoBroker
    b = CryptoBroker(ccxt_provider="binance", api_key="", api_secret="")
    assert b.get_balance() == {}


def test_crypto_broker_get_usd_balance_not_connected():
    from app.broker.crypto import CryptoBroker
    b = CryptoBroker(ccxt_provider="binance", api_key="", api_secret="")
    assert b.get_usd_balance() == 0.0


def test_crypto_broker_get_open_orders_not_connected():
    from app.broker.crypto import CryptoBroker
    b = CryptoBroker(ccxt_provider="binance", api_key="", api_secret="")
    assert b.get_open_orders() == []


def test_crypto_broker_cancel_order_not_connected():
    from app.broker.crypto import CryptoBroker
    b = CryptoBroker(ccxt_provider="binance", api_key="", api_secret="")
    assert b.cancel_order("order123", "BTC-USD") is False


def test_crypto_broker_get_positions_not_connected():
    from app.broker.crypto import CryptoBroker
    b = CryptoBroker(ccxt_provider="binance", api_key="", api_secret="")
    assert b.get_positions() == []


def test_crypto_broker_get_market_snapshot_not_connected():
    from app.broker.crypto import CryptoBroker
    b = CryptoBroker(ccxt_provider="binance", api_key="", api_secret="")
    assert b.get_market_snapshot("BTC-USD") is None


# --- Connected broker (mock ccxt exchange) ---

def _make_connected_broker():
    """Create a CryptoBroker with a mocked ccxt exchange."""
    from app.broker.crypto import CryptoBroker
    b = CryptoBroker.__new__(CryptoBroker)
    b.ccxt_provider = "binance"
    b.api_key = "key"
    b.api_secret = "secret"
    b.testnet = False
    b.organization_id = None
    mock_exchange = MagicMock()
    b._exchange = mock_exchange
    b._connected = True
    return b, mock_exchange


def test_get_balance_connected():
    b, mock_ex = _make_connected_broker()
    mock_ex.fetch_balance.return_value = {"total": {"BTC": 0.5, "USDT": 1000.0, "ETH": 0.0}}
    bal = b.get_balance()
    assert "BTC" in bal
    assert "USDT" in bal


def test_get_usd_balance_usdt():
    b, mock_ex = _make_connected_broker()
    mock_ex.fetch_balance.return_value = {"total": {"USDT": 2500.0}}
    assert b.get_usd_balance() == 2500.0


def test_get_positions_connected():
    b, mock_ex = _make_connected_broker()
    mock_ex.fetch_balance.return_value = {
        "total": {"BTC": 0.1, "USDT": 1000.0, "ETH": 0.5}
    }
    positions = b.get_positions()
    tickers = [p["ticker"] for p in positions]
    assert any("BTC" in t for t in tickers)
    assert any("ETH" in t for t in tickers)
    # Stablecoins excluded
    assert not any("USDT" in t for t in tickers)


def test_get_market_snapshot_connected():
    b, mock_ex = _make_connected_broker()
    mock_ex.fetch_ticker.return_value = {
        "last": 90000.0, "bid": 89999.0, "ask": 90001.0, "baseVolume": 1234.5
    }
    snap = b.get_market_snapshot("BTC-USD")
    assert snap is not None
    assert snap["last"] == 90000.0
    assert snap["data_source"] == "ccxt"


def test_get_market_snapshot_exception_returns_none():
    b, mock_ex = _make_connected_broker()
    mock_ex.fetch_ticker.side_effect = Exception("Network error")
    assert b.get_market_snapshot("BTC-USD") is None


def test_submit_bracket_order_connected():
    b, mock_ex = _make_connected_broker()
    mock_ex.create_limit_order.return_value = {"id": "ORD123"}
    result = b.submit_bracket_order("BTC-USD", "BUY", 0.1, 90000, 85000, 95000)
    assert result["status"] == "submitted"
    assert result["entry_order_id"] == "ORD123"
    assert result["protection_pending"] is True
    # No SELL leg may exist before the entry has actually filled.
    mock_ex.create_order.assert_not_called()


def test_submit_protective_stop_connected():
    b, mock_ex = _make_connected_broker()
    mock_ex.create_order.return_value = {"id": "SL456"}

    result = b.submit_protective_stop("BTC-USD", 0.1, 85000, "protect-1")

    assert result["status"] == "submitted"
    assert result["entry_order_id"] == "SL456"
    assert mock_ex.create_order.call_args.kwargs["side"] == "sell"


def test_submit_bracket_order_entry_fails_returns_error():
    b, mock_ex = _make_connected_broker()
    mock_ex.create_limit_order.side_effect = Exception("Insufficient funds")
    result = b.submit_bracket_order("BTC-USD", "BUY", 0.1, 90000, 85000, 95000)
    assert result["status"] == "error"
    assert "Insufficient funds" in result["error"]


def test_factory_explicit_exchange_key_wins_over_org_default(db_session, org_and_account):
    """Each signal must route to its own active crypto venue, not the org default."""
    from app.broker.crypto import get_crypto_broker_for_org
    from app.models.config import SystemConfig

    org, _ = org_and_account
    db_session.add(SystemConfig(
        key="crypto_exchange_key", organization_id=org.id,
        value="CRYPTO_INDEPENDENTRESERVE",
    ))
    db_session.commit()

    broker = get_crypto_broker_for_org(org.id, exchange_key="CRYPTO_MEXC")
    assert broker.ccxt_provider == "mexc"


def test_get_open_orders_connected():
    b, mock_ex = _make_connected_broker()
    mock_ex.fetch_open_orders.return_value = [{"id": "O1"}, {"id": "O2"}]
    orders = b.get_open_orders()
    assert len(orders) == 2


def test_cancel_order_connected():
    b, mock_ex = _make_connected_broker()
    mock_ex.cancel_order.return_value = True
    result = b.cancel_order("O1", "BTC-USD")
    assert result is True


def test_cancel_order_exception():
    b, mock_ex = _make_connected_broker()
    mock_ex.cancel_order.side_effect = Exception("Order not found")
    assert b.cancel_order("O1", "BTC-USD") is False


# --- get_crypto_broker_for_org ---

def test_get_crypto_broker_for_org_returns_instance(db_session, org_and_account):
    from app.broker.crypto import get_crypto_broker_for_org
    org, _ = org_and_account
    broker = get_crypto_broker_for_org(org.id)
    assert broker is not None
    # Without credentials, should not be connected
    assert broker.is_connected is False
