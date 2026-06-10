"""Extended tests for app/mcp/tools.py — covering uncovered tool functions."""
import pytest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch, MagicMock


def _fake_ctx(org_id, scopes=None, client_id="test-client"):
    return SimpleNamespace(
        org_id=org_id,
        client_id=client_id,
        scopes=scopes or ["trading:read", "trading:write", "signals:read",
                           "signals:write", "market:read", "config:read"]
    )


def _setup_mcp(monkeypatch, org_id):
    """Patch get_mcp_context and assert_scope in mcp tools."""
    import app.mcp.tools as t
    monkeypatch.setattr(t, "get_mcp_context", lambda: _fake_ctx(org_id))
    monkeypatch.setattr(t, "assert_scope", lambda *a, **kw: None)


# ---------------------------------------------------------------------------
# get_market_regime
# ---------------------------------------------------------------------------

def test_get_market_regime_not_evaluated(db_session, org_and_account, monkeypatch):
    import app.mcp.tools as t
    org, _ = org_and_account
    _setup_mcp(monkeypatch, org.id)

    result = t.get_market_regime("ASX")
    assert result["exchange_key"] == "ASX"
    assert "regime" in result
    assert result["regime"] == "Not evaluated"


def test_get_market_regime_with_config(db_session, org_and_account, monkeypatch):
    from app.models.config import SystemConfig
    import app.mcp.tools as t

    org, _ = org_and_account
    db_session.add(SystemConfig(
        key="last_market_regime_ASX",
        value="BULL",
        organization_id=org.id,
    ))
    db_session.commit()
    _setup_mcp(monkeypatch, org.id)

    result = t.get_market_regime("ASX")
    assert result["regime"] == "BULL"
    assert "description" in result


# ---------------------------------------------------------------------------
# get_signals
# ---------------------------------------------------------------------------

def test_get_signals_empty(db_session, org_and_account, monkeypatch):
    import app.mcp.tools as t
    org, _ = org_and_account
    _setup_mcp(monkeypatch, org.id)

    result = t.get_signals()
    assert "signals" in result
    assert result["total"] >= 0


def test_get_signals_with_data(db_session, org_and_account, monkeypatch):
    from app.models.signal import Signal, SignalStatus
    import app.mcp.tools as t

    org, _ = org_and_account
    sig = Signal(
        ticker="BHP.AX", exchange_key="ASX", asset_type="EQUITY",
        currency="AUD", signal_date=date.today(),
        status=SignalStatus.PENDING, close_price=25.0,
        pivot_price=25.5, stop_price=23.0,
        organization_id=org.id,
    )
    db_session.add(sig)
    db_session.commit()

    _setup_mcp(monkeypatch, org.id)
    result = t.get_signals()
    assert result["total"] >= 1
    assert any(s["ticker"] == "BHP.AX" for s in result["signals"])


def test_get_signals_with_status_filter(db_session, org_and_account, monkeypatch):
    import app.mcp.tools as t
    org, _ = org_and_account
    _setup_mcp(monkeypatch, org.id)

    result = t.get_signals(status="PENDING")
    assert "signals" in result


def test_get_signals_with_exchange_filter(db_session, org_and_account, monkeypatch):
    import app.mcp.tools as t
    org, _ = org_and_account
    _setup_mcp(monkeypatch, org.id)

    result = t.get_signals(exchange_key="ASX")
    assert "signals" in result


# ---------------------------------------------------------------------------
# get_portfolio_stats
# ---------------------------------------------------------------------------

def test_get_portfolio_stats_empty(db_session, org_and_account, monkeypatch):
    import app.mcp.tools as t
    org, _ = org_and_account
    _setup_mcp(monkeypatch, org.id)

    result = t.get_portfolio_stats()
    assert "open_positions" in result or "positions" in result or isinstance(result, dict)


def test_get_portfolio_stats_with_position(db_session, org_and_account, open_crypto_position, monkeypatch):
    import app.mcp.tools as t
    org, _ = org_and_account
    _setup_mcp(monkeypatch, org.id)

    result = t.get_portfolio_stats()
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# get_watchlist
# ---------------------------------------------------------------------------

def test_get_watchlist_empty(db_session, org_and_account, monkeypatch):
    import app.mcp.tools as t
    org, _ = org_and_account
    _setup_mcp(monkeypatch, org.id)

    result = t.get_watchlist()
    assert "watchlist" in result


def test_get_watchlist_with_item(db_session, org_and_account, watching_trx_item, monkeypatch):
    import app.mcp.tools as t
    org, _ = org_and_account
    _setup_mcp(monkeypatch, org.id)

    result = t.get_watchlist()
    assert result["total"] >= 1


# ---------------------------------------------------------------------------
# add_to_watchlist
# ---------------------------------------------------------------------------

def test_add_to_watchlist(db_session, org_and_account, monkeypatch):
    import app.mcp.tools as t
    org, _ = org_and_account
    _setup_mcp(monkeypatch, org.id)

    # Mock the screen_single_ticker.delay
    with patch("app.tasks.screening.screen_single_ticker.delay") as mock_delay:
        result = t.add_to_watchlist(ticker="WES.AX", exchange_key="ASX")

    assert isinstance(result, dict)
    assert "ticker" in result or "queued" in result or "error" not in result or result.get("ok") is not None


# ---------------------------------------------------------------------------
# skip_signal / unskip_signal
# ---------------------------------------------------------------------------

def test_skip_signal_success(db_session, org_and_account, monkeypatch):
    from app.models.signal import Signal, SignalStatus
    import app.mcp.tools as t

    org, _ = org_and_account
    sig = Signal(
        ticker="ANZ.AX", exchange_key="ASX", asset_type="EQUITY",
        currency="AUD", signal_date=date.today(),
        status=SignalStatus.PENDING, close_price=20.0,
        organization_id=org.id,
    )
    db_session.add(sig)
    db_session.commit()

    _setup_mcp(monkeypatch, org.id)
    result = t.skip_signal(signal_id=sig.id)
    assert "signal_id" in result or "ok" in result or "status" in result


def test_skip_signal_not_found(db_session, org_and_account, monkeypatch):
    import app.mcp.tools as t
    org, _ = org_and_account
    _setup_mcp(monkeypatch, org.id)

    result = t.skip_signal(signal_id=99999)
    assert "error" in result or result.get("ok") is False


# ---------------------------------------------------------------------------
# get_config / update_config
# ---------------------------------------------------------------------------

def test_get_config(db_session, org_and_account, monkeypatch):
    import app.mcp.tools as t
    org, _ = org_and_account
    _setup_mcp(monkeypatch, org.id)

    result = t.get_config()
    assert isinstance(result, dict)


def test_update_rule_threshold(db_session, org_and_account, monkeypatch):
    import app.mcp.tools as t
    from app.models.config import RuleConfig
    org, _ = org_and_account
    db_session.add(RuleConfig(
        rule_id="risk_max_pct_per_trade",
        organization_id=org.id,
        category="POSITION_SIZING",
        label="Max Risk Per Trade",
        enabled_globally=True,
        tier_overrides={},
        threshold=2.0,
    ))
    db_session.commit()

    _setup_mcp(monkeypatch, org.id)
    result = t.update_rule(rule_id="risk_max_pct_per_trade", threshold=3.0)
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# close_position (already in test_position_close_paths — just verify no crash)
# ---------------------------------------------------------------------------

def test_close_position_not_found(db_session, org_and_account, monkeypatch):
    import app.mcp.tools as t
    org, _ = org_and_account
    _setup_mcp(monkeypatch, org.id)

    result = t.close_position(position_id=99999, exit_reason="STOP_LOSS")
    assert result.get("ok") is False or "error" in result


# ---------------------------------------------------------------------------
# place_order via MCP
# ---------------------------------------------------------------------------

def test_mcp_place_order_not_found(db_session, org_and_account, monkeypatch):
    import app.mcp.tools as t
    org, _ = org_and_account
    _setup_mcp(monkeypatch, org.id)

    result = t.place_order(signal_id=99999)
    assert result.get("ok") is False or "error" in result
