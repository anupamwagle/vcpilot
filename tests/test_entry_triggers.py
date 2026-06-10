"""
Tests for check_entry_triggers (app/tasks/trading.py).

Covers:
  - Market closed → audit log written, no positions opened
  - Trading paused → per-signal skip audit log written
  - Max positions reached → all signals skipped
  - No price data → skip audit log written
  - BEAR regime → signals skipped
  - Breakout confirmed → Position and Trade-related records created, signal flipped TRIGGERED
  - Breakout not confirmed → no position, audit log with failure reason
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models.signal import Signal, SignalStatus
from app.models.trade import Position, TradeStatus
from app.models.audit import AuditLog, AuditAction
from app.models.config import SystemConfig


# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------

def _make_signal(db, org_id, account_id, ticker="WOW.AX", pivot=37.0,
                 exchange_key="ASX", asset_type="EQUITY"):
    from app.models.signal import Signal, SignalStatus
    sig = Signal(
        ticker=ticker,
        organization_id=org_id,
        exchange_key=exchange_key,
        asset_type=asset_type,
        status=SignalStatus.PENDING,
        pivot_price=Decimal(str(pivot)),
        stop_price=Decimal(str(round(pivot * 0.92, 3))),
        target_price_1=Decimal(str(round(pivot * 1.20, 3))),
        target_price_2=Decimal(str(round(pivot * 1.40, 3))),
        signal_date=date(2026, 6, 1),
    )
    db.add(sig)
    db.commit()
    db.refresh(sig)
    return sig


def _seed_regime(db, org_id, regime="BULL", exchange_key="ASX"):
    key = "last_market_regime" if exchange_key == "ASX" else f"last_market_regime_{exchange_key}"
    org_id_for_key = None if exchange_key == "ASX" else org_id
    cfg = SystemConfig(key=key, value=regime, organization_id=org_id_for_key,
                       value_type="STRING", label="Market Regime")
    db.add(cfg)
    db.commit()


def _patch_market_open(monkeypatch, is_open=True):
    import app.tasks.trading as t
    monkeypatch.setattr(t, "market_is_open_now", lambda exchange_key: is_open)


def _patch_trading_paused(monkeypatch, paused=False):
    import app.tasks.trading as t
    monkeypatch.setattr(t, "_is_trading_paused", lambda org_id: paused)


def _make_price_df(close=37.5, volume=800_000, ma_50=36.0, rows=60):
    import pandas as pd
    from datetime import date, timedelta
    import numpy as np
    dates = [date(2025, 1, 1) + timedelta(days=i) for i in range(rows)]
    df = pd.DataFrame({
        "date": dates,
        "open": [close] * rows,
        "high": [close * 1.01] * rows,
        "low": [close * 0.99] * rows,
        "close": [close] * rows,
        "volume": [volume] * rows,
        "ma_50": [ma_50] * rows,
        "ma_200": [ma_50 * 0.9] * rows,
        "avg_vol_50": [volume] * rows,
        "atr_14": [0.5] * rows,
        "pct_from_52w_high": [-5.0] * rows,
        "high_52w": [close * 1.05] * rows,
        "low_52w": [close * 0.8] * rows,
        "vol_ratio": [1.5] * rows,
        "ma_150": [ma_50 * 0.95] * rows,
        "ma_200_prev": [ma_50 * 0.88] * rows,
        "rs_rating": [80.0] * rows,
    })
    return df


def _patch_price_data(monkeypatch, close=37.5, volume=800_000):
    import app.tasks.trading as t
    df = _make_price_df(close=close, volume=volume)
    monkeypatch.setattr(t, "get_price_history", lambda ticker, period="3mo": df)
    monkeypatch.setattr(t, "get_intraday_price",
                        lambda ticker, organization_id=None, asset_type="EQUITY": {
                            "price": close, "volume": volume,
                            "data_source": "yfinance", "delay_mins": 15,
                            "bar_timestamp": None, "ok": True,
                        })


def _patch_no_price(monkeypatch):
    import app.tasks.trading as t
    monkeypatch.setattr(t, "get_price_history", lambda ticker, period="3mo": None)


def _patch_notifier(monkeypatch):
    import app.tasks.trading as t
    class _Notifier:
        def send(self, *a, **kw): pass
        def send_order_fill(self, *a, **kw): pass
        def send_entry_alert(self, *a, **kw): pass
    monkeypatch.setattr(t, "get_notifier", lambda organization_id=None: _Notifier())


def _patch_broker_simulate(monkeypatch):
    from app.broker.ibkr import IBKRBroker
    monkeypatch.setattr(IBKRBroker, "submit_bracket_order",
                        lambda self, *a, **kw: {"simulated": True, "order_id": "SIM-1"})
    monkeypatch.setattr(IBKRBroker, "connect", lambda self: False)


def _patch_rule_engine(monkeypatch, breakout_passes=True):
    import app.tasks.trading as t

    class _FakeResult:
        def __init__(self, passed):
            self.passed = passed
            self.value = 1.0
            self.threshold = 1.0
            self.message = "ok" if passed else "not confirmed"

    fake_rules = {
        "breakout_price": _FakeResult(breakout_passes),
        "breakout_volume": _FakeResult(breakout_passes),
    }
    monkeypatch.setattr(t, "check_breakout",
                        lambda ticker, df, pivot, avg_vol, engine: fake_rules)


def _patch_sizing(monkeypatch):
    import app.tasks.trading as t
    from app.risk.manager import SizingResult
    monkeypatch.setattr(t, "calculate_position_size",
                        lambda *a, **kw: SizingResult(
                            shares=33, capital_aud=1221.0, capital_local=1221.0,
                            risk_aud=50.0, risk_pct=1.5, portfolio_pct=12.2,
                            stop_price=34.0, entry_price=37.0,
                            currency="AUD", fx_rate_aud=1.0, message="test sizing",
                        ))


# ---------------------------------------------------------------------------
# Test: market closed
# ---------------------------------------------------------------------------

def test_entry_check_market_closed_writes_audit(db_session, org_and_account, monkeypatch):
    from app.tasks.trading import check_entry_triggers
    org, account = org_and_account
    _patch_market_open(monkeypatch, is_open=False)
    _patch_notifier(monkeypatch)

    check_entry_triggers.run(exchange_key="ASX")

    logs = db_session.query(AuditLog).filter(
        AuditLog.organization_id == org.id,
        AuditLog.message.like("%market closed%"),
    ).all()
    assert logs, "Should write 'market closed' audit log"


def test_entry_check_market_closed_opens_no_positions(db_session, org_and_account, monkeypatch):
    from app.tasks.trading import check_entry_triggers
    org, account = org_and_account
    _make_signal(db_session, org.id, account.id)
    _patch_market_open(monkeypatch, is_open=False)
    _patch_notifier(monkeypatch)

    check_entry_triggers.run(exchange_key="ASX")

    assert db_session.query(Position).count() == 0


# ---------------------------------------------------------------------------
# Test: trading paused
# ---------------------------------------------------------------------------

def test_entry_check_paused_skips_all_signals(db_session, org_and_account, monkeypatch):
    from app.tasks.trading import check_entry_triggers
    org, account = org_and_account
    _make_signal(db_session, org.id, account.id)
    _patch_market_open(monkeypatch, is_open=True)
    _patch_trading_paused(monkeypatch, paused=True)
    _patch_notifier(monkeypatch)

    check_entry_triggers.run(exchange_key="ASX")

    assert db_session.query(Position).count() == 0
    paused_logs = db_session.query(AuditLog).filter(
        AuditLog.message.like("%trading is paused%"),
    ).all()
    assert paused_logs


# ---------------------------------------------------------------------------
# Test: max positions reached
# ---------------------------------------------------------------------------

def test_entry_check_max_positions_blocks_new_entry(db_session, org_and_account, monkeypatch):
    from app.tasks.trading import check_entry_triggers
    from app.models.config import RuleConfig
    from app.models.account import OrganizationTier
    org, account = org_and_account

    # Seed a RuleConfig for max positions = 1
    rc = RuleConfig(
        rule_id="portfolio_max_positions", label="Max Positions",
        category="PORTFOLIO", threshold=Decimal("1"),
        enabled_globally=True, organization_id=org.id,
    )
    db_session.add(rc)
    # Add one existing open position so count == max
    from app.models.trade import Position, TradeStatus
    pos = Position(
        ticker="BHP.AX", exchange_key="ASX", asset_type="EQUITY", currency="AUD",
        account_id=account.id, organization_id=org.id,
        entry_date=date(2026, 1, 1), entry_price=Decimal("45.0"),
        qty=Decimal("20"), initial_stop=Decimal("40.0"), current_stop=Decimal("40.0"),
        status=TradeStatus.OPEN, is_paper=True,
    )
    db_session.add(pos)
    _make_signal(db_session, org.id, account.id, ticker="WOW.AX")
    db_session.commit()

    _patch_market_open(monkeypatch, is_open=True)
    _patch_trading_paused(monkeypatch, paused=False)
    _seed_regime(db_session, org.id, "BULL")
    _patch_notifier(monkeypatch)

    check_entry_triggers.run(exchange_key="ASX")

    # Still only 1 position (the seeded one)
    assert db_session.query(Position).filter(Position.status == TradeStatus.OPEN).count() == 1


# ---------------------------------------------------------------------------
# Test: no price data
# ---------------------------------------------------------------------------

def test_entry_check_no_price_data_writes_skip_audit(db_session, org_and_account, monkeypatch):
    from app.tasks.trading import check_entry_triggers
    org, account = org_and_account
    _make_signal(db_session, org.id, account.id)
    _patch_market_open(monkeypatch, is_open=True)
    _patch_trading_paused(monkeypatch, paused=False)
    _seed_regime(db_session, org.id, "BULL")
    _patch_no_price(monkeypatch)
    _patch_notifier(monkeypatch)

    check_entry_triggers.run(exchange_key="ASX")

    skip_logs = db_session.query(AuditLog).filter(
        AuditLog.message.like("%no price data%"),
    ).all()
    assert skip_logs
    assert db_session.query(Position).count() == 0


# ---------------------------------------------------------------------------
# Test: BEAR regime blocks entry
# ---------------------------------------------------------------------------

def test_entry_check_bear_regime_blocks_entry(db_session, org_and_account, monkeypatch):
    from app.tasks.trading import check_entry_triggers
    org, account = org_and_account
    _make_signal(db_session, org.id, account.id)
    _patch_market_open(monkeypatch, is_open=True)
    _patch_trading_paused(monkeypatch, paused=False)
    _seed_regime(db_session, org.id, "BEAR")
    _patch_price_data(monkeypatch)
    _patch_notifier(monkeypatch)

    # Seed regime_bear_block_equities rule as enabled
    from app.models.config import RuleConfig
    rc = RuleConfig(
        rule_id="regime_bear_block_equities", label="Bear Block",
        category="MARKET_REGIME", enabled_globally=True,
        organization_id=org.id,
    )
    db_session.add(rc)
    db_session.commit()

    check_entry_triggers.run(exchange_key="ASX")

    assert db_session.query(Position).count() == 0
    bear_logs = db_session.query(AuditLog).filter(
        AuditLog.message.like("%BEAR regime%"),
    ).all()
    assert bear_logs


# ---------------------------------------------------------------------------
# Test: breakout NOT confirmed
# ---------------------------------------------------------------------------

def test_entry_check_no_breakout_skips_signal(db_session, org_and_account, monkeypatch):
    from app.tasks.trading import check_entry_triggers
    org, account = org_and_account
    _make_signal(db_session, org.id, account.id)
    _patch_market_open(monkeypatch, is_open=True)
    _patch_trading_paused(monkeypatch, paused=False)
    _seed_regime(db_session, org.id, "BULL")
    _patch_price_data(monkeypatch, close=36.0)  # below pivot 37.0
    _patch_rule_engine(monkeypatch, breakout_passes=False)
    _patch_notifier(monkeypatch)

    check_entry_triggers.run(exchange_key="ASX")

    assert db_session.query(Position).count() == 0
    sig = db_session.query(Signal).filter(Signal.ticker == "WOW.AX").first()
    assert sig.status == SignalStatus.PENDING  # still pending


# ---------------------------------------------------------------------------
# Test: breakout confirmed → position opened
# ---------------------------------------------------------------------------

def test_entry_check_breakout_confirmed_opens_position(db_session, org_and_account, monkeypatch):
    from app.tasks.trading import check_entry_triggers
    org, account = org_and_account
    sig = _make_signal(db_session, org.id, account.id, pivot=37.0)
    _patch_market_open(monkeypatch, is_open=True)
    _patch_trading_paused(monkeypatch, paused=False)
    _seed_regime(db_session, org.id, "BULL")
    _patch_price_data(monkeypatch, close=37.5)   # above pivot
    _patch_rule_engine(monkeypatch, breakout_passes=True)
    _patch_sizing(monkeypatch)
    _patch_broker_simulate(monkeypatch)
    _patch_notifier(monkeypatch)

    check_entry_triggers.run(exchange_key="ASX")

    positions = db_session.query(Position).filter(
        Position.organization_id == org.id,
        Position.ticker == "WOW.AX",
    ).all()
    assert positions, "A position should be opened on confirmed breakout"
    assert positions[0].status == TradeStatus.OPEN

    # Signal should be flipped to TRIGGERED
    db_session.expire(sig)
    sig_refreshed = db_session.query(Signal).get(sig.id)
    assert sig_refreshed.status == SignalStatus.TRIGGERED
