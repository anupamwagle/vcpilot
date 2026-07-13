"""Tests for app/screener/market_regime.py — evaluate_market_regime() and helpers."""
from datetime import date, timedelta
import pandas as pd
import pytest
from app.screener.market_regime import (
    evaluate_market_regime, MarketRegime, is_trading_allowed, get_size_multiplier
)


class Eng:
    def __init__(self, thresholds=None, disabled=None):
        self._t = thresholds or {}
        self._d = disabled or set()
    def is_enabled(self, rule_id): return rule_id not in self._d
    def threshold(self, rule_id): return self._t.get(rule_id)


def _index_df(close=5500.0, ma200_base=5000.0, rows=250, vol_pattern="flat"):
    """Build an index DataFrame with close above/below its 200MA."""
    dates = [date(2024, 1, 1) + timedelta(days=i) for i in range(rows)]
    closes = [ma200_base + 0.5 * i for i in range(rows)]  # gradually rising
    closes[-1] = close  # override last close
    volumes = []
    for i in range(rows):
        if vol_pattern == "dist" and i > rows - 10 and i % 2 == 0:
            volumes.append(2_000_000)  # high volume on down days
        else:
            volumes.append(1_000_000)
    return pd.DataFrame({
        "date": dates, "close": closes,
        "open": closes, "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes], "volume": volumes,
    })


def _universe_df(n_above=150, n_below=50):
    rows = n_above + n_below
    closes  = [100.0] * n_above + [50.0] * n_below
    ma200s  = [90.0]  * n_above + [60.0] * n_below   # above: close > ma200; below: close < ma200
    return pd.DataFrame({"close": closes, "ma_200": ma200s})


# --- index_above_200ma ---

def test_bull_when_all_criteria_pass():
    regime, _ = evaluate_market_regime(
        _index_df(close=5500, ma200_base=4000),  # close well above 200MA
        _universe_df(n_above=160, n_below=40),    # 80% above → passes ≥60% threshold
        Eng(),
    )
    assert regime == MarketRegime.BULL


def test_bear_when_index_below_200ma():
    regime, results = evaluate_market_regime(
        _index_df(close=3000, ma200_base=5000),   # close below 200MA average
        _universe_df(n_above=40, n_below=160),    # also low breadth
        Eng(),
    )
    assert regime == MarketRegime.BEAR
    assert not results["regime_index_above_200ma"].passed


def test_caution_when_one_criterion_fails():
    # Index above 200MA ✓, but breadth low ✗
    regime, _ = evaluate_market_regime(
        _index_df(close=5500, ma200_base=4000),
        _universe_df(n_above=80, n_below=120),    # 40% above → fails 60% threshold
        Eng(),
    )
    assert regime == MarketRegime.CAUTION


# --- breadth ---

def test_breadth_passes_above_threshold():
    _, results = evaluate_market_regime(
        _index_df(close=5500, ma200_base=4000),
        _universe_df(n_above=150, n_below=50),    # 75% → passes
        Eng(),
    )
    assert results["regime_pct_stocks_above_200ma"].passed


def test_breadth_fails_below_threshold():
    _, results = evaluate_market_regime(
        _index_df(close=5500, ma200_base=4000),
        _universe_df(n_above=50, n_below=150),    # 25% → fails
        Eng(),
    )
    assert not results["regime_pct_stocks_above_200ma"].passed


def test_breadth_skipped_for_crypto():
    regime, results = evaluate_market_regime(
        _index_df(close=5500, ma200_base=4000),
        _universe_df(50, 150),
        Eng(),
        exchange_key="CRYPTO_INDEPENDENTRESERVE",
    )
    # breadth + dist day rules are skipped for crypto
    assert "regime_pct_stocks_above_200ma" not in results
    assert "regime_distribution_days" not in results


def test_empty_universe_df_breadth():
    _, results = evaluate_market_regime(
        _index_df(close=5500, ma200_base=4000),
        pd.DataFrame({"close": [], "ma_200": []}),
        Eng(),
    )
    assert not results["regime_pct_stocks_above_200ma"].passed  # 0% → fails


# --- distribution days ---

def test_distribution_days_within_limit():
    _, results = evaluate_market_regime(
        _index_df(close=5500, ma200_base=4000, vol_pattern="flat"),
        _universe_df(160, 40),
        Eng(thresholds={"regime_distribution_days": 4}),
    )
    assert results["regime_distribution_days"].passed


def test_no_rules_enabled_returns_bull():
    regime, _ = evaluate_market_regime(
        _index_df(), _universe_df(),
        Eng(disabled={"regime_index_above_200ma", "regime_pct_stocks_above_200ma",
                      "regime_distribution_days"}),
    )
    assert regime == MarketRegime.BULL


def test_insufficient_data_for_dist_days():
    small_df = _index_df(rows=10)  # fewer than 26 rows
    regime, results = evaluate_market_regime(
        small_df, _universe_df(8, 2),
        Eng(disabled={"regime_pct_stocks_above_200ma"}),
    )
    # Dist days defaults to 0 when insufficient data → passes
    assert results["regime_distribution_days"].passed


# --- crypto single-criterion regime (regression: used to be stuck at CAUTION) ---

def test_crypto_bull_when_index_above_200ma():
    regime, _ = evaluate_market_regime(
        _index_df(close=5500, ma200_base=4000),   # BTC well above its 200MA
        _universe_df(50, 150),
        Eng(),
        exchange_key="CRYPTO_INDEPENDENTRESERVE",
    )
    assert regime == MarketRegime.BULL


def test_crypto_bear_when_index_below_200ma():
    # Previously a crypto market could NEVER reach BEAR: with only the single
    # index rule enabled, 0/1 fell through to CAUTION. Below 200MA must be BEAR.
    regime, results = evaluate_market_regime(
        _index_df(close=3000, ma200_base=5000),   # BTC below its 200MA
        _universe_df(50, 150),
        Eng(),
        exchange_key="CRYPTO_INDEPENDENTRESERVE",
    )
    assert regime == MarketRegime.BEAR
    assert not results["regime_index_above_200ma"].passed


# --- helpers ---

def test_is_trading_allowed_bull():
    assert is_trading_allowed(MarketRegime.BULL) is True


def test_is_trading_allowed_caution():
    assert is_trading_allowed(MarketRegime.CAUTION) is False


def test_is_trading_allowed_bear():
    assert is_trading_allowed(MarketRegime.BEAR) is False


def test_size_multiplier_bull():
    assert get_size_multiplier(MarketRegime.BULL) == 1.0


def test_size_multiplier_caution():
    assert get_size_multiplier(MarketRegime.CAUTION) == 0.5


def test_size_multiplier_bear():
    assert get_size_multiplier(MarketRegime.BEAR) == 0.0
