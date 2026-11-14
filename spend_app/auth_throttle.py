"""Small in-memory throttle for the hub's account endpoints.

A self-hosted hub is a single process, so a process-local window is enough to
turn unlimited online guessing into a bounded, observable cost. A restart
clears the window; the KDF still makes each attempt expensive.
"""
from __future__ import annotations

import threading
import time

WINDOW_SECONDS = 15 * 60
MAX_FAILURES = 5
LOCKOUT_SECONDS = 15 * 60


class Throttle:
    def __init__(
        self,
        *,
        window: float = WINDOW_SECONDS,
        max_failures: int = MAX_FAILURES,
        lockout: float = LOCKOUT_SECONDS,
    ) -> None:
        self.window = float(window)
        self.max_failures = int(max_failures)
        self.lockout = float(lockout)
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str, *, now: float | None = None) -> int:
        """Seconds until ``key`` may try again, or 0 when it is not locked."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            until = self._locked_until.get(key)
            if until is None:
                return 0
            if until > moment:
                return max(1, int(until - moment) + 1)
            self._locked_until.pop(key, None)
            self._failures.pop(key, None)
            return 0

    def fail(self, key: str, *, now: float | None = None) -> int:
        """Record a failure and return the retry-after seconds (0 when open)."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            if self._locked_until.get(key, 0) > moment:
                return max(1, int(self._locked_until[key] - moment) + 1)
            seen = [stamp for stamp in self._failures.get(key, []) if moment - stamp <= self.window]
            seen.append(moment)
            if len(seen) >= self.max_failures:
                self._failures.pop(key, None)
                self._locked_until[key] = moment + self.lockout
                return int(self.lockout) + 1
            self._failures[key] = seen
            return 0

    def succeed(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()
            self._locked_until.clear()


_DEFAULT = Throttle()


def retry_after(key: str) -> int:
    return _DEFAULT.retry_after(key)


def fail(key: str) -> int:
    return _DEFAULT.fail(key)


def succeed(key: str) -> None:
    _DEFAULT.succeed(key)


def reset() -> None:
    _DEFAULT.reset()
