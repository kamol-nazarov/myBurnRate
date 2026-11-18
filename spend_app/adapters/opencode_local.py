"""OpenCode local session-aggregate ingest.

Files read (read-only):
  - ``%USERPROFILE%\\.local\\share\\opencode\\opencode.db`` table ``session``,
    opened with SQLite ``mode=ro`` + ``PRAGMA query_only=ON``.

Official vs observed:
  Observed local session aggregates (not per-turn). ``cost`` is kept only when
  the session reports a positive value. Sessions with
  ``providerID=traycer-openrouter`` are skipped; Traycer is the per-turn
  authority for that mirror.

Not stored:
  Prompts, messages, titles, or full filesystem paths. Project is the
  directory name of ``directory`` / ``path`` / ``project_id``. Token columns
  and optional session cost only.

Live-session rule:
  This adapter has no live-session collector. Opening the OpenCode editor or
  an idle shell is not live.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from spend_app.adapters.common import UsageRow, persist_rows, stable_id
from spend_app.adapters.local_common import (
    project_name,
    number,
    optional_number,
    parse_millis,
    parse_iso_time,
    positive_cost,
    sqlite_read_only,
)
from spend_app.db import (
    TOOL_KEY_OVERRIDES,
    record_coverage_gap,
    write_opencode_progress,
)
from spend_app.pricing import PricingEngine
from spend_app.opencode_providers import tool_key_for_provider


SOURCE = "opencode_local"
# Traycer also writes these sessions into OpenCode. Per-turn authority is the
# Traycer chat projection, so the session aggregate would double-count.
MIRRORED_PROVIDERS = {"traycer-openrouter"}
BASELINE_ISSUE = (
    "Initial cumulative baseline attributed to the first observation; the "
    "store keeps no per-turn timestamps for earlier history."
)
DELTA_ISSUE = (
    "Observed-interval delta from cumulative session counters; timing is the "
    "observation instant and model attribution uses the session's current model."
)
COUNTER_COLUMNS = ("input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens")
HISTORY_REPAIR = "opencode.cumulative-repair:v2:"


def ingest(*, database_path: Path, pricing: PricingEngine, source_database: Path) -> dict:
    from spend_app.adapters.opencode_granular import ingest as collect
    try:
        return collect(database_path=database_path, pricing=pricing, source_database=source_database)
    except (OSError, ValueError, sqlite3.Error):
        from spend_app.adapters.common import failed_result
        return failed_result(database_path=database_path, source=SOURCE, reason="OpenCode source is missing, unreadable or incompatible. Recheck the configured source.")


def canonical_model(model: str) -> str:
    return f"opencode:{model.strip().lower()}"


@dataclass(frozen=True)
class SessionSnapshot:
    session_id: str
    provider_id: str
    model_id: str
    project: str | None
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int | None
    cost_usd: float | None
    observed_at: datetime


def read_session_snapshots(path: Path) -> list[SessionSnapshot]:
    """Cumulative per-session totals exactly as the store reports them."""
    snapshots: list[SessionSnapshot] = []
    connection = sqlite_read_only(path)
    try:
        query = """
            SELECT id,project_id,directory,path,model,cost,tokens_input,tokens_output,
                   tokens_reasoning,tokens_cache_read,tokens_cache_write,time_updated
            FROM session
            WHERE COALESCE(tokens_input,0)+COALESCE(tokens_output,0)+
                  COALESCE(tokens_cache_read,0)+COALESCE(tokens_cache_write,0)>0
        """
        cursor = connection.execute(query)
        for row in cursor:
            (
                session_id,
                _project_id,
                directory,
                project_path,
                model_json,
                cost,
                input_value,
                output_value,
                reasoning_value,
                cache_read_value,
                cache_write_value,
                updated_at,
            ) = row
            try:
                model_info = json.loads(model_json) if model_json else {}
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(model_info, dict):
                continue
            provider_id = str(model_info.get("providerID") or "unknown")
            if provider_id in MIRRORED_PROVIDERS:
                continue
            observed_at = parse_millis(updated_at)
            if observed_at is None:
                continue
            project_source = directory or project_path or None
            snapshots.append(
                SessionSnapshot(
                    session_id=str(session_id),
                    provider_id=provider_id,
                    model_id=str(model_info.get("id") or "unknown"),
                    project=project_name(project_source) if isinstance(project_source, str) else None,
                    input_tokens=number(input_value),
                    cached_input_tokens=number(cache_read_value),
                    cache_write_tokens=number(cache_write_value),
                    output_tokens=number(output_value),
                    reasoning_tokens=optional_number(reasoning_value),
                    cost_usd=positive_cost(cost),
                    observed_at=observed_at,
                )
            )
    finally:
        connection.close()
    return snapshots


def parse_database(path: Path) -> list[UsageRow]:
    """Raw cumulative snapshot reader (compatibility): one row per session.

    Rows carry the store's own cumulative totals at its update time. Ingest
    no longer persists these directly; it derives baseline/delta rows from
    them. Mirrored providers stay excluded.
    """
    return [
        _snapshot_row(snapshot)
        for snapshot in read_session_snapshots(path)
    ]


def _snapshot_row(snapshot: SessionSnapshot) -> UsageRow:
    return UsageRow(
        source=SOURCE,
        tool_key=tool_key_for_provider(snapshot.provider_id),
        model_key=canonical_model(snapshot.model_id),
        occurred_at=snapshot.observed_at,
        session_id=snapshot.session_id,
        project=snapshot.project,
        input_tokens=snapshot.input_tokens + snapshot.cached_input_tokens,
        cached_input_tokens=snapshot.cached_input_tokens,
        cache_write_tokens=snapshot.cache_write_tokens,
        cache_write_1h_tokens=0,
        output_tokens=snapshot.output_tokens,
        reasoning_tokens=snapshot.reasoning_tokens,
        cost_usd=snapshot.cost_usd,
        raw_id=stable_id("opencode-local", snapshot.session_id, snapshot.provider_id),
    )


def _counts(snapshot: SessionSnapshot) -> tuple[int, int, int, int]:
    return (
        snapshot.input_tokens,
        snapshot.cached_input_tokens,
        snapshot.cache_write_tokens,
        snapshot.output_tokens,
    )


def plan_rows(
    snapshots: list[SessionSnapshot], previous: dict[str, dict | None]
) -> tuple[list[UsageRow], dict[str, dict], list[tuple[str, str]]]:
    """Baseline/delta rows plus the progress state to commit with them.

    ``previous`` maps ``session_id\\x1fprovider_id`` to the stored progress
    row (or None), which can come from an earlier provider of the same session.
    Rows keep stable identities when the same observation is replayed.
    """
    rows: list[UsageRow] = []
    next_state: dict[str, dict] = {}
    labels: list[tuple[str, str]] = []
    planned = {}
    for snapshot in sorted(snapshots, key=lambda item: item.observed_at):
        key = f"{snapshot.session_id}\x1f{snapshot.provider_id}"
        prev = planned.get(snapshot.session_id, previous.get(key))
        totals = _counts(snapshot)
        if prev and prev.get("ambiguous"):
            labels.append(("", "opencode_ambiguous_cumulative_checkpoint"))
            continue
        if prev and parse_iso_time(prev.get("observed_at")) and snapshot.observed_at < parse_iso_time(prev["observed_at"]):
            labels.append(("", "opencode_stale_cumulative_snapshot"))
            continue
        if prev is None:
            # Initial unattributed baseline: count once, label the coarseness.
            rows.append(
                UsageRow(
                    source=SOURCE,
                    tool_key=tool_key_for_provider(snapshot.provider_id),
                    model_key=canonical_model(snapshot.model_id),
                    occurred_at=snapshot.observed_at,
                    session_id=snapshot.session_id,
                    project=snapshot.project,
                    # Normalized storage: input includes fresh + cached reads.
                    input_tokens=snapshot.input_tokens + snapshot.cached_input_tokens,
                    cached_input_tokens=snapshot.cached_input_tokens,
                    cache_write_tokens=snapshot.cache_write_tokens,
                    cache_write_1h_tokens=0,
                    output_tokens=snapshot.output_tokens,
                    reasoning_tokens=snapshot.reasoning_tokens,
                    cost_usd=snapshot.cost_usd,
                    raw_id=stable_id(
                        "opencode-baseline",
                        snapshot.session_id,
                        snapshot.provider_id,
                        int(snapshot.observed_at.timestamp() * 1000),
                        totals,
                    ),
                )
            )
            labels.append((rows[-1].raw_id, BASELINE_ISSUE))
        else:
            prev_totals = tuple(prev[column] for column in COUNTER_COLUMNS)
            if any(now < was for now, was in zip(totals, prev_totals)):
                # This legacy shape has no reset/request identity. Appending
                # a second baseline would invent usage on corrections/copies.
                labels.append(("", "opencode_cumulative_decrease_requires_granular_evidence"))
                continue
            elif totals != prev_totals:
                delta_input = max(0, snapshot.input_tokens - prev["input_tokens"])
                delta_cached = max(0, snapshot.cached_input_tokens - prev["cached_input_tokens"])
                delta_write = max(0, snapshot.cache_write_tokens - prev["cache_write_tokens"])
                delta_output = max(0, snapshot.output_tokens - prev["output_tokens"])
                if delta_input + delta_cached + delta_write + delta_output > 0:
                    rows.append(
                        UsageRow(
                            source=SOURCE,
                            tool_key=tool_key_for_provider(snapshot.provider_id),
                            model_key=canonical_model(snapshot.model_id),
                            occurred_at=snapshot.observed_at,
                            session_id=snapshot.session_id,
                            project=snapshot.project,
                            input_tokens=delta_input + delta_cached,
                            cached_input_tokens=delta_cached,
                            cache_write_tokens=delta_write,
                            cache_write_1h_tokens=0,
                            output_tokens=delta_output,
                            reasoning_tokens=optional_delta(
                                snapshot.reasoning_tokens, prev.get("reasoning_tokens")
                            ) if delta_output else None,
                            cost_usd=cost_delta(snapshot.cost_usd, prev.get("cost_usd")),
                            raw_id=stable_id(
                                "opencode-delta",
                                snapshot.session_id,
                                snapshot.provider_id,
                                int(snapshot.observed_at.timestamp() * 1000),
                                totals,
                            ),
                        )
                    )
                    labels.append((rows[-1].raw_id, DELTA_ISSUE))
        planned[snapshot.session_id] = next_state[key] = {
            "input_tokens": snapshot.input_tokens,
            "cached_input_tokens": snapshot.cached_input_tokens,
            "cache_write_tokens": snapshot.cache_write_tokens,
            "output_tokens": snapshot.output_tokens,
            "reasoning_tokens": snapshot.reasoning_tokens,
            "cost_usd": snapshot.cost_usd,
            "model_id": snapshot.model_id,
            "observed_at": snapshot.observed_at.isoformat().replace("+00:00", "Z"),
        }
    return rows, next_state, labels


def optional_delta(current, earlier):
    if current is None or earlier is None or current < earlier:
        return None
    return current - earlier


def cost_delta(current, earlier):
    if current is None or earlier is None:
        return None
    return float(max(Decimal(0), Decimal(str(current)) - Decimal(str(earlier))))


def progress_for_location(connection, scope, session_id):
    """Look up the logical session checkpoint across providers and moves."""
    scoped_id = scope + ":" + session_id
    location, logical = {}, {}
    for row in connection.execute(
        "SELECT * FROM opencode_session_progress WHERE session_id IN (?,?)",
        (session_id, scoped_id),
    ):
        (logical if row["session_id"] == session_id else location)[row["provider_id"]] = dict(row)
    progress = {**location, **logical}
    if not progress:
        return None, logical
    by_time = [(parse_iso_time(row["observed_at"]), row) for row in progress.values()]
    if any(moment is None for moment, _ in by_time):
        return {"ambiguous": True}, logical
    most_recent = max(moment for moment, _ in by_time)
    candidates = [row for moment, row in by_time if moment == most_recent]
    high_water = tuple(max(row[col] for row in candidates) for col in COUNTER_COLUMNS)
    candidates = [row for row in candidates if tuple(row[col] for col in COUNTER_COLUMNS) == high_water]
    if not candidates:
        return {"ambiguous": True}, logical
    latest = candidates[0].copy()
    for key in ("reasoning_tokens", "cost_usd"):
        if any(row[key] != latest[key] for row in candidates):
            latest[key] = None
    return latest, logical


def _ingest_cumulative(*, database_path: Path, pricing: PricingEngine, source_database: Path,
                       snapshots: list[SessionSnapshot] | None = None) -> dict:
    from spend_app.connection_paths import identity
    import hashlib
    scope = hashlib.sha256(identity(source_database.resolve()).encode()).hexdigest()[:24]
    files = int(source_database.is_file())
    if snapshots is None:
        snapshots = read_session_snapshots(source_database) if files else []
    rows_by_id = {}
    next_state = {}
    labels = []
    issues = []
    deferred = set()
    repaired_sessions = set()
    history = {"historicalCorrections": 0, "historicalUnresolved": 0}

    def prepare(connection, _):
        from spend_app.adapters.opencode_granular import reconcile_cumulative, reattribute_cumulative
        from spend_app.adapters.opencode_cumulative_repair import (
            history_key, repair_provider_switch, repair_retained_provider_switches,
        )
        from spend_app.adapters.usage_repair import read_meta, write_meta

        # persist_rows starts an ingest_run before invoking this hook. Its
        # SQLite write transaction pins the checkpoint read through commit.
        previous = {}
        for snapshot in snapshots:
            session = snapshot.session_id
            latest, logical = progress_for_location(connection, scope, session)
            previous[f"{session}\x1f{snapshot.provider_id}"] = latest
            if logical and (len(logical) > 1 or snapshot.provider_id not in logical):
                key = history_key(session)
                status = read_meta(connection, key, None)
                if status is None:
                    if len(logical) > 1:
                        corrected = repair_provider_switch(connection, pricing, session, logical)
                        status = "unresolved" if corrected is None else "repaired"
                        history["historicalCorrections"] += corrected or 0
                        if corrected:
                            repaired_sessions.add(session)
                    else:
                        status = "current"
                    write_meta(connection, key, status)
                if status == "unresolved":
                    issues.append("opencode_prior_provider_switch_could_not_be_verified")
                    history["historicalUnresolved"] += 1
        corrected, repaired, unresolved, pending = repair_retained_provider_switches(connection, pricing)
        repaired_sessions.update(repaired)
        history["historicalCorrections"] += corrected
        if unresolved:
            issues.append("opencode_retained_provider_switches_could_not_be_verified")
            history["retainedHistoricalUnresolved"] = unresolved
        if pending:
            history["historicalRepairPending"] = True
        reattribute_cumulative(connection, snapshots)
        rows, planned, found = plan_rows(snapshots, previous)
        next_state.update(planned)
        labels.extend(found)
        issues.extend(issue for raw_id, issue in found if not raw_id)
        result = reconcile_cumulative(connection, rows, snapshots, issues, deferred)
        rows_by_id.update((row.raw_id, row) for row in result)
        return result

    def finalize(connection: sqlite3.Connection) -> None:
        overrides = TOOL_KEY_OVERRIDES.get() or {}
        for key, state in next_state.items():
            session_id, provider_id = key.split("\x1f", 1)
            if (session_id, provider_id) in deferred:
                continue
            write_opencode_progress(
                connection,
                session_id=session_id,
                provider_id=provider_id,
                input_tokens=state["input_tokens"],
                cached_input_tokens=state["cached_input_tokens"],
                cache_write_tokens=state["cache_write_tokens"],
                output_tokens=state["output_tokens"],
                reasoning_tokens=state["reasoning_tokens"],
                cost_usd=state["cost_usd"],
                model_id=state["model_id"],
                observed_at=state["observed_at"],
            )
            write_opencode_progress(
                connection,
                session_id=scope + ":" + session_id,
                provider_id=provider_id,
                input_tokens=state["input_tokens"],
                cached_input_tokens=state["cached_input_tokens"],
                cache_write_tokens=state["cache_write_tokens"],
                output_tokens=state["output_tokens"],
                reasoning_tokens=state["reasoning_tokens"],
                cost_usd=state["cost_usd"],
                model_id=state["model_id"],
                observed_at=state["observed_at"],
            )
        for raw_id, issue in labels:
            row = rows_by_id.get(raw_id)
            if row is None:
                continue
            record_coverage_gap(
                connection,
                raw_id=raw_id,
                source=SOURCE,
                tool_key=overrides.get(row.tool_key, row.tool_key),
                model_key=row.model_key,
                occurred_at=row.occurred_at.isoformat().replace("+00:00", "Z"),
                issue=issue,
            )
        sessions = repaired_sessions | {row.session_id for row in rows_by_id.values()}
        if sessions:
            from spend_app.adapters.traycer_local import reconcile_opencode_mirrors

            reconcile_opencode_mirrors(connection, sessions)

    result = persist_rows(
        database_path=database_path,
        pricing=pricing,
        source=SOURCE,
        usage_rows=(),
        prepare=prepare,
        finalize=finalize,
        issues=issues,
    )
    return {**result, **history, "files": files, "eventsSeen": len(snapshots), "eventsAccepted": len(snapshots) if not issues else result["eventsAccepted"], "issues": sorted(set(issues))}
