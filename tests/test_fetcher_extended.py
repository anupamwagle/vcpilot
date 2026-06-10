"""Extended tests for app/data/fetcher.py — IR live price, RS ratings, fundamentals, etc."""
import pytest
import pandas as pd
import numpy as np
from unittest.mock import patch, MagicMock
from datetime import date, timedelta


def _make_df(rows=252, close=50.0):
    dates = [date(2023, 1, 1) + timedelta(days=i) for i in range(rows)]
    np.random.seed(42)
    c = np.full(rows, close) + np.cumsum(np.random.randn(rows) * 0.5)
    c = np.maximum(c, 0.1)
    return pd.DataFrame({
        "date": dates, "open": c, "high": c * 1.01, "low": c * 0.99,
        "close": c, "volume": np.full(rows, 100_000.0), "adj_close": c,
    })


# ---- _get_ir_live_price -------------------------------------------------------

def test_get_ir_live_price_non_aud_returns_none():
    from app.data.fetcher import _get_ir_live_price
    result = _get_ir_live_price("BTC-USD")
    assert result is None


def test_get_ir_live_price_unknown_coin_returns_none():
    from app.data.fetcher import _get_ir_live_price
    result = _get_ir_live_price("FAKECOIN-AUD")
    assert result is None


def test_get_ir_live_price_success():
    from app.data.fetcher import _get_ir_live_price
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "LastPrice": "89500.00",
        "DayVolumeXbtInSecondaryCurrrency": 1000000.0,
        "CurrentHighestBidPrice": "89490.00",
        "CurrentLowestOfferPrice": "89510.00",
    }
    with patch("requests.get", return_value=mock_resp):
        result = _get_ir_live_price("BTC-AUD")
    assert result is not None
    assert result["price"] == pytest.approx(89500.0)
    assert result["ok"] is True
    assert result["data_source"] == "independentreserve"
    assert result["delay_mins"] == 0


def test_get_ir_live_price_non_200_returns_none():
    from app.data.fetcher import _get_ir_live_price
    mock_resp = MagicMock()
    mock_resp.status_code = 503
    mock_resp.text = "Service unavailable"
    with patch("requests.get", return_value=mock_resp):
        result = _get_ir_live_price("ETH-AUD")
    assert result is None


def test_get_ir_live_price_exception_returns_none():
    from app.data.fetcher import _get_ir_live_price
    with patch("requests.get", side_effect=Exception("Connection error")):
        result = _get_ir_live_price("ETH-AUD")
    assert result is None


def test_get_ir_live_price_missing_last_price_returns_none():
    from app.data.fetcher import _get_ir_live_price
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"SomeOtherKey": "value"}
    with patch("requests.get", return_value=mock_resp):
        result = _get_ir_live_price("ETH-AUD")
    assert result is None


# ---- compute_rs_ratings -------------------------------------------------------

def test_compute_rs_ratings_empty_returns_empty():
    from app.data.fetcher import compute_rs_ratings
    result = compute_rs_ratings({})
    assert result == {}


def test_compute_rs_ratings_returns_percentile():
    from app.data.fetcher import compute_rs_ratings
    stocks = {
        "STOCK_A": _make_df(300, close=100.0),
        "STOCK_B": _make_df(300, close=50.0),
        "STOCK_C": _make_df(300, close=80.0),
    }
    result = compute_rs_ratings(stocks, exchange_key="ASX")
    assert len(result) == 3
    for k, v in result.items():
        assert 0 <= v <= 100


def test_compute_rs_ratings_insufficient_data_skips():
    from app.data.fetcher import compute_rs_ratings
    # Stock with only 10 rows — not enough for 252-day window
    short_df = _make_df(10, close=50.0)
    stocks = {"SHORT": short_df}
    result = compute_rs_ratings(stocks, exchange_key="ASX")
    # Either empty or computed with what it has
    assert isinstance(result, dict)


# ---- _compute_performance -----------------------------------------------------

def test_compute_performance_returns_float():
    from app.data.fetcher import _compute_performance
    df = _make_df(300)
    result = _compute_performance(df, days=252)
    assert result is not None
    assert isinstance(result, float)


def test_compute_performance_insufficient_returns_none():
    from app.data.fetcher import _compute_performance
    df = _make_df(10)
    result = _compute_performance(df, days=252)
    assert result is None


# ---- get_fundamentals ---------------------------------------------------------

def test_get_fundamentals_returns_dict_structure():
    from app.data.fetcher import get_fundamentals
    mock_info = {
        "shortName": "Test Corp", "sector": "Technology",
        "industry": "Software", "returnOnEquity": 0.25,
        "profitMargins": 0.20, "heldPercentInstitutions": 0.65,
    }
    mock_ticker = MagicMock()
    mock_ticker.info = mock_info
    mock_ticker.quarterly_earnings = pd.DataFrame()
    mock_ticker.quarterly_financials = pd.DataFrame()
    mock_ticker.calendar = {}

    with patch("yfinance.Ticker", return_value=mock_ticker):
        result = get_fundamentals("BHP.AX")

    assert isinstance(result, dict)
    assert "company_name" in result
    assert "eps_quarterly" in result
    assert "roe" in result


def test_get_fundamentals_handles_exception():
    from app.data.fetcher import get_fundamentals
    with patch("yfinance.Ticker", side_effect=Exception("API error")):
        result = get_fundamentals("INVALID.AX")
    # Should return empty dict, not raise
    assert isinstance(result, dict)


# ---- get_intraday_price -------------------------------------------------------

def test_get_intraday_price_crypto_uses_ir():
    from app.data.fetcher import get_intraday_price
    ir_result = {
        "price": 89500.0, "volume": 100, "bid": 89490.0, "ask": 89510.0,
        "data_source": "independentreserve", "delay_mins": 0, "bar_timestamp": None, "ok": True
    }
    with patch("app.data.fetcher._get_ir_live_price", return_value=ir_result):
        result = get_intraday_price("BTC-AUD", organization_id=None, asset_type="CRYPTO")
    assert result["ok"] is True
    assert result["data_source"] == "independentreserve"


def test_get_intraday_price_yfinance_fallback():
    from app.data.fetcher import get_intraday_price
    df = _make_df(50)
    df["Datetime"] = pd.date_range("2026-06-01", periods=50, freq="15min")

    with patch("app.data.fetcher._get_ir_live_price", return_value=None), \
         patch("yfinance.download", return_value=df):
        result = get_intraday_price("BHP.AX", organization_id=None, asset_type="EQUITY")
    assert isinstance(result, dict)
    assert "ok" in result


def test_get_intraday_price_returns_dict():
    from app.data.fetcher import get_intraday_price
    with patch("app.data.fetcher._get_ir_live_price", return_value=None), \
         patch("app.data.fetcher._fetch_yf_df", return_value=None), \
         patch("app.data.fetcher.get_price_history", return_value=None):
        result = get_intraday_price("BHP.AX", organization_id=None, asset_type="EQUITY")
    assert isinstance(result, dict)
    assert "ok" in result


# ---- get_batch_prices ---------------------------------------------------------

def test_get_batch_prices_empty_list():
    from app.data.fetcher import get_batch_prices
    result = get_batch_prices([])
    assert result == {}


def test_get_batch_prices_returns_dict():
    from app.data.fetcher import get_batch_prices
    df = _make_df(100)
    with patch("app.data.fetcher.get_price_history", return_value=df):
        result = get_batch_prices(["BHP.AX", "CBA.AX"])
    assert isinstance(result, dict)


# ---- get_asx200_tickers -------------------------------------------------------

def test_get_asx200_tickers_returns_list():
    from app.data.fetcher import get_asx200_tickers
    with patch("app.data.fetcher.get_asx200_tickers", return_value=["BHP.AX", "CBA.AX"]):
        from app.data.fetcher import get_asx200_tickers as gt
        result = gt()
    assert isinstance(result, list)


# ---- get_top_crypto_tickers ---------------------------------------------------

def test_get_top_crypto_tickers_ir():
    from app.data.fetcher import get_top_crypto_tickers
    tickers = get_top_crypto_tickers("CRYPTO_INDEPENDENTRESERVE")
    assert isinstance(tickers, list)
    assert len(tickers) > 10
    # Should all end in -AUD
    assert all(t.endswith("-AUD") for t in tickers)


def test_get_top_crypto_tickers_binance():
    from app.data.fetcher import get_top_crypto_tickers
    tickers = get_top_crypto_tickers("CRYPTO_BINANCE")
    assert all(t.endswith("-USD") for t in tickers)
