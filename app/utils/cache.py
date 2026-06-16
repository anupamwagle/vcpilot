import json
from typing import Any, Optional, Dict
import redis
from loguru import logger
from app.config import settings

class RedisCache:
    def __init__(self):
        self.enabled = False
        self._client = None
        try:
            # Initialize Redis connection
            self._client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
            # Test connection
            self._client.ping()
            self.enabled = True
            logger.info("Redis cache initialized and connected successfully")
        except Exception as e:
            logger.warning(f"Failed to initialize Redis cache, falling back to local memory: {e}")

        self._local_cache: Dict[str, Any] = {}

    def get(self, key: str) -> Optional[Any]:
        if self.enabled and self._client:
            try:
                val = self._client.get(key)
                if val is not None:
                    return json.loads(val)
            except Exception as e:
                logger.debug(f"Redis cache get failed for key {key}: {e}")
        return self._local_cache.get(key)

    def set(self, key: str, value: Any, expire_seconds: int = 300) -> bool:
        """Store value in cache with expiration (default 5 minutes)."""
        try:
            serialized = json.dumps(value)
            if self.enabled and self._client:
                self._client.set(key, serialized, ex=expire_seconds)
                return True
        except Exception as e:
            logger.debug(f"Redis cache set failed for key {key}: {e}")
        
        self._local_cache[key] = value
        # Basic in-memory expiration is not implemented, but since it's just a fallback, it's fine.
        return True

    def mget(self, keys: list[str]) -> list[Optional[Any]]:
        """Fetch multiple keys in a single round-trip."""
        if not keys:
            return []
        
        if self.enabled and self._client:
            try:
                values = self._client.mget(keys)
                result = []
                for val in values:
                    if val is not None:
                        result.append(json.loads(val))
                    else:
                        result.append(None)
                return result
            except Exception as e:
                logger.debug(f"Redis cache mget failed: {e}")
        
        return [self._local_cache.get(k) for k in keys]

    def delete(self, key: str) -> bool:
        if self.enabled and self._client:
            try:
                self._client.delete(key)
                return True
            except Exception as e:
                logger.debug(f"Redis cache delete failed for key {key}: {e}")

        if key in self._local_cache:
            del self._local_cache[key]
            return True
        return False

    def delete_prefix(self, prefix: str) -> int:
        """Delete all keys matching the given prefix. Returns count of deleted keys."""
        deleted = 0
        if self.enabled and self._client:
            try:
                # Use SCAN to avoid blocking Redis on large key sets
                cursor = 0
                pattern = f"{prefix}*"
                keys_to_delete = []
                while True:
                    cursor, keys = self._client.scan(cursor, match=pattern, count=100)
                    keys_to_delete.extend(keys)
                    if cursor == 0:
                        break
                if keys_to_delete:
                    deleted = self._client.delete(*keys_to_delete)
                return deleted
            except Exception as e:
                logger.debug(f"Redis cache delete_prefix failed for prefix {prefix}: {e}")

        # In-memory fallback
        keys_to_remove = [k for k in list(self._local_cache.keys()) if k.startswith(prefix)]
        for k in keys_to_remove:
            del self._local_cache[k]
        return len(keys_to_remove)

    def set_raw(self, key: str, value: str, expire_seconds: int = 300) -> bool:
        """Store a raw string (e.g. pre-rendered HTML) without JSON encoding."""
        if self.enabled and self._client:
            try:
                self._client.set(key, value, ex=expire_seconds)
                return True
            except Exception as e:
                logger.debug(f"Redis cache set_raw failed for key {key}: {e}")
        self._local_cache[key] = value
        return True

    def get_raw(self, key: str) -> Optional[str]:
        """Retrieve a raw string value (stored via set_raw)."""
        if self.enabled and self._client:
            try:
                val = self._client.get(key)
                return val  # already a string (decode_responses=True)
            except Exception as e:
                logger.debug(f"Redis cache get_raw failed for key {key}: {e}")
        v = self._local_cache.get(key)
        return v if isinstance(v, str) else None

# Global cache instance
cache = RedisCache()
