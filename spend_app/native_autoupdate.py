"""Background update checks for installed copies of the native window.

Privacy boundary: only an *installer* copy ever checks on its own. Portable and
source copies, ``--demo`` and the smoke test never contact the update feed
unless the user presses "Check for updates". Nothing here installs anything.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from spend_app.native_prefs import PreferenceStore

LOGGER = logging.getLogger(__name__)

LAST_NOTIFIED_KEY = "lastNotifiedUpdate"
FIRST_DELAY_SECONDS = 60.0
INTERVAL_SECONDS = 24 * 60 * 60.0


class UpdateChecker:
    def __init__(
        self,
        *,
        status: Callable[[], dict],
        check: Callable[[], dict],
        store: PreferenceStore,
        push: Callable[[dict], None],
        notify: Callable[[str], None],
        first_delay: float = FIRST_DELAY_SECONDS,
        interval: float = INTERVAL_SECONDS,
    ) -> None:
        self._status = status
        self._check = check
        self._store = store
        self._push = push
        self._notify = notify
        self._first_delay = first_delay
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def eligible(self) -> bool:
        """True only for an installer copy; anything else is never checked."""
        try:
            return self._status().get("kind") == "installer"
        except Exception:  # noqa: BLE001 -- an unreadable status means "do not check"
            return False

    def start(self) -> bool:
        if self._thread is not None or not self.eligible():
            return False
        self._thread = threading.Thread(
            target=self._run, name="burnrate-update-check", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> dict | None:
        """One check. Returns the result when a newer version is available.

        Never raises. A version is announced with one notification, ever: the
        last announced version is stored so a restart does not repeat it.
        """
        try:
            if not self.eligible():
                return None
            result = self._check()
            if not isinstance(result, dict) or not result.get("ok"):
                return None
            version = str(result.get("version") or "")
            if not result.get("available") or not version:
                return None
            self._push(result)
            if self._store.read().get(LAST_NOTIFIED_KEY) != version:
                # Persist first: a crash drops one notification, never repeats it.
                self._store.update({LAST_NOTIFIED_KEY: version})
                self._notify(version)
            return result
        except Exception:
            LOGGER.debug("Automatic update check failed", exc_info=True)
            return None

    def _run(self) -> None:
        if self._stop.wait(self._first_delay):
            return
        while True:
            self.run_once()
            if self._stop.wait(self._interval):
                return
