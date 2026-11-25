"""Claude message/request revisions, without upstream max-token heuristics.

Identity/layout evidence: Tokscale claudecode.rs at a3209ff and Token Monitor
Claude paths.js at e07aeb4. MIT notices are retained in NOTICE.
"""
from dataclasses import replace
from datetime import UTC, datetime

from spend_app.adapters.local_common import project_name
from spend_app.adapters.common import UsageRow, stable_id
from spend_app.adapters.event_identity import Observation
from spend_app.adapters.opencode_schema import count
from spend_app.source_health import quarantine
from spend_app.timeutil import parse_utc

PARSER_VERSION = "claude-message-5"
FIELDS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
DURATION_FIELDS = ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")


def merge_observations(group, *, saved=(), issues=None):
    """Merge reported fields, retaining their own revision rather than file age."""
    issues = issues if issues is not None else []
    values, ranks, conflicts = {}, {}, {}
    legacy_models = []
    stamps = [o.row.occurred_at for o in group]
    latest = max(group, key=lambda o: (o.revision, o.authority))
    latest_rank = (latest.revision, latest.authority)

    def merge_fields(fields, field_ranks, field_conflicts, fallback_rank):
        for name, conflict_rank in field_conflicts.items():
            rank = tuple(conflict_rank)
            if rank >= ranks.get(name, (-1, -1)):
                conflicts[name] = ranks[name] = rank
                values.pop(name, None)
        for name, value in fields.items():
            rank = tuple(field_ranks.get(name, fallback_rank))
            old_rank = ranks.get(name, (-1, -1))
            if rank < old_rank or name in conflicts and rank <= conflicts[name]:
                continue
            if rank == old_rank and name in values and values[name] != value:
                conflicts[name] = rank
                values.pop(name, None)
            else:
                values[name] = value
                conflicts.pop(name, None)
            ranks[name] = rank

    for record, existing in saved:
        record = record or {}
        components = dict(record.get("components", {}))
        stored_rank = record.get("rank", [0, 0, 0])
        fallback_rank = (stored_rank[0], stored_rank[2] if len(stored_rank) > 2 else 0)
        latest_rank = max(latest_rank, fallback_rank)
        saved_conflicts = record.get("field_conflicts", {})
        if existing:
            old_time = parse_utc(existing["occurred_at"])
            if old_time:
                stamps.append(old_time)
            if "model" not in components and "model" not in saved_conflicts:
                old_model = existing.get("model_key")
                if old_model and old_model != "unknown":
                    legacy_models.append((fallback_rank, old_model))
            if existing.get("telemetry_complete", True):
                seed = dict(input_tokens=existing["input_tokens"] - existing["cached_input_tokens"],
                            cache_read_input_tokens=existing["cached_input_tokens"],
                            cache_creation_input_tokens=existing["cache_write_tokens"],
                            output_tokens=existing["output_tokens"])
                # A pre-identity positive stored duration is evidence. The
                # legacy default zero is not, nor is a duration reconstructed
                # from a newer identity record's saved component fields.
                if not record.get("components") and existing.get("cache_write_1h_tokens", 0) > 0:
                    seed["ephemeral_1h_input_tokens"] = existing["cache_write_1h_tokens"]
                for name, value in seed.items():
                    if name not in saved_conflicts:
                        components.setdefault(name, value)
        merge_fields(components, record.get("field_ranks", {}), saved_conflicts, fallback_rank)
    for item in group:
        merge_fields(dict(item.components), dict(item.field_ranks), dict(item.field_conflicts),
                     (item.revision, item.authority))

    model = values.get("model", "unknown")
    if legacy_models and "model" not in conflicts:
        legacy_rank, legacy_model = max(legacy_models)
        if ranks.get("model", (-1, -1)) < legacy_rank:
            # The old parser stored no model-specific rank. Keep its value
            # until a replay supplies evidence as recent as the saved record.
            model = legacy_model
            values.pop("model", None)
            ranks.pop("model", None)
    if conflicts:
        issues.append("claude_equal_authority_field_conflict")

    write = values.get("cache_creation_input_tokens")
    five, hour = values.get("ephemeral_5m_input_tokens"), values.get("ephemeral_1h_input_tokens")
    duration_fields = ("cache_creation_input_tokens", *DURATION_FIELDS)
    malformed = any(name in values and values[name] is None for name in duration_fields)
    if write is None and five is not None and hour is not None and "cache_creation_input_tokens" not in conflicts:
        write = five + hour
    invalid_duration = (malformed or write is not None and (
        hour is not None and hour > write or five is not None and five > write
        or hour is not None and five is not None and hour + five != write))
    if invalid_duration:
        issues.append("claude_cache_duration_conflict")
        hour = None
    elif hour is None and write is not None and five is not None and "ephemeral_1h_input_tokens" not in conflicts:
        hour = write - five
    if write is not None and write > 0 and hour is None and not invalid_duration:
        issues.append("claude_cache_duration_unknown")
    complete = (all(name in values for name in ("input_tokens", "cache_read_input_tokens", "output_tokens"))
                and write is not None and (write == 0 or hour is not None)
                and not conflicts and not invalid_duration)
    measured_write = write if write is not None else (five or 0) + (hour or 0)
    inp = values.get("input_tokens", 0) + values.get("cache_read_input_tokens", 0)
    output = values.get("output_tokens", 0)
    row = replace(latest.row, model_key=model, occurred_at=min(stamps), input_tokens=inp,
                  cached_input_tokens=values.get("cache_read_input_tokens", 0), cache_write_tokens=measured_write,
                  cache_write_1h_tokens=hour or 0, output_tokens=output, telemetry_complete=complete,
                  unclassified_tokens=max(0, values.get("total_tokens", 0) - inp - measured_write - output) if not complete else 0)
    aliases = tuple(dict.fromkeys(alias for o in group for alias in o.aliases))
    envelopes = tuple(dict.fromkeys(envelope for o in group for envelope in o.request_envelopes))
    return replace(latest, row=row, aliases=aliases, revision=latest_rank[0], authority=latest_rank[1],
                   components=tuple(values.items()), field_ranks=tuple(ranks.items()),
                   field_conflicts=tuple(conflicts.items()), request_envelopes=envelopes)


def reduce_records_into(records, health, fallback_session, *, observations: dict, known_scope: dict, session: dict) -> set[str]:
    """Fold ``records`` into existing reducer state; return touched observation keys."""
    touched: set[str] = set()
    for outer in records:
        message = outer.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("usage"), dict) or outer.get("type") not in {None, "assistant"}:
            continue
        if message.get("model") == "<synthetic>":
            continue
        message_id = message.get("id")
        request_id = outer.get("requestId")
        if not isinstance(message_id, str) or not message_id:
            message_id = None
        if not isinstance(request_id, str) or not request_id:
            request_id = None
        legacy_message = message_id or outer.get("uuid")
        if not message_id and not request_id and not isinstance(legacy_message, str):
            health.note(quarantine("claude_request_identity_missing"))
            continue
        identity = (message_id, request_id or legacy_message)
        reported_provider = outer.get("providerId") or outer.get("provider_id") or message.get("providerId") or message.get("provider_id")
        if not isinstance(reported_provider, str) or not reported_provider:
            reported_provider = None
        provider = reported_provider or known_scope.get(identity, "native")
        known_scope[identity] = provider
        key = stable_id("claude-message", provider, message_id or "request", request_id or legacy_message)
        previous = observations.get(key)
        when = parse_utc(str(outer.get("timestamp") or ""))
        if when is None and previous:
            when = datetime.fromtimestamp(previous.revision, UTC)
        if when is None:
            health.note(quarantine("claude_usage_time_missing"))
            continue
        if previous and when.timestamp() < previous.revision:
            health.note(quarantine("claude_older_usage_revision"))
        usage = message["usage"]
        values = {}
        for name in FIELDS:
            value = count(usage.get(name))
            if value is not None or name == "cache_creation_input_tokens" and usage.get(name) is not None:
                values[name] = value
        cache = usage.get("cache_creation")
        if isinstance(cache, dict):
            for name in DURATION_FIELDS:
                if cache.get(name) is not None:
                    values[name] = count(cache[name])
        if cache is not None and not isinstance(cache, dict):
            health.note(quarantine("claude_optional_cache_metadata_invalid"))
        details = usage.get("output_tokens_details")
        reasoning = count(details.get("thinking_tokens")) if isinstance(details, dict) else None
        if reasoning is None and previous:
            reasoning = previous.row.reasoning_tokens
        model = message.get("model")
        if isinstance(model, str) and model and model != "unknown":
            values["model"] = model
        sid = outer.get("sessionId")
        sid = sid if isinstance(sid, str) and sid else fallback_session
        cwd = outer.get("cwd")
        project = project_name(cwd) if isinstance(cwd, str) else previous.row.project if previous else None
        aliases = (f"claude-local:{sid}:{legacy_message}",) if legacy_message else ()
        envelope = outer.get("uuid")
        request_envelopes = ()
        if isinstance(envelope, str) and envelope and isinstance(legacy_message, str):
            scope = stable_id("claude-envelope-v2", sid, legacy_message, envelope)
            fallback = stable_id("claude-message", provider, message_id or "request", legacy_message)
            provider_evidence = stable_id("claude-provider", reported_provider) if reported_provider else None
            request_envelopes = ((scope, provider_evidence, fallback, key if request_id else None),)
        total = count(usage.get("total_tokens"))
        if total is not None:
            values["total_tokens"] = total
        row = UsageRow(source="claude_local", tool_key="claude-code", model_key="unknown",
                       occurred_at=when, session_id=sid, project=project,
                       input_tokens=0, cached_input_tokens=0, cache_write_tokens=0,
                       cache_write_1h_tokens=0, output_tokens=0, reasoning_tokens=reasoning,
                       cost_usd=None, raw_id=key)
        incoming = Observation(row, aliases, when.timestamp(), "claude_message", int(bool(message.get("stop_reason"))),
                               tuple(values.items()), request_envelopes=request_envelopes)
        errors = []
        observations[key] = merge_observations(([previous] if previous else []) + [incoming], issues=errors)
        touched.add(key)
        for error in errors:
            health.note(quarantine(error))
        health.note_ok()
        if not session:
            session.update({"id": sid, "project": project, "started_at": when,
                            "model_key": observations[key].row.model_key})
    return touched


def reduce_records(records, health, fallback_session):
    observations, known_scope = {}, {}
    session = {}
    reduce_records_into(records, health, fallback_session, observations=observations, known_scope=known_scope, session=session)
    return session, list(observations.values())
