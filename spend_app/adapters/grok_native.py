"""Observed Grok session updates/signals, reduced without conversation content."""
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
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
                             inp, cached, writes, 0, out, reasoning, None,
                             stable_id("grok-native-usage", *key, ordinal), unsplit, complete))
    return rows, issues


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
    unified = [row for row in rows.values() if row.raw_id.startswith("grok-local:")]
    if unified:
        # updates.jsonl is a per-turn ledger. A unified line only replaces
        # the native row when session, time, and token counts are the same turn.
        fingerprints = {
            (row.session_id, row.occurred_at.replace(microsecond=0), row.input_tokens, row.output_tokens)
            for row in unified
        }
        for raw_id, row in list(rows.items()):
            turn = (row.session_id, row.occurred_at.replace(microsecond=0), row.input_tokens, row.output_tokens)
            if raw_id.startswith("grok-native-usage:") and turn in fingerprints:
                if raw_id in existing:
                    delete_event(connection, raw_id)
                del rows[raw_id]
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
