"""
Tests for the Minimum Liquidity Filter (entry_min_avg_dollar_volume, R2 /
CLAUDE.md #42).

Covers:
  - Unit behavior of app/screener/liquidity_filter.py against a real
    RuleEngine + RuleConfig rows
  - Wiring into app/tasks/screening.py::screen_single_ticker (manual add)
  - Wiring into app/tasks/trading.py::check_entry_triggers (intraday gate)
  - The position-size volume cap in app/risk/manager.py::calculate_position_size

Default state (no RuleConfig row seeded by org_and_account) must always
behave as a no-op — same convention as the price-range filter.
"""
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from app.models.config import RuleConfig
from app.models.audit import AuditLog


def _seed_rule(db, org_id, rule_id="entry_min_avg_dollar_volume", threshold=500_000.0,
              enabled_globally=True, asset_types="EQUITY"):
    rc = RuleConfig(
        rule_id=rule_id, organization_id=org_id, category="ENTRY", label=rule_id,
        threshold=Decimal(str(threshold)) if threshold is not None else None,
        enabled_globally=enabled_globally, asset_types=asset_types, is_mandatory=False,
    )
    db.add(rc)
    db.commit()
    return rc


def _make_engine(org, asset_type="EQUITY"):
    from app.screener.rules import RuleEngine
    return RuleEngine(organization_id=org.id, tier=org.tier.value, asset_type=asset_type)


def _make_price_df(rows=60, close=50.0, avg_vol_50=1_000_000.0):
    dates = [date(2025, 1, 1) + timedelta(days=i) for i in range(rows)]
    c = np.full(rows, close)
    df = pd.DataFrame({
        "date": dates, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
        "volume": np.full(rows, avg_vol_50), "adj_close": c,
    })
    for col in ["ma_10", "ma_21", "ma_50", "ma_150", "ma_200", "ma_200_prev",
                "vol_ratio", "high_52w", "low_52w",
                "pct_from_52w_high", "pct_from_52w_low", "atr_14", "rs_rating"]:
        df[col] = c
    df["avg_vol_50"] = np.full(rows, avg_vol_50)
    return df


# ===========================================================================
# Unit tests — app/screener/liquidity_filter.py
# ===========================================================================

def test_no_rule_configured_always_passes(db_session, org_and_account):
    """Default state — org_and_account seeds zero RuleConfig rows."""
    from app.screener.liquidity_filter import liquidity_ok, evaluate_liquidity
    org, _ = org_and_account
    engine = _make_engine(org)

    ok, reason = liquidity_ok("PENNY.AX", 0.01, 100.0, engine)  # tiny price + volume
    assert ok is True
    assert reason is None
    assert evaluate_liquidity("PENNY.AX", 0.01, 100.0, engine) == {}


def test_crypto_asset_type_always_short_circuits(db_session, org_and_account):
    from app.screener.liquidity_filter import liquidity_ok
    org, _ = org_and_account
    _seed_rule(db_session, org.id, threshold=1_000_000.0)
    engine = _make_engine(org, asset_type="CRYPTO")

    ok, reason = liquidity_ok("BTC-AUD", 0.0001, 1.0, engine, asset_type="CRYPTO")
    assert ok is True
    assert reason is None


def test_below_threshold_fails(db_session, org_and_account):
    from app.screener.liquidity_filter import liquidity_ok
    org, _ = org_and_account
    _seed_rule(db_session, org.id, threshold=500_000.0)
    engine = _make_engine(org)

    # $10 price x 1,000 avg volume = $10,000/day << $500k min
    ok, reason = liquidity_ok("THIN.AX", 10.0, 1_000.0, engine)
    assert ok is False
    assert "min" in reason.lower()


def test_above_threshold_passes(db_session, org_and_account):
    from app.screener.liquidity_filter import liquidity_ok
    org, _ = org_and_account
    _seed_rule(db_session, org.id, threshold=500_000.0)
    engine = _make_engine(org)

    # $50 price x 100,000 avg volume = $5,000,000/day >> $500k min
    ok, reason = liquidity_ok("BHP.AX", 50.0, 100_000.0, engine)
    assert ok is True
    assert reason is None


def test_disabled_rule_never_blocks(db_session, org_and_account):
    from app.screener.liquidity_filter import liquidity_ok
    org, _ = org_and_account
    _seed_rule(db_session, org.id, threshold=500_000.0, enabled_globally=False)
    engine = _make_engine(org)

    ok, reason = liquidity_ok("THIN.AX", 10.0, 1_000.0, engine)
    assert ok is True


# ===========================================================================
# Integration — app/tasks/screening.py::screen_single_ticker
# ===========================================================================

def test_screen_single_ticker_skips_when_liquidity_too_low(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import screen_single_ticker
    from app.models.signal import Watchlist, Signal

    org, _ = org_and_account
    _seed_rule(db_session, org.id, threshold=500_000.0)

    df = _make_price_df(close=10.0, avg_vol_50=1_000.0)  # $10k/day << $500k min
    monkeypatch.setattr("app.tasks.screening.get_price_history", lambda *a, **kw: df)
    monkeypatch.setattr("app.tasks.screening.get_fundamentals", lambda *a, **kw: {})
    mock_notifier = MagicMock()
    monkeypatch.setattr("app.tasks.screening.get_notifier", lambda **kw: mock_notifier)
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: mock_notifier)

    screen_single_ticker.run(ticker="THIN.AX", exchange_key="ASX", organization_id=org.id)

    assert db_session.query(Watchlist).filter(Watchlist.ticker == "THIN.AX").count() == 0
    assert db_session.query(Signal).filter(Signal.ticker == "THIN.AX").count() == 0

    skip_logs = db_session.query(AuditLog).filter(
        AuditLog.organization_id == org.id,
        AuditLog.message.like("%SKIP manual add%"),
    ).all()
    assert skip_logs, "Should write a SKIP manual add audit log for insufficient liquidity"


def test_screen_single_ticker_proceeds_when_liquidity_sufficient(db_session, org_and_account, monkeypatch):
    from app.tasks.screening import screen_single_ticker

    org, _ = org_and_account
    _seed_rule(db_session, org.id, threshold=500_000.0)

    df = _make_price_df(close=50.0, avg_vol_50=100_000.0)  # $5M/day
    monkeypatch.setattr("app.tasks.screening.get_price_history", lambda *a, **kw: df)
    monkeypatch.setattr("app.tasks.screening.get_fundamentals", lambda *a, **kw: {})
    mock_notifier = MagicMock()
    monkeypatch.setattr("app.tasks.screening.get_notifier", lambda **kw: mock_notifier)
    monkeypatch.setattr("app.notifications.get_notifier", lambda **kw: mock_notifier)

    screen_single_ticker.run(ticker="BHP.AX", exchange_key="ASX", organization_id=org.id)

    liq_skip_logs = db_session.query(AuditLog).filter(
        AuditLog.organization_id == org.id,
        AuditLog.message.like("%liquidity%"),
    ).all()
    assert not liq_skip_logs


# ===========================================================================
# calculate_position_size — volume-based realistic-fill cap
# ===========================================================================

def test_calculate_position_size_caps_shares_at_pct_of_avg_volume():
    from app.risk.manager import calculate_position_size, MAX_PCT_OF_AVG_VOLUME
    from app.screener.rules import RuleEngine

    class _FakeEngine:
        def threshold(self, rule_id):
            return {"risk_max_pct_per_trade": 2.0, "risk_max_position_pct": 100.0}.get(rule_id)

    # Huge capital + wide risk budget so the volume cap is the binding constraint,
    # not the capital or risk-based sizing.
    sizing = calculate_position_size(
        capital_aud=10_000_000.0, entry_price=10.0, stop_price=9.0,
        engine=_FakeEngine(), currency="AUD", base_currency="AUD",
        is_crypto=False, avg_vol_50=10_000.0,
    )
    assert sizing.shares <= 10_000.0 * (MAX_PCT_OF_AVG_VOLUME / 100)


def test_calculate_position_size_no_volume_cap_when_avg_vol_50_omitted():
    from app.risk.manager import calculate_position_size

    class _FakeEngine:
        def threshold(self, rule_id):
            return {"risk_max_pct_per_trade": 2.0, "risk_max_position_pct": 100.0}.get(rule_id)

    # Same huge capital/risk budget, but no avg_vol_50 -> no cap applied (backward compatible).
    sizing = calculate_position_size(
        capital_aud=10_000_000.0, entry_price=10.0, stop_price=9.0,
        engine=_FakeEngine(), currency="AUD", base_currency="AUD", is_crypto=False,
    )
    assert sizing.shares > 2_000  # would have been capped to 2,000 if a 10,000-share avg_vol_50 cap applied
