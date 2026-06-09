"""
Regression test for the live MCP crash:
  get_positions(include_closed=True) -> "Error executing tool get_positions:
  type object 'Trade' has no attribute 'closed_at'"

Root cause: the tool queried/serialised non-existent columns
(`Trade.closed_at`, `Trade.realised_pnl`, `Position.stop_price`, `Position.target_price`,
`Position.opened_at`, `Position.pnl_pct`) — the real columns are
`Trade.exit_date` / `Trade.net_pnl_aud`, `Position.current_stop` / `Position.target_1` /
`Position.entry_date` / `Position.unrealised_pct`. Any agent or admin asking "show me
my closed trades" got a hard error instead of data — exactly the kind of thing that
must not happen once real money is on the line.
"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from app.models.trade import Position, Trade, TradeStatus, ExitReason


def _fake_mcp_ctx(org_id, client_id="test-client"):
    return SimpleNamespace(org_id=org_id, client_id=client_id)


def test_get_positions_with_include_closed_does_not_crash(db_session, org_and_account, monkeypatch):
    import app.mcp.tools as mcp_tools

    org, account = org_and_account
    monkeypatch.setattr(mcp_tools, "get_mcp_context", lambda: _fake_mcp_ctx(org.id))
    monkeypatch.setattr(mcp_tools, "assert_scope", lambda *_a, **_kw: None)

    # One open position
    open_pos = Position(
        ticker="BTC-AUD", exchange_key="CRYPTO_INDEPENDENTRESERVE", asset_type="CRYPTO",
        currency="AUD", account_id=account.id, organization_id=org.id,
        entry_date=date(2026, 6, 1), entry_price=90000.0, qty=0.005,
        initial_stop=81000.0, current_stop=81000.0, current_price=92000.0,
        unrealised_pnl=10.0, unrealised_pct=2.2, target_1=108000.0,
        status=TradeStatus.OPEN, is_paper=True,
    )
    db_session.add(open_pos)

    # One recently-closed trade
    closed_trade = Trade(
        ticker="DOGE-AUD", exchange_key="CRYPTO_INDEPENDENTRESERVE", asset_type="CRYPTO",
        currency="AUD", account_id=account.id, organization_id=org.id,
        entry_date=date(2026, 5, 28), exit_date=date(2026, 6, 2), hold_days=5,
        entry_price=0.40, exit_price=0.36, qty=1000,
        gross_pnl_aud=-40.0, net_pnl_aud=-40.0, pnl_pct=-10.0,
        initial_stop=0.34, exit_reason=ExitReason.STOP_LOSS, is_paper=True,
        cgt_eligible_discount=False, created_at=datetime.utcnow() - timedelta(days=6),
    )
    db_session.add(closed_trade)
    db_session.commit()

    result = mcp_tools.get_positions(include_closed=True)

    assert "error" not in result, f"get_positions must not error, got: {result}"
    assert result["open_count"] == 1
    assert len(result["open"]) == 1
    assert result["open"][0]["ticker"] == "BTC-AUD"
    assert result["open"][0]["stop_price"] == 81000.0
    assert result["open"][0]["target_price"] == 108000.0

    assert len(result["closed"]) == 1
    closed = result["closed"][0]
    assert closed["ticker"] == "DOGE-AUD"
    assert closed["realised_pnl"] == -40.0
    assert closed["exit_reason"] == "STOP_LOSS"
    assert closed["closed_at"] == "2026-06-02"


def test_get_positions_excludes_old_closed_trades(db_session, org_and_account, monkeypatch):
    """Only trades created within the last 30 days should appear in `closed`."""
    import app.mcp.tools as mcp_tools

    org, account = org_and_account
    monkeypatch.setattr(mcp_tools, "get_mcp_context", lambda: _fake_mcp_ctx(org.id))
    monkeypatch.setattr(mcp_tools, "assert_scope", lambda *_a, **_kw: None)

    old_trade = Trade(
        ticker="XRP-AUD", exchange_key="CRYPTO_INDEPENDENTRESERVE", asset_type="CRYPTO",
        currency="AUD", account_id=account.id, organization_id=org.id,
        entry_date=date(2026, 3, 1), exit_date=date(2026, 3, 10), hold_days=9,
        entry_price=1.0, exit_price=0.9, qty=100,
        gross_pnl_aud=-10.0, net_pnl_aud=-10.0, pnl_pct=-10.0,
        initial_stop=0.85, exit_reason=ExitReason.STOP_LOSS, is_paper=True,
        cgt_eligible_discount=False, created_at=datetime.utcnow() - timedelta(days=90),
    )
    db_session.add(old_trade)
    db_session.commit()

    result = mcp_tools.get_positions(include_closed=True)
    assert result["closed"] == []
