"""
Tests for check_exit_rules_task (app/tasks/trading.py).

Focuses on the regression paths that have caused silent failures:
  - Decimal × float TypeError (the bug that caused "no exit checks logged yet")
  - Per-position audit logs always written (holding / error / skipped)
  - No-stop-price guard
  - No-price-data guard
"""
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from app.models.trade import Position, TradeStatus
from app.models.audit import AuditLog, AuditAction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_open_position(db, org_id, account_id, ticker="WOW.AX",
                         entry_price=37.0, stop=34.0, qty=33):
    pos = Position(
        ticker=ticker,
        exchange_key="ASX",
        asset_type="EQUITY",
        currency="AUD",
        account_id=account_id,
        organization_id=org_id,
        entry_date=date(2026, 5, 1),
        entry_price=Decimal(str(entry_price)),
        qty=Decimal(str(qty)),
        initial_stop=Decimal(str(stop)),
        current_stop=Decimal(str(stop)),
        status=TradeStatus.OPEN,
        is_paper=True,
    )
    db.add(pos)
    db.commit()
    db.refresh(pos)
    return pos


def _make_price_df(close=38.0, volume=500_000, ma_50=36.0, rows=60):
    from datetime import date, timedelta
    dates = [date(2025, 1, 1) + timedelta(days=i) for i in range(rows)]
    return pd.DataFrame({
        "date": dates, "open": [close]*rows, "high": [close*1.01]*rows,
        "low": [close*0.99]*rows, "close": [close]*rows, "volume": [volume]*rows,
        "ma_50": [ma_50]*rows, "ma_200": [ma_50*0.9]*rows,
        "avg_vol_50": [float(volume)]*rows, "atr_14": [0.5]*rows,
        "pct_from_52w_high": [-5.0]*rows, "high_52w": [close*1.05]*rows,
        "low_52w": [close*0.8]*rows, "vol_ratio": [1.0]*rows,
        "ma_150": [ma_50*0.95]*rows, "ma_200_prev": [ma_50*0.88]*rows,
        "rs_rating": [75.0]*rows,
    })


def _patch_common(monkeypatch, close=38.0, volume=500_000):
    import app.tasks.trading as t
    df = _make_price_df(close=close, volume=volume)
    monkeypatch.setattr(t, "market_is_open_now", lambda exchange_key: True)
    monkeypatch.setattr(t, "get_price_history", lambda ticker, period="6mo": df)

    class _Notifier:
        def send(self, *a, **kw): pass
        def send_exit_alert(self, *a, **kw): pass
    monkeypatch.setattr(t, "get_notifier", lambda organization_id=None: _Notifier())

    # Stub fundamentals so no network call is made
    import app.data.fetcher as fetcher
    monkeypatch.setattr(fetcher, "get_fundamentals",
                        lambda ticker: {"next_earnings_date": None})


def _exit_check_logs(db, org_id, ticker):
    return db.query(AuditLog).filter(
        AuditLog.organization_id == org_id,
        AuditLog.action == AuditAction.TASK_RUN,
        AuditLog.ticker == ticker,
        AuditLog.message.like("Exit check @%"),
    ).all()


# ---------------------------------------------------------------------------
# Regression: Decimal × float must not raise TypeError
# ---------------------------------------------------------------------------

def test_decimal_qty_does_not_raise(db_session, org_and_account, monkeypatch):
    """
    Regression guard for: unsupported operand type(s) for *: 'float' and 'decimal.Decimal'
    Position.qty and entry_price are Decimal from SQLAlchemy Numeric columns.
    The task must not crash when doing arithmetic on them.
    """
    from app.tasks.trading import check_exit_rules_task
    org, account = org_and_account
    _make_open_position(db_session, org.id, account.id, qty=33)
    _patch_common(monkeypatch, close=38.0)

    # Must not raise — previously crashed silently here
    check_exit_rules_task.run(exchange_key="ASX")


# ---------------------------------------------------------------------------
# Audit log always written for each position
# ---------------------------------------------------------------------------

def test_holding_position_writes_audit_log(db_session, org_and_account, monkeypatch):
    """A position comfortably above stop must produce a 'holding' audit log."""
    from app.tasks.trading import check_exit_rules_task
    org, account = org_and_account
    _make_open_position(db_session, org.id, account.id,
                        ticker="WOW.AX", entry_price=37.0, stop=34.0)
    _patch_common(monkeypatch, close=38.5)   # well above stop

    check_exit_rules_task.run(exchange_key="ASX")

    logs = _exit_check_logs(db_session, org.id, "WOW.AX")
    assert logs, "Should write per-position audit log even when holding"
    assert any("holding" in l.message for l in logs)


def test_no_stop_price_writes_skipped_audit_log(db_session, org_and_account, monkeypatch):
    """Position with current_stop=0 must be skipped with an explanatory audit log."""
    from app.tasks.trading import check_exit_rules_task
    org, account = org_and_account
    pos = _make_open_position(db_session, org.id, account.id, stop=0.0)
    _patch_common(monkeypatch)

    check_exit_rules_task.run(exchange_key="ASX")

    logs = db_session.query(AuditLog).filter(
        AuditLog.organization_id == org.id,
        AuditLog.message.like("Exit check @ %: skipped%"),
    ).all()
    assert logs, "Should write skipped audit log for position with no stop"


def test_no_price_data_writes_skipped_audit_log(db_session, org_and_account, monkeypatch):
    """Position where price history is unavailable writes a skipped log."""
    from app.tasks.trading import check_exit_rules_task
    import app.tasks.trading as t
    org, account = org_and_account
    _make_open_position(db_session, org.id, account.id)
    monkeypatch.setattr(t, "market_is_open_now", lambda exchange_key: True)
    monkeypatch.setattr(t, "get_price_history", lambda ticker, period="6mo": None)

    class _Notifier:
        def send(self, *a, **kw): pass
        def send_exit_alert(self, *a, **kw): pass
    monkeypatch.setattr(t, "get_notifier", lambda organization_id=None: _Notifier())

    check_exit_rules_task.run(exchange_key="ASX")

    logs = db_session.query(AuditLog).filter(
        AuditLog.organization_id == org.id,
        AuditLog.message.like("%skipped%no price data%"),
    ).all()
    assert logs


def test_exception_in_evaluate_writes_error_audit_log(db_session, org_and_account, monkeypatch):
    """Any unexpected exception must write an error audit log, not silently vanish."""
    from app.tasks.trading import check_exit_rules_task
    import app.tasks.trading as t

    org, account = org_and_account
    _make_open_position(db_session, org.id, account.id)

    monkeypatch.setattr(t, "market_is_open_now", lambda exchange_key: True)
    monkeypatch.setattr(t, "get_price_history", lambda ticker, period="6mo": _make_price_df())

    # Make evaluate_exit_rules throw unexpectedly
    monkeypatch.setattr(t, "evaluate_exit_rules",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("simulated error")))

    import app.data.fetcher as fetcher
    monkeypatch.setattr(fetcher, "get_fundamentals", lambda ticker: {"next_earnings_date": None})

    class _Notifier:
        def send(self, *a, **kw): pass
        def send_exit_alert(self, *a, **kw): pass
    monkeypatch.setattr(t, "get_notifier", lambda organization_id=None: _Notifier())

    check_exit_rules_task.run(exchange_key="ASX")

    error_logs = db_session.query(AuditLog).filter(
        AuditLog.organization_id == org.id,
        AuditLog.message.like("Exit check @ %: error%"),
    ).all()
    assert error_logs, "Should write error audit log when evaluate_exit_rules throws"


# ---------------------------------------------------------------------------
# Market closed path
# ---------------------------------------------------------------------------

def test_market_closed_writes_audit_and_returns(db_session, org_and_account, monkeypatch):
    from app.tasks.trading import check_exit_rules_task
    import app.tasks.trading as t
    org, account = org_and_account
    monkeypatch.setattr(t, "market_is_open_now", lambda exchange_key: False)

    check_exit_rules_task.run(exchange_key="ASX")

    logs = db_session.query(AuditLog).filter(
        AuditLog.organization_id == org.id,
        AuditLog.message.like("%market closed%"),
    ).all()
    assert logs
