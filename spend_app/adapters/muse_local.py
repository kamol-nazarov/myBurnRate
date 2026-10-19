"""Muse Code local session-log ingest.

Files read (read-only):
  - ``~/.local/share/muse/sessions/YYYY/MM/DD/*.jsonl`` — append-only session
    event logs. Files are opened ``O_RDONLY``; only records carrying an
    explicit usage object are kept (see ``muse_records``).

Not stored:
  Prompts, responses, tool input/output, or attachments. Records without a
  usage object are other event kinds and are ignored without error.

Official vs observed:
  Observed local Muse Code session logs. The record schema is undocumented;
  a sample that carries no recognizable usage records does not verify. This
  module never reads ``~/.config/muse/auth.json``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from spend_app.adapters.common import UsageRow, failed_result, persist_rows, public_error
from spend_app.connection_paths import adapter_files
from spend_app.db import initialize, upsert_session
from spend_app.pricing import PricingEngine
from spend_app.source_health import parse_jsonl_record

from spend_app.adapters.local_common import open_text_read_only
from spend_app.adapters.muse_records import PARSER_VERSION, reduce_records

SOURCE = "muse_local"
_FILE_COUNTS = {}
_FILE_SIGNATURES: dict[str, tuple[int, int]] = {}


def reset_file_cache() -> None:
    _FILE_SIGNATURES.clear()
    _FILE_COUNTS.clear()


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_file_with_health(path: Path) -> tuple[dict, list[UsageRow], object]:
    from spend_app.source_health import SourceHealth

    health = SourceHealth()
    records = []
    with open_text_read_only(path) as handle:
        for index, line in enumerate(handle, 1):
            record, _ = parse_jsonl_record(line, line_number=index, location="muse", health=health)
            if record is not None:
                records.append(record)
    parsed_lines = health.parsed
    rows, session = reduce_records(records, file_key=path.stem, health=health)
    # Accepted-observation accounting is separate from JSON line counts.
    health.parsed = parsed_lines
    return session, rows, health


def ingest(*, database_path: Path, pricing: PricingEngine, session_glob: str) -> dict:
    try:
        return _ingest(database_path=database_path, pricing=pricing, session_glob=session_glob)
    except Exception as exc:
        return failed_result(
            database_path=database_path, source=SOURCE, reason=public_error(exc)
        )


def _ingest(*, database_path: Path, pricing: PricingEngine, session_glob: str) -> dict:
    initialize(database_path)
    parsed_files = 0
    parsed_events = 0
    confirmed_events = 0
    issues = []
    duplicates_removed = 0
    skipped_files = 0
    quarantined = 0
    usage_rows: list[UsageRow] = []
    session_rows: list[dict] = []
    pending_signatures: list[tuple[str, tuple[int, int], int, bool]] = []
    for file_name in sorted(adapter_files(session_glob)):
        path = Path(file_name)
        try:
            from spend_app.connection_paths import stat_path

            stat = stat_path(path)
        except OSError:
            continue
        from spend_app.connection_paths import cache_identity, file_signature

        cache_key = cache_identity(database_path, path, PARSER_VERSION)
        try:
            signature = file_signature(path, stat)
        except OSError:
            issues.append("muse_file_unavailable")
            continue
        if _FILE_SIGNATURES.get(cache_key) == signature:
            skipped_files += 1
            confirmed_events += _FILE_COUNTS.get(cache_key, 0)
            continue
        try:
            session, events, health = parse_file_with_health(path)
        except OSError:
            issues.append("muse_file_unavailable")
            continue
        raw_usage_lines = health.parsed
        if health.partial:
            issues.append("muse_partial_source_metadata")
        quarantined += health.quarantined + health.skipped
        if not session:
            _FILE_SIGNATURES[cache_key] = signature
            continue
        parsed_files += 1
        duplicates_removed += max(0, raw_usage_lines - len(events))
        parsed_events += len(events)
        latest_time: datetime | None = None
        latest_model: str | None = None
        for row in events:
            usage_rows.append(row)
            latest_time = row.occurred_at
            latest_model = row.model_key
        if latest_time and latest_model:
            session_rows.append(
                {
                    "session_id": session["id"],
                    "project": session["project"],
                    "started_at": _iso(session["started_at"]),
                    "ended_at": _iso(latest_time),
                    "model_key": latest_model,
                }
            )
        pending_signatures.append((cache_key, signature, len(events), not (health.partial or health.quarantined or health.skipped)))

    def finalize(connection):
        for session in session_rows:
            upsert_session(
                connection,
                session_id=session["session_id"],
                tool_key="muse",
                project=session["project"],
                started_at=session["started_at"],
                ended_at=session["ended_at"],
                model_key=session["model_key"],
            )

    result = persist_rows(
        database_path=database_path,
        pricing=pricing,
        source=SOURCE,
        usage_rows=usage_rows,
        finalize=finalize,
        issues=issues,
    )
    for cache_key, signature, count, file_clean in pending_signatures:
        if not issues or file_clean:
            _FILE_SIGNATURES[cache_key] = signature
            _FILE_COUNTS[cache_key] = count
    result["files"] = parsed_files
    result["filesSkippedUnchanged"] = skipped_files
    result["eventsSeen"] = parsed_events + confirmed_events
    result["eventsAccepted"] = result.get("eventsAccepted", 0) + confirmed_events
    result["issues"] = sorted(set(issues))
    result["duplicatesRemoved"] = duplicates_removed
    result["quarantined"] = result.get("quarantined", 0) + quarantined
    return result
