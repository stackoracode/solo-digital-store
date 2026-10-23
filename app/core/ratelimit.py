"""Fixed-window rate limiter. Redis in deployed environments, in-process memory for dev/tests."""

from __future__ import annotations

import threading
import time
from functools import lru_cache

from app.config import get_settings


class RateLimiter:
    def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        raise NotImplementedError

    def reset(self) -> None:  # pragma: no cover - test helper
        pass


class MemoryRateLimiter(RateLimiter):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[int, float]] = {}

    def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        with self._lock:
            count, reset_at = self._buckets.get(key, (0, now + window_seconds))
            if now >= reset_at:
                count, reset_at = 0, now + window_seconds
            count += 1
            self._buckets[key] = (count, reset_at)
            if len(self._buckets) > 100_000:  # bound memory under attack
                self._buckets = {k: v for k, v in self._buckets.items() if v[1] > now}
            return count <= limit

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


class RedisRateLimiter(RateLimiter):
    def __init__(self, url: str) -> None:
        import redis

        self._redis = redis.Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)
        self._fallback = MemoryRateLimiter()

    def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        bucket = f"rl:{key}:{int(time.time() // window_seconds)}"
        try:
            pipe = self._redis.pipeline()
            pipe.incr(bucket)
            pipe.expire(bucket, window_seconds + 1)
            count = int(pipe.execute()[0])
        except Exception:  # noqa: BLE001 - fail closed-ish: fall back to per-process limits
            return self._fallback.hit(key, limit, window_seconds)
        return count <= limit


@lru_cache
def get_limiter() -> RateLimiter:
    url = get_settings().redis_url
    return RedisRateLimiter(url) if url else MemoryRateLimiter()
