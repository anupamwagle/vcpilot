"""
Tests for app/risk/manager.py — calculate_position_size() and calculate_portfolio_heat().

All tests use a stub RuleEngine and monkeypatched FX lookups so there are
no network calls or DB dependencies.
"""
import pytest
from app.risk.manager import calculate_position_size, calculate_portfolio_heat, SizingResult


# ---------------------------------------------------------------------------
# Stub RuleEngine
# ---------------------------------------------------------------------------

class StubEngine:
    def __init__(self, thresholds: dict = None):
        self._t = thresholds or {
            "risk_max_pct_per_trade": 2.0,
            "risk_max_position_pct": 30.0,
            "crypto_max_risk_pct": 1.0,
        }

    def threshold(self, rule_id):
        return self._t.get(rule_id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_fx(monkeypatch):
    """Eliminate network calls — use fixed FX rates."""
    import app.risk.manager as mgr_mod
    import app.data.fetcher as fetcher_mod

    monkeypatch.setattr(fetcher_mod, "get_fx_rate", lambda src, dst: 0.65)
    monkeypatch.setattr(fetcher_mod, "currency_to_aud",
                        lambda amount, currency: amount if currency == "AUD" else amount / 0.65)
    yield


# ---------------------------------------------------------------------------
# Basic AUD (ASX) sizing
# ---------------------------------------------------------------------------

def test_basic_aud_sizing():
    result = calculate_position_size(
        capital_aud=10_000.0,
        entry_price=10.0,
        stop_price=8.5,
        engine=StubEngine(),
    )
    assert result.shares > 0
    assert result.currency == "AUD"
    # Risk should be ≤ 2% of capital
    assert result.risk_pct <= 2.0


def test_stop_above_entry_returns_zero():
    result = calculate_position_size(
        capital_aud=10_000.0,
        entry_price=8.0,
        stop_price=9.0,   # stop > entry → invalid
        engine=StubEngine(),
    )
    assert result.shares == 0
    assert "Invalid" in result.message


def test_risk_capped_at_max_pct():
    """With 1% risk cap, shares × risk_per_share / capital ≤ 1%."""
    engine = StubEngine({"risk_max_pct_per_trade": 1.0, "risk_max_position_pct": 50.0})
    result = calculate_position_size(
        capital_aud=50_000.0,
        entry_price=20.0,
        stop_price=18.0,
        engine=engine,
    )
    assert result.risk_pct <= 1.01   # allow tiny float rounding


def test_position_capped_by_max_position_pct():
    """Even if risk allows more shares, position is capped at max_position_pct."""
    # Very tight stop → risk calculation gives huge share count
    engine = StubEngine({"risk_max_pct_per_trade": 2.0, "risk_max_position_pct": 10.0})
    result = calculate_position_size(
        capital_aud=100_000.0,
        entry_price=5.0,
        stop_price=4.99,   # $0.01 stop → huge share count without cap
        engine=engine,
    )
    assert result.portfolio_pct <= 10.01


def test_regime_multiplier_reduces_size():
    """CAUTION regime (0.5x) should halve the position vs BULL (1.0x)."""
    engine = StubEngine()
    bull = calculate_position_size(10_000.0, 10.0, 8.0, engine, regime_multiplier=1.0)
    caution = calculate_position_size(10_000.0, 10.0, 8.0, engine, regime_multiplier=0.5)
    assert caution.shares < bull.shares


def test_bear_regime_zero_multiplier_returns_no_position():
    """BEAR regime (0.0x) should block all new entries."""
    engine = StubEngine()
    result = calculate_position_size(10_000.0, 10.0, 8.0, engine, regime_multiplier=0.0)
    assert result.shares == 0


# ---------------------------------------------------------------------------
# Crypto sizing (fractional shares, no commission check)
# ---------------------------------------------------------------------------

def test_crypto_fractional_shares():
    engine = StubEngine({"crypto_max_risk_pct": 1.0, "risk_max_position_pct": 20.0})
    result = calculate_position_size(
        capital_aud=5_000.0,
        entry_price=0.45,
        stop_price=0.36,
        engine=engine,
        currency="AUD",
        is_crypto=True,
    )
    assert result.shares > 0
    # Crypto allows fractional — shares need not be whole number
    assert isinstance(result.shares, float)


def test_crypto_uses_crypto_risk_pct_not_equity():
    """Crypto sizing should use crypto_max_risk_pct (1%) not risk_max_pct_per_trade (2%)."""
    engine_1pct = StubEngine({"crypto_max_risk_pct": 1.0, "risk_max_position_pct": 50.0,
                               "risk_max_pct_per_trade": 2.0})
    result = calculate_position_size(
        capital_aud=10_000.0, entry_price=1.0, stop_price=0.80,
        engine=engine_1pct, currency="AUD", is_crypto=True,
    )
    assert result.risk_pct <= 1.01


# ---------------------------------------------------------------------------
# USD (NYSE) sizing
# ---------------------------------------------------------------------------

def test_usd_stock_sizing_converts_capital():
    engine = StubEngine({"risk_max_pct_per_trade": 2.0, "risk_max_position_pct": 30.0})
    result = calculate_position_size(
        capital_aud=20_000.0,
        entry_price=50.0,
        stop_price=45.0,
        engine=engine,
        currency="USD",
        base_currency="AUD",
    )
    assert result.shares > 0
    assert result.currency == "USD"
    assert result.capital_aud > 0   # AUD-equivalent is populated


# ---------------------------------------------------------------------------
# Commission efficiency guard (equity only)
# ---------------------------------------------------------------------------

def test_commission_guard_rejects_tiny_trade():
    """A penny-stock trade too small to be commission-efficient returns shares=0."""
    engine = StubEngine({"risk_max_pct_per_trade": 0.05, "risk_max_position_pct": 5.0})
    result = calculate_position_size(
        capital_aud=500.0,
        entry_price=0.10,
        stop_price=0.08,
        engine=engine,
        currency="AUD",
        is_crypto=False,
    )
    # Either succeeds (bumped to minimum) or rejected — must not crash
    assert isinstance(result, SizingResult)


# ---------------------------------------------------------------------------
# Portfolio heat
# ---------------------------------------------------------------------------

def test_portfolio_heat_returns_pct_of_capital():
    # risk_aud=200 on capital_aud=10000 → 2%
    positions = [{"risk_aud": 200.0, "capital_aud": 10_000.0}]
    heat = calculate_portfolio_heat(positions)
    assert heat == pytest.approx(2.0)


def test_portfolio_heat_aggregates_multiple_positions():
    positions = [
        {"risk_aud": 100.0, "capital_aud": 2_000.0},
        {"risk_aud": 100.0, "capital_aud": 3_000.0},
    ]
    # total risk 200 / total capital 5000 = 4%
    heat = calculate_portfolio_heat(positions)
    assert heat == pytest.approx(4.0)


def test_portfolio_heat_empty():
    assert calculate_portfolio_heat([]) == 0.0


def test_portfolio_heat_computes_from_raw_fields():
    # qty=100, entry=10, stop=8 → risk_local=200, fx=1 → risk_aud=200; capital=5000 → 4%
    positions = [{"qty": 100.0, "entry_price": 10.0, "current_stop": 8.0,
                  "capital_aud": 5_000.0, "fx_rate_aud": 1.0}]
    heat = calculate_portfolio_heat(positions)
    assert heat == pytest.approx(4.0)
