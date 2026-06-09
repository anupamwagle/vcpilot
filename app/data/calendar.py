"""
Multi-Exchange Market Calendar — trading days, public holidays, and session times.
Uses pandas_market_calendars with per-exchange calendar instances.

Supported exchanges:
  ASX             — Australian Securities Exchange (AEST/AEDT)
  NYSE            — New York Stock Exchange (ET)
  NASDAQ          — NASDAQ (ET, same schedule as NYSE)
  CRYPTO_*        — All crypto exchanges trade 24/7/365

Usage:
    from app.data.calendar import is_trading_day, market_is_open_now

    is_trading_day("ASX", date.today())
    market_is_open_now("NYSE")
    get_market_session("NASDAQ")  # returns open/close times in UTC
"""
from __future__ import annotations
from datetime import date, datetime, timedelta
from typing import Optional
import pytz
import pandas_market_calendars as mcal
from loguru import logger

# ---------------------------------------------------------------------------
# Calendar factory — one instance per exchange, cached
# ---------------------------------------------------------------------------

_calendars: dict[str, object] = {}

# Exchange → pandas_market_calendars calendar name
_CALENDAR_MAP: dict[str, str] = {
    "ASX":    "ASX",
    "NYSE":   "NYSE",
    "NASDAQ": "NASDAQ",
}

# Exchange → local timezone name
_TIMEZONE_MAP: dict[str, str] = {
    "ASX":    "Australia/Sydney",
    "NYSE":   "America/New_York",
    "NASDAQ": "America/New_York",
}

# Exchange → (open_hour, open_min, close_hour, close_min) in LOCAL time
_SESSION_MAP: dict[str, tuple] = {
    "ASX":    (10, 0, 16, 12),
    "NYSE":   (9,  30, 16, 0),
    "NASDAQ": (9,  30, 16, 0),
}


def _get_calendar(exchange_key: str):
    """Return (cached) pandas_market_calendars instance for this exchange."""
    if exchange_key not in _calendars:
        cal_name = _CALENDAR_MAP.get(exchange_key)
        if cal_name:
            _calendars[exchange_key] = mcal.get_calendar(cal_name)
        # Crypto has no calendar object — handled via is_crypto check
    return _calendars.get(exchange_key)


def _is_crypto(exchange_key: str) -> bool:
    """Crypto exchanges trade 24/7 — always open."""
    return exchange_key.startswith("CRYPTO_") or exchange_key == "CRYPTO"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_trading_day(exchange_key: str, dt: date) -> bool:
    """Return True if dt is a trading day for the given exchange."""
    if _is_crypto(exchange_key):
        return True  # Crypto never closes
    cal = _get_calendar(exchange_key)
    if cal is None:
        logger.warning(f"Unknown exchange '{exchange_key}' — defaulting to ASX calendar")
        cal = _get_calendar("ASX")
    schedule = cal.schedule(start_date=dt.isoformat(), end_date=dt.isoformat())
    return not schedule.empty


def market_is_open_now(exchange_key: str = "ASX") -> bool:
    """Check if the given exchange is currently in its trading session."""
    if _is_crypto(exchange_key):
        return True

    now_local = _local_now(exchange_key)
    if not is_trading_day(exchange_key, now_local.date()):
        return False

    session = _SESSION_MAP.get(exchange_key)
    if not session:
        return False

    open_h, open_m, close_h, close_m = session
    open_dt  = now_local.replace(hour=open_h,  minute=open_m,  second=0, microsecond=0)
    close_dt = now_local.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
    return open_dt <= now_local <= close_dt


def today_is_trading_day(exchange_key: str = "ASX") -> bool:
    """Return True if today is a trading day for the exchange."""
    from app.utils.time_helper import get_current_date
    return is_trading_day(exchange_key, get_current_date())


def next_trading_day(exchange_key: str, dt: date) -> date:
    """Return the next trading day after dt."""
    if _is_crypto(exchange_key):
        return dt + timedelta(days=1)
    candidate = dt + timedelta(days=1)
    for _ in range(14):
        if is_trading_day(exchange_key, candidate):
            return candidate
        candidate += timedelta(days=1)
    raise ValueError(f"No trading day found within 14 days of {dt} for {exchange_key}")


def previous_trading_day(exchange_key: str, dt: date) -> date:
    """Return the most recent trading day before dt."""
    if _is_crypto(exchange_key):
        return dt - timedelta(days=1)
    candidate = dt - timedelta(days=1)
    for _ in range(14):
        if is_trading_day(exchange_key, candidate):
            return candidate
        candidate -= timedelta(days=1)
    raise ValueError(f"No previous trading day found within 14 days of {dt} for {exchange_key}")


def get_trading_days(exchange_key: str, start: date, end: date) -> list[date]:
    """Return list of trading days between start and end (inclusive)."""
    if _is_crypto(exchange_key):
        days = []
        current = start
        while current <= end:
            days.append(current)
            current += timedelta(days=1)
        return days
    cal = _get_calendar(exchange_key)
    if cal is None:
        return []
    schedule = cal.schedule(start_date=start.isoformat(), end_date=end.isoformat())
    return [d.date() for d in schedule.index]


def minutes_to_open(exchange_key: str = "ASX") -> int:
    """Minutes until next session open. Returns 0 if market is open now."""
    if _is_crypto(exchange_key):
        return 0
    if market_is_open_now(exchange_key):
        return 0

    tz = pytz.timezone(_TIMEZONE_MAP.get(exchange_key, "UTC"))
    now_local = datetime.now(tz)
    session = _SESSION_MAP.get(exchange_key)
    if not session:
        return 9999
    open_h, open_m, close_h, close_m = session

    candidate = now_local.date()
    for _ in range(10):
        if is_trading_day(exchange_key, candidate):
            open_dt = tz.localize(datetime(candidate.year, candidate.month, candidate.day, open_h, open_m))
            if open_dt > now_local:
                return int((open_dt - now_local).total_seconds() / 60)
        candidate += timedelta(days=1)
    return 9999


def get_exchange_timezone(exchange_key: str) -> str:
    """Return the pytz timezone string for the exchange."""
    if _is_crypto(exchange_key):
        return "UTC"
    return _TIMEZONE_MAP.get(exchange_key, "UTC")


# ---------------------------------------------------------------------------
# Backward-compatible ASX helpers (preserve existing callers)
# ---------------------------------------------------------------------------

def asx_is_trading_day(dt: date) -> bool:
    return is_trading_day("ASX", dt)

def asx_market_is_open_now() -> bool:
    return market_is_open_now("ASX")

def asx_today_is_trading_day() -> bool:
    return today_is_trading_day("ASX")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _local_now(exchange_key: str) -> datetime:
    """Return current time in the exchange's local timezone."""
    from app.utils.time_helper import get_current_time
    tz_name = _TIMEZONE_MAP.get(exchange_key, "UTC")
    tz = pytz.timezone(tz_name)
    utc_now = get_current_time()
    if utc_now.tzinfo is None:
        utc_now = pytz.utc.localize(utc_now)
    return utc_now.astimezone(tz)
