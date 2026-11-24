"""Regression tests for broker-paper isolation and the authenticated MCP transport."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


def test_disconnection_never_fakes_paper_fill(monkeypatch):
    from app.broker.ibkr import IBKRBroker, settings
    monkeypatch.setattr(settings, "ibkr_simulate", False)
    broker = IBKRBroker()
    for result in (
        broker.submit_bracket_order("BHP.AX", "BUY", 1, 40, 38, 48, simulate_on_disconnect=True),
        broker.submit_market_buy("BHP.AX", 1, simulate_on_disconnect=True),
        broker.submit_market_sell("BHP.AX", 1, simulate_on_disconnect=True),
    ):
        assert result["status"] == "error"
    assert broker.cancel_order(12)[0] is False


def test_connection_does_not_try_live_port_or_random_client(monkeypatch):
    import app.broker.ibkr as module
    broker = module.IBKRBroker()
    broker.account, broker.paper_mode, broker.port, broker.client_id = "DU123", True, 4004, 7
    broker._last_fail_times.clear()
    ib = MagicMock()
    ib.connect.side_effect = TimeoutError()
    monkeypatch.setattr(module, "IB", lambda: ib)
    monkeypatch.setattr(module.settings, "ibkr_simulate", False)
    assert broker.connect() is False
    assert ib.connect.call_count == 1
    assert ib.connect.call_args.kwargs["port"] == 4004
    assert ib.connect.call_args.kwargs["clientId"] == 7
    broker._last_fail_times.clear()


@pytest.mark.parametrize("managed", [["U123"], ["DU999"], []])
def test_connection_rejects_wrong_managed_account(monkeypatch, managed):
    import app.broker.ibkr as module
    broker = module.IBKRBroker()
    broker.account, broker.paper_mode, broker.port = "DU123", True, 4004
    broker._last_fail_times.clear()
    ib = MagicMock()
    ib.managedAccounts.return_value = managed
    monkeypatch.setattr(module, "IB", lambda: ib)
    monkeypatch.setattr(module.settings, "ibkr_simulate", False)
    assert broker.connect() is False
    assert not broker.is_connected
    ib.disconnect.assert_called()
    broker._last_fail_times.clear()


def test_live_mode_rejected_before_gateway_connection(monkeypatch):
    import app.broker.ibkr as module
    broker = module.IBKRBroker()
    broker.paper_mode = False
    factory = MagicMock()
    monkeypatch.setattr(module, "IB", factory)
    assert broker.connect() is False
    factory.assert_not_called()


def test_lock_backend_failure_blocks_execution(monkeypatch):
    import redis
    from app.tasks.trading import _acquire_org_lock
    monkeypatch.setattr(redis, "from_url", MagicMock(side_effect=ConnectionError()))
    assert _acquire_org_lock("test-only") is False


def credential(db, org):
    from app.models.mcp import MCPCredential
    from app.mcp.auth import create_access_token
    cred = MCPCredential(organization_id=org.id, name="test", client_id="test-paper-client",
                         client_secret_hash="unused", client_secret_preview="unused",
                         scopes=["trading:read"], expires_at=datetime.utcnow() + timedelta(days=1), is_active=True)
    db.add(cred)
    db.commit()
    return cred, create_access_token(org.id, ["trading:read", "trading:write"], cred.id, cred.client_id)


def test_mcp_streamable_http_checks_scopes_and_revocation(db_session, org_and_account):
    from starlette.testclient import TestClient
    from app.mcp.server import create_mcp_app
    cred, token = credential(db_session, org_and_account[0])
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    with TestClient(create_mcp_app(), base_url="http://localhost") as client:
        response = client.post("/", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "paper-test", "version": "1"}}})
        assert response.status_code == 200, response.text
        listed = client.post("/", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert "place_order" in {tool["name"] for tool in listed.json()["result"]["tools"]}
        read = client.post("/", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_positions", "arguments": {}}})
        assert not read.json()["result"].get("isError"), read.text
        write = client.post("/", headers=headers, json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "place_order", "arguments": {"signal_id": 999}}})
        assert write.json()["result"]["isError"] is True
        cred.is_active = False
        db_session.commit()
        assert client.post("/", headers=headers, json={}).status_code == 401


def test_mcp_database_failure_denies_request(db_session, org_and_account, monkeypatch):
    from starlette.testclient import TestClient
    from app.mcp.server import create_mcp_app
    import app.database
    _, token = credential(db_session, org_and_account[0])
    monkeypatch.setattr(app.database, "get_db", MagicMock(side_effect=ConnectionError()))
    with TestClient(create_mcp_app()) as client:
        assert client.post("/", headers={"Authorization": f"Bearer {token}"}, json={}).status_code == 503


def test_recovery_restricts_even_write_scoped_credentials(db_session, org_and_account, monkeypatch):
    from starlette.testclient import TestClient
    from app.mcp.server import create_mcp_app
    cred, token = credential(db_session, org_and_account[0])
    cred.scopes = ["trading:read", "trading:write"]
    db_session.commit()
    monkeypatch.setenv("ASTRA_RECOVERY_MODE", "true")
    with TestClient(create_mcp_app(), base_url="http://localhost") as client:
        result = client.post("/", headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"},
                             json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "resume_trading", "arguments": {}}})
        assert result.json()["result"]["isError"] is True


def test_order_intent_survives_lost_broker_reply(db_session, org_and_account, monkeypatch):
    from app.models.signal import Signal, SignalStatus
    from app.models.trade import Order, Position
    from app.trading.order_executor import execute_signal_order
    from app.broker.ibkr import IBKRBroker
    org, _ = org_and_account
    signal = Signal(organization_id=org.id, ticker="BHP.AX", exchange_key="ASX", asset_type="EQUITY",
                    signal_date=datetime.utcnow().date(), pivot_price=40, stop_price=38, target_price_1=48,
                    status=SignalStatus.PENDING)
    db_session.add(signal)
    db_session.commit()
    monkeypatch.setattr("app.risk.manager.calculate_position_size", lambda **kw: SimpleNamespace(shares=1))
    monkeypatch.setattr(IBKRBroker, "connect", lambda self: False)
    submit = MagicMock(side_effect=TimeoutError("reply lost after submission"))
    monkeypatch.setattr(IBKRBroker, "submit_bracket_order", submit)
    assert execute_signal_order(signal.id, org.id, force_entry_price=40)["ok"] is False
    assert execute_signal_order(signal.id, org.id, force_entry_price=40)["ok"] is False
    assert submit.call_count == 1
    assert db_session.query(Order).count() == 1
    assert db_session.query(Position).count() == 0


@pytest.mark.parametrize("price", [float("nan"), float("inf"), -1, 0])
def test_invalid_prices_never_reach_broker(db_session, org_and_account, monkeypatch, price):
    from app.models.signal import Signal, SignalStatus
    from app.trading.order_executor import execute_signal_order
    from app.broker.ibkr import IBKRBroker
    org, _ = org_and_account
    signal = Signal(organization_id=org.id, ticker="BHP.AX", signal_date=datetime.utcnow().date(),
                    pivot_price=40, stop_price=38, target_price_1=48, status=SignalStatus.PENDING)
    db_session.add(signal)
    db_session.commit()
    submit = MagicMock()
    monkeypatch.setattr(IBKRBroker, "submit_bracket_order", submit)
    assert execute_signal_order(signal.id, org.id, force_entry_price=price)["ok"] is False
    submit.assert_not_called()
