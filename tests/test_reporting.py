"""Tests for app/tasks/reporting.py — generate_daily_report, health_check, send_notification_message."""
import pytest
from datetime import date
from unittest.mock import patch, MagicMock


# --- generate_daily_report ---

def test_generate_daily_report_returns_dict(db_session, org_and_account):
    from app.tasks.reporting import generate_daily_report
    org, _ = org_and_account
    report = generate_daily_report(organization_id=org.id)
    assert isinstance(report, dict)
    assert "date" in report
    assert "signals_count" in report
    assert "open_positions" in report
    assert "pnl_today_aud" in report
    assert "pnl_total_aud" in report
    assert "market_regime" in report


def test_generate_daily_report_no_org(db_session, org_and_account):
    from app.tasks.reporting import generate_daily_report
    # organization_id=None → global scope
    report = generate_daily_report(organization_id=None)
    assert isinstance(report, dict)
    assert report["pnl_today_aud"] == 0.0


def test_generate_daily_report_counts_signals(db_session, org_and_account):
    from app.tasks.reporting import generate_daily_report
    from app.models.signal import Signal, SignalStatus
    from app.utils.time_helper import get_current_date
    org, _ = org_and_account
    db_session.add(Signal(
        organization_id=org.id, ticker="BHP.AX", exchange_key="ASX",
        signal_date=get_current_date(), status=SignalStatus.PENDING,
        pivot_price=45.0, stop_price=42.0, rs_rating=80, trend_score=7,
        close_price=45.5,
    ))
    db_session.commit()
    report = generate_daily_report(organization_id=org.id)
    assert report["signals_count"] == 1


def test_generate_daily_report_sums_pnl(db_session, org_and_account):
    from app.tasks.reporting import generate_daily_report
    from app.models.trade import Trade, TradeStatus
    from app.utils.time_helper import get_current_date
    from decimal import Decimal
    org, acct = org_and_account
    today = get_current_date()
    db_session.add(Trade(
        organization_id=org.id, account_id=acct.id,
        ticker="BHP.AX", exchange_key="ASX",
        entry_date=today, exit_date=today, hold_days=1,
        entry_price=Decimal("40.0"), exit_price=Decimal("50.0"),
        qty=Decimal("100"), gross_pnl_aud=Decimal("1000"), net_pnl_aud=Decimal("950"),
        pnl_pct=Decimal("25.0"), initial_stop=Decimal("38.0"),
        exit_reason="PROFIT_TARGET_1", cgt_eligible_discount=False,
    ))
    db_session.commit()
    report = generate_daily_report(organization_id=org.id)
    assert report["pnl_today_aud"] == 950.0
    assert report["pnl_total_aud"] == 950.0


def test_generate_daily_report_shows_unknown_regime_when_no_config(db_session, org_and_account):
    from app.tasks.reporting import generate_daily_report
    org, _ = org_and_account
    # No SystemConfig for last_market_regime → UNKNOWN
    report = generate_daily_report(organization_id=org.id)
    assert report["market_regime"] in ("UNKNOWN", "BULL", "CAUTION", "BEAR", "NOT_EVALUATED")


# --- health_check task ---

def test_health_check_writes_heartbeat(db_session, org_and_account):
    from app.tasks.reporting import health_check
    from app.models.config import SystemConfig
    org, _ = org_and_account
    health_check.run()
    # Global heartbeat written
    row = db_session.query(SystemConfig).filter(
        SystemConfig.key == "last_heartbeat",
        SystemConfig.organization_id == None,
    ).first()
    assert row is not None
    assert row.value is not None


def test_health_check_writes_per_org_heartbeat(db_session, org_and_account):
    from app.tasks.reporting import health_check
    from app.models.config import SystemConfig
    org, _ = org_and_account
    health_check.run()
    row = db_session.query(SystemConfig).filter(
        SystemConfig.key == "last_heartbeat",
        SystemConfig.organization_id == org.id,
    ).first()
    assert row is not None


def test_health_check_updates_existing_heartbeat(db_session, org_and_account):
    from app.tasks.reporting import health_check
    from app.models.config import SystemConfig
    # Pre-seed a stale heartbeat
    db_session.add(SystemConfig(
        key="last_heartbeat", value="2000-01-01T00:00:00",
        label="Last Worker Heartbeat", group="system", organization_id=None,
    ))
    db_session.commit()
    health_check.run()
    row = db_session.query(SystemConfig).filter(
        SystemConfig.key == "last_heartbeat",
        SystemConfig.organization_id == None,
    ).first()
    assert row.value != "2000-01-01T00:00:00"


# --- send_notification_message task ---

def test_send_notification_message_calls_notifier_method(db_session, org_and_account):
    from app.tasks.reporting import send_notification_message
    org, _ = org_and_account
    mock_notifier = MagicMock()
    with patch("app.notifications.get_notifier", return_value=mock_notifier):
        send_notification_message.run(
            organization_id=org.id,
            method_name="send",
            args=["hello"],
            kwargs={},
        )
    mock_notifier.send.assert_called_once_with("hello")


def test_send_notification_message_unknown_method_does_not_raise(db_session, org_and_account):
    from app.tasks.reporting import send_notification_message
    org, _ = org_and_account
    mock_notifier = MagicMock(spec=[])  # no methods
    with patch("app.notifications.get_notifier", return_value=mock_notifier):
        # Should not raise — just logs error
        send_notification_message.run(
            organization_id=org.id,
            method_name="nonexistent_method",
        )


# --- send_daily_report task ---

def test_send_daily_report_calls_notifier(db_session, org_and_account):
    from app.tasks.reporting import send_daily_report
    org, _ = org_and_account
    mock_notifier = MagicMock()
    with patch("app.tasks.reporting.get_notifier", return_value=mock_notifier):
        send_daily_report.run(organization_id=org.id)
    mock_notifier.send_daily_report.assert_called_once()
