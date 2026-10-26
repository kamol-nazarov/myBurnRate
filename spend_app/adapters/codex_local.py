"""Codex Desktop local usage ingest.

Files read (read-only):
  - ``%USERPROFILE%\\.codex\\sessions\\**\\*.jsonl`` — ``token_count`` telemetry
  - Live cards (activity poll, not this ingest): ``state_5.sqlite`` and
    ``thread_history_1.sqlite``, opened with SQLite ``mode=ro`` +
    ``PRAGMA query_only=ON``

Official vs observed:
  Official local Codex session logs. JSONL fields are observed from Codex.
  Known originators are ingested. ``traycer-agents`` stays out so Traycer's
  own ledger is the authority for those sessions.

Not stored:
  Prompts, responses, tool calls, or full filesystem paths. Project is the
  directory name of ``cwd`` only. Persisted fields are tokens, model, session
  id, timestamps, and ``raw_id``.

Live-session rule:
  A thread is live only while a turn is ``inprogress``/``running``,
  ``completed_at`` is NULL, the thread is not archived, and ``updated_at`` is
  within 6 hours. An idle Codex window is not live.
"""

from __future__ import annotations

import glob
from spend_app.connection_paths import adapter_files
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from spend_app.adapters.common import UsageRow, persist_rows, failed_result, public_error
from spend_app.source_health import SourceHealth, parse_jsonl_record
from spend_app.timeutil import parse_utc
from spend_app.adapters.local_common import number
from spend_app.adapters.local_common import open_text_read_only
from spend_app.db import connect, initialize, upsert_session
from spend_app.pricing import PricingEngine


SOURCE = "codex_local"
DESKTOP_ORIGINATOR = "Codex Desktop"
from spend_app.adapters.codex_records import ORIGINATORS, PARSER_VERSION
SUPPORTED_ORIGINATORS = set(ORIGINATORS)
_FILE_COUNTS = {}
_FILE_SIGNATURES: dict[str, tuple[int, int]] = {}


def reset_file_cache() -> None:
    from spend_app.adapters import file_cache

    _FILE_SIGNATURES.clear()
    _FILE_COUNTS.clear()
    file_cache.forget(SOURCE)


def _parse_time(value: str) -> datetime | None:

    return parse_utc(value)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ParsedEvent:
    session_id: str
    project: str | None
    model_key: str
    occurred_at: datetime
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int | None
    raw_id: str
    observation: object = None


def parse_file(path: Path) -> tuple[dict, list[ParsedEvent]]:
    session, events, _health = parse_file_with_health(path)
    return session, events


def parse_file_with_health(path: Path) -> tuple[dict, list[ParsedEvent], SourceHealth]:
    from spend_app.adapters.codex_records import reduce_records
    health = SourceHealth()
    records = []
    with open_text_read_only(path) as handle:
        for index, line in enumerate(handle, 1):
            record, _ = parse_jsonl_record(line, line_number=index, location="codex", health=health)
            if record is not None:
                records.append(record)
    session, observations = reduce_records(records, health, path.stem)
    events = [ParsedEvent(session_id=o.row.session_id, project=o.row.project, model_key=o.row.model_key,
                          occurred_at=o.row.occurred_at, input_tokens=o.row.input_tokens, cached_input_tokens=o.row.cached_input_tokens,
                          cache_write_tokens=o.row.cache_write_tokens, output_tokens=o.row.output_tokens,
                          reasoning_tokens=o.row.reasoning_tokens, raw_id=o.row.raw_id, observation=o) for o in observations]
    return session, events, health


def ingest(
    *,
    database_path: Path,
    pricing: PricingEngine,
    session_glob: str,
) -> dict:
    try:
        return _ingest(database_path=database_path, pricing=pricing, session_glob=session_glob)
    except Exception as exc:
        # A crash must be visible per source (A03): record a failed run
        # instead of letting the exception escape without a health record.
        return failed_result(
            database_path=database_path, source=SOURCE, reason=public_error(exc)
        )


def _ingest(*, database_path: Path, pricing: PricingEngine, session_glob: str) -> dict:
    from spend_app.adapters import file_cache
    from spend_app.adapters.event_identity import reconcile
    from spend_app.connection_paths import cache_identity, file_signature, stat_path

    initialize(database_path)
    file_cache.warm(database_path, SOURCE, _FILE_SIGNATURES, _FILE_COUNTS)
    parsed_files = 0
    parsed_events = 0
    confirmed_events = 0
    issues = []
    skipped_files = 0
    skipped_originator = 0
    quarantined = 0
    originator_files: dict[str, int] = {}
    batch: dict[str, list] = {"usage_rows": [], "observations": [], "session_rows": [], "pending": [], "empty": []}
    results: list[dict] = []

    def flush() -> None:
        usage_rows, observations, session_rows = batch["usage_rows"], batch["observations"], batch["session_rows"]
        pending, empty = batch["pending"], batch["empty"]
        for name in batch:
            batch[name] = []

        def cacheable():
            # A file with unusable records is cached only when the pass had no
            # issue at all; reconcile's issues are known by the time this runs.
            return [(key, signature, count, 0) for key, signature, count, file_clean in pending if not issues or file_clean]

        def finalize(connection):
            for session in session_rows:
                upsert_session(
                    connection,
                    session_id=session["session_id"],
                    tool_key="codex",
                    project=session["project"],
                    started_at=session["started_at"],
                    ended_at=session["ended_at"],
                    model_key=session["model_key"],
                )
            file_cache.save(connection, SOURCE, [*cacheable(), *((key, signature, 0, 0) for key, signature in empty)])

        results.append(persist_rows(
            database_path=database_path, pricing=pricing, source=SOURCE, usage_rows=usage_rows,
            prepare=(lambda connection, rows: reconcile(connection, observations, issues)) if observations else None,
            finalize=finalize, issues=issues))
        for cache_key, signature, count, _unusable in cacheable():
            _FILE_SIGNATURES[cache_key] = signature
            _FILE_COUNTS[cache_key] = count

    for file_name in sorted(adapter_files(session_glob)):
        path = Path(file_name)
        try:
            stat = stat_path(path)
        except OSError:
            continue
        cache_key = cache_identity(database_path, path, PARSER_VERSION)
        cached = _FILE_SIGNATURES.get(cache_key)
        try:
            signature = cached if file_cache.unchanged(cached, stat) else file_signature(path, stat)
        except OSError:
            issues.append("codex_file_unavailable")
            continue
        if cached == signature:
            skipped_files += 1
            confirmed_events += _FILE_COUNTS.get(cache_key, 0)
            continue
        try:
            session, events, health = parse_file_with_health(path)
        except OSError:
            issues.append("codex_file_unavailable")
            continue
        quarantined += health.quarantined + health.skipped
        if health.partial:
            issues.append("codex_partial_source_metadata")
        if not session:
            issues.append("codex_session_metadata_missing")
            _FILE_SIGNATURES[cache_key] = signature
            batch["empty"].append((cache_key, signature))
            continue
        if not session.get("supported_origin", session.get("originator") in SUPPORTED_ORIGINATORS):
            skipped_originator += 1
            issues.append("codex_unsupported_originator")
            key = "unsupported_originator"
            originator_files[key] = originator_files.get(key, 0) + 1
            _FILE_SIGNATURES[cache_key] = signature
            batch["empty"].append((cache_key, signature))
            continue
        parsed_files += 1
        parsed_events += len(events)
        latest_time: datetime | None = None
        latest_model: str | None = None
        for parsed in events:
            if parsed.observation is not None:
                batch["observations"].append(parsed.observation)
            batch["usage_rows"].append(
                UsageRow(
                    source=SOURCE,
                    tool_key="codex",
                    model_key=parsed.model_key,
                    occurred_at=parsed.occurred_at,
                    session_id=parsed.session_id,
                    project=parsed.project,
                    input_tokens=parsed.input_tokens,
                    cached_input_tokens=parsed.cached_input_tokens,
                    cache_write_tokens=parsed.cache_write_tokens,
                    cache_write_1h_tokens=0,
                    output_tokens=parsed.output_tokens,
                    reasoning_tokens=parsed.reasoning_tokens,
                    cost_usd=None,
                    raw_id=parsed.raw_id,
                    telemetry_complete=parsed.observation.row.telemetry_complete if parsed.observation else True,
                    unclassified_tokens=parsed.observation.row.unclassified_tokens if parsed.observation else 0,
                )
            )
            latest_time = parsed.occurred_at
            latest_model = parsed.model_key
        if latest_time and latest_model:
            batch["session_rows"].append(
                {
                    "session_id": session["id"],
                    "project": session.get("project"),
                    "started_at": _iso(session["started_at"]),
                    "ended_at": _iso(latest_time),
                    "model_key": latest_model,
                }
            )
        batch["pending"].append((cache_key, signature, len(events), not (health.partial or health.quarantined or health.skipped)))
        if len(batch["usage_rows"]) >= file_cache.BATCH_EVENTS:
            flush()
    # Always once more: records this pass's run, sessions and remaining files.
    flush()
    result = file_cache.merge_results(results)
    result["files"] = parsed_files
    result["filesSkippedUnchanged"] = skipped_files
    result["filesSkippedOriginator"] = skipped_originator
    result["filesByOriginator"] = originator_files
    result["unsupportedOriginators"] = sorted(
        key for key in originator_files if key not in SUPPORTED_ORIGINATORS
    )
    result["quarantined"] = result.get("quarantined", 0) + quarantined
    result["eventsSeen"] = parsed_events + confirmed_events
    result["eventsAccepted"] = result.get("eventsAccepted", 0) + confirmed_events
    result["issues"] = sorted(set(issues))
    return result
