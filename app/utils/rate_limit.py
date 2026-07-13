"""
Redis-backed rate limiting and account lockout for auth surfaces.

Fails open on Redis errors — refusing legitimate login/auth traffic during an
infra blip is worse than a brief lapse in throttling, matching the pattern
used for trading-critical Redis locks elsewhere (see CLAUDE.md #40,
_acquire_org_lock in app/tasks/trading.py).
"""
from loguru import logger
from app.utils.cache import cache


def _client():
    return cache._client if cache.enabled else None


def increment(key: str, window_seconds: int) -> int:
    """Increment a counter, creating it with a TTL on first increment.
    Returns the new count, or 0 if Redis is unavailable (never trips a
    threshold — callers compare with >=)."""
    client = _client()
    if client is None:
        return 0
    try:
        count = client.incr(key)
        if count == 1:
            client.expire(key, window_seconds)
        return count
    except Exception as e:
        logger.debug(f"rate_limit increment failed for {key}: {e}")
        return 0


def reset(key: str) -> None:
    client = _client()
    if client is None:
        return
    try:
        client.delete(key)
    except Exception as e:
        logger.debug(f"rate_limit reset failed for {key}: {e}")


def is_set(key: str) -> bool:
    client = _client()
    if client is None:
        return False
    try:
        return bool(client.exists(key))
    except Exception as e:
        logger.debug(f"rate_limit is_set check failed for {key}: {e}")
        return False


def set_with_ttl(key: str, ttl_seconds: int, value: str = "1") -> None:
    client = _client()
    if client is None:
        return
    try:
        client.set(key, value, ex=ttl_seconds)
    except Exception as e:
        logger.debug(f"rate_limit set_with_ttl failed for {key}: {e}")


def client_ip(request) -> str:
    """Source IP — honour reverse-proxy headers (Cloudflare / X-Forwarded-For),
    matching the pattern used for activity-log IP capture in web/main.py."""
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if ip:
        return ip
    client = getattr(request, "client", None)
    return client.host if client else "unknown"


def check_ip_throttle(request, bucket: str, max_requests: int = 10, window_seconds: int = 60) -> bool:
    """Returns True if the request should proceed, False if this client IP has
    exceeded max_requests to `bucket` within window_seconds."""
    count = increment(f"throttle:{bucket}:{client_ip(request)}", window_seconds)
    return count == 0 or count <= max_requests
