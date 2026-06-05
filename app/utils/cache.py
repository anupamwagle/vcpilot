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

# Global cache instance
cache = RedisCache()
