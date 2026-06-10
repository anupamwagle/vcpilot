"""
Tests for app/data/fetcher.py — normalize_ticker, get_price_history AUD→USD fallback,
and get_batch_prices AUD→USD fallback.
"""
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# normalize_ticker
# ---------------------------------------------------------------------------

from app.data.fetcher import normalize_ticker


def test_normalize_asx_appends_ax():
    r = normalize_ticker("BHP", "ASX")
    assert r["yfinance_ticker"] == "BHP.AX"
    assert r["display_code"] == "BHP"
    assert r["currency"] == "AUD"


def test_normalize_asx_preserves_existing_suffix():
    r = normalize_ticker("BHP.AX", "ASX")
    assert r["yfinance_ticker"] == "BHP.AX"


def test_normalize_crypto_ir_appends_aud():
    r = normalize_ticker("BTC", "CRYPTO_INDEPENDENTRESERVE")
    assert r["yfinance_ticker"] == "BTC-AUD"
    assert r["currency"] == "AUD"


def test_normalize_crypto_binance_appends_usd():
    r = normalize_ticker("BTC", "CRYPTO_BINANCE")
    assert r["yfinance_ticker"] == "BTC-USD"
    assert r["currency"] == "USD"


def test_normalize_us_equity_no_suffix():
    r = normalize_ticker("AAPL", "NYSE")
    assert r["yfinance_ticker"] == "AAPL"
    assert r["currency"] == "USD"


def test_normalize_already_formatted_crypto():
    r = normalize_ticker("ETH-AUD", "CRYPTO_INDEPENDENTRESERVE")
    assert r["yfinance_ticker"] == "ETH-AUD"


# ---------------------------------------------------------------------------
# get_price_history — AUD→USD fallback
# ---------------------------------------------------------------------------

from app.data.fetcher import get_price_history


def _make_df():
    from datetime import date, timedelta
    import numpy as np
    rows = 60
    dates = [date(2025, 1, 1) + timedelta(days=i) for i in range(rows)]
    return pd.DataFrame({
        "date": dates, "open": [1.0]*rows, "high": [1.1]*rows,
        "low": [0.9]*rows, "close": [1.0]*rows,
        "adj_close": [1.0]*rows, "volume": [100_000]*rows,
    })


def test_get_price_history_returns_df_on_success(monkeypatch):
    monkeypatch.setattr("app.data.fetcher._fetch_yf_df", lambda ticker, period, interval: _make_df())
    df = get_price_history("BTC-AUD")
    assert df is not None
    assert "close" in df.columns
    assert len(df) > 0


def test_get_price_history_falls_back_to_usd_when_aud_missing(monkeypatch):
    """If -AUD returns None, must try -USD and return that data."""
    call_log = []

    def fake_fetch(ticker, period, interval):
        call_log.append(ticker)
        if ticker.endswith("-AUD"):
            return None
        return _make_df()

    monkeypatch.setattr("app.data.fetcher._fetch_yf_df", fake_fetch)
    df = get_price_history("PENDLE-AUD")
    assert df is not None, "Should fall back to PENDLE-USD"
    assert "PENDLE-USD" in call_log


def test_get_price_history_returns_none_when_both_missing(monkeypatch):
    monkeypatch.setattr("app.data.fetcher._fetch_yf_df", lambda *a, **kw: None)
    df = get_price_history("UNKNOWNCOIN-AUD")
    assert df is None


def test_get_price_history_no_fallback_for_non_crypto(monkeypatch):
    """ASX tickers (.AX) should NOT trigger the -AUD→-USD fallback."""
    call_log = []

    def fake_fetch(ticker, period, interval):
        call_log.append(ticker)
        return None  # always fail

    monkeypatch.setattr("app.data.fetcher._fetch_yf_df", fake_fetch)
    get_price_history("WOW.AX")
    assert all("USD" not in t for t in call_log), "Should not attempt USD fallback for .AX tickers"


# ---------------------------------------------------------------------------
# get_batch_prices — AUD→USD fallback
# ---------------------------------------------------------------------------

from app.data.fetcher import get_batch_prices


def test_get_batch_prices_falls_back_for_empty_aud_tickers(monkeypatch):
    """Tickers with empty batch results that end in -AUD should retry as -USD."""
    usd_data = _make_df()

    # Patch yf.download to return empty → triggers individual per-ticker fallback path
    monkeypatch.setattr("yfinance.download", lambda *a, **kw: pd.DataFrame())

    # Patch _fetch_yf_df so the real get_price_history AUD→USD logic runs correctly:
    # -AUD returns None (no data), -USD returns data
    def fake_fetch(ticker, period, interval):
        if ticker.endswith("-AUD"):
            return None
        if ticker.endswith("-USD"):
            return usd_data
        return None

    monkeypatch.setattr("app.data.fetcher._fetch_yf_df", fake_fetch)

    results = get_batch_prices(["PENDLE-AUD"], period="2y")
    # The -AUD key should be populated via the -USD fallback inside get_price_history
    assert "PENDLE-AUD" in results
    assert results["PENDLE-AUD"] is not None
