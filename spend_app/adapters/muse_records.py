"""Muse Code session-log usage reduction.

Identity/layout evidence: Muse Code 1.2.1 (R2847) keeps settings and
credentials under ``~/.config/muse`` and append-only session event logs under
``~/.local/share/muse/sessions/YYYY/MM/DD/*.jsonl`` (both verified against the
shipped binary; Windows installs keep the same home-relative layout). The
session-log record schema is undocumented, so only records carrying an
explicit usage object with token counts become observations. Every other
event kind (file edits, tool calls, terminal markers) is ignored without
error — an event log is expected to contain them.
"""

from __future__ import annotations

from datetime import UTC, datetime

from spend_app.adapters.local_common import project_name
from spend_app.adapters.common import UsageRow, stable_id
from spend_app.adapters.opencode_schema import count
from spend_app.source_health import quarantine
from spend_app.timeutil import parse_utc

PARSER_VERSION = "muse-session-3"

_CACHED_KEYS = (
    "cache_read_input_tokens",
    "cached_input_tokens",
    "prompt_tokens_cached",
    "cached_tokens",
    "cache_read_tokens",
    "cache_read",
    "cacheReadTokens",
)
_WRITE_KEYS = (
    "cache_creation_input_tokens",
    "cache_write_tokens",
    "cache_creation_tokens",
    "cacheWriteTokens",
)
_PROMPT_KEYS = ("prompt_tokens", "promptTokens", "input_total_tokens")
_INPUT_KEYS = ("input_tokens", "inputTokens")
_OUTPUT_KEYS = (
    "output_tokens",
    "completion_tokens",
    "outputTokens",
    "completionTokens",
    "generated_tokens",
)
_REASONING_KEYS = ("reasoning_tokens", "reasoningTokens", "thinking_tokens")
_TOTAL_KEYS = ("total_tokens", "totalTokens")
# MSP durable records stamp time as epoch-ms or epoch-us ``recorded_at``
# (schema/msp transcripts/records.jsonl envelope); the fallback names cover
# friendlier formats Muse may emit elsewhere.
_STAMP_KEYS = ("recorded_at", "timestamp", "ts", "time", "created_at", "occurred_at")
_SESSION_KEYS = ("session_id", "sessionId", "sid")
_ID_KEYS = ("event_id", "eventId", "uuid", "id")
_PROJECT_KEYS = ("cwd", "project", "workspace", "workspace_root", "workspaceRoot", "projectPath")


def _first_count(mapping: dict, keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = count(mapping.get(key))
        if value is not None:
            return value
    return None


def _usage_slot(record: dict) -> dict | None:
    for slot in ("usage", "tokens"):
        value = record.get(slot)
        if isinstance(value, dict):
            return value
    payload = record.get("payload")
    if isinstance(payload, dict):
        for slot in ("usage", "tokens", "model_usage"):
            value = payload.get(slot)
            if isinstance(value, dict):
                return value
        event = payload.get("event")
        if isinstance(event, dict):
            for slot in ("usage", "tokens"):
                value = event.get(slot)
                if isinstance(value, dict):
                    return value
            return event
        return payload
    for holder in ("message", "response", "data"):
        nested = record.get(holder)
        if isinstance(nested, dict):
            for slot in ("usage", "tokens"):
                value = nested.get(slot)
                if isinstance(value, dict):
                    return value
    return None


def usage_from_record(record: dict) -> dict | None:
    """Token components for one record, or None when it reports no usage.

    Input semantics follow the fields the record actually carries: an
    ``input_tokens``-style field excludes cached reads (Anthropic layout),
    while a ``prompt_tokens``-style field includes them (OpenAI layout), so
    the fresh component is derived by subtraction and never double-counted.
    """
    usage = _usage_slot(record)
    if usage is None:
        return None
    cached = _first_count(usage, _CACHED_KEYS)
    write = _first_count(usage, _WRITE_KEYS)
    output = _first_count(usage, _OUTPUT_KEYS)
    fresh = _first_count(usage, _INPUT_KEYS)
    if fresh is None:
        prompt = _first_count(usage, _PROMPT_KEYS)
        if prompt is not None:
            fresh = max(0, prompt - (cached or 0))
    if fresh is None and output is None:
        return None
    total = _first_count(usage, _TOTAL_KEYS)
    measured_input = (fresh or 0) + (cached or 0)
    unclassified = (
        max(0, total - measured_input - (write or 0) - (output or 0))
        if total is not None and (fresh is None or output is None)
        else 0
    )
    return {
        "input_tokens": measured_input,
        "cached_input_tokens": cached or 0,
        "cache_write_tokens": write or 0,
        "cache_write_1h_tokens": 0,
        "output_tokens": output or 0,
        "reasoning_tokens": _first_count(usage, _REASONING_KEYS),
        "unclassified_tokens": unclassified,
        "telemetry_complete": fresh is not None and output is not None,
    }


def _epoch_to_seconds(value: float) -> float:
    if value > 1e17:
        return value / 1_000_000_000
    if value > 1e14:
        return value / 1_000_000
    if value > 1e11:
        return value / 1_000
    return value


def _when_from(record: dict):
    for key in _STAMP_KEYS:
        raw = record.get(key)
        if not raw:
            continue
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            seconds = _epoch_to_seconds(float(raw))
            try:
                return datetime.fromtimestamp(seconds, UTC)
            except (OverflowError, OSError, ValueError):
                continue
        if isinstance(raw, str) and raw.strip():
            text = raw.strip()
            if text.isdigit():
                seconds = _epoch_to_seconds(float(int(text)))
                try:
                    return datetime.fromtimestamp(seconds, UTC)
                except (OverflowError, OSError, ValueError):
                    continue
            when = parse_utc(text)
            if when is not None:
                return when
    return None


def _model_key(record: dict) -> str:
    candidates = [record]
    payload = record.get("payload")
    if isinstance(payload, dict):
        candidates.append(payload)
        event = payload.get("event")
        if isinstance(event, dict):
            candidates.append(event)
    for node in candidates:
        for key in ("model", "model_id", "modelId"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                cleaned = value.strip()
                if "/" in cleaned:
                    cleaned = cleaned.rsplit("/", 1)[1]
                return "muse:" + cleaned.lower()
    return "muse:unknown"


def _session_id(record: dict, fallback: str) -> str:
    stream = record.get("stream")
    if isinstance(stream, dict) and isinstance(stream.get("id"), str) and stream["id"]:
        return stream["id"]
    for key in _SESSION_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return fallback


def _project(record: dict) -> str | None:
    candidates = [record]
    payload = record.get("payload")
    if isinstance(payload, dict):
        candidates.append(payload)
        event = payload.get("event")
        if isinstance(event, dict):
            candidates.append(event)
    for node in candidates:
        for key in _PROJECT_KEYS:
            value = node.get(key)
            if isinstance(value, str) and value:
                return project_name(value)
    return None


def _raw_id(record: dict, *, file_key: str, session_id: str, when, index: int) -> str:
    for key in _ID_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value:
            return stable_id("muse-turn", file_key, value)
    return stable_id("muse-turn", file_key, session_id, round(when.timestamp(), 6), index)


def reduce_records(records, *, file_key: str = "session", health=None):
    """Observations from parsed JSONL records. Returns (rows, session)."""
    rows: list[UsageRow] = []
    session: dict = {}
    session_project: str | None = None
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        rec_project = _project(record)
        if rec_project and not session_project:
            session_project = rec_project
        parts = usage_from_record(record)
        if parts is None:
            continue
        when = _when_from(record)
        if when is None:
            if health is not None:
                health.note(quarantine("muse_usage_time_missing"))
            continue
        session_id = _session_id(record, file_key)
        project = rec_project or session_project
        rows.append(
            UsageRow(
                source="muse_local",
                tool_key="muse",
                model_key=_model_key(record),
                occurred_at=when,
                session_id=session_id,
                project=project,
                cost_usd=None,
                raw_id=_raw_id(record, file_key=file_key, session_id=session_id, when=when, index=index),
                **parts,
            )
        )
        if health is not None:
            health.note_ok()
        if not session:
            session = {
                "id": session_id,
                "project": project,
                "started_at": when,
                "model_key": _model_key(record),
            }
    return rows, session
