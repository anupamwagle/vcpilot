"""Tests for app/broker/ibkr.py — IBKRBroker simulation path."""
import pytest
from unittest.mock import MagicMock, patch


# --- _simulate_order helper ---

def test_simulate_order_returns_simulated_status():
    from app.broker.ibkr import _simulate_order
    result = _simulate_order("BHP.AX", "BUY", 100, 25.50, 23.00, "ref1")
    assert result["status"] == "simulated"
    assert result["ticker"] == "BHP.AX"
    assert result["qty"] == 100
    assert "ibkr_parent_id" in result


# --- IBKRBroker instantiation ---

def test_ibkr_broker_creates_without_error():
    from app.broker.ibkr import IBKRBroker
    b = IBKRBroker()
    assert b is not None
    assert b._connected is False


def test_ibkr_broker_with_org_id_loads_from_db(db_session, org_and_account):
    from app.broker.ibkr import IBKRBroker
    org, _ = org_and_account
    # Should not raise even if no SystemConfig rows exist
    b = IBKRBroker(organization_id=org.id)
    assert b is not None


# --- connect / is_connected ---

def test_ibkr_connect_without_ib_insync_returns_false():
    """Without ib_insync installed or in simulate mode, connect() returns False."""
    from app.broker.ibkr import IBKRBroker
    b = IBKRBroker()
    # IBKR simulation is always active in test environment (no IBKR gateway)
    result = b.connect()
    assert result is False
    assert b.is_connected is False


def test_ibkr_context_manager():
    from app.broker.ibkr import IBKRBroker
    with IBKRBroker() as b:
        assert b is not None
    assert b._connected is False


# --- Simulation-mode methods (not connected) ---

def test_submit_bracket_order_simulation():
    from app.broker.ibkr import IBKRBroker
    b = IBKRBroker()
    result = b.submit_bracket_order("BHP.AX", "BUY", 100, 25.50, 23.00, 28.00)
    assert result["status"] == "simulated"
    assert result["ticker"] == "BHP.AX"


def test_cancel_order_simulation_returns_true():
    from app.broker.ibkr import IBKRBroker
    b = IBKRBroker()
    result = b.cancel_order(12345)
    assert result is True


def test_get_open_positions_not_connected():
    from app.broker.ibkr import IBKRBroker
    b = IBKRBroker()
    assert b.get_open_positions() == []


def test_get_open_orders_not_connected():
    from app.broker.ibkr import IBKRBroker
    b = IBKRBroker()
    assert b.get_open_orders() == []


def test_get_market_snapshot_not_connected():
    from app.broker.ibkr import IBKRBroker
    b = IBKRBroker()
    assert b.get_market_snapshot("BHP.AX") is None


def test_get_account_summary_not_connected():
    from app.broker.ibkr import IBKRBroker
    b = IBKRBroker()
    assert b.get_account_summary() == {}


def test_get_net_liquidation_not_connected():
    from app.broker.ibkr import IBKRBroker
    b = IBKRBroker()
    # Not connected → get_account_summary returns {} → NetLiquidation default 0
    val = b.get_net_liquidation()
    assert val == 0.0 or val is None


# --- _build_contract (when IB not available returns None) ---

def test_build_contract_ib_unavailable():
    from app.broker import ibkr as ibkr_mod
    orig = ibkr_mod.IB_AVAILABLE
    try:
        ibkr_mod.IB_AVAILABLE = False
        from app.broker.ibkr import IBKRBroker
        b = IBKRBroker.__new__(IBKRBroker)
        b.organization_id = None
        b._ib = None
        b._connected = False
        result = b._build_contract("BHP.AX", "ASX")
        assert result is None
    finally:
        ibkr_mod.IB_AVAILABLE = orig


# --- disconnect ---

def test_disconnect_when_not_connected_is_noop():
    from app.broker.ibkr import IBKRBroker
    b = IBKRBroker()
    b.disconnect()  # Should not raise
    assert b._connected is False
