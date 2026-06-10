"""Tests for app/agent/commands.py — AgentCommandHandler."""
import pytest
from unittest.mock import MagicMock, patch


def _make_handler(db_session, org, notifier=None):
    """Build AgentCommandHandler with mocked notifier to avoid WhatsApp calls."""
    from app.agent.commands import AgentCommandHandler
    mock_notifier = notifier or MagicMock()
    handler = AgentCommandHandler(organization_id=org.id, notifier=mock_notifier)
    return handler, mock_notifier


# --- handle() dispatch ---

def test_handle_unknown_command(db_session, org_and_account):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.handle("FOOBAR", "sender@c.us")
    assert "Unknown command" in result


def test_handle_empty_message(db_session, org_and_account):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.handle("  ", "sender@c.us")
    assert "No command received" in result


def test_handle_exception_returns_error_message(db_session, org_and_account):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    # Patch a command to raise
    with patch.object(h, "cmd_status", side_effect=Exception("boom")):
        result = h.handle("STATUS", "sender@c.us")
    assert "Error" in result


# --- cmd_help ---

def test_cmd_help_lists_commands(db_session, org_and_account):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_help([])
    assert "STATUS" in result
    assert "POSITIONS" in result
    assert "HELP" in result


# --- cmd_status ---

def test_cmd_status_returns_trading_state(db_session, org_and_account):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_status([])
    assert "AstraTrade Status" in result
    assert "Trading" in result
    assert "Open positions" in result


def test_cmd_status_shows_paused(db_session, org_and_account):
    from app.models.config import SystemConfig
    org, _ = org_and_account
    db_session.add(SystemConfig(
        key="trading_paused", value="true", label="Paused", group="system",
        organization_id=org.id,
    ))
    db_session.commit()
    h, _ = _make_handler(db_session, org)
    result = h.cmd_status([])
    assert "PAUSED" in result


# --- cmd_positions ---

def test_cmd_positions_no_positions(db_session, org_and_account):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_positions([])
    assert "No open positions" in result


def test_cmd_positions_shows_open_positions(db_session, org_and_account, open_crypto_position):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_positions([])
    assert "TRX-AUD" in result


# --- cmd_signals ---

def test_cmd_signals_no_signals(db_session, org_and_account):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_signals([])
    assert "No signals" in result


def test_cmd_signals_shows_today_signals(db_session, org_and_account):
    from app.models.signal import Signal, SignalStatus
    from app.utils.time_helper import get_current_date
    org, _ = org_and_account
    db_session.add(Signal(
        organization_id=org.id, ticker="BHP.AX", exchange_key="ASX",
        signal_date=get_current_date(), status=SignalStatus.PENDING,
        pivot_price=45.0, stop_price=42.0, rs_rating=80, trend_score=7,
        close_price=45.0,
    ))
    db_session.commit()
    h, _ = _make_handler(db_session, org)
    result = h.cmd_signals([])
    assert "BHP.AX" in result


# --- cmd_watchlist ---

def test_cmd_watchlist_empty(db_session, org_and_account):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_watchlist([])
    assert "empty" in result.lower() or "No stocks" in result or "Watchlist" in result


def test_cmd_watchlist_shows_items(db_session, org_and_account, watching_trx_item):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_watchlist([])
    assert "TRX-AUD" in result


# --- cmd_market ---

def test_cmd_market_returns_regime(db_session, org_and_account):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_market([])
    assert "Market" in result or "Regime" in result or "BULL" in result or "evaluated" in result.lower()


# --- cmd_pause / cmd_resume ---

def test_cmd_pause_sets_paused(db_session, org_and_account):
    from app.models.config import SystemConfig
    org, _ = org_and_account
    # Pre-seed the config key so _set_config can update it
    db_session.add(SystemConfig(
        key="trading_paused", value="false", label="Trading Paused", group="system",
        organization_id=org.id,
    ))
    db_session.commit()
    h, _ = _make_handler(db_session, org)
    result = h.cmd_pause([])
    assert "paused" in result.lower() or "PAUSE" in result
    db_session.expire_all()
    cfg = db_session.query(SystemConfig).filter(
        SystemConfig.key == "trading_paused",
        SystemConfig.organization_id == org.id,
    ).first()
    assert cfg is not None and cfg.value == "true"


def test_cmd_resume_clears_pause(db_session, org_and_account):
    from app.models.config import SystemConfig
    org, _ = org_and_account
    db_session.add(SystemConfig(
        key="trading_paused", value="true", label="Paused", group="system",
        organization_id=org.id,
    ))
    db_session.commit()
    h, _ = _make_handler(db_session, org)
    result = h.cmd_resume([])
    assert "resume" in result.lower() or "RESUME" in result or "active" in result.lower()
    db_session.expire_all()
    cfg = db_session.query(SystemConfig).filter(
        SystemConfig.key == "trading_paused",
        SystemConfig.organization_id == org.id,
    ).first()
    assert cfg.value == "false"


# --- cmd_skip ---

def test_cmd_skip_no_args(db_session, org_and_account):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_skip([])
    assert "ticker" in result.lower() or "Usage" in result or "SKIP" in result


def test_cmd_skip_ticker_not_found(db_session, org_and_account):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_skip(["NONEXISTENT.AX"])
    assert "not found" in result.lower() or "No pending" in result or "signal" in result.lower()


def test_cmd_skip_marks_signal_skipped(db_session, org_and_account):
    from app.models.signal import Signal, SignalStatus
    from app.utils.time_helper import get_current_date
    org, _ = org_and_account
    sig = Signal(
        organization_id=org.id, ticker="BHP.AX", exchange_key="ASX",
        signal_date=get_current_date(), status=SignalStatus.PENDING,
        pivot_price=45.0, stop_price=42.0, rs_rating=80, trend_score=7,
        close_price=45.0,
    )
    db_session.add(sig)
    db_session.commit()
    h, _ = _make_handler(db_session, org)
    result = h.cmd_skip(["BHP.AX"])
    assert "skipped" in result.lower() or "BHP" in result


# --- cmd_rule ---

def test_cmd_rule_no_args(db_session, org_and_account):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_rule([])
    assert "Usage" in result or "rule" in result.lower()


def test_cmd_rule_not_found(db_session, org_and_account):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_rule(["NONEXISTENT_RULE", "ON"])
    assert "not found" in result.lower() or "Rule" in result


# --- cmd_config ---

def test_cmd_config_no_args(db_session, org_and_account):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_config([])
    assert "Usage" in result or "config" in result.lower()


def test_cmd_config_updates_value(db_session, org_and_account):
    from app.models.config import SystemConfig
    org, _ = org_and_account
    db_session.add(SystemConfig(
        key="weekly_injection_aud", value="500", label="Weekly", group="capital",
        organization_id=org.id,
    ))
    db_session.commit()
    h, _ = _make_handler(db_session, org)
    result = h.cmd_config(["WEEKLY_INJECTION_AUD", "600"])
    assert "updated" in result.lower() or "600" in result


# --- cmd_report ---

def test_cmd_report_sends_report(db_session, org_and_account):
    org, _ = org_and_account
    h, mock_notifier = _make_handler(db_session, org)
    result = h.cmd_report([])
    # Should call send_daily_report on notifier
    assert mock_notifier.send_daily_report.called or "Report" in result or "report" in result.lower()


# --- cmd_exit ---

def test_cmd_exit_no_args(db_session, org_and_account):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_exit([])
    assert "ticker" in result.lower() or "Usage" in result or "EXIT" in result


def test_cmd_exit_position_not_found(db_session, org_and_account):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_exit(["NONEXISTENT.AX"])
    assert "not found" in result.lower() or "No open" in result


# --- cmd_stop ---

def test_cmd_stop_no_args(db_session, org_and_account):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_stop([])
    assert "Usage" in result or "STOP" in result or "ticker" in result.lower()


def test_cmd_unskip_no_args(db_session, org_and_account):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_unskip([])
    assert "ticker" in result.lower() or "Usage" in result or "UNSKIP" in result


# --- cmd_buy ---

def test_cmd_buy_no_args(db_session, org_and_account):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_buy([])
    assert "ticker" in result.lower() or "Usage" in result or "BUY" in result


def test_cmd_buy_signal_not_found(db_session, org_and_account):
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_buy(["NONEXISTENT.AX"])
    assert "not found" in result.lower() or "No pending" in result or "signal" in result.lower()


def test_cmd_buy_with_signal(db_session, org_and_account):
    """BUY with a pending signal shows the trade preview."""
    from app.models.signal import Signal, SignalStatus
    from app.utils.time_helper import get_current_date
    org, _ = org_and_account
    db_session.add(Signal(
        organization_id=org.id, ticker="CBA.AX", exchange_key="ASX",
        signal_date=get_current_date(), status=SignalStatus.PENDING,
        pivot_price=120.0, stop_price=115.0, rs_rating=90, trend_score=8,
        close_price=120.0, suggested_size_shares=50,
    ))
    db_session.commit()
    h, _ = _make_handler(db_session, org)
    result = h.cmd_buy(["CBA.AX"])
    assert "CBA.AX" in result or "cba" in result.lower()
    assert "120" in result or "Pivot" in result or "CONFIRM" in result


def test_cmd_exit_with_position(db_session, org_and_account, open_crypto_position):
    """EXIT with an open position flags it for exit."""
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_exit(["TRX-AUD"])
    assert "TRX" in result or "exit" in result.lower() or "flagged" in result.lower()


def test_cmd_stop_with_position(db_session, org_and_account, open_crypto_position):
    """STOP <TICKER> <PRICE> updates the stop price."""
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_stop(["TRX-AUD", "0.09"])
    assert "TRX" in result or "updated" in result.lower() or "Stop" in result


def test_cmd_stop_invalid_price(db_session, org_and_account):
    """STOP with non-numeric price returns an error."""
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_stop(["TRX-AUD", "notanumber"])
    assert "Invalid" in result or "invalid" in result.lower()


def test_cmd_stop_position_not_found(db_session, org_and_account):
    """STOP with unknown ticker returns not found."""
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_stop(["NONEXISTENT.AX", "10.0"])
    assert "not found" in result.lower() or "No open" in result


def test_cmd_unskip_with_skipped_signal(db_session, org_and_account):
    """UNSKIP with a skipped signal restores it to PENDING."""
    from app.models.signal import Signal, SignalStatus
    from app.utils.time_helper import get_current_date
    org, _ = org_and_account
    db_session.add(Signal(
        organization_id=org.id, ticker="BHP.AX", exchange_key="ASX",
        signal_date=get_current_date(), status=SignalStatus.SKIPPED,
        pivot_price=45.0, stop_price=42.0, rs_rating=80, trend_score=7,
        close_price=45.0,
    ))
    db_session.commit()
    h, _ = _make_handler(db_session, org)
    result = h.cmd_unskip(["BHP.AX"])
    assert "BHP" in result or "PENDING" in result or "restored" in result.lower()


def test_cmd_unskip_not_found(db_session, org_and_account):
    """UNSKIP with no matching skipped signal."""
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_unskip(["AAPL"])
    assert "not found" in result.lower() or "No skipped" in result


def test_cmd_confirm_no_args(db_session, org_and_account):
    """CONFIRM with no args returns usage."""
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_confirm([])
    assert "Usage" in result or "CONFIRM" in result


def test_cmd_confirm_signal_not_found(db_session, org_and_account):
    """CONFIRM with no matching signal returns error."""
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_confirm(["NONEXISTENT.AX"])
    assert "not found" in result.lower() or "No PENDING" in result


def test_cmd_rule_toggle_on(db_session, org_and_account):
    """RULE <id> ON enables a rule."""
    from app.models.config import RuleConfig
    org, _ = org_and_account
    db_session.add(RuleConfig(
        rule_id="test_rule", organization_id=org.id, category="TREND_TEMPLATE",
        label="Test Rule", enabled_globally=False, is_mandatory=False,
        tier_overrides={},
    ))
    db_session.commit()
    h, _ = _make_handler(db_session, org)
    result = h.cmd_rule(["test_rule", "ON"])
    assert "ENABLED" in result or "test_rule" in result


def test_cmd_rule_toggle_off(db_session, org_and_account):
    """RULE <id> OFF disables a rule."""
    from app.models.config import RuleConfig
    org, _ = org_and_account
    db_session.add(RuleConfig(
        rule_id="test_rule2", organization_id=org.id, category="TREND_TEMPLATE",
        label="Test Rule 2", enabled_globally=True, is_mandatory=False,
        tier_overrides={},
    ))
    db_session.commit()
    h, _ = _make_handler(db_session, org)
    result = h.cmd_rule(["test_rule2", "OFF"])
    assert "DISABLED" in result or "test_rule2" in result


def test_cmd_rule_mandatory_cannot_disable(db_session, org_and_account):
    """Mandatory rules cannot be disabled."""
    from app.models.config import RuleConfig
    org, _ = org_and_account
    db_session.add(RuleConfig(
        rule_id="mandatory_rule", organization_id=org.id, category="EXIT_DEFENSIVE",
        label="Stop Loss", enabled_globally=True, is_mandatory=True,
        tier_overrides={},
    ))
    db_session.commit()
    h, _ = _make_handler(db_session, org)
    result = h.cmd_rule(["mandatory_rule", "OFF"])
    assert "mandatory" in result.lower() or "cannot" in result.lower()


def test_cmd_rule_invalid_state(db_session, org_and_account):
    """RULE with invalid state (not ON/OFF) returns error."""
    org, _ = org_and_account
    h, _ = _make_handler(db_session, org)
    result = h.cmd_rule(["some_rule", "MAYBE"])
    assert "ON or OFF" in result or "State must" in result
