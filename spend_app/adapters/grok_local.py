"""Observed Grok unified logs plus explicitly selected native usage metadata.

Historical models are session/process-generation scoped. Changed logs replay
with stable 0.3.0 IDs; only committed reads are cached. Native update/signals
reconciliation and coarse remainders live in grok_native. No token estimates,
current-model guesses, credentials or inference are used.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from spend_app.adapters.common import UsageRow, persist_rows, stable_id
from spend_app.adapters.local_common import number, optional_number, parse_iso_time, sqlite_read_only, store_read_only
from spend_app.pricing import PricingEngine


SOURCE = "grok_local"
TOOL_KEY = "grok"
MODEL_PREFIX = "supergrok:"
DEFAULT_MODEL = "grok-4.6"
TURN_MESSAGE = "shell.turn.inference_done"
MODEL_MESSAGE = "model changed"
SESSION_MESSAGE = "session created"

# Reader state per log path: byte offset already persisted plus the
# session -> model / project maps learned from lines before that offset.
_STATE: dict[str, dict] = {}
# updates.jsonl path -> (file signature, parsed rows, issues). reduce_updates is
# a pure function of one file's records, so an unchanged file reuses its parse
# instead of re-reading hundreds of megabytes of session updates every pass.
_UPDATES: dict[str, tuple] = {}


def reset_state() -> None:
    from spend_app.adapters import grok_native
    _STATE.clear()
    _UPDATES.clear()
    grok_native.reset_state()


def canonical_model(model: str | None) -> str:
    return MODEL_PREFIX + (str(model or DEFAULT_MODEL).strip().lower() or DEFAULT_MODEL)


def _fresh_state() -> dict:
    return {"offset": 0, "models": {}, "cwds": {}, "generations": {}}


def parse_log(path: Path, state: dict | None = None) -> tuple[list[UsageRow], dict]:
    """Read appended complete lines after checking generation/prefix anchors."""
    from spend_app.connection_paths import confined, file_signature
    from spend_app.adapters.grok_records import reduce_records
    signature = file_signature(path)
    if state and tuple(state.get("signature", ())) == signature:
        return [], dict(state, confirmed=state.get("accepted", 0))
    import hashlib
    stat = path.stat()
    generation = (stat.st_dev, stat.st_ino, getattr(stat, 'st_birthtime_ns', None))
    records, issues = [], []
    with confined(path).open("rb") as handle:
        def anchors(offset):
            handle.seek(0)
            head = handle.read(min(4096, offset))
            handle.seek(max(0, offset-4096))
            tail = handle.read(min(4096, offset))
            return hashlib.sha256(head+tail).hexdigest()
        offset = state.get('offset', 0) if state else 0
        append = bool(state and offset and stat.st_size >= offset and tuple(state.get('generation', ())) == generation
                      and state.get('anchors') == anchors(offset))
        offset = offset if append else 0
        handle.seek(offset)
        while True:
            line = handle.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                break
            offset += len(line)
            try:
                records.append(json.loads(line))
            except (ValueError, UnicodeError):
                issues.append("malformed_grok_metadata")
        prefix = anchors(offset)
    rows, next_state, parsed_issues = reduce_records(records, state if append else None)
    confirmed = state.get('accepted', 0) if append else 0
    next_state.update(signature=signature, accepted=confirmed + len(rows), confirmed=confirmed,
                      offset=offset, generation=generation, anchors=prefix,
                      issues=issues + parsed_issues)
    return rows, next_state


def coverage_start(path: Path, database_path: Path | None = None) -> datetime | None:
    """Earliest Grok-local coverage instant.

    The CLI truncates ``unified.jsonl``, so the first remaining line can move
    forward. Traycer skip uses the minimum of that head timestamp and any
    already-persisted ``grok_local`` ``occurred_at``, so rotation cannot
    re-open history Traycer already lost to grok_local.
    """
    file_start = None
    try:
        from spend_app.connection_paths import confined
        fd = os.open(confined(path), os.O_RDONLY)
        with os.fdopen(fd, "rb") as handle:
            first = handle.readline()
        record = json.loads(first.decode("utf-8", errors="replace"))
        if isinstance(record, dict):
            file_start = parse_iso_time(record.get("ts"))
    except (OSError, ValueError):
        file_start = None
    db_start = None
    db_file = Path(database_path) if database_path is not None else None
    if db_file is not None and db_file.is_file():
        try:
            connection = store_read_only(db_file)
            try:
                row = connection.execute(
                    "SELECT MIN(occurred_at) FROM (SELECT occurred_at FROM usage_events WHERE source=? UNION ALL SELECT occurred_at FROM unpriced_usage_events WHERE source=?)",
                    (SOURCE, SOURCE),
                ).fetchone()
            finally:
                connection.close()
            if row and row[0]:
                db_start = parse_iso_time(row[0])
        except (OSError, sqlite3.Error):
            db_start = None
    candidates = [stamp for stamp in (file_start, db_start) if stamp is not None]
    return min(candidates) if candidates else None


def ingest(*, database_path: Path, pricing: PricingEngine, log_path: Path) -> dict:
    from spend_app.connection_paths import cache_identity
    from spend_app.adapters.grok_native import read_source, reconcile
    if log_path.is_dir() or log_path.name in {"updates.jsonl", "signals.json"}:
        from spend_app.adapters import unchanged
        from spend_app.adapters.grok_native import files as native_files, remember_sessions, session_memo
        # Listing ~1k session files is the costliest step of an idle pass; list once.
        try:
            listed = list(native_files(log_path))
        except Exception:
            listed = None
        cache_key, current, replayed = unchanged.check(
            database_path, SOURCE, log_path,
            (lambda: listed) if listed is not None else (lambda: list(native_files(log_path))), pricing)
        if replayed is not None:
            return replayed
        rows, issues, files, models = read_source(log_path, listed)
        memo_key = cache_identity(database_path, log_path, "grok-native-reconcile-v1")
        memo = session_memo(memo_key)
        result = persist_rows(database_path=database_path, pricing=pricing, source=SOURCE, usage_rows=rows,
                              prepare=lambda connection, _: reconcile(connection, rows, issues, memo, models),
                              issues=issues)
        remember_sessions(memo_key, memo)
        result = {**result, "files": files, "issues": sorted(set(issues))}
        unchanged.remember(database_path, SOURCE, cache_key, current, result)
        return result
    key = cache_identity(database_path, log_path, "grok-generation-v1")
    if not log_path.is_file():
        from spend_app.adapters.common import failed_result
        return failed_result(database_path=database_path, source=SOURCE, reason="Grok usage log is missing or moved. Recheck the configured path.")
    usage_rows, next_state = parse_log(log_path, _STATE.get(key))
    issues = list(next_state.get("issues", []))
    if log_path.name == "unified.jsonl" and log_path.parent.name == "logs":
        native_rows, native_issues, native_files = _session_updates(log_path.parent.parent)
        usage_rows.extend(native_rows)
        issues.extend(native_issues)
    else:
        native_files = 0
    result = persist_rows(
        database_path=database_path, pricing=pricing, source=SOURCE,
        usage_rows=usage_rows, issues=issues,
        prepare=lambda connection, rows: reconcile(connection, rows, issues),
    )
    if not issues:
        _STATE[key] = next_state
    result["eventsAccepted"] = result.get("eventsAccepted", 0) + next_state.get("confirmed", 0)
    result["issues"] = sorted(set(issues))
    return {**result, "files": 1 + native_files, "rows": len(usage_rows)}


def _session_updates(root: Path) -> tuple[list[UsageRow], list[str], int]:
    """Per-turn totals in sessions/*/updates.jsonl. unified.jsonl is a smaller sample."""
    from spend_app.adapters.grok_native import reduce_updates
    from spend_app.connection_paths import source_files

    rows: list[UsageRow] = []
    issues: list[str] = []
    found = 0
    if not root.is_dir():
        return rows, issues, found
    from spend_app.connection_paths import LocationError
    try:
        update_files = list(source_files(root, "sessions/*/*/updates.jsonl"))
    except LocationError:
        return rows, issues, found
    from spend_app.connection_paths import confined, file_signature
    seen = set()
    for file in update_files:
        found += 1
        key = str(file)
        seen.add(key)
        try:
            signature = file_signature(file)
        except OSError:
            signature = None
        cached = _UPDATES.get(key)
        if signature is not None and cached and cached[0] == signature:
            rows.extend(cached[1])
            issues.extend(cached[2])
            continue
        records = []
        file_issues = []
        try:
            with confined(file).open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        file_issues.append("malformed_grok_native_metadata")
        except OSError:
            issues.append("grok_native_source_unavailable")
            continue
        parsed, errors = reduce_updates(records)
        file_issues.extend(errors)
        rows.extend(parsed)
        issues.extend(file_issues)
        if signature is not None:
            _UPDATES[key] = (signature, tuple(parsed), tuple(file_issues))
    for key in [key for key in _UPDATES if key not in seen]:
        _UPDATES.pop(key, None)
    return rows, issues, found
