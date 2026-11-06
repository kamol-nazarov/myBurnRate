"""Bound expensive read work without borrowing the shared HTTP worker pool."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor


class ReadCapacityExceeded(Exception):
    """The read worker pool is full or shutting down; callers may retry."""


class BoundedReadExecutor:
    def __init__(self, max_workers: int = 1):
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self._slots = threading.BoundedSemaphore(max_workers)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="spend-read"
        )
        self._state_lock = threading.Lock()
        self._closed = False

    async def run(self, function, *args, **kwargs):
        # Admission is nonblocking and there is no unbounded executor queue.
        context = contextvars.copy_context()
        with self._state_lock:
            if self._closed or not self._slots.acquire(blocking=False):
                raise ReadCapacityExceeded()
            try:
                future = self._executor.submit(context.run, function, *args, **kwargs)
            except BaseException:
                self._slots.release()
                raise
        # Cancellation of the HTTP waiter cannot release a running worker's slot.
        future.add_done_callback(lambda _future: self._slots.release())
        return await asyncio.wrap_future(future)

    def close(self):
        with self._state_lock:
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=True)
