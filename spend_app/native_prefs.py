"""Single owner of the native window's ``native-window.json`` preferences file.

Every writer (the bridge's toggles, the geometry saver, the alert state) goes
through one :class:`PreferenceStore` per file. ``update`` re-reads the file under
a lock, merges only the keys it was given and replaces the file atomically, so a
writer can never drop another writer's keys, including keys this build does not
know about.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

_STORES: dict[str, PreferenceStore] = {}
_STORES_LOCK = threading.Lock()


class PreferenceStore:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()

    def read(self) -> dict:
        if self.path is None:
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def update(self, changes: dict) -> None:
        """Merge ``changes`` into the file. A ``None`` value removes the key."""
        if self.path is None:
            return
        with self._lock:
            data = self.read()
            for key, value in changes.items():
                if value is None:
                    data.pop(key, None)
                else:
                    data[key] = value
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
                tmp.write_text(json.dumps(data), encoding="utf-8")
                tmp.replace(self.path)
            except OSError:
                pass


def store_for(path: Path | None) -> PreferenceStore:
    """The shared store for ``path`` so every writer in the process uses one lock."""
    if path is None:
        return PreferenceStore(None)
    key = os.path.normcase(os.path.abspath(path))
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = _STORES[key] = PreferenceStore(Path(path))
        return store
