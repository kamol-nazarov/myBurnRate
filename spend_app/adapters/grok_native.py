"""Observed Grok session updates/signals, reduced without conversation content."""
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from spend_app.adapters.common import UsageRow, stable_id
from spend_app.adapters.local_common import parse_millis
from spend_app.adapters.opencode_schema import count
from spend_app.adapters.usage_repair import read_meta, write_meta, delete_event
from spend_app.connection_paths import source_files, confined


def files(root, budget=100000):
    from spend_app.connection_paths import source_files
    root = Path(root)
    patterns = ("logs/unified.jsonl", "sessions/*/*/updates.jsonl", "sessions/*/*/signals.json") if root.name == ".grok" or (root / "logs").is_dir() else ("**/updates.jsonl", "**/signals.json", "unified.jsonl")
    return source_files(root, patterns, budget)


def reduce_updates(records):
    rows, issues, positions = [], [], {}
    for record in records:
        params = record.get("params", {}) if isinstance(record, dict) else {}
        update, meta = params.get("update", {}), params.get("_meta", {})
        if not isinstance(update, dict) or not isinstance(meta, dict):
            continue
        usage, sid = update.get("usage"), params.get("sessionId")
        if not isinstance(usage, dict):
            continue
        stamp = parse_millis(meta.get("agentTimestampMs"))
        if not sid or not stamp:
            issues.append("grok_native_identity_or_time_unavailable")
            continue
        def get(*keys):
            return next((count(usage[key]) for key in keys if key in usage), None)
        inp = get("inputTokens", "input_tokens", "promptTokens")
        out = get("outputTokens", "output_tokens", "completionTokens")
        cached = get("cachedReadTokens", "cacheReadTokens", "cache_read_input_tokens")
        writes = get("cachedWriteTokens", "cacheWriteTokens", "cacheCreationTokens", "cache_creation_input_tokens")
        reasoning = get("reasoningTokens", "thoughtTokens", "thinkingTokens")
        total = get("totalTokens", "total_tokens")
        complete = all(v is not None for v in (inp, out, cached, writes))
        unsplit = 0
        if inp is None or out is None:
            if total is None:
                issues.append("grok_native_usage_unavailable")
                continue
            unsplit, inp, out, cached, writes, reasoning, complete = total, 0, 0, 0, 0, None, False
        cached, writes = cached or 0, writes or 0
        if cached > inp or reasoning is not None and reasoning > out:
            issues.append("invalid_grok_native_components")
            continue
        models = usage.get("modelUsage")
        model = next(iter(models)) if isinstance(models, dict) and len(models) == 1 else "unknown"
        key = (sid, stamp.isoformat(), meta.get("eventId"))
        ordinal = positions.get(key, 0)
        positions[key] = ordinal + 1
        rows.append(UsageRow("grok_local", "grok", "supergrok:" + model.lower(), stamp, sid, None,
                             inp, cached, writes, 0, out, reasoning, turn_charge(usage),
                             stable_id("grok-native-usage", *key, ordinal), unsplit, complete))
    return rows, issues


TICKS_PER_USD = 10_000_000_000


def turn_charge(usage):
    """Grok Build's own charge for a turn, in USD, or None.

    costUsdTicks (1 USD = 1e10 ticks) is the sum of the turn's model calls at
    xAI's published rates, each call on its own long-context tier. A turn
    record adds several calls together, so this is the only exact price for
    it: the same rates applied to the summed prompt would wrongly put the
    whole turn on the long-context tier.
    """
    ticks = usage.get("costUsdTicks")
    if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 0:
        return None
    return ticks / TICKS_PER_USD


CALL_PREFIX = "grok-local:"
TURN_PREFIX = "grok-native-usage:"
CLOCK_SLACK = timedelta(seconds=2)


def covered_calls(calls, turns):
    """IDs of per-call rows that a completed turn record already counts.

    calls and turns are (id, when, input_tokens, output_tokens) for one
    session. unified.jsonl logs every model call; updates.jsonl logs one
    total per completed turn. A turn covers the run of calls that ends at the
    turn and, walking back, adds up to its input and output; it never reaches
    back past the previous turn. Calls of a turn that never completed (the
    CLI was stopped, or the turn is still running) stay counted.
    """
    pending = sorted(calls, key=lambda call: call[1])
    covered = set()
    previous_end = None
    for _turn_id, end, turn_input, turn_output in sorted(turns, key=lambda turn: turn[1]):
        candidates = [call for call in pending if call[0] not in covered and call[1] <= end + CLOCK_SLACK
                      and (previous_end is None or call[1] > previous_end + CLOCK_SLACK)]
        total_input = total_output = 0
        for call_id, _when, call_input, call_output in reversed(candidates):
            if total_input + call_input > turn_input or total_output + call_output > turn_output:
                break
            total_input += call_input
            total_output += call_output
            covered.add(call_id)
            if total_input == turn_input and total_output == turn_output:
                break
        previous_end = end
    return covered


def drop_covered_calls(connection, session_ids=None) -> int:
    """Delete stored per-call Grok rows that a turn row in the same session counts.

    Works on any database, including a hub holding rows pushed by computers
    on older versions (ids prefixed ``m:<machine>:``). Returns rows deleted.
    """
    from spend_app.sync import strip_machine_prefix
    from spend_app.timeutil import parse_utc

    query = ("SELECT raw_id, session_id, occurred_at, input_tokens, output_tokens FROM {table} "
             "WHERE tool_key='grok' AND session_id IS NOT NULL")
    params: tuple = ()
    if session_ids is not None:
        sessions = sorted({sid for sid in session_ids if sid})
        if not sessions:
            return 0
        query += f" AND session_id IN ({','.join('?' for _ in sessions)})"
        params = tuple(sessions)
    by_session: dict = {}
    for table in ("usage_events", "unpriced_usage_events"):
        for raw_id, sid, at, tokens_in, tokens_out in connection.execute(query.format(table=table), params):
            local_id = strip_machine_prefix(raw_id)
            kind = "calls" if local_id.startswith(CALL_PREFIX) else "turns" if local_id.startswith(TURN_PREFIX) else None
            when = parse_utc(at)
            if kind and when is not None:
                by_session.setdefault(sid, {"calls": [], "turns": []})[kind].append((raw_id, when, tokens_in, tokens_out))
    removed = 0
    for groups in by_session.values():
        if not groups["calls"] or not groups["turns"]:
            continue
        for raw_id in covered_calls(groups["calls"], groups["turns"]):
            delete_event(connection, raw_id)
            removed += 1
    return removed


def signal_row(payload, sid, observed_at):
    if not isinstance(payload, dict):
        raise ValueError("invalid_grok_signals")
    before = count(payload.get("totalTokensBeforeCompaction", 0))
    total = count(payload.get("totalTokens"))
    context = count(payload.get("contextTokensUsed"))
    # Current signals.json stores context window stats, not a spend total.
    if total is None:
        return None
    if before is None:
        raise ValueError("invalid_grok_signals")
    # Observed producer contract: total includes compaction when context is
    # present; otherwise total is the post-compaction segment.
    value = max(total, before + context) if context is not None else before + total
    return UsageRow("grok_local", "grok", "supergrok:unknown", observed_at, sid, None,
                    0, 0, 0, 0, 0, None, None, stable_id("grok-native-total", sid), value, False)


def _read_file(file, parse_log, issues):
    """Rows for one source file, a pure function of its path and content.

    Issues are appended to ``issues`` as they are found, so a read that fails
    part-way still reports what it saw, as the uncached reader always did.
    """
    if file.name == "unified.jsonl":
        parsed, state = parse_log(file)
        issues.extend(state.get("issues", []))
        return parsed
    if file.name == "signals.json":
        with confined(file).open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        row = signal_row(payload, file.parent.name, datetime.fromtimestamp(file.stat().st_mtime, UTC))
        return [row] if row is not None else []
    records = []
    with confined(file).open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                records.append(json.loads(line))
            except ValueError:
                issues.append("malformed_grok_native_metadata")
    parsed, errors = reduce_updates(records)
    issues.extend(errors)
    return parsed


def read_source(root, listed=None):
    """Rows and issues from every source file, in listing order.

    An active session appends to its updates.jsonl and the shared unified log
    on every turn, so some file changes on almost every pass. Each file's parse
    is cached against its signature, taken before the read, so a pass re-reads
    only the files that changed instead of every session ever recorded.
    """
    from spend_app.adapters.grok_local import parse_log
    from spend_app.connection_paths import file_signature
    rows, issues, found = [], [], 0
    seen = set()
    for file in (files(root) if listed is None else listed):
        found += 1
        key = str(file)
        seen.add(key)
        try:
            signature = file_signature(file)
        except (OSError, ValueError):
            signature = None
        cached = _FILES.get(key)
        if signature is not None and cached is not None and cached[0] == signature:
            rows.extend(cached[1])
            issues.extend(cached[2])
            continue
        file_issues = []
        try:
            parsed = _read_file(file, parse_log, file_issues)
        except (OSError, ValueError):
            _FILES.pop(key, None)
            issues.extend(file_issues)
            issues.append("grok_native_source_unavailable")
            continue
        rows.extend(parsed)
        issues.extend(file_issues)
        if signature is not None:
            _FILES[key] = (signature, tuple(parsed), tuple(file_issues))
    prefix = os.path.join(str(root), "")
    for key in [key for key in _FILES if key.startswith(prefix) and key not in seen]:
        _FILES.pop(key, None)
    return rows, issues, found


# path -> (file signature, parsed rows, issues) for read_source.
_FILES: dict[str, tuple] = {}
# reconcile cache key -> {"sessions": {sid: (incoming, output, issues)},
#                         "traycer": bool | None, "full_at": monotonic seconds}
_RECONCILED: dict[tuple, dict] = {}


def reset_state() -> None:
    _FILES.clear()
    _RECONCILED.clear()


def session_memo(key):
    """Working reconcile memo for one pass.

    A session whose incoming rows equal those of the last committed pass, with
    the same Traycer scope, reconciles to the same rows, so its previous output
    is reused instead of re-reading its stored events. Like the unchanged-pass
    guard, every session is fully re-reconciled at least every FORCE_SECONDS,
    so a stored-row change the inputs cannot show is picked up within minutes.
    """
    import time
    from spend_app.adapters.unchanged import FORCE_SECONDS
    now = time.monotonic()
    cached = _RECONCILED.get(key) if key is not None else None
    if cached is None or now - cached["full_at"] >= FORCE_SECONDS:
        return {"sessions": {}, "traycer": None, "full_at": now}
    return {"sessions": dict(cached["sessions"]), "traycer": cached["traycer"], "full_at": cached["full_at"]}


def remember_sessions(key, memo) -> None:
    """Commit a pass's memo once its transaction committed."""
    if key is None or memo is None or "next" not in memo:
        return
    _RECONCILED[key] = {"sessions": memo["next"], "traycer": memo["traycer"], "full_at": memo["full_at"]}


def reconcile(connection, incoming, issues, memo=None):
    # A Traycer chat ID is not a Grok session ID. A native cumulative signal
    # has no start boundary with which to prove its otherwise-uncovered share.
    # Disabling Traycer preserves that history; it must not make a new signal
    # safe to layer over it. Preserve existing measurements and report the gap.
    traycer_history = any(connection.execute(
        f"SELECT 1 FROM {table} WHERE source=? AND tool_key=? LIMIT 1", ("traycer_local", "grok")
    ).fetchone() for table in ("usage_events", "unpriced_usage_events"))
    if traycer_history and any(row.raw_id.startswith("grok-native-total:") for row in incoming):
        issues.append("grok_native_total_traycer_history_scope_unproven")
        incoming = [row for row in incoming if not row.raw_id.startswith("grok-native-total:")]
    if memo is not None:
        if memo["traycer"] is not None and memo["traycer"] != traycer_history:
            memo["sessions"] = {}
        memo["traycer"] = traycer_history
    by_session: dict = {}
    for row in incoming:
        by_session.setdefault(row.session_id, []).append(row)
    output, reconciled = [], {}
    for sid, session_rows in by_session.items():
        session_rows = tuple(session_rows)
        previous = memo["sessions"].get(sid) if memo is not None else None
        if previous is not None and previous[0] == session_rows:
            rows, notes = previous[1], previous[2]
        else:
            notes = []
            rows = tuple(_reconcile_session(connection, sid, session_rows, notes))
            notes = tuple(notes)
        issues.extend(notes)
        output.extend(rows)
        reconciled[sid] = (session_rows, rows, notes)
    if memo is not None:
        memo["next"] = reconciled
    return output


def _reconcile_session(connection, sid, incoming, issues):
    from spend_app.adapters.opencode_granular import row_from_record
    key = "grok.native.v1:" + stable_id(sid)
    state = read_meta(connection, key, {})
    existing = {}
    for table in ("usage_events", "unpriced_usage_events"):
        for record in connection.execute(f"SELECT * FROM {table} WHERE source=? AND session_id=?", ("grok_local", sid)):
            row = row_from_record(dict(record))
            existing[row.raw_id] = row
    rows = dict(existing)
    for row in incoming:
        if row.session_id == sid:
            rows[row.raw_id] = row
    # updates.jsonl is the per-turn ledger and carries Grok Build's own charge;
    # a per-call row from unified.jsonl that a turn already counts is dropped.
    calls = [(row.raw_id, row.occurred_at, row.input_tokens, row.output_tokens)
             for row in rows.values() if row.raw_id.startswith(CALL_PREFIX)]
    turns = [(row.raw_id, row.occurred_at, row.input_tokens, row.output_tokens)
             for row in rows.values() if row.raw_id.startswith(TURN_PREFIX)]
    for raw_id in covered_calls(calls, turns):
        if raw_id in existing:
            delete_event(connection, raw_id)
        del rows[raw_id]
    turn_models = {row.model_key for row in rows.values() if row.raw_id.startswith(TURN_PREFIX)} - {"supergrok:unknown"}
    for raw_id, row in list(rows.items()):
        # The unified log loses its "model changed" lines when the CLI trims
        # it; a call left without a model takes the one model its session's
        # completed turns name.
        if row.raw_id.startswith(CALL_PREFIX) and row.model_key == "supergrok:unknown" and len(turn_models) == 1:
            rows[raw_id] = replace(row, model_key=next(iter(turn_models)))
        # A turn stored before its charge was read is re-inserted, not updated,
        # so the corrected row syncs to the hub again.
        stored = existing.get(raw_id)
        if (stored is not None and raw_id.startswith(TURN_PREFIX) and stored.cost_usd is None
                and row.cost_usd is not None):
            delete_event(connection, raw_id)
    coarse_id = stable_id("grok-native-total", sid)
    coarse = next((r for r in incoming if r.raw_id == coarse_id), None)
    if coarse and state and coarse.unclassified_tokens < state["total"]:
        issues.append("grok_signal_decrease_without_revision_evidence")
        coarse = None
    if coarse and (not state or coarse.unclassified_tokens > state["total"] and coarse.occurred_at.timestamp() >= state["observed"]):
        state = {"total": coarse.unclassified_tokens, "observed": coarse.occurred_at.timestamp()}
    if state:
        stamp = datetime.fromtimestamp(state["observed"], UTC)
        covered = sum(r.input_tokens + r.output_tokens + r.cache_write_tokens + r.unclassified_tokens
                      for r in rows.values() if r.raw_id != coarse_id and r.occurred_at <= stamp)
        remainder = max(0, state["total"] - covered)
        template = coarse or rows.get(coarse_id)
        if template and remainder:
            rows[coarse_id] = replace(template, unclassified_tokens=remainder, occurred_at=stamp)
        elif coarse_id in rows:
            if coarse_id in existing:
                delete_event(connection, coarse_id)
            del rows[coarse_id]
        write_meta(connection, key, state)
    return rows.values()
