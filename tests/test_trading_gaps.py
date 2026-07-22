"""
Tests targeting uncovered paths in app/tasks/trading.py.
Focuses on trading-paused skip, broker error path, exit rule execution.
"""
import pytest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch


def _make_open_position(db, org_id, ticker="BHP.AX", exchange_key="ASX",
                        entry_price=40.0, qty=100, stop=38.0):
    from app.models.trade import Position, TradeStatus
    from app.models.account import Account
    account = db.query(Account).filter(Account.organization_id == org_id).first()
    pos = Position(
        ticker=ticker,
        exchange_key=exchange_key,
        asset_type="EQUITY" if not exchange_key.startswith("CRYPTO") else "CRYPTO",
        organization_id=org_id,
        account_id=account.id if account else None,
        status=TradeStatus.OPEN,
        entry_price=entry_price,
        qty=qty,
        current_stop=stop,
        initial_stop=stop,
        target_1=entry_price * 1.2,
        target_2=entry_price * 1.4,
        entry_date=date.today(),
        is_paper=True,
    )
    db.add(pos)
    db.commit()
    return pos


def _make_pending_signal(db, org_id, ticker="BHP.AX", exchange_key="ASX"):
    from app.models.signal import Signal, SignalStatus
    sig = Signal(
        ticker=ticker, exchange_key=exchange_key, asset_type="EQUITY",
        currency="AUD", signal_date=date.today(),
        status=SignalStatus.PENDING,
        close_price=40.0, pivot_price=41.0, stop_price=38.0,
        organization_id=org_id,
    )
    db.add(sig)
    db.commit()
    return sig


# ────────────────────────────────────────────────────────────
# check_entry_triggers — trading paused path
# ────────────────────────────────────────────────────────────

def test_check_entry_triggers_paused_writes_audit(db_session, org_and_account, monkeypatch):
    from app.tasks.trading import check_entry_triggers
    from app.models.audit import AuditLog

    org, _ = org_and_account
    sig = _make_pending_signal(db_session, org.id)

    monkeypatch.setattr("app.tasks.trading.market_is_open_now", lambda *a, **kw: True)
    monkeypatch.setattr("app.tasks.trading._is_trading_paused", lambda org_id: True)
    monkeypatch.setattr("app.utils.time_helper.get_current_date", lambda: date.today())

    check_entry_triggers.run("ASX")

    # Should have written a paused audit log entry
    logs = db_session.query(AuditLog).filter(
        AuditLog.organization_id == org.id,
        AuditLog.ticker == sig.ticker,
    ).all()
    assert any("paused" in (l.message or "").lower() for l in logs)


# ────────────────────────────────────────────────────────────
# check_entry_triggers — broker error path
# ────────────────────────────────────────────────────────────

def test_check_entry_triggers_broker_error_leaves_signal_pending(db_session, org_and_account, monkeypatch):
    from app.tasks.trading import check_entry_triggers
    from app.models.signal import Signal, SignalStatus
    from app.risk.manager import SizingResult

    org, _ = org_and_account
    sig = _make_pending_signal(db_session, org.id)

    sizing = SizingResult(10, 10, 400.0, 380.0, 20.0, 200.0, 38.0, 40.0, "AUD", 1.0, "OK")

    monkeypatch.setattr("app.tasks.trading.market_is_open_now", lambda *a, **kw: True)
    monkeypatch.setattr("app.tasks.trading._is_trading_paused", lambda org_id: False)
    monkeypatch.setattr("app.utils.time_helper.get_current_date", lambda: date.today())
    monkeypatch.setattr("app.tasks.trading.get_notifier",
                        lambda organization_id=None: MagicMock())
    monkeypatch.setattr("app.tasks.trading.get_intraday_price",
                        lambda *a, **kw: {"ok": True, "price": 41.5, "volume": 100000,
                                          "data_source": "yfinance", "delay_mins": 15,
                                          "bar_timestamp": None})
    monkeypatch.setattr("app.risk.manager.calculate_position_size", lambda **kw: sizing)

    mock_engine = MagicMock()
    mock_engine.threshold.return_value = None
    mock_engine.is_enabled.return_value = True
    mock_engine.clear_signal_overrides = MagicMock()
    monkeypatch.setattr("app.tasks.trading.RuleEngine", lambda **kw: mock_engine)

    from app.broker.ibkr import IBKRBroker
    monkeypatch.setattr(IBKRBroker, "connect", lambda self: False)
    monkeypatch.setattr(IBKRBroker, "submit_bracket_order",
                        lambda self, **kw: {"status": "error", "error": "margin insufficient"})

    check_entry_triggers.run("ASX")

    db_session.expire_all()
    sig_refreshed = db_session.query(Signal).filter(Signal.id == sig.id).first()
    assert sig_refreshed.status == SignalStatus.PENDING


# ────────────────────────────────────────────────────────────
# check_entry_triggers — market closed path
# ────────────────────────────────────────────────────────────

def test_check_entry_triggers_market_closed_returns_early(db_session, org_and_account, monkeypatch):
    from app.tasks.trading import check_entry_triggers

    monkeypatch.setattr("app.tasks.trading.market_is_open_now", lambda *a, **kw: False)

    # Should not raise
    check_entry_triggers.run("ASX")


# ────────────────────────────────────────────────────────────
# check_exit_rules_task — exit triggered writes Trade
# ────────────────────────────────────────────────────────────

def test_check_exit_rules_task_defers_equity_stop_to_broker_sync(db_session, org_and_account, monkeypatch):
    from app.tasks.trading import check_exit_rules_task
    from app.models.trade import Trade, Position, TradeStatus
    from app.screener.exit_rules import ExitSignal

    org, _ = org_and_account
    pos = _make_open_position(db_session, org.id, entry_price=40.0, stop=38.0)

    exit_signal = ExitSignal(
        should_exit=True,
        reason="STOP_LOSS",
        message="Stop hit",
        exit_type="FULL",
    )

    monkeypatch.setattr("app.tasks.trading.market_is_open_now", lambda *a, **kw: True)
    monkeypatch.setattr("app.tasks.trading._is_trading_paused", lambda org_id: False)
    monkeypatch.setattr("app.utils.time_helper.get_current_date", lambda: date.today())
    monkeypatch.setattr("app.tasks.trading.get_notifier",
                        lambda organization_id=None: MagicMock())
    monkeypatch.setattr("app.tasks.trading.get_intraday_price",
                        lambda *a, **kw: {"ok": True, "price": 37.5, "volume": 200000,
                                          "data_source": "yfinance", "delay_mins": 15,
                                          "bar_timestamp": None})

    mock_engine = MagicMock()
    mock_engine.threshold.return_value = None
    monkeypatch.setattr("app.tasks.trading.RuleEngine", lambda **kw: mock_engine)
    monkeypatch.setattr("app.tasks.trading.evaluate_exit_rules",
                        lambda *a, **kw: [exit_signal])

    from app.broker.ibkr import IBKRBroker
    monkeypatch.setattr(IBKRBroker, "connect", lambda self: False)
    monkeypatch.setattr(IBKRBroker, "submit_bracket_order",
                        lambda self, **kw: {"status": "simulated", "order_id": "SIM-EXIT"})

    check_exit_rules_task.run("ASX")

    db_session.expire_all()
    pos_refreshed = db_session.query(Position).filter(Position.id == pos.id).first()
    assert pos_refreshed.status == TradeStatus.OPEN

    trade = db_session.query(Trade).filter(
        Trade.organization_id == org.id, Trade.ticker == "BHP.AX"
    ).first()
    assert trade is None


def test_check_exit_rules_task_failed_breakout_creates_trade(db_session, org_and_account, monkeypatch):
    """
    R3 / CLAUDE.md #42, end-to-end wiring: a Position with pivot_price carried
    over (T1's creation paths) that closes back below its pivot within the
    configured window gets closed via the real (unmocked) evaluate_exit_rules,
    with exit_reason=FAILED_BREAKOUT.
    """
    from app.tasks.trading import check_exit_rules_task
    from app.models.trade import Trade, Position, TradeStatus, ExitReason
    from app.models.account import Account

    org, _ = org_and_account
    account = db_session.query(Account).filter(Account.organization_id == org.id).first()
    pos = Position(
        ticker="BHP.AX", exchange_key="ASX", asset_type="EQUITY",
        organization_id=org.id, account_id=account.id if account else None,
        status=TradeStatus.OPEN, entry_price=40.0, qty=100,
        current_stop=36.0, initial_stop=36.0, target_1=48.0, target_2=56.0,
        pivot_price=40.0, entry_date=date.today() - timedelta(days=1),  # entered yesterday
        is_paper=True,
    )
    db_session.add(pos)
    db_session.commit()

    monkeypatch.setattr("app.tasks.trading.market_is_open_now", lambda *a, **kw: True)
    monkeypatch.setattr("app.tasks.trading._is_trading_paused", lambda org_id: False)
    monkeypatch.setattr("app.utils.time_helper.get_current_date", lambda: date.today())
    monkeypatch.setattr("app.tasks.trading.get_notifier", lambda organization_id=None: MagicMock())
    # Close (39.0) back below pivot (40.0), well above the stop (36.0).
    monkeypatch.setattr("app.tasks.trading.get_intraday_price",
                        lambda *a, **kw: {"ok": True, "price": 39.0, "volume": 200000,
                                          "data_source": "yfinance", "delay_mins": 15,
                                          "bar_timestamp": None})
    monkeypatch.setattr("app.tasks.trading.get_price_history",
                        lambda *a, **kw: __import__("pandas").DataFrame({
                            "date": [date.today() - timedelta(days=i) for i in range(60, 0, -1)],
                            "open": [39.0] * 60, "high": [39.5] * 60, "low": [38.5] * 60,
                            "close": [39.0] * 60, "volume": [200000] * 60,
                            "avg_vol_50": [200000] * 60,
                        }))
    monkeypatch.setattr("app.data.fetcher.get_fundamentals", lambda *a, **kw: {})

    class _FailedBreakoutOnlyEngine:
        def is_enabled(self, rule_id):
            return rule_id == "exit_failed_breakout"
        def threshold(self, rule_id):
            return 3.0 if rule_id == "exit_failed_breakout" else None

    monkeypatch.setattr("app.tasks.trading.RuleEngine", lambda **kw: _FailedBreakoutOnlyEngine())

    from app.broker.ibkr import IBKRBroker
    monkeypatch.setattr(IBKRBroker, "connect", lambda self: False)
    monkeypatch.setattr(IBKRBroker, "submit_bracket_order",
                        lambda self, **kw: {"status": "simulated", "order_id": "SIM-EXIT"})

    check_exit_rules_task.run("ASX")

    db_session.expire_all()
    pos_refreshed = db_session.query(Position).filter(Position.id == pos.id).first()
    assert pos_refreshed.status == TradeStatus.CLOSED

    trade = db_session.query(Trade).filter(
        Trade.organization_id == org.id, Trade.ticker == "BHP.AX"
    ).first()
    assert trade is not None
    assert trade.exit_reason == ExitReason.FAILED_BREAKOUT


# ────────────────────────────────────────────────────────────
# update_position_pnl_task — updates current_price
# ────────────────────────────────────────────────────────────

def test_update_position_pnl_task_crypto_updates(db_session, org_and_account, monkeypatch):
    from app.tasks.trading import update_position_pnl_task
    from app.models.trade import Position

    org, _ = org_and_account
    pos = _make_open_position(db_session, org.id, ticker="BTC-AUD",
                               exchange_key="CRYPTO_INDEPENDENTRESERVE",
                               entry_price=80000.0, qty=0.1, stop=75000.0)

    monkeypatch.setattr("app.data.fetcher.get_intraday_price",
                        lambda *a, **kw: {"ok": True, "price": 85000.0, "volume": 500,
                                          "data_source": "ir", "delay_mins": 0,
                                          "bar_timestamp": None})

    update_position_pnl_task.run()

    db_session.expire_all()
    pos_refreshed = db_session.query(Position).filter(Position.id == pos.id).first()
    assert pos_refreshed.current_price == 85000.0


# ────────────────────────────────────────────────────────────
# sync_stop_orders — no positions path (fast return)
# ────────────────────────────────────────────────────────────

def test_sync_stop_orders_no_positions(db_session, org_and_account, monkeypatch):
    from app.tasks.trading import sync_stop_orders

    monkeypatch.setattr("app.utils.time_helper.get_current_date", lambda: date.today())
    monkeypatch.setattr("app.tasks.trading.get_notifier",
                        lambda organization_id=None: MagicMock())

    # Should not raise when no open positions
    sync_stop_orders.run()
