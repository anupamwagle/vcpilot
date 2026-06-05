"""
ASX Market Calendar — trading days, public holidays, and session times.
Uses pandas_market_calendars with the ASX exchange definition.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta
import pandas_market_calendars as mcal
from loguru import logger

_asx_cal = mcal.get_calendar("ASX")


def is_trading_day(dt: date) -> bool:
    """Return True if the given date is an ASX trading day."""
    schedule = _asx_cal.schedule(
        start_date=dt.isoformat(),
        end_date=dt.isoformat(),
    )
    return not schedule.empty


def next_trading_day(dt: date) -> date:
    """Return the next ASX trading day after dt."""
    candidate = dt + timedelta(days=1)
    for _ in range(14):  # Max 2 weeks (handles long holiday periods)
        if is_trading_day(candidate):
            return candidate
        candidate += timedelta(days=1)
    raise ValueError(f"Could not find next trading day within 14 days of {dt}")


def previous_trading_day(dt: date) -> date:
    """Return the most recent ASX trading day before dt."""
    candidate = dt - timedelta(days=1)
    for _ in range(14):
        if is_trading_day(candidate):
            return candidate
        candidate -= timedelta(days=1)
    raise ValueError(f"Could not find previous trading day within 14 days of {dt}")


def get_trading_days(start: date, end: date) -> list[date]:
    """Return list of ASX trading days between start and end (inclusive)."""
    schedule = _asx_cal.schedule(
        start_date=start.isoformat(),
        end_date=end.isoformat(),
    )
    return [d.date() for d in schedule.index]


def today_is_trading_day() -> bool:
    from app.utils.time_helper import get_current_date
    return is_trading_day(get_current_date())


def market_is_open_now() -> bool:
    """Check if ASX is currently in session (AEST 10:00–16:12)."""
    from app.utils.time_helper import get_current_time
    now_aest = get_current_time()
    if not is_trading_day(now_aest.date()):
        return False
    open_time  = now_aest.replace(hour=10, minute=0, second=0, microsecond=0)
    close_time = now_aest.replace(hour=16, minute=12, second=0, microsecond=0)
    return open_time <= now_aest <= close_time


def minutes_to_open() -> int:
    """Minutes until next ASX open. Returns 0 if market is open."""
    import pytz
    from app.utils.time_helper import get_current_time
    aest = pytz.timezone("Australia/Sydney")
    now_aest = get_current_time()
    if market_is_open_now():
        return 0
    candidate = now_aest.date()
    for _ in range(10):
        if is_trading_day(candidate):
            open_dt = aest.localize(datetime(
                candidate.year, candidate.month, candidate.day, 10, 0
            ))
            if open_dt > now_aest:
                delta = open_dt - now_aest
                return int(delta.total_seconds() / 60)
        candidate += timedelta(days=1)
    return 9999
