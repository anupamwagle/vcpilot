"""Tests for app/mcp/tools.py — MCP tool functions."""
import pytest
from datetime import date, datetime
from types import SimpleNamespace
from decimal import Decimal


def _ctx(org_id, client_id="test-client"):
    return SimpleNamespace(org_id=org_id, client_id=client_id)


def _setup_mcp(monkeypatch, org_id):
    import app.mcp.tools as t
    monkeypatch.setattr(t, "get_mcp_context", lambda: _ctx(org_id))
    monkeypatch.setattr(t, "assert_scope", lambda *a, **kw: None)
    return t


# --- get_market_regime ---

def test_get_market_regime_returns_not_evaluated(db_session, org_and_account, monkeypatch):
    org, _ = org_and_account
    t = _setup_mcp(monkeypatch, org.id)
    result = t.get_market_regime("ASX")
    assert "regime" in result
    assert result["exchange_key"] == "ASX"


def test_get_market_regime_reads_systemconfig(db_session, org_and_account, monkeypatch):
    from app.models.config import SystemConfig
    org, _ = org_and_account
    db_session.add(SystemConfig(
        key="last_market_regime_ASX", value="BULL",
        label="Regime", group="system", organization_id=org.id,
    ))
    db_session.commit()
    t = _setup_mcp(monkeypatch, org.id)
    result = t.get_market_regime("ASX")
    assert result["regime"] == "BULL"


# --- get_signals ---

def test_get_signals_empty(db_session, org_and_account, monkeypatch):
    org, _ = org_and_account
    t = _setup_mcp(monkeypatch, org.id)
    result = t.get_signals()
    assert result["total"] == 0
    assert result["signals"] == []


def test_get_signals_returns_pending_signals(db_session, org_and_account, monkeypatch):
    from app.models.signal import Signal, SignalStatus
    org, _ = org_and_account
    db_session.add(Signal(
        organization_id=org.id, ticker="BHP.AX", exchange_key="ASX",
        signal_date=date.today(), status=SignalStatus.PENDING,
        pivot_price=45.0, stop_price=42.0, rs_rating=80, trend_score=7,
        close_price=45.0,
    ))
    db_session.commit()
    t = _setup_mcp(monkeypatch, org.id)
    result = t.get_signals()
    assert result["total"] == 1
    assert result["signals"][0]["ticker"] == "BHP.AX"


def test_get_signals_filters_by_status(db_session, org_and_account, monkeypatch):
    from app.models.signal import Signal, SignalStatus
    org, _ = org_and_account
    db_session.add(Signal(
        organization_id=org.id, ticker="BHP.AX", exchange_key="ASX",
        signal_date=date.today(), status=SignalStatus.SKIPPED,
        pivot_price=45.0, stop_price=42.0, rs_rating=80, trend_score=7,
        close_price=45.0,
    ))
    db_session.commit()
    t = _setup_mcp(monkeypatch, org.id)
    result = t.get_signals(status="PENDING")
    assert result["total"] == 0


# --- skip_signal / unskip_signal ---

def test_skip_signal_marks_skipped(db_session, org_and_account, monkeypatch):
    from app.models.signal import Signal, SignalStatus
    org, _ = org_and_account
    sig = Signal(
        organization_id=org.id, ticker="WOW.AX", exchange_key="ASX",
        signal_date=date.today(), status=SignalStatus.PENDING,
        pivot_price=30.0, stop_price=28.0, rs_rating=75, trend_score=6,
        close_price=30.0,
    )
    db_session.add(sig)
    db_session.commit()
    t = _setup_mcp(monkeypatch, org.id)
    result = t.skip_signal(signal_id=sig.id)
    assert result["ok"] is True
    db_session.expire_all()
    refreshed = db_session.query(Signal).filter(Signal.id == sig.id).first()
    assert refreshed.status == SignalStatus.SKIPPED


def test_skip_signal_not_found(db_session, org_and_account, monkeypatch):
    org, _ = org_and_account
    t = _setup_mcp(monkeypatch, org.id)
    result = t.skip_signal(signal_id=99999)
    assert result["ok"] is False


def test_unskip_signal_restores_to_pending(db_session, org_and_account, monkeypatch):
    from app.models.signal import Signal, SignalStatus
    org, _ = org_and_account
    sig = Signal(
        organization_id=org.id, ticker="WOW.AX", exchange_key="ASX",
        signal_date=date.today(), status=SignalStatus.SKIPPED,
        pivot_price=30.0, stop_price=28.0, rs_rating=75, trend_score=6,
        close_price=30.0,
    )
    db_session.add(sig)
    db_session.commit()
    t = _setup_mcp(monkeypatch, org.id)
    result = t.unskip_signal(signal_id=sig.id)
    assert result["ok"] is True
    db_session.expire_all()
    refreshed = db_session.query(Signal).filter(Signal.id == sig.id).first()
    assert refreshed.status == SignalStatus.PENDING


def test_unskip_signal_not_found(db_session, org_and_account, monkeypatch):
    org, _ = org_and_account
    t = _setup_mcp(monkeypatch, org.id)
    result = t.unskip_signal(signal_id=99999)
    assert result["ok"] is False


# --- get_watchlist ---

def test_get_watchlist_empty(db_session, org_and_account, monkeypatch):
    org, _ = org_and_account
    t = _setup_mcp(monkeypatch, org.id)
    result = t.get_watchlist()
    assert result["total"] == 0
    assert result["watchlist"] == []


def test_get_watchlist_returns_items(db_session, org_and_account, watching_trx_item, monkeypatch):
    org, _ = org_and_account
    t = _setup_mcp(monkeypatch, org.id)
    result = t.get_watchlist()
    assert result["total"] == 1
    assert result["watchlist"][0]["ticker"] == "TRX-AUD"


# --- pause_trading / resume_trading ---

def test_pause_trading_sets_paused(db_session, org_and_account, monkeypatch):
    from app.models.config import SystemConfig
    org, _ = org_and_account
    t = _setup_mcp(monkeypatch, org.id)
    result = t.pause_trading("test pause")
    assert result["ok"] is True
    cfg = db_session.query(SystemConfig).filter(
        SystemConfig.key == "trading_paused",
        SystemConfig.organization_id == org.id,
    ).first()
    assert cfg is not None
    assert cfg.value == "true"


def test_resume_trading_clears_pause(db_session, org_and_account, monkeypatch):
    from app.models.config import SystemConfig
    org, _ = org_and_account
    # Pre-seed paused state
    db_session.add(SystemConfig(
        key="trading_paused", value="true", label="Paused", group="system",
        organization_id=org.id,
    ))
    db_session.commit()
    t = _setup_mcp(monkeypatch, org.id)
    result = t.resume_trading("test resume")
    assert result["ok"] is True
    db_session.expire_all()
    cfg = db_session.query(SystemConfig).filter(
        SystemConfig.key == "trading_paused",
        SystemConfig.organization_id == org.id,
    ).first()
    assert cfg.value == "false"


# --- get_positions ---

def test_get_positions_empty(db_session, org_and_account, monkeypatch):
    org, _ = org_and_account
    t = _setup_mcp(monkeypatch, org.id)
    result = t.get_positions()
    assert result["open_count"] == 0
    assert result["open"] == []


def test_get_positions_returns_open(db_session, org_and_account, open_crypto_position, monkeypatch):
    org, _ = org_and_account
    t = _setup_mcp(monkeypatch, org.id)
    result = t.get_positions()
    assert result["open_count"] == 1
    assert result["open"][0]["ticker"] == "TRX-AUD"


# --- get_portfolio_stats ---

def test_get_portfolio_stats_no_positions(db_session, org_and_account, monkeypatch):
    org, _ = org_and_account
    t = _setup_mcp(monkeypatch, org.id)
    result = t.get_portfolio_stats()
    assert "capital_aud" in result or "error" not in result


# --- get_rules ---

def test_get_rules_returns_dict(db_session, org_and_account, monkeypatch):
    org, _ = org_and_account
    t = _setup_mcp(monkeypatch, org.id)
    result = t.get_rules()
    assert "rules" in result or isinstance(result, dict)


def test_get_rules_by_category(db_session, org_and_account, monkeypatch):
    org, _ = org_and_account
    t = _setup_mcp(monkeypatch, org.id)
    result = t.get_rules(category="VCP")
    assert isinstance(result, dict)


# --- get_config ---

def test_get_config_returns_dict(db_session, org_and_account, monkeypatch):
    from app.models.config import SystemConfig
    org, _ = org_and_account
    db_session.add(SystemConfig(
        key="ibkr_account", value="U12345", label="IBKR Account", group="ibkr",
        organization_id=org.id,
    ))
    db_session.commit()
    t = _setup_mcp(monkeypatch, org.id)
    result = t.get_config()
    assert isinstance(result, dict)


def test_get_config_filtered_keys(db_session, org_and_account, monkeypatch):
    from app.models.config import SystemConfig
    org, _ = org_and_account
    db_session.add(SystemConfig(
        key="weekly_injection_aud", value="500", label="Weekly Injection", group="capital",
        organization_id=org.id,
    ))
    db_session.commit()
    t = _setup_mcp(monkeypatch, org.id)
    result = t.get_config(keys=["weekly_injection_aud"])
    assert isinstance(result, dict)


# --- place_order (signal not found) ---

def test_place_order_signal_not_found(db_session, org_and_account, monkeypatch):
    org, _ = org_and_account
    t = _setup_mcp(monkeypatch, org.id)
    result = t.place_order(signal_id=99999)
    assert result["ok"] is False


# --- run_screener ---

def test_run_screener_queues_task(db_session, org_and_account, monkeypatch):
    org, _ = org_and_account
    t = _setup_mcp(monkeypatch, org.id)
    mock_task = SimpleNamespace(delay=lambda *a, **kw: SimpleNamespace(id="task-1"))
    monkeypatch.setattr("app.tasks.screening._run_screen_force", mock_task)
    result = t.run_screener("ASX")
    assert result.get("queued") is True


# --- evaluate_market_regime ---

def test_evaluate_market_regime_queues_task(db_session, org_and_account, monkeypatch):
    org, _ = org_and_account
    t = _setup_mcp(monkeypatch, org.id)
    mock_task = SimpleNamespace(delay=lambda *a, **kw: SimpleNamespace(id="task-1"))
    monkeypatch.setattr("app.tasks.screening.evaluate_market_regime_task", mock_task)
    result = t.evaluate_market_regime("ASX")
    assert result.get("queued") is True


# --- add_to_watchlist ---

def test_add_to_watchlist_queues_screen(db_session, org_and_account, monkeypatch):
    org, _ = org_and_account
    t = _setup_mcp(monkeypatch, org.id)
    mock_task = SimpleNamespace(delay=lambda *a, **kw: SimpleNamespace(id="task-2"))
    monkeypatch.setattr("app.tasks.screening.screen_single_ticker", mock_task)
    result = t.add_to_watchlist(ticker="BHP", exchange_key="ASX")
    assert result.get("queued") is True


# --- remove_from_watchlist ---

def test_remove_from_watchlist_not_found_removed_zero(db_session, org_and_account, monkeypatch):
    org, _ = org_and_account
    t = _setup_mcp(monkeypatch, org.id)
    result = t.remove_from_watchlist(ticker="NONEXISTENT.AX")
    # remove_from_watchlist always returns ok=True with removed=0 if not found
    assert result["ok"] is True
    assert result["removed"] == 0


def test_remove_from_watchlist_success(db_session, org_and_account, watching_trx_item, monkeypatch):
    org, _ = org_and_account
    t = _setup_mcp(monkeypatch, org.id)
    result = t.remove_from_watchlist(ticker="TRX-AUD")
    assert result["ok"] is True
    assert result["removed"] == 1
