"""Short-lived shared cache for horizontally-scalable conversation state (NFR-5).

Multi-turn conversation state (:mod:`copilot.orchestrator.conversation`) cannot
live in a process global: the service is stateless and horizontally scaled, so a
follow-up request may land on a *different* replica than the one that started the
conversation. This module defines the tiny cache seam that keeps that state
coherent across replicas — an async ``get`` / ``set(key, value, ttl)`` interface
plus an in-memory default implementation.

:class:`TTLCache` is the development / single-process implementation (a dict with
monotonic expiry). In production the same :class:`Cache` interface is backed by a
short-lived shared store (Redis) so prewarm and conversation state stay coherent
across replicas — swap the implementation, not the callers. No real Redis
dependency is pulled in at M2; the interface is the seam.

Design notes:
- The expiry clock is injectable (``clock``) so TTL behaviour is deterministically
  testable without sleeping.
- Values are stored by reference in the in-memory impl; a Redis-backed impl would
  serialise them (the pydantic models used as values are JSON-round-trippable).
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Generic, TypeVar

__all__ = ["Cache", "TTLCache", "Clock"]

T = TypeVar("T")

#: A monotonic clock: returns a non-decreasing float in seconds. ``time.monotonic``
#: by default; tests inject a fake to advance time deterministically.
Clock = Callable[[], float]


class Cache(ABC, Generic[T]):
    """The short-lived shared-cache seam (NFR-5).

    Two async operations, keyed by string: fetch a live value or ``None`` when
    absent/expired, and store a value with a time-to-live. The production
    implementation is a Redis-backed store; :class:`TTLCache` is the in-memory
    default. Callers depend on this interface, never on the concrete store.
    """

    @abstractmethod
    async def get(self, key: str) -> T | None:
        """Return the live value for ``key``, or ``None`` if absent or expired."""

    @abstractmethod
    async def set(self, key: str, value: T, ttl: float) -> None:
        """Store ``value`` under ``key`` for ``ttl`` seconds (a fresh ttl on each set)."""


class TTLCache(Cache[T]):
    """In-memory :class:`Cache` with monotonic per-key expiry.

    The default implementation used in development and tests: a plain dict mapping
    a key to ``(expires_at, value)``, where ``expires_at`` is a monotonic
    timestamp. Expired entries are dropped lazily on read. This is the swap seam
    for a Redis-backed store in production — same interface, different backing.
    """

    def __init__(self, *, clock: Clock = time.monotonic) -> None:
        self._store: dict[str, tuple[float, T]] = {}
        self._clock = clock

    async def get(self, key: str) -> T | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._clock() >= expires_at:
            # Expired: drop it so it can never be resurrected, and read as absent.
            self._store.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: T, ttl: float) -> None:
        self._store[key] = (self._clock() + ttl, value)
