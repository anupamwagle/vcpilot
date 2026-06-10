"""Tests for utility functions in app/data/fetcher.py."""
import pytest
import pandas as pd
import numpy as np
from datetime import date, timedelta


# --- normalize_ticker ---

def test_normalize_ticker_asx_adds_suffix():
    from app.data.fetcher import normalize_ticker
    r = normalize_ticker("BHP", "ASX")
    assert r["yfinance_ticker"] == "BHP.AX"
    assert r["display_code"] == "BHP"
    assert r["currency"] == "AUD"
    assert r["asset_type"] == "EQUITY"


def test_normalize_ticker_asx_already_has_suffix():
    from app.data.fetcher import normalize_ticker
    r = normalize_ticker("BHP.AX", "ASX")
    assert r["yfinance_ticker"] == "BHP.AX"
    assert r["display_code"] == "BHP"


def test_normalize_ticker_nyse():
    from app.data.fetcher import normalize_ticker
    r = normalize_ticker("AAPL", "NYSE")
    assert r["yfinance_ticker"] == "AAPL"
    assert r["display_code"] == "AAPL"
    assert r["currency"] == "USD"
    assert r["asset_type"] == "EQUITY"


def test_normalize_ticker_nyse_strips_ax_suffix():
    from app.data.fetcher import normalize_ticker
    r = normalize_ticker("AAPL.AX", "NYSE")
    assert r["yfinance_ticker"] == "AAPL"


def test_normalize_ticker_crypto_ir_adds_aud():
    from app.data.fetcher import normalize_ticker
    r = normalize_ticker("BTC", "CRYPTO_INDEPENDENTRESERVE")
    assert r["yfinance_ticker"] == "BTC-AUD"
    assert r["display_code"] == "BTC"
    assert r["currency"] == "AUD"
    assert r["asset_type"] == "CRYPTO"


def test_normalize_ticker_crypto_binance_adds_usd():
    from app.data.fetcher import normalize_ticker
    r = normalize_ticker("ETH", "CRYPTO_BINANCE")
    assert r["yfinance_ticker"] == "ETH-USD"
    assert r["currency"] == "USD"


def test_normalize_ticker_crypto_strips_existing_suffix():
    from app.data.fetcher import normalize_ticker
    r = normalize_ticker("BTC-USD", "CRYPTO_INDEPENDENTRESERVE")
    assert r["yfinance_ticker"] == "BTC-AUD"
    assert r["display_code"] == "BTC"


def test_normalize_ticker_unknown_exchange_passthrough():
    from app.data.fetcher import normalize_ticker
    r = normalize_ticker("WEIRD", "SOME_EXCHANGE")
    assert r["yfinance_ticker"] == "WEIRD"
    assert r["currency"] == "USD"


def test_normalize_ticker_lowercase_input():
    from app.data.fetcher import normalize_ticker
    r = normalize_ticker("bhp", "ASX")
    assert r["yfinance_ticker"] == "BHP.AX"


# --- get_fx_rate — same currency shortcut ---

def test_get_fx_rate_same_currency_returns_one():
    from app.data.fetcher import get_fx_rate
    assert get_fx_rate("AUD", "AUD") == 1.0
    assert get_fx_rate("USD", "USD") == 1.0


# --- aud_to_currency / currency_to_aud same-currency shortcut ---

def test_aud_to_currency_aud_is_passthrough():
    from app.data.fetcher import aud_to_currency
    assert aud_to_currency(1234.56, "AUD") == 1234.56


def test_currency_to_aud_aud_is_passthrough():
    from app.data.fetcher import currency_to_aud
    assert currency_to_aud(500.0, "AUD") == 500.0


# --- get_top_crypto_tickers ---

def test_get_top_crypto_tickers_ir_returns_aud():
    from app.data.fetcher import get_top_crypto_tickers
    tickers = get_top_crypto_tickers("CRYPTO_INDEPENDENTRESERVE")
    assert len(tickers) > 50
    assert all(t.endswith("-AUD") for t in tickers)
    assert "BTC-AUD" in tickers


def test_get_top_crypto_tickers_binance_returns_usd():
    from app.data.fetcher import get_top_crypto_tickers
    tickers = get_top_crypto_tickers("CRYPTO_BINANCE")
    assert "BTC-USD" in tickers
    assert all(t.endswith("-USD") for t in tickers)


# --- _add_indicators ---

def _make_df(rows=250, close=50.0):
    dates = [date(2023, 1, 1) + timedelta(days=i) for i in range(rows)]
    np.random.seed(42)
    close_arr = np.full(rows, close) + np.random.randn(rows) * 0.5
    return pd.DataFrame({
        "date": dates,
        "open": close_arr,
        "high": close_arr * 1.01,
        "low":  close_arr * 0.99,
        "close": close_arr,
        "volume": np.full(rows, 500_000.0),
    })


def test_add_indicators_adds_moving_averages():
    from app.data.fetcher import _add_indicators
    df = _add_indicators(_make_df(250))
    assert "ma_50" in df.columns
    assert "ma_150" in df.columns
    assert "ma_200" in df.columns


def test_add_indicators_adds_atr():
    from app.data.fetcher import _add_indicators
    df = _add_indicators(_make_df(250))
    assert "atr_14" in df.columns
    # ATR should be positive for non-trivial data
    assert df["atr_14"].dropna().iloc[-1] > 0


def test_add_indicators_adds_52w_range():
    from app.data.fetcher import _add_indicators
    df = _add_indicators(_make_df(250))
    assert "high_52w" in df.columns
    assert "pct_from_52w_high" in df.columns


def test_add_indicators_adds_vol_ratio():
    from app.data.fetcher import _add_indicators
    df = _add_indicators(_make_df(250))
    assert "vol_ratio" in df.columns
    assert "avg_vol_50" in df.columns


def test_add_indicators_empty_returns_empty():
    from app.data.fetcher import _add_indicators
    import pandas as pd
    result = _add_indicators(pd.DataFrame())
    assert result.empty


def test_add_indicators_none_returns_none():
    from app.data.fetcher import _add_indicators
    assert _add_indicators(None) is None


# --- _compute_performance ---

def test_compute_performance_positive_return():
    from app.data.fetcher import _compute_performance
    df = _make_df(60)
    df["close"] = 50.0  # flat
    df.loc[df.index[-1], "close"] = 60.0  # last bar up 20%
    result = _compute_performance(df, 30)
    assert result is not None
    assert result > 0


def test_compute_performance_insufficient_data_returns_none():
    from app.data.fetcher import _compute_performance
    df = _make_df(5)  # only 5 rows, asking for 30 days
    result = _compute_performance(df, 30)
    assert result is None
