"""Traycer local usage from the host usage ledger.

Per-CLI turns come from ``usage_analytics_facts`` when that table exists.
Older hosts still use ``chat_projection``. Quota and live-agent signals that
spawn the Traycer CLI (``agent profile-rate-limits`` / ``agent list``) live
in ``spend_app.limits`` and are experimental undocumented interfaces.
"""

from __future__ import annotations

import glob
from spend_app.connection_paths import adapter_files
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from spend_app.adapters.common import UsageRow, persist_rows, stable_id
from spend_app.adapters.local_common import (
    classify_traycer_usage,
    number,
    positive_cost,
    sqlite_read_only,
    store_read_only,
)
from spend_app.pricing import PricingEngine


SOURCE = "traycer_local"
INGESTED_HARNESSES = {"grok", "openrouter"}
# (resolved chat.db path, chat_id) -> (version, parsed rows). Traycer keeps
# every projection (hundreds of MB of JSON) in one table; re-reading and
# re-parsing all of it every 15 seconds cost more than a CPU-second per cycle.
# A projection only changes when its op-log position (through_seq) advances,
# so unchanged chats reuse their parsed rows without touching the JSON.
# through_seq is stored before the JSON column, so listing it is a few
# milliseconds; updated_at sits after the JSON and would walk every overflow
# page (about 0.1 s per scan on a 1 GB store).
_PROJECTION_CACHE: dict[tuple[str, str], tuple[object, tuple[UsageRow, ...]]] = {}
_VERSION_COLUMNS = ("through_seq", "updated_at")


def reset_projection_cache() -> None:
    _PROJECTION_CACHE.clear()


def projection_index(connection: sqlite3.Connection) -> list[tuple[str, object]] | None:
    """List (chat_id, version) for every projection, cheapest column first.

    Returns None when the table is missing. A version of None means the store
    exposes no change marker (fixtures), so callers re-parse every projection.
    """
    for column in _VERSION_COLUMNS:
        try:
            rows = connection.execute(f"SELECT chat_id,{column} FROM chat_projection").fetchall()
        except sqlite3.OperationalError:
            continue
        return [(str(chat_id), version) for chat_id, version in rows]
    try:
        rows = connection.execute("SELECT chat_id FROM chat_projection").fetchall()
    except sqlite3.OperationalError:
        return None
    return [(str(chat_id), None) for (chat_id,) in rows]


HARNESS_TOOLS = {
    "claude": "claude-code",
    "codex": "codex",
    "grok": "grok",
    "opencode": "opencode",
    "openrouter": "openrouter",
    "antigravity": "antigravity",
}
# OpenCode's own database is the authority for that CLI. OpenRouter has no
# local session ledger, so those Traycer facts stay.
NATIVE_SOURCES = {
    "claude-code": ("claude_local",),
    "codex": ("codex_local",),
    "grok": ("grok_local",),
}


def fact_model(harness: str, model: str) -> str:
    lowered = (model or "unknown").strip().lower() or "unknown"
    if harness == "grok":
        return f"supergrok:{lowered}"
    if harness == "openrouter":
        return canonical_model("openrouter", model or "unknown")
    if harness == "opencode":
        return "opencode:" + lowered.split(":")[-1]
    if harness in {"claude", "codex", "antigravity"}:
        return lowered
    return f"{harness}:{lowered}"


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def parse_facts(path: Path, *, observations: bool = False) -> list | None:
    """Return parsed fact rows, or None when this database is not the facts ledger."""
    connection = sqlite_read_only(path)
    try:
        if "usage_analytics_facts" not in _table_names(connection):
            return None
        fetched = connection.execute(
            """
            SELECT fact_id, occurred_at, chat_id, harness_id, model, usage_completeness,
                   uncached_input_tokens, cache_read_input_tokens, cache_creation_tokens,
                   output_tokens, reasoning_tokens, cost_usd, cost_provenance, tui_session_id
            FROM usage_analytics_facts
            """
        ).fetchall()
    finally:
        connection.close()
    rows = []
    for (
        fact_id, occurred_ms, chat_id, harness, model, completeness,
        uncached, cache_read, cache_create, output, reasoning, cost, provenance, tui_session,
    ) in fetched:
        harness = str(harness or "").strip().lower()
        if not harness or str(completeness or "") == "absent":
            continue
        timestamp_ms = number(occurred_ms)
        if timestamp_ms <= 0 or not fact_id:
            continue
        fresh = int(number(uncached))
        cached = int(number(cache_read))
        writes = int(number(cache_create))
        produced = int(number(output))
        if fresh == 0 and cached == 0 and writes == 0 and produced == 0:
            continue
        tool = HARNESS_TOOLS.get(harness, harness)
        session = str(tui_session).strip() if tui_session else ""
        if not session and chat_id:
            session = f"traycer:{chat_id}"
        reported = cost if str(provenance or "") == "providerReported" else 0
        from spend_app.adapters.event_identity import Observation
        from dataclasses import replace
        row = UsageRow(
            source=SOURCE,
            tool_key=tool,
            model_key=fact_model(harness, str(model or "unknown")),
            occurred_at=datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC),
            session_id=session or None,
            project=str(chat_id) if chat_id else None,
            input_tokens=fresh + cached,
            cached_input_tokens=cached,
            cache_write_tokens=writes,
            cache_write_1h_tokens=0,
            output_tokens=produced,
            reasoning_tokens=int(number(reasoning)) or None,
            cost_usd=positive_cost(reported),
            raw_id=stable_id("traycer-fact", str(fact_id)),
            unclassified_tokens=0,
            telemetry_complete=str(completeness or "") == "measured",
        )
        logical = stable_id("traycer-fact", str(fact_id))
        observation = Observation(replace(row, raw_id=logical), (row.raw_id,), timestamp_ms, "traycer-facts")
        rows.append(observation if observations else observation.row)
    return rows


def covered_sessions(database_path: Path | None) -> set[tuple[str, str]]:
    path = Path(database_path) if database_path else None
    if path is None or not path.is_file():
        return set()
    found: set[tuple[str, str]] = set()
    try:
        # BURNRATE's own store: outside the approved Traycer location by design.
        connection = store_read_only(path)
    except (OSError, sqlite3.Error):
        return found
    try:
        tables = _table_names(connection)
        for table in ("usage_events", "unpriced_usage_events"):
            if table not in tables:
                continue
            for tool, sources in NATIVE_SOURCES.items():
                marks = ",".join("?" for _ in sources)
                query = (
                    f"SELECT session_id FROM {table} "
                    f"WHERE tool_key=? AND source IN ({marks}) AND session_id IS NOT NULL"
                )
                for (session_id,) in connection.execute(query, (tool, *sources)):
                    if session_id:
                        found.add((tool, str(session_id)))
    except sqlite3.Error:
        return found
    finally:
        connection.close()
    return found


def drop_native_duplicates(connection: sqlite3.Connection) -> None:
    for table in ("usage_events", "unpriced_usage_events"):
        try:
            connection.execute(
                f"""
                DELETE FROM {table}
                WHERE source=? AND rowid IN (
                    SELECT traycer.rowid FROM {table} traycer
                    JOIN {table} native
                      ON native.source != traycer.source
                     AND native.tool_key = traycer.tool_key
                     AND native.session_id = traycer.session_id
                    WHERE traycer.source=? AND traycer.session_id IS NOT NULL
                )
                """,
                (SOURCE, SOURCE),
            )
        except sqlite3.OperationalError:
            continue


def iter_databases(database_glob: str) -> list[Path]:
    direct = Path(database_glob)
    if direct.is_file():
        return [direct]
    matches = [Path(name) for name in sorted(adapter_files(database_glob)) if Path(name).is_file()]
    facts = [path for path in matches if path.name == "app-assets.db"]
    if facts:
        return facts
    if direct.name == "app-assets.db":
        root = direct.parent.parent
        fallback = sorted(root.glob("host/epic-state/*/chat/chat.db"))
        if fallback:
            return fallback
    return matches


def inspect_source(path: Path) -> dict:
    from spend_app.connection_paths import LocationError

    root = path if path.is_dir() else path.parent
    facts = root / "app-assets" / "app-assets.db"
    if path.is_file():
        facts = path if path.name == "app-assets.db" else facts
    candidates = [facts] if facts.is_file() else []
    if root.is_dir():
        candidates.extend(list(root.glob("host/epic-state/*/chat/chat.db"))[:3])
    if not candidates and path.is_file():
        candidates = [path]
    if not candidates:
        raise LocationError("Traycer usage ledger was not found. Install Traycer or connect its host folder.")
    usable = False
    for candidate in candidates:
        try:
            connection = sqlite_read_only(candidate)
        except (OSError, sqlite3.Error):
            continue
        try:
            names = _table_names(connection)
            if "usage_analytics_facts" in names:
                usable = connection.execute(
                    "SELECT 1 FROM usage_analytics_facts WHERE usage_completeness != 'absent' LIMIT 1"
                ).fetchone() is not None
                if usable:
                    break
            elif "chat_projection" in names:
                usable = connection.execute("SELECT 1 FROM chat_projection LIMIT 1").fetchone() is not None
                if usable:
                    break
        finally:
            connection.close()
    if not usable and not any(item.is_file() for item in candidates):
        raise LocationError("Traycer usage ledger was not found. Install Traycer or connect its host folder.")
    return {
        "state": "readable",
        "usableSample": usable,
        "detail": "Traycer per-CLI session usage." if usable else "Traycer is installed; waiting for a recorded turn.",
    }


def canonical_model(harness: str, model: str) -> str:
    lowered = model.strip().lower()
    if harness == "grok":
        return f"supergrok:{lowered}"
    if harness == "openrouter":
        lowered = lowered.removeprefix("openrouter:")
        if lowered == "z-ai/glm-5.3-flash":
            lowered = "glm-5.3-flash"
        return f"openrouter:{lowered}"
    return f"{harness}:{lowered}"


def settings_candidates(metadata: dict):
    item = metadata.get("item")
    if isinstance(item, dict):
        yield item
    items = metadata.get("items")
    if isinstance(items, list):
        for candidate in items:
            if isinstance(candidate, dict):
                yield candidate


def parse_projection(*, path: Path, chat_id: str, projection_json: str, observations=False) -> list[UsageRow]:
    projection = json.loads(projection_json)
    root = projection.get("settings") if isinstance(projection.get("settings"), dict) else {}
    current_harness = root.get("harnessId")
    current_model = root.get("model")
    rows: list[UsageRow] = []
    events = projection.get("events")
    if not isinstance(events, list):
        return rows

    for event_index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        body = event.get("body")
        if not isinstance(body, dict):
            continue
        metadata = body.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        for item in settings_candidates(metadata):
            settings = item.get("settings")
            if not isinstance(settings, dict):
                continue
            current_harness = settings.get("harnessId") or current_harness
            current_model = settings.get("model") or current_model

        usage = metadata.get("usage")
        if current_harness not in INGESTED_HARNESSES or not isinstance(usage, dict):
            continue
        timestamp_ms = number(body.get("timestamp"))
        if timestamp_ms <= 0:
            continue
        tokens = classify_traycer_usage(usage)
        model_key = canonical_model(str(current_harness), str(current_model or "unknown"))
        raw_id = stable_id(
            "traycer-local",
            str(path.resolve()),
            chat_id,
            event_index,
            timestamp_ms,
            current_harness,
            current_model,
        )
        row = UsageRow(
                source=SOURCE,
                tool_key=str(current_harness),
                model_key=model_key,
                occurred_at=datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC),
                session_id=f"traycer:{chat_id}",
                project=path.parents[2].name if len(path.parents) > 2 else None,
                input_tokens=tokens.input_tokens,
                cached_input_tokens=tokens.cached_input_tokens,
                cache_write_tokens=tokens.cache_write_tokens,
                cache_write_1h_tokens=0,
                output_tokens=tokens.output_tokens,
                reasoning_tokens=None,
                cost_usd=positive_cost(usage.get("costUsd")),
                raw_id=raw_id,
                unclassified_tokens=tokens.unclassified_tokens,
                telemetry_complete=tokens.telemetry_complete,
            )
        from dataclasses import replace
        from spend_app.adapters.event_identity import Observation
        logical = stable_id("traycer-event", chat_id, current_harness, event.get("id") or event_index, timestamp_ms)
        observation = Observation(replace(row, raw_id=logical), (raw_id,), timestamp_ms, "traycer-projection")
        rows.append(observation if observations else observation.row)
    return rows


def parse_database(path: Path, *, database_path=None, observations=False, issues=None) -> list[UsageRow]:
    rows: list[UsageRow] = []
    from spend_app.connection_paths import cache_identity
    issues = issues if issues is not None else []
    facts = parse_facts(path, observations=True)
    if facts is not None:
        return facts if observations else [item.row for item in facts]
    connection = sqlite_read_only(path)
    stat = path.stat()
    cache_prefix = (cache_identity(database_path or path, path, "traycer-identity-v1"),
                    (stat.st_dev, stat.st_ino, getattr(stat, "st_birthtime_ns", None)))
    seen: set[tuple[str, str]] = set()
    try:
        index = projection_index(connection)
        if index is None:
            raise ValueError("incompatible_traycer_schema")
        for chat_id, version in index:
            key = (cache_prefix, chat_id)
            seen.add(key)
            cached = _PROJECTION_CACHE.get(key) if version is not None else None
            if cached is not None and cached[0] == version:
                rows.extend(cached[1])
                continue
            fetched = connection.execute(
                "SELECT projection_json FROM chat_projection WHERE chat_id=?", (chat_id,)
            ).fetchone()
            if fetched is None or fetched[0] is None:
                continue
            try:
                parsed = parse_projection(
                    path=path,
                    chat_id=chat_id,
                    projection_json=fetched[0],
                    observations=True,
                )
            except (TypeError, ValueError, AttributeError):
                issues.append("invalid_traycer_projection")
                continue
            rows.extend(parsed)
            if version is not None:
                _PROJECTION_CACHE[key] = (version, tuple(parsed))
    finally:
        connection.close()
    for key in [key for key in _PROJECTION_CACHE if key[0] == cache_prefix and key not in seen]:
        _PROJECTION_CACHE.pop(key, None)
    return rows if observations else [o.row for o in rows]


def ingest(
    *,
    database_path: Path,
    pricing: PricingEngine,
    database_glob: str,
    grok_covered_from: datetime | None = None,
) -> dict:
    usage_rows: list[UsageRow] = []
    files = 0
    mirrored = 0
    issues, observations = [], []
    already = covered_sessions(database_path)
    opencode_db = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    try:
        from spend_app.providers import default_opencode_database
        opencode_db = default_opencode_database()
    except Exception:
        pass
    opencode_present = opencode_db.is_file()
    for path in iter_databases(database_glob):
        if not path.is_file():
            continue
        files += 1
        try:
            parsed = parse_database(path, database_path=database_path, observations=True, issues=issues)
        except (OSError, ValueError, sqlite3.DatabaseError):
            issues.append("traycer_source_unavailable_or_incompatible")
            continue
        for observation in parsed:
            row = observation.row
            # Traycer runs Grok through the grok CLI, whose own log (grok_local)
            # records every call. Where that log exists, it is the authority;
            # emitting the projection's copy too would count each turn twice.
            if row.tool_key == "grok" and grok_covered_from is not None and row.occurred_at >= grok_covered_from:
                mirrored += 1
                continue
            if row.tool_key == "opencode" and opencode_present:
                mirrored += 1
                continue
            # BURNRATE no longer tracks Cursor, including runs Traycer launches.
            if row.tool_key == "cursor":
                continue
            if row.session_id and (row.tool_key, row.session_id) in already:
                mirrored += 1
                continue
            usage_rows.append(row)
            observations.append(observation)
    from spend_app.adapters.event_identity import reconcile

    def prepare(connection, _rows):
        drop_native_duplicates(connection)
        return reconcile(connection, observations, issues)

    result = persist_rows(
        database_path=database_path,
        pricing=pricing,
        source=SOURCE,
        usage_rows=usage_rows, issues=issues,
        prepare=prepare,
    )
    return {
        **result,
        "files": files,
        "issues": sorted(set(issues)),
        "eventsSeen": len(usage_rows),
        "mirroredGrokEvents": mirrored,
        "unclassifiedEvents": sum(not row.telemetry_complete for row in usage_rows),
    }
