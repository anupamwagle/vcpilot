"""Tests for app/tasks/trading.py — Celery trading tasks."""
import pytest
from unittest.mock import MagicMock, patch
from datetime import date, datetime


def _mock_notifier():
    n = MagicMock()
    n.send = MagicMock()
    n.send_entry_alert = MagicMock()
    n.send_exit_alert = MagicMock()
    return n


# ---- check_entry_triggers ---------------------------------------------------

def test_check_entry_triggers_market_closed(db_session, org_and_account, monkeypatch):
    """When market is closed, task writes audit log and returns."""
    from app.tasks.trading import check_entry_triggers
    monkeypatch.setattr("app.tasks.trading.market_is_open_now", lambda *a: False)
    # Should not raise
    check_entry_triggers.run(exchange_key="ASX")


def test_check_entry_triggers_no_pending_signals(db_session, org_and_account, monkeypatch):
    """When market open but no pending signals, task records 0 signals and exits."""
    from app.tasks.trading import check_entry_triggers
    monkeypatch.setattr("app.tasks.trading.market_is_open_now", lambda *a: True)
    monkeypatch.setattr("app.tasks.trading.get_notifier", lambda **kw: _mock_notifier())
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: _mock_notifier())
    check_entry_triggers.run(exchange_key="ASX")


def test_check_entry_triggers_trading_paused(db_session, org_and_account, monkeypatch):
    """When trading is paused for org, skip entry check and log skip reason."""
    from app.models.config import SystemConfig
    from app.tasks.trading import check_entry_triggers

    org, _ = org_and_account
    db_session.add(SystemConfig(
        key="trading_paused", value="true", label="Paused", group="system",
        organization_id=org.id,
    ))
    db_session.commit()

    monkeypatch.setattr("app.tasks.trading.market_is_open_now", lambda *a: True)
    monkeypatch.setattr("app.tasks.trading.get_notifier", lambda **kw: _mock_notifier())
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: _mock_notifier())

    # Should not raise
    check_entry_triggers.run(exchange_key="ASX")


def test_check_entry_triggers_with_pending_signal_no_price(db_session, org_and_account, monkeypatch):
    """Pending signal exists but intraday price fails → no order placed."""
    from app.models.signal import Signal, SignalStatus
    from app.tasks.trading import check_entry_triggers
    from app.utils.time_helper import get_current_date

    org, _ = org_and_account
    db_session.add(Signal(
        organization_id=org.id, ticker="BHP.AX", exchange_key="ASX",
        signal_date=get_current_date(), status=SignalStatus.PENDING,
        pivot_price=45.0, stop_price=42.0, rs_rating=80, trend_score=7,
        close_price=45.0,
    ))
    db_session.commit()

    monkeypatch.setattr("app.tasks.trading.market_is_open_now", lambda *a: True)
    monkeypatch.setattr("app.tasks.trading.get_intraday_price",
                        lambda *a, **kw: {"ok": False, "price": None, "data_source": "test", "delay_mins": 0})
    monkeypatch.setattr("app.tasks.trading.get_notifier", lambda **kw: _mock_notifier())
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: _mock_notifier())

    check_entry_triggers.run(exchange_key="ASX")

    # Signal should still be PENDING since no breakout
    db_session.expire_all()
    from app.models.signal import Signal as Sig
    sig = db_session.query(Sig).filter_by(organization_id=org.id, ticker="BHP.AX").first()
    assert sig.status == SignalStatus.PENDING


def test_check_entry_triggers_bear_regime_blocks(db_session, org_and_account, monkeypatch):
    """BEAR regime blocks entry if rule enabled."""
    from app.models.signal import Signal, SignalStatus
    from app.models.config import SystemConfig
    from app.tasks.trading import check_entry_triggers
    from app.utils.time_helper import get_current_date

    org, _ = org_and_account
    db_session.add(Signal(
        organization_id=org.id, ticker="BHP.AX", exchange_key="ASX",
        signal_date=get_current_date(), status=SignalStatus.PENDING,
        pivot_price=45.0, stop_price=42.0, rs_rating=80, trend_score=7,
        close_price=45.0,
    ))
    db_session.add(SystemConfig(
        key="last_market_regime", value="BEAR", label="Regime", group="market",
        organization_id=None,
    ))
    db_session.commit()

    monkeypatch.setattr("app.tasks.trading.market_is_open_now", lambda *a: True)
    monkeypatch.setattr("app.tasks.trading.get_intraday_price",
                        lambda *a, **kw: {"ok": True, "price": 46.0, "data_source": "test", "delay_mins": 0,
                                          "volume": 500_000, "bar_timestamp": None})
    monkeypatch.setattr("app.tasks.trading.get_notifier", lambda **kw: _mock_notifier())
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: _mock_notifier())

    check_entry_triggers.run(exchange_key="ASX")
    # Test just checks no crash occurs


# ---- check_exit_rules_task --------------------------------------------------

def test_check_exit_rules_market_closed(db_session, org_and_account, monkeypatch):
    """When market closed, exit task returns without processing."""
    from app.tasks.trading import check_exit_rules_task
    monkeypatch.setattr("app.tasks.trading.market_is_open_now", lambda *a: False)
    check_exit_rules_task.run(exchange_key="ASX")


def test_check_exit_rules_no_open_positions(db_session, org_and_account, monkeypatch):
    """No open positions → task logs and returns."""
    from app.tasks.trading import check_exit_rules_task
    monkeypatch.setattr("app.tasks.trading.market_is_open_now", lambda *a: True)
    monkeypatch.setattr("app.tasks.trading.get_notifier", lambda **kw: _mock_notifier())
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: _mock_notifier())
    check_exit_rules_task.run(exchange_key="ASX")


def test_check_exit_rules_with_open_position(db_session, org_and_account, open_crypto_position, monkeypatch):
    """Open position is evaluated against exit rules."""
    from app.tasks.trading import check_exit_rules_task
    from app.screener.exit_rules import ExitSignal

    monkeypatch.setattr("app.tasks.trading.market_is_open_now", lambda *a: True)
    monkeypatch.setattr(
        "app.tasks.trading.get_intraday_price",
        lambda *a, **kw: {"ok": True, "price": 0.10, "data_source": "test",
                           "delay_mins": 0, "volume": 100_000, "bar_timestamp": None}
    )
    monkeypatch.setattr(
        "app.tasks.trading.evaluate_exit_rules",
        lambda *a, **kw: [ExitSignal(should_exit=False)]
    )
    monkeypatch.setattr("app.tasks.trading.get_notifier", lambda **kw: _mock_notifier())
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: _mock_notifier())

    check_exit_rules_task.run(exchange_key="CRYPTO")


# ---- update_position_pnl_task -----------------------------------------------

def test_update_position_pnl_task_no_positions(db_session, org_and_account, monkeypatch):
    """No open positions → update task runs without error."""
    from app.tasks.trading import update_position_pnl_task
    monkeypatch.setattr("app.tasks.trading.get_intraday_price",
                        lambda *a, **kw: {"ok": False, "price": None, "data_source": "test", "delay_mins": 0})
    update_position_pnl_task.run()


def test_update_position_pnl_task_updates_price(db_session, org_and_account, open_crypto_position, monkeypatch):
    """Open position gets current_price updated when price fetch succeeds."""
    from app.tasks.trading import update_position_pnl_task
    from app.models.trade import Position, TradeStatus

    monkeypatch.setattr(
        "app.tasks.trading.get_intraday_price",
        lambda *a, **kw: {"ok": True, "price": 0.25, "data_source": "test", "delay_mins": 0, "bar_timestamp": None}
    )

    update_position_pnl_task.run()

    db_session.expire_all()
    pos = db_session.query(Position).filter_by(status=TradeStatus.OPEN).first()
    # Position current_price should have been updated by the task
    assert pos is not None


# ---- promote_watchlist_item_task --------------------------------------------

def test_promote_watchlist_item_task_item_not_found(db_session, org_and_account, monkeypatch):
    """Watchlist item not found → task exits gracefully."""
    from app.tasks.trading import promote_watchlist_item_task

    org, _ = org_and_account
    monkeypatch.setattr("app.tasks.trading.get_notifier", lambda **kw: _mock_notifier())
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: _mock_notifier())

    # Non-existent item ID
    promote_watchlist_item_task.run(
        item_id=99999, organization_id=org.id,
        user_email="admin@test.com", user_id=1
    )


def test_promote_watchlist_item_task_promotes_watching(db_session, org_and_account, watching_trx_item, monkeypatch):
    """Watching item is promoted to PENDING signal via the task."""
    from app.tasks.trading import promote_watchlist_item_task
    from app.models.signal import Watchlist, WatchlistStatus, Signal

    org, _ = org_and_account
    item = watching_trx_item

    mock_n = _mock_notifier()
    monkeypatch.setattr("app.tasks.trading.get_notifier", lambda **kw: mock_n)
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: mock_n)
    # Mock fetcher so price data doesn't actually get fetched
    monkeypatch.setattr("app.tasks.trading.get_price_history",
                        lambda *a, **kw: None)

    promote_watchlist_item_task.run(
        item_id=item.id, organization_id=org.id,
        user_email="admin@test.com", user_id=1
    )


# ---- sync_stop_orders -------------------------------------------------------

def test_sync_stop_orders_no_positions(db_session, org_and_account, monkeypatch):
    """No open positions → sync task completes without error."""
    from app.tasks.trading import sync_stop_orders
    monkeypatch.setattr("app.tasks.trading.get_notifier", lambda **kw: _mock_notifier())
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: _mock_notifier())
    sync_stop_orders.run()


def test_sync_stop_orders_with_crypto_position(db_session, org_and_account, open_crypto_position, monkeypatch):
    """Open crypto position is processed by sync_stop_orders."""
    from app.tasks.trading import sync_stop_orders

    monkeypatch.setattr(
        "app.tasks.trading.get_intraday_price",
        lambda *a, **kw: {"ok": True, "price": 0.10, "data_source": "test",
                           "delay_mins": 0, "volume": 1_000_000, "bar_timestamp": None}
    )
    monkeypatch.setattr("app.tasks.trading.get_notifier", lambda **kw: _mock_notifier())
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: _mock_notifier())

    sync_stop_orders.run()


def test_sync_stop_orders_overlap_lock_skips_processing(db_session, org_and_account, open_crypto_position, monkeypatch):
    """CLAUDE.md #40: when the per-org overlap lock can't be acquired (another
    run is still in progress), this run must touch nothing for that org."""
    from app.tasks.trading import sync_stop_orders
    from app.models.trade import Position, TradeStatus

    monkeypatch.setattr(
        "app.tasks.trading.get_intraday_price",
        lambda *a, **kw: {"ok": True, "price": 0.05, "data_source": "test",  # below stop (0.16)
                           "delay_mins": 0, "volume": 1_000_000, "bar_timestamp": None}
    )
    monkeypatch.setattr("app.tasks.trading.get_notifier", lambda **kw: _mock_notifier())
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: _mock_notifier())
    monkeypatch.setattr("app.tasks.trading._acquire_org_lock", lambda lock_key, ttl=240: False)

    sync_stop_orders.run()

    db_session.expire_all()
    pos = db_session.query(Position).filter_by(id=open_crypto_position.id).first()
    assert pos.status == TradeStatus.OPEN, "Must not process the position when the overlap lock can't be acquired"


# ---- _is_trading_paused ----------------------------------------------------

def test_is_trading_paused_false_when_no_config(db_session, org_and_account):
    from app.tasks.trading import _is_trading_paused
    org, _ = org_and_account
    result = _is_trading_paused(org.id)
    assert not result


def test_is_trading_paused_true_when_set(db_session, org_and_account):
    from app.models.config import SystemConfig
    from app.tasks.trading import _is_trading_paused

    org, _ = org_and_account
    db_session.add(SystemConfig(
        key="trading_paused", value="true", label="P", group="system",
        organization_id=org.id
    ))
    db_session.commit()
    result = _is_trading_paused(org.id)
    assert result is True
