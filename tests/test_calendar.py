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


# --- previous_trading_day ---

def test_previous_trading_day_asx():
    from app.data.calendar import previous_trading_day
    tuesday = date(2026, 6, 9)
    prev = previous_trading_day("ASX", tuesday)
    assert prev < tuesday


def test_previous_trading_day_crypto():
    from app.data.calendar import previous_trading_day
    today = date(2026, 6, 10)
    prev = previous_trading_day("CRYPTO_INDEPENDENTRESERVE", today)
    assert prev == date(2026, 6, 9)


def test_previous_trading_day_asx_from_monday_skips_weekend():
    from app.data.calendar import previous_trading_day
    monday = date(2026, 6, 9)
    prev = previous_trading_day("ASX", monday)
    # Queen's Birthday Mon June 8 is holiday → previous is Fri June 5
    assert prev == date(2026, 6, 5)


# --- get_trading_days ---

def test_get_trading_days_crypto_returns_all_days():
    from app.data.calendar import get_trading_days
    start = date(2026, 6, 1)
    end = date(2026, 6, 7)
    days = get_trading_days("CRYPTO_BINANCE", start, end)
    assert len(days) == 7


def test_get_trading_days_asx_weekends_excluded():
    from app.data.calendar import get_trading_days
    start = date(2026, 6, 8)   # Monday (holiday)
    end = date(2026, 6, 14)    # Sunday
    days = get_trading_days("ASX", start, end)
    # Should exclude Saturday, Sunday and Monday (Queen's Birthday)
    assert len(days) <= 4


# --- minutes_to_open ---

def test_minutes_to_open_crypto_is_zero():
    from app.data.calendar import minutes_to_open
    result = minutes_to_open("CRYPTO")
    assert result == 0


def test_minutes_to_open_asx_returns_int(monkeypatch):
    from app.data import calendar as cal
    from app.data.calendar import minutes_to_open
    # Mock market closed at 7am
    monkeypatch.setattr(cal, "_local_now", lambda exchange_key: datetime(2026, 6, 9, 7, 0))
    monkeypatch.setattr(cal, "market_is_open_now", lambda *a: False)
    result = minutes_to_open("ASX")
    assert isinstance(result, int)
    assert result >= 0


def test_minutes_to_open_unknown_exchange():
    from app.data.calendar import minutes_to_open
    result = minutes_to_open("UNKNOWN_EXCHANGE")
    assert result == 9999


# --- get_exchange_timezone ---

def test_get_exchange_timezone_asx():
    from app.data.calendar import get_exchange_timezone
    tz = get_exchange_timezone("ASX")
    assert "Sydney" in tz or "Australia" in tz


def test_get_exchange_timezone_nyse():
    from app.data.calendar import get_exchange_timezone
    tz = get_exchange_timezone("NYSE")
    assert "New_York" in tz or "America" in tz


def test_get_exchange_timezone_crypto_utc():
    from app.data.calendar import get_exchange_timezone
    tz = get_exchange_timezone("CRYPTO_BINANCE")
    assert tz == "UTC"


def test_get_exchange_timezone_unknown():
    from app.data.calendar import get_exchange_timezone
    tz = get_exchange_timezone("SOME_UNKNOWN_EXCHANGE")
    assert tz == "UTC"


# --- backward-compat ASX helpers ---

def test_asx_is_trading_day_delegates():
    from app.data.calendar import asx_is_trading_day
    saturday = date(2026, 6, 13)
    assert asx_is_trading_day(saturday) is False
    monday = date(2026, 6, 9)
    assert isinstance(asx_is_trading_day(monday), bool)


def test_asx_market_is_open_now_returns_bool():
    from app.data.calendar import asx_market_is_open_now
    result = asx_market_is_open_now()
    assert isinstance(result, bool)


def test_asx_today_is_trading_day_returns_bool():
    from app.data.calendar import asx_today_is_trading_day
    result = asx_today_is_trading_day()
    assert isinstance(result, bool)


# --- _local_now ---

def test_local_now_returns_localized_datetime():
    from app.data.calendar import _local_now
    result = _local_now("ASX")
    assert result.tzinfo is not None


def test_local_now_crypto_uses_utc():
    from app.data.calendar import _local_now
    result = _local_now("CRYPTO")
    assert result.tzinfo is not None
