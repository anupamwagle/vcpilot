"""Extended tests for app/utils/time_helper.py and app/utils/cache.py."""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, date


# ---- time_helper.py --------------------------------------------------------

def test_get_current_time_returns_datetime():
    from app.utils.time_helper import get_current_time
    result = get_current_time()
    assert isinstance(result, datetime)


def test_get_current_date_returns_date():
    from app.utils.time_helper import get_current_date
    result = get_current_date()
    assert isinstance(result, date)


def _mock_settings(mock_time_enabled, mock_current_time):
    """Return a mock settings object for time_helper patching."""
    m = type("MockSettings", (), {
        "mock_time_enabled": mock_time_enabled,
        "mock_current_time": mock_current_time,
    })()
    return m


def test_get_current_time_mock_with_datetime_string(monkeypatch):
    monkeypatch.setattr("app.config.settings",
                        _mock_settings(True, "2026-06-10 10:30:00"))
    from app.utils.time_helper import get_current_time
    result = get_current_time()
    assert result.year == 2026
    assert result.month == 6
    assert result.day == 10
    assert result.hour == 10
    assert result.minute == 30


def test_get_current_time_mock_with_date_only(monkeypatch):
    monkeypatch.setattr("app.config.settings",
                        _mock_settings(True, "2026-01-15"))
    from app.utils.time_helper import get_current_time
    result = get_current_time()
    assert result.year == 2026
    assert result.month == 1
    assert result.day == 15


def test_get_current_time_mock_with_hhmm_format(monkeypatch):
    monkeypatch.setattr("app.config.settings",
                        _mock_settings(True, "2026-03-20 14:00"))
    from app.utils.time_helper import get_current_time
    result = get_current_time()
    assert result.hour == 14
    assert result.minute == 0


def test_get_current_time_mock_invalid_falls_back_to_real(monkeypatch):
    monkeypatch.setattr("app.config.settings",
                        _mock_settings(True, "not-a-date"))
    from app.utils.time_helper import get_current_time
    result = get_current_time()
    assert isinstance(result, datetime)


def test_get_current_time_mock_empty_string_uses_real(monkeypatch):
    monkeypatch.setattr("app.config.settings",
                        _mock_settings(True, ""))
    from app.utils.time_helper import get_current_time
    result = get_current_time()
    assert isinstance(result, datetime)


def test_get_current_time_mock_disabled_uses_real(monkeypatch):
    monkeypatch.setattr("app.config.settings",
                        _mock_settings(False, ""))
    from app.utils.time_helper import get_current_time
    result = get_current_time()
    assert isinstance(result, datetime)
    now = datetime.now(result.tzinfo)
    diff = abs((now - result).total_seconds())
    assert diff < 5


# ---- cache.py (RedisCache) ---------------------------------------------------

def _make_cache_no_redis():
    """Create a RedisCache where Redis is unavailable (falls back to local memory)."""
    from app.utils.cache import RedisCache
    with patch("redis.Redis.from_url", side_effect=Exception("Redis unavailable")):
        c = RedisCache()
    assert c.enabled is False
    return c


def test_redis_cache_falls_back_to_local_when_unavailable():
    c = _make_cache_no_redis()
    assert c.enabled is False
    assert c._local_cache == {}


def test_redis_cache_set_and_get_local():
    c = _make_cache_no_redis()
    c.set("mykey", {"foo": "bar"})
    result = c.get("mykey")
    assert result == {"foo": "bar"}


def test_redis_cache_get_missing_returns_none():
    c = _make_cache_no_redis()
    result = c.get("nonexistent")
    assert result is None


def test_redis_cache_delete_existing():
    c = _make_cache_no_redis()
    c.set("delme", 42)
    result = c.delete("delme")
    assert result is True
    assert c.get("delme") is None


def test_redis_cache_delete_nonexistent():
    c = _make_cache_no_redis()
    result = c.delete("doesnotexist")
    assert result is False


def test_redis_cache_mget_empty():
    c = _make_cache_no_redis()
    result = c.mget([])
    assert result == []


def test_redis_cache_mget_multiple_keys():
    c = _make_cache_no_redis()
    c.set("k1", 10)
    c.set("k2", 20)
    result = c.mget(["k1", "k2", "k3"])
    assert result[0] == 10
    assert result[1] == 20
    assert result[2] is None


def test_redis_cache_with_redis_connected():
    """Test the Redis-connected path using a mock client."""
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    mock_client.get.return_value = '{"value": 99}'
    mock_client.set.return_value = True
    mock_client.mget.return_value = ['{"a": 1}', None, '{"b": 2}']

    with patch("redis.Redis.from_url", return_value=mock_client):
        from app.utils.cache import RedisCache
        c = RedisCache()

    assert c.enabled is True
    val = c.get("anykey")
    assert val == {"value": 99}


def test_redis_cache_get_redis_exception_falls_back():
    """When Redis.get raises, fall back to local cache."""
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    mock_client.get.side_effect = Exception("Redis timeout")

    with patch("redis.Redis.from_url", return_value=mock_client):
        from app.utils.cache import RedisCache
        c = RedisCache()

    c._local_cache["fallback_key"] = "local_value"
    result = c.get("fallback_key")
    assert result == "local_value"


def test_redis_cache_delete_with_redis():
    """Redis delete path."""
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    mock_client.delete.return_value = 1

    with patch("redis.Redis.from_url", return_value=mock_client):
        from app.utils.cache import RedisCache
        c = RedisCache()

    result = c.delete("any_key")
    assert result is True
    mock_client.delete.assert_called_once_with("any_key")


def test_redis_cache_mget_with_redis():
    """Redis mget path."""
    mock_client = MagicMock()
    mock_client.ping.return_value = True
    mock_client.mget.return_value = ['{"x": 1}', None]

    with patch("redis.Redis.from_url", return_value=mock_client):
        from app.utils.cache import RedisCache
        c = RedisCache()

    result = c.mget(["k1", "k2"])
    assert result[0] == {"x": 1}
    assert result[1] is None
