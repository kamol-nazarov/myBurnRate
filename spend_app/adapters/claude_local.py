"""Claude Code local transcript ingest.

Files read (read-only):
  - ``%USERPROFILE%\\.claude\\projects\\**\\*.jsonl`` — assistant ``usage``
    envelopes. JSONL is opened ``O_RDONLY``.
  - Live cards (activity poll, not this ingest): tail (~1 MiB) of those same
    project JSONL files.

Official vs observed:
  Official local Claude Code transcripts. Usage fields (including 5m/1h cache
  writes) are taken from the ``message.usage`` object. This module does not
  implement Claude OAuth refresh or write ``~/.claude/.credentials.json``.

Not stored:
  Prompts, responses, tool input/output, or attachments. Full JSONL lines are
  parsed in memory for usage extraction only. Project is the directory name of
  ``cwd`` (else the parent folder name).

Live-session rule:
  Live only while the trailing turn is non-terminal. A user event without a
  later assistant ``stop_reason`` in ``{end_turn, stop_sequence, max_tokens,
  refusal}`` counts as in-flight. An idle editor with no in-progress turn is
  not live. Message text is not stored.
"""

from __future__ import annotations

from collections import OrderedDict
import glob
import hashlib
import os
from spend_app.connection_paths import APPROVED_ROOT, adapter_files, confined, readonly_fd
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from spend_app.adapters.common import UsageRow, persist_rows, failed_result, public_error, stable_id
from spend_app.source_health import SourceHealth, parse_jsonl_record
from spend_app.timeutil import parse_utc
from spend_app.adapters.local_common import open_text_read_only
from spend_app.db import connect, initialize, upsert_session
from spend_app.pricing import PricingEngine


SOURCE = "claude_local"
from spend_app.adapters.claude_records import PARSER_VERSION
_FILE_COUNTS = {}
_FILE_SIGNATURES: dict[str, tuple[int, int]] = {}
# Quarantined-record count of a cached file whose content has permanently
# unusable records (older revisions, conflicting cache durations). Re-reading
# identical bytes cannot change that, so the file is cached like a clean one
# and its count is replayed so the pass still reports it as partial.
_FILE_QUARANTINED: dict = {}
# Bounded in-memory tail state for large active append-only transcripts (>= 256 KiB).
# Lets a 15-second pass read and reconcile only newly appended turns instead of
# re-parsing 100–300 MB files from byte 0.
_TAIL_MIN_BYTES = 256 * 1024
_TAIL_MAX_ENTRIES = 64
_TAIL_STATE: OrderedDict[tuple, dict] = OrderedDict()


def reset_file_cache() -> None:
    from spend_app.adapters import file_cache

    _FILE_SIGNATURES.clear()
    _FILE_COUNTS.clear()
    _FILE_QUARANTINED.clear()
    _TAIL_STATE.clear()
    file_cache.forget(SOURCE)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _nonneg_int(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _optional_nonneg_int(value: object) -> int | None:
    """Missing detail stays NULL; only a reported number becomes a count."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ParsedEvent:
    session_id: str
    project: str | None
    model_key: str
    occurred_at: datetime
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    cache_write_1h_tokens: int
    output_tokens: int
    reasoning_tokens: int | None
    raw_id: str
    observation: object = None


def parse_file(path: Path) -> tuple[dict, list[ParsedEvent]]:
    session, events, _health = parse_file_with_health(path)
    return session, events


def _events_from_observations(observations) -> list[ParsedEvent]:
    return [
        ParsedEvent(
            session_id=o.row.session_id,
            project=o.row.project,
            model_key=o.row.model_key,
            occurred_at=o.row.occurred_at,
            input_tokens=o.row.input_tokens,
            cached_input_tokens=o.row.cached_input_tokens,
            cache_write_tokens=o.row.cache_write_tokens,
            cache_write_1h_tokens=o.row.cache_write_1h_tokens,
            output_tokens=o.row.output_tokens,
            reasoning_tokens=o.row.reasoning_tokens,
            raw_id=o.row.raw_id,
            observation=o,
        )
        for o in observations
    ]


def parse_file_with_health(path: Path) -> tuple[dict, list[ParsedEvent], SourceHealth]:
    from spend_app.adapters.claude_records import reduce_records
    health = SourceHealth()
    records = []
    with open_text_read_only(path) as handle:
        for index, line in enumerate(handle, 1):
            record, _ = parse_jsonl_record(line, line_number=index, location="claude", health=health)
            if record is not None:
                records.append(record)
    parsed_lines = health.parsed
    session, observations = reduce_records(records, health, path.stem)
    # The reducer's accepted-observation accounting is separate from JSON lines.
    health.parsed = parsed_lines
    return session, _events_from_observations(observations), health


_DEFAULT_PARSE_FILE_WITH_HEALTH = parse_file_with_health


def _parse_for_ingest(path: Path, stat, tail: dict | None) -> tuple[dict, list[ParsedEvent], int, int, bool, int, dict | None]:
    """Parse a transcript, reading only newly appended bytes when safe.

    Returns ``(session, changed_events, total_events, total_unusable, is_partial, raw_usage_lines, next_tail)``.
    """
    from spend_app.adapters.claude_records import reduce_records_into

    # Respect tests that monkeypatch parse_file_with_health, and keep small files
    # on the simple path so we never retain tail state for tiny transcripts.
    if parse_file_with_health is not _DEFAULT_PARSE_FILE_WITH_HEALTH or (
        tail is None and stat.st_size < _TAIL_MIN_BYTES
    ):
        session, events, health = parse_file_with_health(path)
        unusable = health.quarantined + health.skipped
        return session, events, len(events), unusable, health.partial, health.parsed, None

    ino = getattr(stat, "st_ino", 0)
    ctime_ns = getattr(stat, "st_ctime_ns", 0)

    # Incremental tail fast path: file grew in place with matching identity and prefix.
    if (
        tail is not None
        and stat.st_size > tail["offset"]
        and ino == tail["ino"]
        and ctime_ns == tail["ctime_ns"]
    ):
        try:
            with os.fdopen(readonly_fd(confined(path)), "rb") as raw_handle:
                first = raw_handle.read(4096)
                if hashlib.blake2b(first, digest_size=16).hexdigest() == tail["prefix_hash"]:
                    raw_handle.seek(tail["offset"])
                    delta_health = SourceHealth()
                    new_records = []
                    offset = tail["offset"]
                    line_num = tail["line_number"]
                    last_ended_nl = True
                    saw_bytes = False
                    for raw_line in raw_handle:
                        saw_bytes = True
                        offset += len(raw_line)
                        last_ended_nl = raw_line.endswith(b"\n")
                        line_num += 1
                        record, _ = parse_jsonl_record(
                            raw_line.decode("utf-8", errors="replace"),
                            line_number=line_num,
                            location="claude",
                            health=delta_health,
                        )
                        if record is not None:
                            new_records.append(record)
                    if saw_bytes and last_ended_nl:
                        delta_parsed_lines = delta_health.parsed
                        observations = dict(tail["observations"])
                        known_scope = dict(tail["known_scope"])
                        session = dict(tail["session"])
                        touched = reduce_records_into(
                            new_records,
                            delta_health,
                            path.stem,
                            observations=observations,
                            known_scope=known_scope,
                            session=session,
                        )
                        changed_events = _events_from_observations(observations[k] for k in touched)
                        latest_time = tail["latest_time"]
                        for ev in changed_events:
                            if latest_time is None or ev.occurred_at > latest_time:
                                latest_time = ev.occurred_at
                        total_unusable = tail["unusable"] + delta_health.quarantined + delta_health.skipped
                        total_partial = tail["partial"] or delta_health.partial
                        total_parsed_lines = tail["parsed_lines"] + delta_parsed_lines
                        next_tail = {
                            "ino": ino,
                            "ctime_ns": ctime_ns,
                            "offset": offset,
                            "prefix_hash": tail["prefix_hash"],
                            "line_number": line_num,
                            "session": session,
                            "observations": observations,
                            "known_scope": known_scope,
                            "unusable": total_unusable,
                            "partial": total_partial,
                            "parsed_lines": total_parsed_lines,
                            "latest_time": latest_time,
                        }
                        return (
                            session,
                            changed_events,
                            len(observations),
                            total_unusable,
                            total_partial,
                            delta_parsed_lines,
                            next_tail,
                        )
        except OSError:
            raise
        except Exception:
            pass

    # Full parse path (also seeds tail state for large clean newline-terminated files).
    health = SourceHealth()
    records = []
    prefix_buf = bytearray()
    offset = 0
    line_num = 0
    last_ended_nl = True
    with os.fdopen(readonly_fd(confined(path)), "rb") as raw_handle:
        for raw_line in raw_handle:
            if len(prefix_buf) < 4096:
                prefix_buf.extend(raw_line[: 4096 - len(prefix_buf)])
            offset += len(raw_line)
            last_ended_nl = raw_line.endswith(b"\n")
            line_num += 1
            record, _ = parse_jsonl_record(
                raw_line.decode("utf-8", errors="replace"),
                line_number=line_num,
                location="claude",
                health=health,
            )
            if record is not None:
                records.append(record)
    parsed_lines = health.parsed
    observations: dict = {}
    known_scope: dict = {}
    session: dict = {}
    reduce_records_into(
        records,
        health,
        path.stem,
        observations=observations,
        known_scope=known_scope,
        session=session,
    )
    health.parsed = parsed_lines
    events = _events_from_observations(observations.values())
    unusable = health.quarantined + health.skipped
    latest_time = max((ev.occurred_at for ev in events), default=None)
    next_tail = None
    if offset >= _TAIL_MIN_BYTES and last_ended_nl:
        next_tail = {
            "ino": ino,
            "ctime_ns": ctime_ns,
            "offset": offset,
            "prefix_hash": hashlib.blake2b(bytes(prefix_buf), digest_size=16).hexdigest(),
            "line_number": line_num,
            "session": session,
            "observations": observations,
            "known_scope": known_scope,
            "unusable": unusable,
            "partial": health.partial,
            "parsed_lines": parsed_lines,
            "latest_time": latest_time,
        }
    return session, events, len(events), unusable, health.partial, parsed_lines, next_tail


def ingest(*, database_path: Path, pricing: PricingEngine, session_glob: str) -> dict:
    try:
        return _ingest(database_path=database_path, pricing=pricing, session_glob=session_glob)
    except Exception as exc:
        return failed_result(
            database_path=database_path, source=SOURCE, reason=public_error(exc)
        )


def _ingest(*, database_path: Path, pricing: PricingEngine, session_glob: str) -> dict:
    from spend_app.adapters import file_cache
    from spend_app.adapters.event_identity import reconcile
    from spend_app.adapters.usage_repair import read_meta, write_meta
    from spend_app.claude_session_lanes import PRIMARY_KEY, remember, session_id_for_path, session_lanes
    from spend_app.connection_paths import cache_identity, file_signature, stat_path
    from spend_app.db import TOOL_KEY_OVERRIDES

    initialize(database_path)
    # The default run splits sessions that ran under another account's lane out of
    # ~/.claude/projects; a profile run reads its own folder and keeps its override.
    lanes = {} if TOOL_KEY_OVERRIDES.get() else session_lanes(database_path)
    file_cache.warm(database_path, SOURCE, _FILE_SIGNATURES, _FILE_COUNTS, _FILE_QUARANTINED)
    parsed_files = 0
    parsed_events = 0
    confirmed_events = 0
    issues = []
    duplicates_removed = 0
    skipped_files = 0
    quarantined = 0
    batch: dict[str, list] = {"usage_rows": [], "observations": [], "session_rows": [], "pending": [], "empty": [], "tails": []}
    attributed: dict[str, str] = {}
    results: list[dict] = []

    def flush() -> None:
        usage_rows, observations, session_rows = batch["usage_rows"], batch["observations"], batch["session_rows"]
        pending, empty, tails = batch["pending"], batch["empty"], batch["tails"]
        moved = dict(attributed)
        attributed.clear()
        for name in batch:
            batch[name] = []

        models = {}

        def prepare(connection, _rows):
            return reconcile(connection, observations, issues, claude_models=models)

        def finalize(connection):
            for session in session_rows:
                sid = session["session_id"]
                model_key = "unknown"
                key = "usage.identity.v1:" + stable_id(SOURCE, "session-model", sid)
                stored = read_meta(connection, key, None)
                if stored:
                    old_rank = tuple(stored[0])
                    model_key = stored[1]
                else:
                    old_rank = (0, 0)
                    old_session = connection.execute(
                        "SELECT model_key, ended_at FROM sessions WHERE session_id=?", (sid,)
                    ).fetchone()
                    if old_session and old_session["model_key"] != "unknown":
                        old_time = parse_utc(old_session["ended_at"] or "")
                        old_rank = (old_time.timestamp(), 0) if old_time is not None else old_rank
                        model_key = old_session["model_key"]
                candidate = models.get(sid)
                if candidate and candidate[0] >= old_rank:
                    model_key = candidate[1]
                    ranked_model = [list(candidate[0]), model_key]
                    if ranked_model != stored:
                        write_meta(connection, key, ranked_model)
                upsert_session(
                    connection,
                    session_id=sid,
                    tool_key=session["tool_key"],
                    project=session["project"],
                    started_at=session["started_at"],
                    ended_at=session["ended_at"],
                    model_key=model_key,
                )
            remember(connection, moved)
            file_cache.save(connection, SOURCE, [*pending, *((key, signature, 0, 0) for key, signature in empty)])

        results.append(persist_rows(
            database_path=database_path, pricing=pricing, source=SOURCE, usage_rows=usage_rows,
            prepare=prepare if observations else None,
            finalize=finalize, issues=issues))
        # The rows were persisted (a failure raises before this point), so every
        # parsed file is cached: an unchanged signature means identical bytes and
        # an identical parse, whatever records it had to quarantine.
        for cache_key, signature, count, unusable in pending:
            _FILE_SIGNATURES[cache_key] = signature
            _FILE_COUNTS[cache_key] = count
            if unusable:
                _FILE_QUARANTINED[cache_key] = unusable
            else:
                _FILE_QUARANTINED.pop(cache_key, None)
        for cache_key, next_tail in tails:
            if next_tail is None:
                _TAIL_STATE.pop(cache_key, None)
            else:
                _TAIL_STATE[cache_key] = next_tail
                _TAIL_STATE.move_to_end(cache_key)
                while len(_TAIL_STATE) > _TAIL_MAX_ENTRIES:
                    _TAIL_STATE.popitem(last=False)

    paths_resolved = bool(APPROVED_ROOT.get())
    for file_name in sorted(adapter_files(session_glob)):
        path = Path(file_name)
        try:
            stat = stat_path(path)
        except OSError:
            continue
        session_id = session_id_for_path(path)
        tool_key = lanes.get(session_id, PRIMARY_KEY)
        # Only a non-default lane enters the cache identity: a file that moves lanes is
        # re-parsed once (its rows are updated in place), every other file keeps its cache.
        cache_key = cache_identity(
            database_path,
            path,
            PARSER_VERSION if tool_key == PRIMARY_KEY else f"{PARSER_VERSION}+{tool_key}",
            resolved=paths_resolved,
        )
        cached = _FILE_SIGNATURES.get(cache_key)
        try:
            signature = cached if file_cache.unchanged(cached, stat) else file_signature(path, stat)
        except OSError:
            issues.append("claude_file_unavailable")
            continue
        if cached == signature:
            skipped_files += 1
            confirmed_events += _FILE_COUNTS.get(cache_key, 0)
            if _FILE_QUARANTINED.get(cache_key):
                issues.append("claude_partial_source_metadata")
                quarantined += _FILE_QUARANTINED[cache_key]
            continue
        try:
            session, events, total_events, unusable, is_partial, raw_usage_lines, next_tail = _parse_for_ingest(
                path, stat, _TAIL_STATE.get(cache_key)
            )
        except OSError:
            issues.append("claude_file_unavailable")
            continue
        if is_partial:
            issues.append("claude_partial_source_metadata")
        quarantined += unusable
        if not session:
            _FILE_SIGNATURES[cache_key] = signature
            batch["empty"].append((cache_key, signature))
            batch["tails"].append((cache_key, next_tail))
            continue
        parsed_files += 1
        if tool_key != PRIMARY_KEY:
            attributed[session_id] = tool_key
        duplicates_removed += max(0, raw_usage_lines - len(events))
        parsed_events += len(events)
        confirmed_events += max(0, total_events - len(events))
        latest_time: datetime | None = next_tail["latest_time"] if next_tail is not None else None
        for parsed in events:
            if parsed.observation:
                batch["observations"].append(
                    replace(parsed.observation, row=replace(parsed.observation.row, tool_key=tool_key))
                )
            batch["usage_rows"].append(
                UsageRow(
                    source=SOURCE,
                    tool_key=tool_key,
                    model_key=parsed.model_key,
                    occurred_at=parsed.occurred_at,
                    session_id=parsed.session_id,
                    project=parsed.project,
                    input_tokens=parsed.input_tokens,
                    cached_input_tokens=parsed.cached_input_tokens,
                    cache_write_tokens=parsed.cache_write_tokens,
                    cache_write_1h_tokens=parsed.cache_write_1h_tokens,
                    output_tokens=parsed.output_tokens,
                    reasoning_tokens=parsed.reasoning_tokens,
                    cost_usd=None,
                    raw_id=parsed.raw_id,
                    telemetry_complete=parsed.observation.row.telemetry_complete if parsed.observation else True,
                    unclassified_tokens=parsed.observation.row.unclassified_tokens if parsed.observation else 0,
                )
            )
            if latest_time is None or parsed.occurred_at > latest_time:
                latest_time = parsed.occurred_at
        if latest_time is not None:
            batch["session_rows"].append(
                {
                    "tool_key": tool_key,
                    "session_id": session["id"],
                    "project": session["project"],
                    "started_at": _iso(session["started_at"]),
                    "ended_at": _iso(latest_time),
                }
            )
        batch["pending"].append((cache_key, signature, total_events, unusable))
        batch["tails"].append((cache_key, next_tail))
        if len(batch["usage_rows"]) >= file_cache.BATCH_EVENTS:
            flush()
    # Always once more: records this pass's run, sessions and remaining files.
    flush()
    result = file_cache.merge_results(results)
    result["files"] = parsed_files
    result["filesSkippedUnchanged"] = skipped_files
    result["eventsSeen"] = parsed_events + confirmed_events
    result["eventsAccepted"] = result.get("eventsAccepted", 0) + confirmed_events
    result["issues"] = sorted(set(issues))
    result["duplicatesRemoved"] = duplicates_removed
    result["quarantined"] = result.get("quarantined", 0) + quarantined
    return result
