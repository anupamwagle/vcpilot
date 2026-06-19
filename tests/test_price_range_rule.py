"""
Tests for the configurable Share Price Range Filter
(entry_min_share_price / entry_max_share_price).

Covers:
  - Unit behavior of app/screener/price_filter.py (evaluate_price_range,
    price_in_range) against a real RuleEngine + RuleConfig rows
  - The threshold=0.0-is-falsy edge case (RuleConfig.threshold_for_tier)
  - Wiring into app/trading/order_executor.py::execute_signal_order
    (the single shared MCP/WhatsApp order-submission choke point)
  - Wiring into app/tasks/screening.py::screen_single_ticker (manual add)
  - Wiring into app/tasks/trading.py::check_entry_triggers (intraday gate)

Default state (no RuleConfig rows seeded by org_and_account) must always
behave as a no-op — the filter has zero effect until an org explicitly
enables it via /admin/rules and sets a threshold.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from app.models.config import RuleConfig
from app.models.audit import AuditLog


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_rule(db, org_id, rule_id, threshold=None, enabled_globally=True,
               asset_types="EQUITY", label=None):
    rc = RuleConfig(
        rule_id=rule_id,
        organization_id=org_id,
        category="ENTRY",
        label=label or rule_id,
        threshold=Decimal(str(threshold)) if threshold is not None else None,
        enabled_globally=enabled_globally,
        asset_types=asset_types,
        is_mandatory=False,
    )
    db.add(rc)
    db.commit()
    return rc


def _make_engine(org, asset_type="EQUITY"):
    from app.screener.rules import RuleEngine
    return RuleEngine(organization_id=org.id, tier=org.tier.value, asset_type=asset_type)


def _make_price_df(rows=60, close=50.0):
    dates = [date(2025, 1, 1) + timedelta(days=i) for i in range(rows)]
    c = np.full(rows, close)
    df = pd.DataFrame({
        "date": dates, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
        "volume": np.full(rows, 500_000.0), "adj_close": c,
    })
    for col in ["ma_10", "ma_21", "ma_50", "ma_150", "ma_200", "ma_200_prev",
                "avg_vol_50", "vol_ratio", "high_52w", "low_52w",
                "pct_from_52w_high", "pct_from_52w_low", "atr_14", "rs_rating"]:
        df[col] = c
    return df


# ===========================================================================
# Unit tests — app/screener/price_filter.py
# ===========================================================================

def test_no_rules_configured_always_passes(db_session, org_and_account):
    """Default state — org_and_account seeds zero RuleConfig rows."""
    from app.screener.price_filter import price_in_range, evaluate_price_range
    org, _ = org_and_account
    engine = _make_engine(org)

    for price in (0.001, 0.5, 1.0, 50.0, 999.0):
        in_range, reason = price_in_range("BHP.AX", price, engine)
        assert in_range is True
        assert reason is None
    assert evaluate_price_range("BHP.AX", 0.5, engine) == {}


def test_crypto_asset_type_always_short_circuits(db_session, org_and_account):
    """Even with both rules enabled + tight thresholds, CRYPTO always passes."""
    from app.screener.price_filter import price_in_range
    org, _ = org_and_account
    _seed_rule(db_session, org.id, "entry_min_share_price", threshold=10.0)
    _seed_rule(db_session, org.id, "entry_max_share_price", threshold=20.0)
    engine = _make_engine(org, asset_type="CRYPTO")

    in_range, reason = price_in_range("BTC-AUD", 0.0001, engine, asset_type="CRYPTO")
    assert in_range is True
    assert reason is None


def test_min_only_enabled_below_min_fails(db_session, org_and_account):
    from app.screener.price_filter import price_in_range
    org, _ = org_and_account
    _seed_rule(db_session, org.id, "entry_min_share_price", threshold=0.10)
    engine = _make_engine(org)

    in_range, reason = price_in_range("PNV.AX", 0.05, engine)
    assert in_range is False
    assert "min" in reason.lower()


def test_min_only_enabled_above_min_passes_no_max_constraint(db_session, org_and_account):
    from app.screener.price_filter import price_in_range
    org, _ = org_and_account
    _seed_rule(db_session, org.id, "entry_min_share_price", threshold=0.10)
    engine = _make_engine(org)

    # No max rule seeded at all — arbitrarily high price still passes
    in_range, reason = price_in_range("CSL.AX", 250.0, engine)
    assert in_range is True
    assert reason is None


def test_max_only_enabled_above_max_fails(db_session, org_and_account):
    from app.screener.price_filter import price_in_range
    org, _ = org_and_account
    _seed_rule(db_session, org.id, "entry_max_share_price", threshold=1.00)
    engine = _make_engine(org)

    in_range, reason = price_in_range("CSL.AX", 5.0, engine)
    assert in_range is False
    assert "max" in reason.lower()


def test_max_only_enabled_below_max_passes(db_session, org_and_account):
    from app.screener.price_filter import price_in_range
    org, _ = org_and_account
    _seed_rule(db_session, org.id, "entry_max_share_price", threshold=1.00)
    engine = _make_engine(org)

    in_range, reason = price_in_range("PNV.AX", 0.50, engine)
    assert in_range is True


def test_both_enabled_in_band_passes(db_session, org_and_account):
    from app.screener.price_filter import price_in_range
    org, _ = org_and_account
    _seed_rule(db_session, org.id, "entry_min_share_price", threshold=0.10)
    _seed_rule(db_session, org.id, "entry_max_share_price", threshold=1.00)
    engine = _make_engine(org)

    in_range, reason = price_in_range("PLS.AX", 0.50, engine)
    assert in_range is True
    assert reason is None


def test_both_enabled_below_band_fails(db_session, org_and_account):
    from app.screener.price_filter import price_in_range
    org, _ = org_and_account
    _seed_rule(db_session, org.id, "entry_min_share_price", threshold=0.10)
    _seed_rule(db_session, org.id, "entry_max_share_price", threshold=1.00)
    engine = _make_engine(org)

    in_range, reason = price_in_range("PNV.AX", 0.05, engine)
    assert in_range is False
    assert "min" in reason.lower()


def test_both_enabled_above_band_fails(db_session, org_and_account):
    from app.screener.price_filter import price_in_range
    org, _ = org_and_account
    _seed_rule(db_session, org.id, "entry_min_share_price", threshold=0.10)
    _seed_rule(db_session, org.id, "entry_max_share_price", threshold=1.00)
    engine = _make_engine(org)

    in_range, reason = price_in_range("CSL.AX", 250.0, engine)
    assert in_range is False
    assert "max" in reason.lower()


def test_disabled_globally_has_no_effect(db_session, org_and_account):
    """Rule rows exist but enabled_globally=False — must behave like unset."""
    from app.screener.price_filter import price_in_range
    org, _ = org_and_account
    _seed_rule(db_session, org.id, "entry_min_share_price", threshold=10.0, enabled_globally=False)
    _seed_rule(db_session, org.id, "entry_max_share_price", threshold=20.0, enabled_globally=False)
    engine = _make_engine(org)

    in_range, reason = price_in_range("BHP.AX", 0.01, engine)
    assert in_range is True
    assert reason is None


def test_threshold_zero_is_falsy_treated_as_unset(db_session, org_and_account):
    """
    RuleConfig.threshold_for_tier(): `float(self.threshold) if self.threshold
    else None` — a threshold of exactly 0.0 evaluates falsy and resolves to
    None. is_enabled() is True (rule is on), but the filter must still no-op
    for this rule rather than rejecting every price as "below min 0".
    """
    from app.screener.price_filter import evaluate_price_range, price_in_range
    org, _ = org_and_account
    _seed_rule(db_session, org.id, "entry_min_share_price", threshold=0.0, enabled_globally=True)
    engine = _make_engine(org)

    assert engine.is_enabled("entry_min_share_price") is True
    assert engine.threshold("entry_min_share_price") is None

    results = evaluate_price_range("XYZ.AX", 0.0001, engine)
    assert "entry_min_share_price" not in results
    in_range, reason = price_in_range("XYZ.AX", 0.0001, engine)
    assert in_range is True


def test_non_equity_asset_types_value_short_circuits_via_asset_type_param(db_session, org_and_account):
    """asset_type='CRYPTO' is checked before any rule lookup at all."""
    from app.screener.price_filter import evaluate_price_range
    org, _ = org_and_account
    _seed_rule(db_session, org.id, "entry_min_share_price", threshold=10000.0)
    engine = _make_engine(org, asset_type="CRYPTO")
    assert evaluate_price_range("DOGE-AUD", 0.0001, engine, asset_type="CRYPTO") == {}


def test_negative_or_zero_price_returns_no_results(db_session, org_and_account):
    from app.screener.price_filter import evaluate_price_range
    org, _ = org_and_account
    _seed_rule(db_session, org.id, "entry_min_share_price", threshold=0.10)
    engine = _make_engine(org)
    assert evaluate_price_range("BHP.AX", 0, engine) == {}
    assert evaluate_price_range("BHP.AX", None, engine) == {}


# ===========================================================================
# Integration — app/trading/order_executor.py::execute_signal_order
# (Task #5: final defensive gate before order submission, shared by MCP
#  place_order and the WhatsApp agent)
# ===========================================================================

def _make_pending_signal(db, org_id, ticker="BHP.AX", exchange_key="ASX",
                          asset_type="EQUITY", pivot=45.0):
    from app.models.signal import Signal, SignalStatus
    sig = Signal(
        organization_id=org_id, ticker=ticker, exchange_key=exchange_key,
        asset_type=asset_type, currency="AUD",
        signal_date=date.today(), status=SignalStatus.PENDING,
        pivot_price=pivot, stop_price=pivot * 0.93, target_price_1=pivot * 1.20,
        close_price=pivot, rs_rating=80, trend_score=7,
    )
    db.add(sig)
    db.commit()
    return sig


def _patch_ibkr_simulate(monkeypatch):
    from app.broker.ibkr import IBKRBroker
    monkeypatch.setattr(IBKRBroker, "connect", lambda self: False)
    monkeypatch.setattr(
        IBKRBroker, "submit_bracket_order",
        lambda self, **kw: {"simulated": True, "order_id": "SIM-1", "broker": "simulation"},
    )


def test_execute_signal_order_rejects_price_below_min(db_session, org_and_account, monkeypatch):
    from app.trading.order_executor import execute_signal_order
    org, _ = org_and_account
    # Pivot near the (very low) force_entry_price so the existing >10%
    # over-extension check doesn't fire first and mask the price-range gate.
    sig = _make_pending_signal(org_id=org.id, db=db_session, pivot=0.05)
    _seed_rule(db_session, org.id, "entry_min_share_price", threshold=10.0, enabled_globally=True)

    result = execute_signal_order(signal_id=sig.id, organization_id=org.id, force_entry_price=0.05)

    assert result["ok"] is False
    assert "range" in result["error"].lower()

    rejected = db_session.query(AuditLog).filter(
        AuditLog.organization_id == org.id,
        AuditLog.message.like("%Order rejected%"),
    ).all()
    assert rejected, "Should write an audit log entry for the rejection"


def test_execute_signal_order_rejects_price_above_max(db_session, org_and_account, monkeypatch):
    from app.trading.order_executor import execute_signal_order
    org, _ = org_and_account
    sig = _make_pending_signal(org_id=org.id, db=db_session, pivot=250.0)
    _seed_rule(db_session, org.id, "entry_max_share_price", threshold=1.0, enabled_globally=True)

    result = execute_signal_order(signal_id=sig.id, organization_id=org.id, force_entry_price=250.0)

    assert result["ok"] is False
    assert "range" in result["error"].lower()


def test_execute_signal_order_within_range_proceeds_to_fill(db_session, org_and_account, monkeypatch):
    """Both rules enabled but price is inside the band — order still fills."""
    from app.trading.order_executor import execute_signal_order
    from app.risk.manager import SizingResult
    org, _ = org_and_account
    sig = _make_pending_signal(org_id=org.id, db=db_session, pivot=45.0)
    _seed_rule(db_session, org.id, "entry_min_share_price", threshold=10.0, enabled_globally=True)
    _seed_rule(db_session, org.id, "entry_max_share_price", threshold=100.0, enabled_globally=True)

    _patch_ibkr_simulate(monkeypatch)
    mock_notifier = MagicMock()
    monkeypatch.setattr("app.notifications.get_notifier", lambda organization_id=None: mock_notifier)
    sizing = SizingResult(10, 10, 455.0, 420.0, 35.0, 350.0, 42.0, 45.5, "AUD", 1.0, "OK")
    monkeypatch.setattr("app.risk.manager.calculate_position_size", lambda **kw: sizing)

    result = execute_signal_order(signal_id=sig.id, organization_id=org.id, force_entry_price=45.5)

    assert result["ok"] is True
    assert result["entry_price"] == 45.5


def test_execute_signal_order_crypto_bypasses_filter_entirely(db_session, org_and_account, monkeypatch):
    """
    A CRYPTO signal must never be subject to the equity price-range filter,
    even with thresholds configured that would reject this price for equity.
    """
    from app.trading.order_executor import execute_signal_order
    from app.risk.manager import SizingResult
    org, _ = org_and_account
    sig = _make_pending_signal(
        org_id=org.id, db=db_session, ticker="BTC-AUD",
        exchange_key="CRYPTO_INDEPENDENTRESERVE", asset_type="CRYPTO", pivot=44.0,
    )
    # Thresholds that WOULD reject 44.0 if this were treated as equity.
    _seed_rule(db_session, org.id, "entry_min_share_price", threshold=1000.0, enabled_globally=True)

    sizing = SizingResult(0.1, 0.1, 4.5, 4.2, 0.3, 3.0, 42.0, 44.0, "AUD", 1.0, "OK")
    monkeypatch.setattr("app.risk.manager.calculate_position_size", lambda **kw: sizing)
    mock_broker = MagicMock()
    mock_broker.__enter__ = lambda self: mock_broker
    mock_broker.__exit__ = MagicMock(return_value=False)
    mock_broker.submit_bracket_order.return_value = {"simulated": True, "order_id": "CCXT-1", "broker": "ccxt"}
    monkeypatch.setattr("app.broker.crypto.get_crypto_broker_for_org", lambda org_id: mock_broker)
    mock_notifier = MagicMock()
    monkeypatch.setattr("app.notifications.get_notifier", lambda organization_id=None: mock_notifier)

    result = execute_signal_order(signal_id=sig.id, organization_id=org.id, force_entry_price=44.0)

    assert result["ok"] is True
    assert result["broker"] == "ccxt"


# ===========================================================================
# Integration — app/tasks/screening.py::screen_single_ticker
# (Task #3: manual single-ticker add path — hard exclude, no watchlist entry)
# ===========================================================================

def test_screen_single_ticker_skips_when_price_above_max(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import screen_single_ticker
    from app.models.signal import Watchlist, Signal

    org, _ = org_and_account
    _seed_rule(db_session, org.id, "entry_max_share_price", threshold=1.00, enabled_globally=True)

    df = _make_price_df(close=50.0)  # well above the $1.00 max
    monkeypatch.setattr("app.tasks.screening.get_price_history", lambda *a, **kw: df)
    monkeypatch.setattr("app.tasks.screening.get_fundamentals", lambda *a, **kw: {})
    mock_notifier = MagicMock()
    monkeypatch.setattr("app.tasks.screening.get_notifier", lambda **kw: mock_notifier)
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: mock_notifier)

    screen_single_ticker.run(ticker="CSL.AX", exchange_key="ASX", organization_id=org.id)

    assert db_session.query(Watchlist).filter(Watchlist.ticker == "CSL.AX").count() == 0
    assert db_session.query(Signal).filter(Signal.ticker == "CSL.AX").count() == 0

    skip_logs = db_session.query(AuditLog).filter(
        AuditLog.organization_id == org.id,
        AuditLog.message.like("%SKIP manual add%"),
    ).all()
    assert skip_logs, "Should write a SKIP manual add audit log for price out of range"


def test_screen_single_ticker_proceeds_when_price_in_range(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import screen_single_ticker

    org, _ = org_and_account
    _seed_rule(db_session, org.id, "entry_min_share_price", threshold=0.10, enabled_globally=True)
    _seed_rule(db_session, org.id, "entry_max_share_price", threshold=100.00, enabled_globally=True)

    df = _make_price_df(close=50.0)  # inside the configured band
    monkeypatch.setattr("app.tasks.screening.get_price_history", lambda *a, **kw: df)
    monkeypatch.setattr("app.tasks.screening.get_fundamentals", lambda *a, **kw: {})
    mock_notifier = MagicMock()
    monkeypatch.setattr("app.tasks.screening.get_notifier", lambda **kw: mock_notifier)
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: mock_notifier)

    screen_single_ticker.run(ticker="CSL.AX", exchange_key="ASX", organization_id=org.id)

    # Must NOT have been skipped for being out of price range
    price_skip_logs = db_session.query(AuditLog).filter(
        AuditLog.organization_id == org.id,
        AuditLog.message.like("%SKIP manual add%price%"),
    ).all()
    assert not price_skip_logs


def test_screen_single_ticker_default_state_never_skips_on_price(db_session, org_and_account, monkeypatch):
    """No RuleConfig rows seeded at all — the filter must be a complete no-op."""
    from app.tasks.screening import screen_single_ticker

    org, _ = org_and_account
    df = _make_price_df(close=0.0001)  # absurdly low; would fail almost any min
    monkeypatch.setattr("app.tasks.screening.get_price_history", lambda *a, **kw: df)
    monkeypatch.setattr("app.tasks.screening.get_fundamentals", lambda *a, **kw: {})
    mock_notifier = MagicMock()
    monkeypatch.setattr("app.tasks.screening.get_notifier", lambda **kw: mock_notifier)
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: mock_notifier)

    screen_single_ticker.run(ticker="PENNY.AX", exchange_key="ASX", organization_id=org.id)

    range_skip_logs = db_session.query(AuditLog).filter(
        AuditLog.organization_id == org.id,
        AuditLog.message.like("%SKIP manual add — price%"),
    ).all()
    assert not range_skip_logs


# ===========================================================================
# Integration — app/tasks/trading.py::check_entry_triggers
# (Task #4: intraday entry-trigger enforcement, equity-only)
# ===========================================================================

def _make_entry_signal(db, org_id, ticker="WOW.AX", pivot=37.0,
                        exchange_key="ASX", asset_type="EQUITY"):
    from app.models.signal import Signal, SignalStatus
    sig = Signal(
        ticker=ticker, organization_id=org_id, exchange_key=exchange_key,
        asset_type=asset_type, status=SignalStatus.PENDING,
        pivot_price=Decimal(str(pivot)), stop_price=Decimal(str(round(pivot * 0.92, 3))),
        target_price_1=Decimal(str(round(pivot * 1.20, 3))),
        target_price_2=Decimal(str(round(pivot * 1.40, 3))),
        signal_date=date(2026, 6, 1),
    )
    db.add(sig)
    db.commit()
    db.refresh(sig)
    return sig


def _seed_regime(db, org_id, regime="BULL", exchange_key="ASX"):
    from app.models.config import SystemConfig
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


def _patch_entry_price_data(monkeypatch, close=37.5, volume=800_000):
    import app.tasks.trading as t
    df = _make_price_df(close=close, rows=60)
    df = df.rename(columns={})
    # ensure avg_vol_50 column present with sane volume value expected by trading.py
    df["avg_vol_50"] = volume
    monkeypatch.setattr(t, "get_price_history", lambda ticker, period="3mo": df)
    monkeypatch.setattr(t, "get_intraday_price",
                        lambda ticker, organization_id=None, asset_type="EQUITY": {
                            "price": close, "volume": volume,
                            "data_source": "yfinance", "delay_mins": 15,
                            "bar_timestamp": None, "ok": True,
                        })


def _patch_entry_notifier(monkeypatch):
    import app.tasks.trading as t
    class _Notifier:
        def send(self, *a, **kw): pass
        def send_order_fill(self, *a, **kw): pass
        def send_entry_alert(self, *a, **kw): pass
    monkeypatch.setattr(t, "get_notifier", lambda organization_id=None: _Notifier())


def _patch_breakout_confirmed(monkeypatch):
    import app.tasks.trading as t

    class _FakeResult:
        def __init__(self, passed):
            self.passed = passed
            self.value = 1.0
            self.threshold = 1.0
            self.message = "ok"

    fake_rules = {"breakout_price": _FakeResult(True), "breakout_volume": _FakeResult(True)}
    monkeypatch.setattr(t, "check_breakout", lambda ticker, df, pivot, avg_vol, engine: fake_rules)


def _patch_entry_sizing(monkeypatch):
    import app.tasks.trading as t
    from app.risk.manager import SizingResult
    monkeypatch.setattr(t, "calculate_position_size",
                        lambda *a, **kw: SizingResult(
                            shares=33, capital_aud=1221.0, capital_local=1221.0,
                            risk_aud=50.0, risk_pct=1.5, portfolio_pct=12.2,
                            stop_price=34.0, entry_price=37.0,
                            currency="AUD", fx_rate_aud=1.0, message="test sizing",
                        ))


def _patch_entry_broker_simulate(monkeypatch):
    from app.broker.ibkr import IBKRBroker
    monkeypatch.setattr(IBKRBroker, "submit_bracket_order",
                        lambda self, *a, **kw: {"simulated": True, "order_id": "SIM-1"})
    monkeypatch.setattr(IBKRBroker, "connect", lambda self: False)


def test_check_entry_triggers_skips_signal_above_max_price(db_session, org_and_account, monkeypatch):
    from app.tasks.trading import check_entry_triggers
    from app.models.trade import Position

    org, account = org_and_account
    _make_entry_signal(db_session, org.id, ticker="WOW.AX", pivot=37.0)
    _seed_rule(db_session, org.id, "entry_max_share_price", threshold=1.00, enabled_globally=True)

    _patch_market_open(monkeypatch, is_open=True)
    _patch_trading_paused(monkeypatch, paused=False)
    _seed_regime(db_session, org.id, "BULL")
    _patch_entry_price_data(monkeypatch, close=37.5)  # above pivot, well above $1.00 max
    _patch_breakout_confirmed(monkeypatch)
    _patch_entry_sizing(monkeypatch)
    _patch_entry_broker_simulate(monkeypatch)
    _patch_entry_notifier(monkeypatch)

    check_entry_triggers.run(exchange_key="ASX")

    assert db_session.query(Position).filter(Position.ticker == "WOW.AX").count() == 0
    skip_logs = db_session.query(AuditLog).filter(
        AuditLog.organization_id == org.id,
        AuditLog.message.like("%Entry check: skipped%"),
    ).all()
    assert skip_logs, "Should write a skip audit log for the price-range rejection"


def test_check_entry_triggers_within_range_still_opens_position(db_session, org_and_account, monkeypatch):
    """Both rules enabled but the live price is inside the band — unaffected."""
    from app.tasks.trading import check_entry_triggers
    from app.models.trade import Position, TradeStatus

    org, account = org_and_account
    _make_entry_signal(db_session, org.id, ticker="WOW.AX", pivot=37.0)
    _seed_rule(db_session, org.id, "entry_min_share_price", threshold=1.00, enabled_globally=True)
    _seed_rule(db_session, org.id, "entry_max_share_price", threshold=100.00, enabled_globally=True)

    _patch_market_open(monkeypatch, is_open=True)
    _patch_trading_paused(monkeypatch, paused=False)
    _seed_regime(db_session, org.id, "BULL")
    _patch_entry_price_data(monkeypatch, close=37.5)
    _patch_breakout_confirmed(monkeypatch)
    _patch_entry_sizing(monkeypatch)
    _patch_entry_broker_simulate(monkeypatch)
    _patch_entry_notifier(monkeypatch)

    check_entry_triggers.run(exchange_key="ASX")

    positions = db_session.query(Position).filter(
        Position.organization_id == org.id, Position.ticker == "WOW.AX",
    ).all()
    assert positions, "In-band price must not block a confirmed breakout"
    assert positions[0].status == TradeStatus.OPEN
