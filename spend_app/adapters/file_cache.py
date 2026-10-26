"""Which source files a collector already read, remembered across restarts.

Transcript collectors skip a file whose signature (size, modified time, file
id, creation time and a head/tail hash) matches the one recorded when its rows
were saved. That record used to live only in memory, so every restart reread
every transcript (passes of 12 to 20 minutes) while the HUD waited. It is now
also kept in ``ingest_file_cache``, written in the same transaction as the
rows it describes, and loaded once per process.

``unchanged`` lets a pass skip a file from its directory entry alone: when
size, modified time, file id and creation time all match, the file is not
opened to hash its head and tail.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from spend_app.db import connect

TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ingest_file_cache (
    source TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    signature TEXT NOT NULL,
    events INTEGER NOT NULL DEFAULT 0,
    quarantined INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, cache_key)
) WITHOUT ROWID;
"""
# A pass saves its rows in batches of about this many events, so a first read
# of a large history never holds the database's write lock for minutes.
BATCH_EVENTS = 5000

_LOADED: set[tuple[str, str]] = set()
_DATABASES: dict[str, set[str]] = {}
_LOCK = threading.Lock()


def _text(value) -> str:
    return json.dumps(list(value), separators=(",", ":"))


def warm(database_path, source: str, signatures: dict, counts: dict, quarantined: dict | None = None) -> None:
    """Load the persisted record for ``source`` into the collector's dicts, once per process."""
    from spend_app.connection_paths import _database_identity

    marker = (_database_identity(database_path), source)
    with _LOCK:
        if marker in _LOADED:
            return
        with connect(Path(database_path)) as connection:
            connection.executescript(TABLE_SQL)
            rows = connection.execute(
                "SELECT cache_key, signature, events, quarantined FROM ingest_file_cache WHERE source=?", (source,)
            ).fetchall()
        for row in rows:
            try:
                key = tuple(json.loads(row["cache_key"]))
                signature = tuple(json.loads(row["signature"]))
            except (ValueError, TypeError):
                continue
            if key[0] != marker[0]:
                continue  # recorded for this database under another path spelling
            signatures.setdefault(key, signature)
            counts.setdefault(key, int(row["events"] or 0))
            if quarantined is not None and row["quarantined"]:
                quarantined.setdefault(key, int(row["quarantined"]))
        _LOADED.add(marker)
        _DATABASES.setdefault(source, set()).add(str(database_path))


def unchanged(cached, stat) -> bool:
    """True when the directory entry alone proves the file is the one already read."""
    return (
        cached is not None
        and len(cached) >= 4
        and cached[0] == stat.st_size
        and cached[1] == stat.st_mtime_ns
        and cached[2] == getattr(stat, "st_ino", 0)
        and cached[3] == getattr(stat, "st_ctime_ns", 0)
    )


def save(connection, source: str, entries) -> None:
    """Record read files inside the transaction that saved their rows."""
    rows = [(source, _text(key), _text(signature), int(events or 0), int(unusable or 0))
            for key, signature, events, unusable in entries]
    if rows:
        connection.executemany(
            "INSERT OR REPLACE INTO ingest_file_cache(source, cache_key, signature, events, quarantined) "
            "VALUES(?,?,?,?,?)", rows)


def forget(source: str) -> None:
    """Drop the persisted record for ``source`` (a collector's cache reset)."""
    with _LOCK:
        _LOADED.difference_update({marker for marker in _LOADED if marker[1] == source})
        databases = _DATABASES.pop(source, set())
    for database in databases:
        try:
            with connect(Path(database)) as connection:
                connection.execute("DELETE FROM ingest_file_cache WHERE source=?", (source,))
        except Exception:
            continue


def merge_results(results: list[dict]) -> dict:
    """Combine the results of a pass saved in several batches."""
    if len(results) == 1:
        return dict(results[0])
    merged: dict = {}
    order = {"success": 0, "partial": 1, "failed": 2}
    for result in results:
        for key, value in result.items():
            if key == "status":
                current = merged.get("status", "success")
                merged["status"] = value if order.get(value, 0) > order.get(current, 0) else current
            elif isinstance(value, bool) or not isinstance(value, (int, float, list)):
                merged.setdefault(key, value)
            elif isinstance(value, list):
                merged[key] = sorted(set(merged.get(key, [])) | set(value))
            else:
                merged[key] = merged.get(key, 0) + value
    return merged
