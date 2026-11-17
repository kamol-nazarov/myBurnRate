"""Let a second launch of BURNRATE ask the running one to open its full window.

The running process owns which window is up (full or tab) and remembers it, so
a second launch must not poke Win32 windows directly: it signals a named event
and the running process moves its own surface controller. The event lives in
the per-session ``Local\\`` namespace, so it never crosses user sessions.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable

LOGGER = logging.getLogger(__name__)

EVENT_NAME = "Local\\BURNRATE-desktop-activate"
_EVENT_MODIFY_STATE = 0x0002
_WAIT_OBJECT_0 = 0


class Win32Events:
    """The few kernel32 calls, behind one seam so tests can fake them."""

    def __init__(self) -> None:
        import ctypes

        self._k32 = ctypes.windll.kernel32
        self._k32.CreateEventW.restype = ctypes.c_void_p
        self._k32.OpenEventW.restype = ctypes.c_void_p
        self._k32.SetEvent.argtypes = [ctypes.c_void_p]
        self._k32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]

    def create(self, name: str):
        return self._k32.CreateEventW(None, False, False, name) or None

    def open(self, name: str):
        return self._k32.OpenEventW(_EVENT_MODIFY_STATE, False, name) or None

    def signal(self, handle) -> bool:
        return bool(self._k32.SetEvent(handle))

    def wait(self, handle, milliseconds: int) -> bool:
        return self._k32.WaitForSingleObject(handle, milliseconds) == _WAIT_OBJECT_0

    def close(self, handle) -> None:
        self._k32.CloseHandle(handle)


def signal_running_instance(name: str = EVENT_NAME, events=None) -> bool:
    """Ask a running BURNRATE to open its full window. False when none listens."""
    try:
        if events is None:
            if sys.platform != "win32":
                return False
            events = Win32Events()
        handle = events.open(name)
        if not handle:
            return False
        try:
            return events.signal(handle)
        finally:
            events.close(handle)
    except Exception:  # noqa: BLE001 -- no listener, an old build, or a locked-down session
        LOGGER.debug("Could not signal a running instance", exc_info=True)
        return False


class ActivationListener:
    """Runs ``on_activate`` each time another launch signals the named event."""

    POLL_MS = 400

    def __init__(self, on_activate: Callable[[], None], *, name: str = EVENT_NAME, events=None) -> None:
        self._on_activate = on_activate
        self._name = name
        self._events = events
        self._handle = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        try:
            if self._events is None:
                if sys.platform != "win32":
                    return False
                self._events = Win32Events()
            self._handle = self._events.create(self._name)
        except Exception:  # noqa: BLE001
            LOGGER.debug("Activation listener unavailable", exc_info=True)
            return False
        if not self._handle:
            return False
        self._thread = threading.Thread(target=self._run, name="burnrate-activation", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(2.0)
        handle, self._handle = self._handle, None
        if handle:
            try:
                self._events.close(handle)
            except Exception:  # noqa: BLE001
                LOGGER.debug("Activation handle close skipped", exc_info=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                signalled = self._events.wait(self._handle, self.POLL_MS)
            except Exception:  # noqa: BLE001
                LOGGER.debug("Activation wait failed", exc_info=True)
                return
            if signalled and not self._stop.is_set():
                try:
                    self._on_activate()
                except Exception:  # noqa: BLE001
                    LOGGER.debug("Activation handler failed", exc_info=True)
