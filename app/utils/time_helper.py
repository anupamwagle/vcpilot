"""
Time Helper Utility
Provides global access to the system date and time, supporting clock mocking
for historical simulations and testing.
"""
from datetime import datetime, date
import pytz
from loguru import logger

def get_current_time() -> datetime:
    """
    Returns the current datetime. If clock mocking is enabled globally,
    returns the parsed simulated time. Otherwise, returns the actual Sydney time.
    """
    from app.config import settings
    
    sydney_tz = pytz.timezone("Australia/Sydney")
    
    if settings.mock_time_enabled:
        mock_str = settings.mock_current_time
        if mock_str:
            # Try to parse the mocked datetime string
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(mock_str.strip(), fmt)
                    # Localize naive datetime to Sydney/AEST
                    return sydney_tz.localize(dt)
                except ValueError:
                    continue
            logger.error(f"Failed to parse mock_current_time: '{mock_str}'. Format must be YYYY-MM-DD HH:MM:SS.")
            
    # Fallback to actual real time in Sydney timezone
    return datetime.now(sydney_tz)

def get_current_date() -> date:
    """
    Returns the current date, respecting the mock clock if enabled.
    """
    return get_current_time().date()
