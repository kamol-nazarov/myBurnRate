"""Skip re-reconciling a local store whose input files have not changed.

Several local sources re-read and re-reconcile their whole store on every pass.
For an idle harness that is many seconds of identical work, and the local pass
is serial, so it delays every other source and every figure derived from them.
When the input files, the pricing table and the approved location all match
the last clean pass, the pass records a run with that pass's outcome instead.

A full pass still runs at least every ``FORCE_SECONDS``, so a change the
fingerprint cannot see is picked up within minutes rather than never.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from pathlib import Path

from spend_app.db import connect, initialize
from spend_app.ingest import IngestRun

FORCE_SECONDS = 300
_LOCK = threading.Lock()
# key -> (signature, recorded monotonic time, result, run error text)
_LAST: dict[tuple, tuple[tuple, float, dict, str | None]] = {}


def reset_state() -> None:
    with _LOCK:
        _LAST.clear()


def _fingerprint(path: Path) -> tuple:
    # Same confined, read-only file signature the transcript adapters use:
    # size, mtime, identity and a hash of the first and last 4 KiB.
    from spend_app.connection_paths import file_signature

    try:
        return (str(path), *file_signature(path))
    except Exception as exc:  # unreadable or outside the approved location
        return (str(path), "unreadable", type(exc).__name__)


def signature(files: Iterable[Path], pricing) -> tuple:
    """Fingerprint of every input file (and its SQLite WAL) plus the pricing identity."""
    entries = []
    for file in files:
        entries.append(_fingerprint(Path(file)))
        wal = Path(f"{file}-wal")
        if wal.exists():
            entries.append(_fingerprint(wal))
    return (tuple(sorted(entries)), id(pricing), len(getattr(pricing, "prices", ()) or ()))


def key(database_path: Path, location: Path, source: str) -> tuple:
    from spend_app.connection_paths import cache_identity

    return (source, cache_identity(database_path, location, "unchanged-v1"))


def check(database_path: Path, source: str, location: Path, list_files, pricing):
    """(cache key, signature, replayed result). Any failure steps aside for a full pass.

    The full pass reports a missing or unreadable store properly; the guard
    must never be the thing that breaks an ingest.
    """
    try:
        cache_key = key(database_path, Path(location), source)
        current = signature(list_files(), pricing)
    except Exception:
        return None, None, None
    return cache_key, current, replay(database_path, source, cache_key, current)


def replay(database_path: Path, source: str, cache_key: tuple, current: tuple) -> dict | None:
    """Record an unchanged pass with the previous outcome, or None when a full pass is needed."""
    with _LOCK:
        cached = _LAST.get(cache_key)
    if cached is None:
        return None
    previous, recorded_at, result, error = cached
    if previous != current or time.monotonic() - recorded_at >= FORCE_SECONDS:
        return None
    initialize(database_path)
    with connect(database_path) as connection:
        run = IngestRun.start(connection, source)
        run.finish(status=result["status"], error=error)
    return {**result, "eventsWritten": 0, "unpricedEventsWritten": 0, "unchanged": True}


def remember(database_path: Path, source: str, cache_key: tuple, current: tuple, result: dict) -> None:
    """Keep a clean pass's outcome so the next unchanged pass can repeat it."""
    if cache_key is None or current is None:
        return
    if not isinstance(result, dict) or result.get("status") not in {"success", "partial"}:
        with _LOCK:
            _LAST.pop(cache_key, None)
        return
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT error FROM ingest_runs WHERE source=? AND status=? ORDER BY id DESC LIMIT 1",
            (source, result["status"]),
        ).fetchone()
    with _LOCK:
        _LAST[cache_key] = (current, time.monotonic(), dict(result), row[0] if row else None)
