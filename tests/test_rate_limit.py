"""Tests for app/utils/rate_limit.py — Redis-backed throttling/lockout."""
import pytest
from types import SimpleNamespace


class _FakeRedis:
    """Minimal in-memory stand-in for the subset of redis-py used by rate_limit.py."""
    def __init__(self):
        self.store = {}

    def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def expire(self, key, seconds):
        pass  # TTL not simulated — not needed for these tests

    def exists(self, key):
        return 1 if key in self.store else 0

    def delete(self, key):
        self.store.pop(key, None)

    def set(self, key, value, ex=None):
        self.store[key] = value


@pytest.fixture
def fake_redis(monkeypatch):
    from app.utils import rate_limit
    fake = _FakeRedis()
    monkeypatch.setattr(rate_limit, "_client", lambda: fake)
    return fake


def test_increment_creates_and_increments(fake_redis):
    from app.utils import rate_limit
    assert rate_limit.increment("k", 60) == 1
    assert rate_limit.increment("k", 60) == 2
    assert rate_limit.increment("k", 60) == 3


def test_increment_returns_zero_when_redis_unavailable(monkeypatch):
    from app.utils import rate_limit
    monkeypatch.setattr(rate_limit, "_client", lambda: None)
    assert rate_limit.increment("k", 60) == 0


def test_reset_clears_counter(fake_redis):
    from app.utils import rate_limit
    rate_limit.increment("k", 60)
    rate_limit.reset("k")
    assert fake_redis.exists("k") == 0


def test_is_set_and_set_with_ttl(fake_redis):
    from app.utils import rate_limit
    assert rate_limit.is_set("lock") is False
    rate_limit.set_with_ttl("lock", 60)
    assert rate_limit.is_set("lock") is True


def test_check_ip_throttle_allows_under_limit(fake_redis):
    from app.utils import rate_limit
    req = SimpleNamespace(headers={}, client=SimpleNamespace(host="1.2.3.4"))
    for _ in range(10):
        assert rate_limit.check_ip_throttle(req, "test", max_requests=10, window_seconds=60) is True


def test_check_ip_throttle_blocks_over_limit(fake_redis):
    from app.utils import rate_limit
    req = SimpleNamespace(headers={}, client=SimpleNamespace(host="1.2.3.4"))
    for _ in range(10):
        rate_limit.check_ip_throttle(req, "test2", max_requests=10, window_seconds=60)
    assert rate_limit.check_ip_throttle(req, "test2", max_requests=10, window_seconds=60) is False


def test_check_ip_throttle_uses_forwarded_for_header(fake_redis):
    from app.utils import rate_limit
    req1 = SimpleNamespace(headers={"x-forwarded-for": "9.9.9.9"}, client=SimpleNamespace(host="1.2.3.4"))
    req2 = SimpleNamespace(headers={"x-forwarded-for": "9.9.9.9, 5.5.5.5"}, client=SimpleNamespace(host="9.8.7.6"))
    rate_limit.check_ip_throttle(req1, "fwd", max_requests=10, window_seconds=60)
    rate_limit.check_ip_throttle(req2, "fwd", max_requests=10, window_seconds=60)
    # Both requests share the first X-Forwarded-For hop, so they share one counter.
    assert fake_redis.store.get("throttle:fwd:9.9.9.9") == 2


def test_client_ip_falls_back_to_request_client_when_no_forwarded_header():
    from app.utils import rate_limit
    req = SimpleNamespace(headers={}, client=SimpleNamespace(host="1.2.3.4"))
    assert rate_limit.client_ip(req) == "1.2.3.4"


def test_client_ip_handles_missing_client_attribute():
    """Route functions called directly in tests (bypassing the ASGI stack)
    often use a bare stand-in Request with no .client attribute at all."""
    from app.utils import rate_limit
    req = SimpleNamespace(headers={})
    assert rate_limit.client_ip(req) == "unknown"
