"""Tests for app/data/calendar.py — is_trading_day(), market_is_open_now()."""
from datetime import date, datetime
import pytest


# --- is_trading_day(exchange_key, dt) ---

def test_is_trading_day_weekday():
    from app.data.calendar import is_trading_day
    # June 9, 2026 is a Tuesday — definitely a trading day (June 8 is Queen's Birthday)
    assert is_trading_day("ASX", date(2026, 6, 9)) is True


def test_is_trading_day_saturday():
    from app.data.calendar import is_trading_day
    assert is_trading_day("ASX", date(2026, 6, 6)) is False   # Saturday


def test_is_trading_day_sunday():
    from app.data.calendar import is_trading_day
    assert is_trading_day("ASX", date(2026, 6, 7)) is False   # Sunday


def test_is_trading_day_public_holiday():
    from app.data.calendar import is_trading_day
    assert is_trading_day("ASX", date(2026, 12, 25)) is False  # Christmas


def test_is_trading_day_crypto_always_true():
    from app.data.calendar import is_trading_day
    assert is_trading_day("CRYPTO_INDEPENDENTRESERVE", date(2026, 6, 7)) is True
    assert is_trading_day("CRYPTO_BINANCE", date(2026, 12, 25)) is True


def test_is_trading_day_unknown_exchange_defaults_to_asx():
    """Unknown exchange key falls back to ASX calendar."""
    from app.data.calendar import is_trading_day
    # Should not raise even with unknown exchange key
    result = is_trading_day("UNKNOWN_EXCHANGE", date(2026, 6, 8))
    assert isinstance(result, bool)


# --- market_is_open_now ---

def test_market_is_open_now_crypto_always_true(monkeypatch):
    from app.data import calendar as cal
    # Sunday 3am — crypto is open
    monkeypatch.setattr(cal, "_local_now", lambda exchange_key: datetime(2026, 6, 7, 3, 0))
    from app.data.calendar import market_is_open_now
    assert market_is_open_now("CRYPTO_INDEPENDENTRESERVE") is True


def test_market_is_open_now_asx_during_hours(monkeypatch):
    from app.data import calendar as cal
    from app.data.calendar import market_is_open_now
    # June 9 (Tuesday) 10:30am — non-holiday weekday during session
    monkeypatch.setattr(cal, "_local_now", lambda exchange_key: datetime(2026, 6, 9, 10, 30))
    assert market_is_open_now("ASX") is True


def test_market_is_open_now_asx_before_open(monkeypatch):
    from app.data import calendar as cal
    from app.data.calendar import market_is_open_now
    monkeypatch.setattr(cal, "_local_now", lambda exchange_key: datetime(2026, 6, 9, 8, 0))
    assert market_is_open_now("ASX") is False


def test_market_is_open_now_asx_after_close(monkeypatch):
    from app.data import calendar as cal
    from app.data.calendar import market_is_open_now
    monkeypatch.setattr(cal, "_local_now", lambda exchange_key: datetime(2026, 6, 9, 17, 0))
    assert market_is_open_now("ASX") is False


def test_market_is_open_now_asx_weekend(monkeypatch):
    from app.data import calendar as cal
    from app.data.calendar import market_is_open_now
    monkeypatch.setattr(cal, "_local_now", lambda exchange_key: datetime(2026, 6, 6, 11, 0))
    assert market_is_open_now("ASX") is False


def test_market_is_open_now_unknown_exchange_returns_false():
    from app.data.calendar import market_is_open_now
    # Unknown exchange has no session map entry → False
    assert market_is_open_now("UNKNOWN") is False


# --- today_is_trading_day ---

def test_today_is_trading_day_returns_bool():
    from app.data.calendar import today_is_trading_day
    result = today_is_trading_day("ASX")
    assert isinstance(result, bool)


# --- next_trading_day ---

def test_next_trading_day_asx_from_friday():
    from app.data.calendar import next_trading_day
    # Friday 2026-06-05 → Mon June 8 is Queen's Birthday holiday → next is Tuesday June 9
    nxt = next_trading_day("ASX", date(2026, 6, 5))
    assert nxt == date(2026, 6, 9)


def test_next_trading_day_crypto_is_tomorrow():
    from app.data.calendar import next_trading_day
    nxt = next_trading_day("CRYPTO_INDEPENDENTRESERVE", date(2026, 6, 5))
    assert nxt == date(2026, 6, 6)
