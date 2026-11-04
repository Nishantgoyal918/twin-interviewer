"""Redis = cache + coordination (plan 6.3). Every key is namespaced `ti:` and has a TTL.

Two backends with the same tiny surface: Upstash (REST) for real use, Memory for tests and
local dev. Anything here must be rebuildable from Postgres, except rate/spend counters,
which fail closed for voice (plan 10.6).
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from typing import Any, Protocol

from app.settings import get_settings

_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) "
    "else return 0 end"
)


class CacheUnavailable(RuntimeError):
    pass


class Backend(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str, *, px: int | None = None, nx: bool = False) -> bool: ...
    def delete(self, key: str) -> None: ...
    def release_if(self, key: str, token: str) -> None: ...
    def incr_by(self, key: str, amount: float, ttl_s: int) -> float: ...


class MemoryBackend:
    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float | None]] = {}
        self._lock = threading.Lock()

    def _alive(self, key: str) -> str | None:
        item = self._data.get(key)
        if item is None:
            return None
        value, expires = item
        if expires is not None and expires < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._alive(key)

    def set(self, key: str, value: str, *, px: int | None = None, nx: bool = False) -> bool:
        with self._lock:
            if nx and self._alive(key) is not None:
                return False
            expires = time.monotonic() + px / 1000 if px else None
            self._data[key] = (value, expires)
            return True

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def release_if(self, key: str, token: str) -> None:
        with self._lock:
            if self._alive(key) == token:
                self._data.pop(key, None)

    def incr_by(self, key: str, amount: float, ttl_s: int) -> float:
        with self._lock:
            current = float(self._alive(key) or 0)
            new = current + amount
            item = self._data.get(key)
            expires = item[1] if item and item[1] else time.monotonic() + ttl_s
            self._data[key] = (repr(new), expires)
            return new

    def flush(self) -> None:
        with self._lock:
            self._data.clear()


class UpstashBackend:
    def __init__(self, url: str, token: str) -> None:
        from upstash_redis import Redis

        self._r = Redis(url=url, token=token)

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # network / quota errors
            raise CacheUnavailable(str(exc)) from exc

    def get(self, key: str) -> str | None:
        return self._call(self._r.get, key)

    def set(self, key: str, value: str, *, px: int | None = None, nx: bool = False) -> bool:
        res = self._call(self._r.set, key, value, px=px, nx=nx)
        return bool(res)

    def delete(self, key: str) -> None:
        self._call(self._r.delete, key)

    def release_if(self, key: str, token: str) -> None:
        self._call(self._r.eval, _RELEASE_LUA, [key], [token])

    def incr_by(self, key: str, amount: float, ttl_s: int) -> float:
        new = float(self._call(self._r.incrbyfloat, key, amount))
        if new == amount:  # first write: set TTL
            self._call(self._r.expire, key, ttl_s)
        return new


_backend: Backend | None = None


def backend() -> Backend:
    global _backend
    if _backend is None:
        s = get_settings()
        if s.cache_backend == "memory":
            _backend = MemoryBackend()
        else:
            _backend = UpstashBackend(s.upstash_redis_rest_url, s.upstash_redis_rest_token)
    return _backend


def set_backend(b: Backend | None) -> None:
    global _backend
    _backend = b


# ---- key helpers ---------------------------------------------------------------------------

def k_state(session_id: str) -> str:
    return f"ti:sess:{session_id}:state"


def k_recent(session_id: str) -> str:
    return f"ti:sess:{session_id}:recent"


def k_lock(session_id: str) -> str:
    return f"ti:sess:{session_id}:lock"


def k_idem(key: str) -> str:
    return f"ti:idem:{key}"


def k_spec(twin_id: str) -> str:
    return f"ti:twin:{twin_id}:spec"


def k_rl_ip(ip: str, minute: int) -> str:
    return f"ti:rl:ip:{ip}:{minute}"


def k_rl_twin_seconds(twin_id: str, day: str) -> str:
    return f"ti:rl:twin:{twin_id}:{day}:seconds"


def k_rl_twin_sessions(twin_id: str, day: str) -> str:
    return f"ti:rl:twin:{twin_id}:{day}:sessions"


def k_spend(day: str) -> str:
    return f"ti:spend:{day}"


# ---- soft helpers: a cache miss or outage never breaks the request -------------------------

def get_json(key: str) -> Any | None:
    try:
        raw = backend().get(key)
    except CacheUnavailable:
        return None
    return json.loads(raw) if raw else None


def set_json(key: str, value: Any, ttl_s: int) -> None:
    try:
        backend().set(key, json.dumps(value, default=str), px=ttl_s * 1000)
    except CacheUnavailable:
        pass


def delete(key: str) -> None:
    try:
        backend().delete(key)
    except CacheUnavailable:
        pass


class LockBusy(RuntimeError):
    pass


class SessionLock:
    """`SET NX PX` lock with an owner token; released only by its owner (plan 6.3/6.4)."""

    def __init__(self, session_id: str) -> None:
        cfg = get_settings().cfg
        self.key = k_lock(session_id)
        self.ttl_ms = int(cfg("turn.lock_ttl_ms", 90000))
        self.wait_ms = int(cfg("turn.lock_wait_ms", 5000))
        self.token = secrets.token_hex(8)
        self.degraded = False

    def __enter__(self) -> SessionLock:
        deadline = time.monotonic() + self.wait_ms / 1000
        while True:
            try:
                if backend().set(self.key, self.token, px=self.ttl_ms, nx=True):
                    return self
            except CacheUnavailable:
                # Redis down: Postgres unique constraints still protect correctness.
                self.degraded = True
                return self
            if time.monotonic() >= deadline:
                raise LockBusy(self.key)
            time.sleep(0.1)

    def __exit__(self, *exc) -> None:
        if self.degraded:
            return
        try:
            backend().release_if(self.key, self.token)
        except CacheUnavailable:
            pass
