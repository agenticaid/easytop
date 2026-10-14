"""Tiny TTL cache with an injectable clock. Only successful results are stored."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Hashable
from datetime import datetime
from typing import Any


class TtlCache:
    def __init__(self, clock: Callable[[], datetime], ttl_seconds: float = 300.0):
        self._clock = clock
        self._ttl = ttl_seconds
        self._items: dict[Hashable, tuple[datetime, Any]] = {}

    async def get_or_fetch(self, key: Hashable, fetch: Callable[[], Awaitable[Any]]) -> Any:
        now = self._clock()
        hit = self._items.get(key)
        if hit and (now - hit[0]).total_seconds() < self._ttl:
            return hit[1]
        value = await fetch()  # exceptions propagate and are never cached
        self._items[key] = (self._clock(), value)
        return value

    def clear(self) -> None:
        self._items.clear()
