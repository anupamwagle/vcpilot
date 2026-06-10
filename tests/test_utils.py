"""Tests for app/utils/cache.py (RedisCache) and app/utils/time_helper.py."""
import json
import pytest
from unittest.mock import MagicMock, patch


# --- time_helper ---

def test_get_current_date_returns_date():
    from app.utils.time_helper import get_current_date
    from datetime import date
    d = get_current_date()
    assert isinstance(d, date)


def test_get_current_time_returns_datetime():
    from app.utils.time_helper import get_current_time
    from datetime import datetime
    dt = get_current_time()
    assert isinstance(dt, datetime)


# --- RedisCache helpers ---
# We use the in-memory local_cache mode (enabled=False) so no Redis is needed.

def _mem_cache():
    """Build a RedisCache in local-memory mode — no Redis required."""
    from app.utils.cache import RedisCache
    c = RedisCache.__new__(RedisCache)
    c.enabled = False
    c._client = None
    c._local_cache = {}
    return c


def test_redis_cache_set_and_get():
    c = _mem_cache()
    c.set("mykey", {"val": 42}, expire_seconds=60)
    result = c.get("mykey")
    assert result == {"val": 42}


def test_redis_cache_get_miss_returns_none():
    c = _mem_cache()
    assert c.get("nonexistent_key") is None


def test_redis_cache_delete():
    c = _mem_cache()
    c.set("delkey", "hello", 60)
    assert c.get("delkey") is not None
    c.delete("delkey")
    assert c.get("delkey") is None


def test_redis_cache_mget():
    c = _mem_cache()
    c.set("k1", 1, 60)
    c.set("k2", 2, 60)
    results = c.mget(["k1", "k2", "k3"])
    assert results[0] == 1
    assert results[1] == 2
    assert results[2] is None


def test_redis_cache_mget_empty_list():
    c = _mem_cache()
    assert c.mget([]) == []


def test_redis_cache_delete_local_returns_true():
    c = _mem_cache()
    c._local_cache["x"] = "y"
    result = c.delete("x")
    assert result is True
    assert c.get("x") is None


def test_redis_cache_delete_nonexistent_returns_false():
    c = _mem_cache()
    assert c.delete("missing") is False


def test_redis_cache_set_handles_redis_error():
    """set() must fall back to local cache when Redis raises."""
    import redis
    from app.utils.cache import RedisCache
    c = RedisCache.__new__(RedisCache)
    c.enabled = True
    c._local_cache = {}
    mock_client = MagicMock()
    mock_client.set.side_effect = redis.RedisError("boom")
    c._client = mock_client
    # Should fall back to local cache without raising
    c.set("fallback_key", "v", 60)
    assert c._local_cache.get("fallback_key") == "v"


def test_redis_cache_get_falls_back_to_local_on_error():
    import redis
    from app.utils.cache import RedisCache
    c = RedisCache.__new__(RedisCache)
    c.enabled = True
    c._local_cache = {"lkey": "lval"}
    mock_client = MagicMock()
    mock_client.get.side_effect = redis.RedisError("boom")
    c._client = mock_client
    assert c.get("lkey") == "lval"


def test_redis_cache_mget_falls_back_to_local_on_error():
    import redis
    from app.utils.cache import RedisCache
    c = RedisCache.__new__(RedisCache)
    c.enabled = True
    c._local_cache = {"k1": "a", "k2": "b"}
    mock_client = MagicMock()
    mock_client.mget.side_effect = redis.RedisError("boom")
    c._client = mock_client
    results = c.mget(["k1", "k2", "missing"])
    assert results[0] == "a"
    assert results[1] == "b"
    assert results[2] is None
