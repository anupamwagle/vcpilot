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
    key = f"last_market_regime_{exchange_key}"
    cfg = SystemConfig(key=key, value=regime, organization_id=org_id,
                       value_type="STRING", label="Market Regime")
    db.add(cfg)
    db.commit()


def _patch_market_open(monkeypatch, is_open=True):
    import app.tasks.trading as t
    monkeypatch.setattr(t, "market_is_open_now", lambda exchange_key: is_open)
    # Fix wall-clock time to a safe mid-session moment (11:30am Sydney) so the
    # opening-noise guard (CLAUDE.md #40 — skips the first N minutes after the
    # 10:00am ASX open) never makes this test flaky depending on when it
    # actually runs in the real world. Tests targeting the guard itself patch
    # get_current_time separately to a time inside the window.
    import pytz
    from datetime import datetime as _datetime
    fixed = pytz.timezone("Australia/Sydney").localize(_datetime(2026, 7, 1, 11, 30, 0))
    monkeypatch.setattr("app.utils.time_helper.get_current_time", lambda: fixed)


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
# Test: portfolio heat exceeded
#
# calculate_portfolio_heat()/check_portfolio_heat() in app/risk/manager.py were
# fully implemented and unit-tested but never wired into check_entry_triggers —
# the only pre-trade portfolio-level brake enforced was portfolio_max_positions.
# This verifies the new gate actually blocks entries once total open risk
# (as % of account capital) reaches the configured portfolio_max_heat_pct.
# ---------------------------------------------------------------------------

def test_entry_check_portfolio_heat_blocks_new_entry(db_session, org_and_account, monkeypatch):
    from app.tasks.trading import check_entry_triggers
    org, account = org_and_account
    # account.capital_aud = 1000.0 (from fixture). One open position with
    # risk = (45-30)*20 = 300 → heat = 300/1000 = 30%, well above the default 15% max.
    pos = Position(
        ticker="BHP.AX", exchange_key="ASX", asset_type="EQUITY", currency="AUD",
        account_id=account.id, organization_id=org.id,
        entry_date=date(2026, 1, 1), entry_price=Decimal("45.0"),
        qty=Decimal("20"), initial_stop=Decimal("30.0"), current_stop=Decimal("30.0"),
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

    # No new position opened for WOW.AX — only the pre-seeded BHP.AX position exists
    assert db_session.query(Position).filter(Position.status == TradeStatus.OPEN).count() == 1
    heat_logs = db_session.query(AuditLog).filter(
        AuditLog.message.like("%Portfolio heat%"),
    ).all()
    assert heat_logs, "Should write a portfolio-heat skip audit log"


def test_entry_check_portfolio_heat_within_limit_allows_entry(db_session, org_and_account, monkeypatch):
    from app.tasks.trading import check_entry_triggers
    org, account = org_and_account
    # Small open position: risk = (45-44)*1 = 1 → heat = 0.1%, well under 15% max.
    pos = Position(
        ticker="BHP.AX", exchange_key="ASX", asset_type="EQUITY", currency="AUD",
        account_id=account.id, organization_id=org.id,
        entry_date=date(2026, 1, 1), entry_price=Decimal("45.0"),
        qty=Decimal("1"), initial_stop=Decimal("44.0"), current_stop=Decimal("44.0"),
        status=TradeStatus.OPEN, is_paper=True,
    )
    db_session.add(pos)
    sig = _make_signal(db_session, org.id, account.id, ticker="WOW.AX", pivot=37.0)
    db_session.commit()

    _patch_market_open(monkeypatch, is_open=True)
    _patch_trading_paused(monkeypatch, paused=False)
    _seed_regime(db_session, org.id, "BULL")
    _patch_price_data(monkeypatch, close=37.5)
    _patch_rule_engine(monkeypatch, breakout_passes=True)
    _patch_sizing(monkeypatch)
    _patch_broker_simulate(monkeypatch)
    _patch_notifier(monkeypatch)

    check_entry_triggers.run(exchange_key="ASX")

    new_pos = db_session.query(Position).filter(Position.ticker == "WOW.AX").all()
    assert new_pos, "Entry should proceed when portfolio heat is within the configured limit"


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
    assert float(positions[0].pivot_price) == 37.0, (
        "Signal.pivot_price must be carried onto the Position (R3 / CLAUDE.md #42 — "
        "needed by exit_failed_breakout)"
    )

    # Signal should be flipped to TRIGGERED
    db_session.expire(sig)
    sig_refreshed = db_session.query(Signal).get(sig.id)
    assert sig_refreshed.status == SignalStatus.TRIGGERED


# ---------------------------------------------------------------------------
# Test: hard extension guard — don't chase a breakout past the max chase limit
# ---------------------------------------------------------------------------

def test_entry_check_extension_guard_skips_overextended_breakout(db_session, org_and_account, monkeypatch):
    """
    CLAUDE.md #39: check_breakout only validates price-vs-pivot at the moment
    it runs; price can keep moving before submission actually happens. A hard,
    always-applied guard re-checks live price against the pivot right before
    order submission and refuses to chase a breakout more than the seeded
    vcp_max_extension % (default 5%) past the pivot.
    """
    from app.tasks.trading import check_entry_triggers
    org, account = org_and_account
    sig = _make_signal(db_session, org.id, account.id, pivot=37.0)
    _patch_market_open(monkeypatch, is_open=True)
    _patch_trading_paused(monkeypatch, paused=False)
    _seed_regime(db_session, org.id, "BULL")
    _patch_price_data(monkeypatch, close=40.0)   # (40-37)/37 = 8.1% > default 5% max
    _patch_rule_engine(monkeypatch, breakout_passes=True)
    _patch_sizing(monkeypatch)
    _patch_broker_simulate(monkeypatch)
    _patch_notifier(monkeypatch)

    check_entry_triggers.run(exchange_key="ASX")

    positions = db_session.query(Position).filter(
        Position.organization_id == org.id, Position.ticker == "WOW.AX",
    ).all()
    assert not positions, "Must not open a position when the breakout is extended past the max chase limit"

    db_session.expire(sig)
    sig_refreshed = db_session.query(Signal).get(sig.id)
    assert sig_refreshed.status == SignalStatus.PENDING, (
        "Signal must stay PENDING (not TRIGGERED) when skipped for being overextended"
    )

    log = db_session.query(AuditLog).filter(
        AuditLog.ticker == "WOW.AX", AuditLog.message.like("%not chasing%"),
    ).first()
    assert log is not None


def test_entry_check_extension_guard_allows_breakout_within_range(db_session, org_and_account, monkeypatch):
    """A breakout within the max chase limit must proceed normally (regression
    guard against the extension check being overly aggressive)."""
    from app.tasks.trading import check_entry_triggers
    org, account = org_and_account
    _make_signal(db_session, org.id, account.id, pivot=37.0)
    _patch_market_open(monkeypatch, is_open=True)
    _patch_trading_paused(monkeypatch, paused=False)
    _seed_regime(db_session, org.id, "BULL")
    _patch_price_data(monkeypatch, close=38.0)   # (38-37)/37 = 2.7% < default 5% max
    _patch_rule_engine(monkeypatch, breakout_passes=True)
    _patch_sizing(monkeypatch)
    _patch_broker_simulate(monkeypatch)
    _patch_notifier(monkeypatch)

    check_entry_triggers.run(exchange_key="ASX")

    positions = db_session.query(Position).filter(
        Position.organization_id == org.id, Position.ticker == "WOW.AX",
    ).all()
    assert positions, "A breakout within the max chase limit must still open a position"


# ---------------------------------------------------------------------------
# Test: minimum liquidity filter (R2 / CLAUDE.md #42) — intraday re-check
# ---------------------------------------------------------------------------

def test_entry_check_liquidity_filter_skips_thin_stock(db_session, org_and_account, monkeypatch):
    from decimal import Decimal
    from app.tasks.trading import check_entry_triggers
    from app.models.config import RuleConfig
    org, account = org_and_account
    sig = _make_signal(db_session, org.id, account.id, pivot=37.0)
    db_session.add(RuleConfig(
        rule_id="entry_min_avg_dollar_volume", organization_id=org.id, category="ENTRY",
        label="Min liquidity", threshold=Decimal("500000.0"),
        enabled_globally=True, asset_types="EQUITY", is_mandatory=False,
    ))
    db_session.commit()

    _patch_market_open(monkeypatch, is_open=True)
    _patch_trading_paused(monkeypatch, paused=False)
    _seed_regime(db_session, org.id, "BULL")
    # close=37.5 x volume=1,000 = $37,500/day -- far below the $500k min.
    _patch_price_data(monkeypatch, close=37.5, volume=1_000)
    _patch_rule_engine(monkeypatch, breakout_passes=True)
    _patch_sizing(monkeypatch)
    _patch_broker_simulate(monkeypatch)
    _patch_notifier(monkeypatch)

    check_entry_triggers.run(exchange_key="ASX")

    positions = db_session.query(Position).filter(
        Position.organization_id == org.id, Position.ticker == "WOW.AX",
    ).all()
    assert not positions, "Must not open a position for a stock below the minimum liquidity threshold"

    db_session.expire(sig)
    sig_refreshed = db_session.query(Signal).get(sig.id)
    assert sig_refreshed.status == SignalStatus.PENDING

    log = db_session.query(AuditLog).filter(
        AuditLog.ticker == "WOW.AX", AuditLog.message.like("%avg $ volume%"),
    ).first()
    assert log is not None


# ---------------------------------------------------------------------------
# Test: available capital must not be double-counted across signals in one run
# ---------------------------------------------------------------------------

def test_entry_check_available_capital_not_double_counted_across_signals(db_session, org_and_account, monkeypatch):
    """
    check_entry_triggers re-queries open Positions/Orders fresh for every signal it
    processes, and each `with get_db()` block commits immediately — so by the time
    signal 2 is checked, signal 1's newly-opened position is already visible to that
    fresh query. A separate in-memory running total on top of that would subtract
    the same committed capital twice and wrongly starve later signals in a busy run.

    capital_aud=1000 (fixture default), ASX min_required=600. Both signals are sized
    (via a fixed calculate_position_size stub) at 50 shares @ $6 = $300 trade value:
      - Correct (single subtraction): signal 2 sees 1000-300=700 >= 600 -> proceeds.
      - Buggy (double subtraction):   signal 2 would see 1000-300-300=400 < 600 -> skipped.
    """
    from app.tasks.trading import check_entry_triggers
    from app.risk.manager import SizingResult
    import app.tasks.trading as t

    org, account = org_and_account
    _make_signal(db_session, org.id, account.id, ticker="WOW.AX", pivot=6.5)
    _make_signal(db_session, org.id, account.id, ticker="CSL.AX", pivot=6.5)

    _patch_market_open(monkeypatch, is_open=True)
    _patch_trading_paused(monkeypatch, paused=False)
    _seed_regime(db_session, org.id, "BULL")
    _patch_price_data(monkeypatch, close=6.0)
    _patch_rule_engine(monkeypatch, breakout_passes=True)
    _patch_broker_simulate(monkeypatch)
    _patch_notifier(monkeypatch)
    monkeypatch.setattr(t, "calculate_position_size",
                        lambda *a, **kw: SizingResult(
                            shares=50, capital_aud=300.0, capital_local=300.0,
                            risk_aud=10.0, risk_pct=1.0, portfolio_pct=30.0,
                            stop_price=5.98, entry_price=6.0,
                            currency="AUD", fx_rate_aud=1.0, message="fixed test sizing",
                        ))

    check_entry_triggers.run(exchange_key="ASX")

    positions = db_session.query(Position).filter(
        Position.organization_id == org.id,
        Position.ticker.in_(["WOW.AX", "CSL.AX"]),
        Position.status == TradeStatus.OPEN,
    ).all()
    tickers_opened = {p.ticker for p in positions}
    assert tickers_opened == {"WOW.AX", "CSL.AX"}, (
        f"Both signals should open positions — available capital must not be "
        f"double-counted across signals processed in the same run. Opened: {tickers_opened}"
    )


# ---------------------------------------------------------------------------
# T9 safety rails (CLAUDE.md #40): overlap lock, kill switch, daily loss halt,
# opening-noise guard
# ---------------------------------------------------------------------------

def test_entry_check_overlap_lock_skips_when_already_running(db_session, org_and_account, monkeypatch):
    """A run that can't acquire the per-org overlap lock must touch nothing —
    prevents two overlapping runs from double-spending capital / double-submitting."""
    import app.tasks.trading as t
    from app.tasks.trading import check_entry_triggers
    org, account = org_and_account
    sig = _make_signal(db_session, org.id, account.id, pivot=37.0)

    _patch_market_open(monkeypatch, is_open=True)
    _patch_trading_paused(monkeypatch, paused=False)
    _seed_regime(db_session, org.id, "BULL")
    _patch_price_data(monkeypatch, close=37.5)
    _patch_rule_engine(monkeypatch, breakout_passes=True)
    _patch_sizing(monkeypatch)
    _patch_broker_simulate(monkeypatch)
    _patch_notifier(monkeypatch)
    monkeypatch.setattr(t, "_acquire_org_lock", lambda lock_key, ttl=240: False)

    check_entry_triggers.run(exchange_key="ASX")

    positions = db_session.query(Position).filter(
        Position.organization_id == org.id, Position.ticker == "WOW.AX",
    ).all()
    assert not positions, "Must not process any signal when the overlap lock can't be acquired"

    db_session.expire(sig)
    sig_refreshed = db_session.query(Signal).get(sig.id)
    assert sig_refreshed.status == SignalStatus.PENDING


def test_entry_check_kill_switch_skips_all_signals(db_session, org_and_account, monkeypatch):
    """The kill switch must block entries even harder than PAUSE — checked
    before anything else in the per-org loop."""
    from app.tasks.trading import check_entry_triggers
    org, account = org_and_account
    sig = _make_signal(db_session, org.id, account.id, pivot=37.0)
    db_session.add(SystemConfig(key="trading_kill_switch", value="true",
                                organization_id=org.id, label="Kill Switch", group="trading"))
    db_session.commit()

    _patch_market_open(monkeypatch, is_open=True)
    _patch_trading_paused(monkeypatch, paused=False)
    _seed_regime(db_session, org.id, "BULL")
    _patch_price_data(monkeypatch, close=37.5)
    _patch_rule_engine(monkeypatch, breakout_passes=True)
    _patch_sizing(monkeypatch)
    _patch_broker_simulate(monkeypatch)
    _patch_notifier(monkeypatch)

    check_entry_triggers.run(exchange_key="ASX")

    positions = db_session.query(Position).filter(
        Position.organization_id == org.id, Position.ticker == "WOW.AX",
    ).all()
    assert not positions, "Kill switch must block all new entries"

    db_session.expire(sig)
    sig_refreshed = db_session.query(Signal).get(sig.id)
    assert sig_refreshed.status == SignalStatus.PENDING

    log = db_session.query(AuditLog).filter(
        AuditLog.ticker == "WOW.AX", AuditLog.message.like("%kill switch%"),
    ).first()
    assert log is not None


def test_entry_check_daily_loss_halt_skips_all_signals(db_session, org_and_account, monkeypatch):
    """Today's realised losses breaching max_daily_loss_aud must halt new
    entries for the rest of the day and alert once."""
    from unittest.mock import MagicMock
    import app.tasks.trading as t
    from app.tasks.trading import check_entry_triggers
    from app.models.trade import Trade, ExitReason
    org, account = org_and_account
    sig = _make_signal(db_session, org.id, account.id, pivot=37.0)
    db_session.add(SystemConfig(key="max_daily_loss_aud", value="100",
                                organization_id=org.id, label="Max Daily Loss", group="trading"))
    db_session.add(Trade(
        ticker="XYZ.AX", exchange_key="ASX", asset_type="EQUITY", currency="AUD",
        account_id=account.id, organization_id=org.id,
        entry_date=date(2026, 7, 1), exit_date=date(2026, 7, 1), hold_days=0,
        entry_price=10.0, exit_price=8.0, qty=100,
        gross_pnl_aud=-200.0, net_pnl_aud=-200.0, pnl_pct=-0.20,
        initial_stop=9.0, exit_reason=ExitReason.STOP_LOSS, is_paper=True,
    ))
    db_session.commit()

    _patch_market_open(monkeypatch, is_open=True)   # fixes "today" to 2026-07-01
    _patch_trading_paused(monkeypatch, paused=False)
    _seed_regime(db_session, org.id, "BULL")
    _patch_price_data(monkeypatch, close=37.5)
    _patch_rule_engine(monkeypatch, breakout_passes=True)
    _patch_sizing(monkeypatch)
    _patch_broker_simulate(monkeypatch)
    mock_notifier = MagicMock()
    monkeypatch.setattr(t, "get_notifier", lambda organization_id=None: mock_notifier)

    check_entry_triggers.run(exchange_key="ASX")

    positions = db_session.query(Position).filter(
        Position.organization_id == org.id, Position.ticker == "WOW.AX",
    ).all()
    assert not positions, "Max daily loss halt must block new entries"

    db_session.expire(sig)
    sig_refreshed = db_session.query(Signal).get(sig.id)
    assert sig_refreshed.status == SignalStatus.PENDING

    log = db_session.query(AuditLog).filter(
        AuditLog.ticker == "WOW.AX", AuditLog.message.like("%daily loss halt%"),
    ).first()
    assert log is not None
    mock_notifier.send.assert_called_once()


def test_entry_check_opening_noise_guard_skips_near_open(db_session, org_and_account, monkeypatch):
    """The staggered ASX opening auction (10:00-10:09) can confirm false
    breakouts on partial-day volume — must be skipped by default."""
    import pytz
    from datetime import datetime as _datetime
    from app.tasks.trading import check_entry_triggers
    org, account = org_and_account
    sig = _make_signal(db_session, org.id, account.id, pivot=37.0)

    _patch_market_open(monkeypatch, is_open=True)
    _patch_trading_paused(monkeypatch, paused=False)
    _seed_regime(db_session, org.id, "BULL")
    _patch_price_data(monkeypatch, close=37.5)
    _patch_rule_engine(monkeypatch, breakout_passes=True)
    _patch_sizing(monkeypatch)
    _patch_broker_simulate(monkeypatch)
    _patch_notifier(monkeypatch)
    near_open = pytz.timezone("Australia/Sydney").localize(_datetime(2026, 7, 1, 10, 5, 0))
    monkeypatch.setattr("app.utils.time_helper.get_current_time", lambda: near_open)

    check_entry_triggers.run(exchange_key="ASX")

    positions = db_session.query(Position).filter(
        Position.organization_id == org.id, Position.ticker == "WOW.AX",
    ).all()
    assert not positions, "Must not process signals within the opening-noise window"

    db_session.expire(sig)
    sig_refreshed = db_session.query(Signal).get(sig.id)
    assert sig_refreshed.status == SignalStatus.PENDING

    log = db_session.query(AuditLog).filter(
        AuditLog.ticker == "WOW.AX", AuditLog.message.like("%opening-noise%"),
    ).first()
    assert log is not None


def test_entry_check_opening_noise_guard_allows_outside_window(db_session, org_and_account, monkeypatch):
    """Regression guard: outside the opening-noise window, a confirmed
    breakout must still proceed normally."""
    import pytz
    from datetime import datetime as _datetime
    from app.tasks.trading import check_entry_triggers
    org, account = org_and_account
    _make_signal(db_session, org.id, account.id, pivot=37.0)

    _patch_market_open(monkeypatch, is_open=True)
    _patch_trading_paused(monkeypatch, paused=False)
    _seed_regime(db_session, org.id, "BULL")
    _patch_price_data(monkeypatch, close=37.5)
    _patch_rule_engine(monkeypatch, breakout_passes=True)
    _patch_sizing(monkeypatch)
    _patch_broker_simulate(monkeypatch)
    _patch_notifier(monkeypatch)
    outside_window = pytz.timezone("Australia/Sydney").localize(_datetime(2026, 7, 1, 10, 15, 0))
    monkeypatch.setattr("app.utils.time_helper.get_current_time", lambda: outside_window)

    check_entry_triggers.run(exchange_key="ASX")

    positions = db_session.query(Position).filter(
        Position.organization_id == org.id, Position.ticker == "WOW.AX",
    ).all()
    assert positions, "Outside the opening-noise window, a confirmed breakout must still open a position"
