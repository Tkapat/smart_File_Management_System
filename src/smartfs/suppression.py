"""Thread-safe suppression of filesystem events caused by SmartFS itself."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager


def normalize_path(path: str | os.PathLike[str]) -> str:
    """Return a stable absolute path key for suppression lookups."""
    return os.path.abspath(os.path.expanduser(os.fspath(path)))


class SelfWriteSuppressor:
    """Track paths whose metadata writes should be ignored by watchers."""

    def __init__(self, default_ttl: float = 5.0) -> None:
        if default_ttl <= 0:
            raise ValueError("default_ttl must be positive")
        self._default_ttl = default_ttl
        self._lock = threading.Lock()
        self._expiry: dict[str, float] = {}

    def mark(self, path: str | os.PathLike[str], ttl: float | None = None) -> None:
        """Suppress watcher events for a path until the TTL expires."""
        effective = self._default_ttl if ttl is None else ttl
        if effective <= 0:
            raise ValueError("ttl must be positive")
        key = normalize_path(path)
        with self._lock:
            self._expiry[key] = time.monotonic() + effective

    def is_suppressed(self, path: str | os.PathLike[str]) -> bool:
        """Return True when the path has an unexpired suppression entry."""
        key = normalize_path(path)
        now = time.monotonic()
        with self._lock:
            expires = self._expiry.get(key)
            if expires is None:
                return False
            if expires <= now:
                del self._expiry[key]
                return False
            return True

    def clear(self, path: str | os.PathLike[str] | None = None) -> None:
        """Clear one path or the entire suppression set."""
        with self._lock:
            if path is None:
                self._expiry.clear()
            else:
                self._expiry.pop(normalize_path(path), None)

    def purge_expired(self) -> int:
        """Remove expired entries and return the number removed."""
        now = time.monotonic()
        with self._lock:
            expired = [key for key, expiry in self._expiry.items() if expiry <= now]
            for key in expired:
                del self._expiry[key]
            return len(expired)

    @contextmanager
    def suppress(self, path: str | os.PathLike[str], ttl: float | None = None) -> Iterator[None]:
        """Mark a path suppressed for the duration of a block."""
        self.mark(path, ttl)
        yield


GLOBAL_SUPPRESSOR = SelfWriteSuppressor()
