"""Granular OpenCode ingestion with scoped 0.3.0 coarse-history compatibility."""
import json
import sqlite3
from dataclasses import fields, replace
from pathlib import Path

from spend_app.adapters.common import UsageRow, persist_rows, stable_id
from spend_app.adapters.local_common import parse_iso_time, sqlite_read_only
from spend_app.adapters.opencode_schema import merge_messages, parse_message, read_messages, recognize, schema_columns
from spend_app.adapters.usage_repair import COUNTS, coarse_remainder, delete_event, read_meta, write_meta
from spend_app.connection_paths import confined, opencode_files
from spend_app.db import connect


def row_from_record(record):
    values = {field.name: record[field.name] for field in fields(UsageRow) if field.name in record}
    values["occurred_at"] = parse_iso_time(values["occurred_at"])
    values.setdefault("unclassified_tokens", 0)
    values.setdefault("telemetry_complete", True)
    return UsageRow(**values)


def read_source(path):
    messages, cumulative, issues = [], [], []
    formats, files = set(), 0
    for file in opencode_files(path):
        files += 1
        try:
            if file.suffix.lower() == ".json":
                with confined(file).open("r", encoding="utf-8") as stream:
                    data = json.load(stream)
                parsed = parse_message(data, row_id=file.stem, session_id=file.parent.name)
                if parsed:
                    messages.append(parsed)
                elif not isinstance(data, dict) or data.get("role") not in {"user", "assistant"}:
                    issues.append("opencode_malformed_message")
                formats.add("json")
            else:
                db = sqlite_read_only(file)
                try:
                    columns = schema_columns(db)
                    chosen = recognize(columns)
                    formats.update(chosen)
                    if "cumulative" in chosen:
                        cumulative.append(file)
                    if chosen != ("cumulative",):
                        rows, bad = read_messages(db, columns)
                        messages.extend(rows)
                        if bad:
                            issues.append("opencode_malformed_message")
                finally:
                    db.close()
        except (OSError, ValueError, sqlite3.Error):
            issues.append("opencode_source_unavailable")
    if files and not formats:
        raise ValueError("OpenCode source has no supported readable schema.")
    chosen, ambiguous = merge_messages(messages)
    if ambiguous:
        issues.append("opencode_equal_revision_conflict")
    return chosen, cumulative, issues, sorted(formats)


def reconcile_messages(connection, messages, issues):
    """Executed inside persist_rows' existing transaction."""
    from spend_app.adapters.opencode_local import BASELINE_ISSUE, DELTA_ISSUE
    grouped = {}
    for message in messages:
        grouped.setdefault((message.row.session_id, message.provider), []).append(message)
    outputs = []
    for (session, provider), group in grouped.items():
        key = "opencode.granular.v1:" + stable_id(session, provider)
        state = read_meta(connection, key, {"revisions": {}, "allocations": {}})
        providers = [row[0] for row in connection.execute("SELECT provider_id FROM opencode_session_progress WHERE session_id=?", (session,))]
        legacy_id = stable_id("opencode-local", session, provider)
        records = []
        for table in ("usage_events", "unpriced_usage_events"):
            records.extend(dict(row) for row in connection.execute(f"SELECT e.* FROM {table} e LEFT JOIN coverage_gap_events g ON g.raw_id=e.raw_id WHERE e.source='opencode_local' AND e.session_id=? AND (e.raw_id=? OR g.issue IN (?,?))", (session, legacy_id, BASELINE_ISSUE, DELTA_ISSUE)))
        if records and providers != [provider] and any(row["raw_id"] != legacy_id for row in records):
            issues.append("opencode_coarse_provider_scope_ambiguous")
            # Preserve the existing coarse history; only clearly later usage
            # is independent of its historical observation interval.
            cutoff = max(parse_iso_time(row["occurred_at"]) for row in records)
            group = [message for message in group if message.started_at and message.started_at > cutoff]
            records = []
        accepted = []
        for message in group:
            rank = [message.revision, int(message.row.telemetry_complete), int(message.format == "v2")]
            previous = state["revisions"].get(message.row.raw_id)
            if previous and rank < previous:
                continue
            started = message.started_at or parse_iso_time(state.get("starts", {}).get(message.row.raw_id))
            if started is None and any(message.row.occurred_at > parse_iso_time(record["occurred_at"]) for record in records):
                issues.append("opencode_start_evidence_missing_overlap_unresolved")
                continue
            message = replace(message, started_at=started)
            accepted.append(message)
            state["revisions"][message.row.raw_id] = rank
            if message.started_at is not None:
                state.setdefault("starts", {})[message.row.raw_id] = message.started_at.isoformat()
            outputs.append(message.row)
        for record in sorted(records, key=lambda row: (row["occurred_at"], row["raw_id"])):
            coarse = row_from_record(record)
            residual, allocations = coarse_remainder(coarse, accepted, state["allocations"])
            state["allocations"] = allocations
            if residual != coarse:
                delete_event(connection, coarse.raw_id)
                if any(getattr(residual, name) for name in COUNTS) or residual.cost_usd:
                    outputs.append(residual)
                    # Keep its coarse provenance after replacement.
                    connection.execute("INSERT OR REPLACE INTO coverage_gap_events(raw_id,source,tool_key,model_key,occurred_at,issue) VALUES(?,?,?,?,?,?)", (residual.raw_id, residual.source, residual.tool_key, residual.model_key, residual.occurred_at.isoformat(), BASELINE_ISSUE))
        write_meta(connection, key, state)
    return outputs


def reconcile_cumulative(connection, rows, snapshots, issues=None, deferred=None):
    """Reverse import order: granular history can precede a legacy store."""
    from types import SimpleNamespace
    from spend_app.adapters.event_identity import stored_event
    issues = issues if issues is not None else []
    deferred = deferred if deferred is not None else set()
    providers = {snapshot.session_id: snapshot.provider_id for snapshot in snapshots}
    outputs = []
    for coarse in rows:
        provider = providers.get(coarse.session_id)
        key = "opencode.granular.v1:" + stable_id(coarse.session_id, provider)
        state = read_meta(connection, key, {"revisions": {}, "allocations": {}})
        messages = []
        unknown_overlap = False
        for raw_id in state["revisions"]:
            record = stored_event(connection, raw_id)
            if record and record["source"] == "opencode_local" and record["session_id"] == coarse.session_id:
                row = row_from_record(record)
                started = parse_iso_time(state.get("starts", {}).get(raw_id))
                if started is None and row.occurred_at > coarse.occurred_at:
                    unknown_overlap = True
                messages.append(SimpleNamespace(row=row, started_at=started))
        if unknown_overlap:
            issues.append("opencode_start_evidence_missing_overlap_unresolved")
            deferred.add((coarse.session_id, provider))
            continue
        residual, state["allocations"] = coarse_remainder(coarse, messages, state["allocations"])
        if any(getattr(residual, name) for name in COUNTS) or residual.cost_usd:
            outputs.append(residual)
        write_meta(connection, key, state)
    return outputs


def combine_results(results, *, issues=(), formats=()):
    """Aggregate attempt counters, not a new count of unique measured events."""
    result = dict(results[0]) if len(results) == 1 else {"source": "opencode_local"}
    result.setdefault("source", "opencode_local")
    counters = ("eventsSeen", "eventsAccepted", "eventsWritten", "unpricedEventsWritten", "coverageGapsWritten",
                "costBucketsSeen", "costBucketsWritten", "quarantined", "files")
    if len(results) != 1:
        for name in counters:
            if any(name in child for child in results):
                result[name] = sum(child.get(name, 0) for child in results)
    for name in ("unpricedModels", "issues"):
        result[name] = sorted(set(item for child in results for item in child.get(name, [])) | (set(issues) if name == "issues" else set()))
    statuses = {child.get("status", "failed") for child in results}
    if statuses & {"success", "partial"}:
        result["status"] = "partial" if statuses - {"success"} or result["issues"] or result["unpricedModels"] else "success"
    else:
        result["status"] = "failed" if "failed" in statuses or result["issues"] else "skipped"
    result["formats"] = sorted(set(formats))
    return result


# Each session's messages are fingerprinted; a pass reconciles and saves only
# the sessions whose messages changed (OpenCode rewrites its database on every
# turn, so the whole-store shortcut rarely applies and a pass used to redo all
# ~40,000 messages in one long transaction). Fingerprints and the issues each
# session produced persist with the collector's file record, so a restart does
# not redo them either.
SESSION_RECORD = "opencode_local:sessions:v1"
_PRICING_DIGESTS: dict[int, str] = {}


def _pricing_digest(pricing) -> str:
    import hashlib

    digest = _PRICING_DIGESTS.get(id(pricing))
    if digest is None:
        digest = hashlib.blake2b(repr(getattr(pricing, "prices", ())).encode("utf-8"), digest_size=8).hexdigest()
        _PRICING_DIGESTS.clear()
        _PRICING_DIGESTS[id(pricing)] = digest
    return digest


def _session_fingerprints(messages, pricing, snapshots: dict | None = None) -> dict:
    """Per session: its messages, the prices, and its cumulative totals.

    A session's messages reconcile against the coarse rows the cumulative
    step writes from its totals, so a change in those totals changes its
    fingerprint and only that session is read again.
    """
    import hashlib

    grouped = {}
    for message in messages:
        grouped.setdefault(message.row.session_id, []).append(message)
    prices = _pricing_digest(pricing)
    fingerprints = {}
    for session, group in grouped.items():
        digest = hashlib.blake2b(prices.encode("utf-8"), digest_size=16)
        for message in sorted(group, key=lambda item: item.row.raw_id):
            digest.update(repr(message).encode("utf-8"))
        for snapshot in sorted((snapshots or {}).get(session, ()), key=lambda item: (item.provider_id, item.model_id)):
            digest.update(repr(snapshot).encode("utf-8"))
        fingerprints[session] = (digest.hexdigest(), len(group))
    return fingerprints


def _load_session_records(database_path) -> dict:
    from spend_app.adapters import file_cache

    with connect(Path(database_path)) as connection:
        connection.executescript(file_cache.TABLE_SQL)
        rows = connection.execute(
            "SELECT cache_key, signature FROM ingest_file_cache WHERE source=?", (SESSION_RECORD,)).fetchall()
    records = {}
    for row in rows:
        try:
            fingerprint, issues = json.loads(row["signature"])
        except (ValueError, TypeError):
            continue
        records[row["cache_key"]] = (fingerprint, list(issues))
    return records


def ingest(*, database_path, pricing, source_database):
    from spend_app.adapters import unchanged
    path = Path(source_database)
    if not path.exists():
        raise ValueError("OpenCode location is missing or moved.")
    cache_key, current, replayed = unchanged.check(
        database_path, "opencode_local", path, lambda: list(opencode_files(path)), pricing)
    if replayed is not None:
        return replayed
    result = _collect(database_path=database_path, pricing=pricing, path=path)
    unchanged.remember(database_path, "opencode_local", cache_key, current, result)
    return result


def _collect(*, database_path, pricing, path):
    messages, cumulative, issues, formats = read_source(path)
    from spend_app.adapters.opencode_local import _ingest_cumulative, read_session_snapshots
    legacy_results = []
    # One read of the cumulative totals serves both the cumulative step and
    # the session fingerprints, so the two never see different totals.
    snapshots: dict | None = {}
    for legacy in cumulative:
        try:
            legacy_snapshots = read_session_snapshots(legacy) if legacy.is_file() else []
        except (OSError, ValueError, sqlite3.Error):
            legacy_snapshots = None
            snapshots = None
        if snapshots is not None:
            for snapshot in legacy_snapshots:
                snapshots.setdefault(snapshot.session_id, []).append(snapshot)
        try:
            result = _ingest_cumulative(database_path=database_path, pricing=pricing, source_database=legacy,
                                        snapshots=legacy_snapshots)
            legacy_results.append(result)
            issues.extend(result.get("issues", []))
        except (OSError, ValueError, sqlite3.Error):
            issues.append("opencode_cumulative_source_unavailable")
            from spend_app.adapters.common import failed_result
            legacy_results.append(failed_result(database_path=database_path, source="opencode_local", reason="OpenCode cumulative source could not be read."))
    if cumulative and not legacy_results and not messages:
        raise ValueError("OpenCode cumulative source could not be read.")
    if not messages and cumulative:
        return combine_results(legacy_results, issues=issues, formats=formats)
    fingerprints = _session_fingerprints(messages, pricing, snapshots)
    records = _load_session_records(database_path)
    # Without the totals a fingerprint cannot see a cumulative change, so a
    # cumulative step that wrote rows keeps every session in.
    legacy_wrote = any((result.get("eventsWritten") or 0) + (result.get("unpricedEventsWritten") or 0)
                       for result in legacy_results)
    if snapshots is None and legacy_wrote:
        records = {}
    unchanged = {session for session, (fingerprint, _count) in fingerprints.items()
                 if records.get(session, (None,))[0] == fingerprint}
    skipped_messages = sum(fingerprints[session][1] for session in unchanged)
    for session in unchanged:
        issues.extend(records[session][1])
    changed = [message for message in messages if message.row.session_id not in unchanged]
    session_issues: dict = {}

    def prepare(connection, rows):
        outputs = []
        by_session = {}
        for message in changed:
            by_session.setdefault(message.row.session_id, []).append(message)
        for session, group in by_session.items():
            found = session_issues.setdefault(session, [])
            outputs.extend(reconcile_messages(connection, group, found))
            issues.extend(found)
        return outputs

    def finalize(connection):
        connection.executemany(
            "INSERT OR REPLACE INTO ingest_file_cache(source, cache_key, signature, events, quarantined) VALUES(?,?,?,?,0)",
            [(SESSION_RECORD, session, json.dumps([fingerprints[session][0], sorted(set(session_issues.get(session, [])))]),
              fingerprints[session][1]) for session in {message.row.session_id for message in changed}])

    result = persist_rows(database_path=database_path, pricing=pricing, source="opencode_local", usage_rows=[m.row for m in changed],
                          prepare=prepare, finalize=finalize, issues=issues)
    if skipped_messages:
        result = {**result, "eventsSeen": result.get("eventsSeen", 0) + skipped_messages,
                  "eventsAccepted": result.get("eventsAccepted", 0) + skipped_messages,
                  "sessionsSkippedUnchanged": len(unchanged)}
    return combine_results([*legacy_results, result], issues=issues, formats=formats)
