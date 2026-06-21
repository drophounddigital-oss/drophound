"""Simple in-process TTL cache for expensive DB reads.

Keys expire after `ttl` seconds. Call `invalidate(prefix)` to bust a key
family immediately (e.g. after a write).

This is not shared between processes — suitable for a single-worker Render
deployment. If you ever run multiple workers, replace with Redis.
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Any

_store: dict[str, tuple[float, Any]] = {}
_lock = Lock()


def get(key: str) -> Any | None:
    """Return cached value or None if missing / expired."""
    with _lock:
        entry = _store.get(key)
    if entry is None:
        return None
    exp, val = entry
    if time.time() > exp:
        with _lock:
            _store.pop(key, None)
        return None
    return val


def set(key: str, value: Any, ttl: int = 30) -> None:
    with _lock:
        _store[key] = (time.time() + ttl, value)


def invalidate(prefix: str = "") -> None:
    """Delete all keys that start with `prefix` (or all keys if empty)."""
    with _lock:
        keys = [k for k in _store if k.startswith(prefix)]
        for k in keys:
            del _store[k]


def cached(key: str, ttl: int = 30):
    """Decorator: cache the return value of a function under `key` for `ttl` seconds."""
    def decorator(fn):
        def wrapper(*args, **kwargs):
            hit = get(key)
            if hit is not None:
                return hit
            val = fn(*args, **kwargs)
            set(key, val, ttl)
            return val
        return wrapper
    return decorator
