"""Repair older per-provider OpenCode totals only when retained rows prove them."""

from decimal import Decimal
from math import isclose

from spend_app.adapters.common import stable_id
from spend_app.adapters.local_common import parse_iso_time
from spend_app.adapters.opencode_local import (
    BASELINE_ISSUE, COUNTER_COLUMNS, DELTA_ISSUE, HISTORY_REPAIR, SOURCE,
)
from spend_app.adapters.usage_repair import delete_event, read_meta, write_meta
from spend_app.pricing import UnpricedModelError

ZERO_COUNTS = (0, 0, 0, 0)
REPAIR_SCAN = "opencode.cumulative-repair-scan:v2"
SCOPED_CHECKPOINT_PATTERN = "[0-9a-f]" * 24 + ":*"


def history_key(session):
    return HISTORY_REPAIR + stable_id(SOURCE, session).split(":", 1)[1]


def _unresolved_count(connection):
    return connection.execute(
        "SELECT COUNT(*) FROM app_meta WHERE key GLOB ? AND value=?",
        (HISTORY_REPAIR + "*", '"unresolved"'),
    ).fetchone()[0]


def repair_retained_provider_switches(connection, pricing, *, limit=64):
    """Reach closed sessions from retained evidence, in resumable batches."""
    state = read_meta(connection, REPAIR_SCAN, {"after": "", "done": False})
    corrected, repaired = 0, set()
    if state["done"]:
        return corrected, repaired, _unresolved_count(connection), False
    # Ignore the physical-location copies of the logical checkpoint rows.
    sessions = connection.execute(
        "SELECT session_id FROM opencode_session_progress "
        "WHERE session_id > ? AND session_id NOT GLOB ? "
        "GROUP BY session_id HAVING COUNT(*) > 1 ORDER BY session_id LIMIT ?",
        (state["after"], SCOPED_CHECKPOINT_PATTERN, limit),
    ).fetchall()
    for (session,) in sessions:
        key = history_key(session)
        if read_meta(connection, key, None) is None:
            checkpoints = {
                row["provider_id"]: dict(row)
                for row in connection.execute(
                    "SELECT * FROM opencode_session_progress WHERE session_id=?", (session,)
                )
            }
            count = repair_provider_switch(connection, pricing, session, checkpoints)
            write_meta(connection, key, "unresolved" if count is None else "repaired")
            if count:
                repaired.add(session)
                corrected += count
        state["after"] = session
    state["done"] = len(sessions) < limit
    write_meta(connection, REPAIR_SCAN, state)
    return corrected, repaired, _unresolved_count(connection), not state["done"]


def _checkpoints_share_source(connection, session_id, checkpoints):
    """Require one location to corroborate all terminal logical checkpoints."""
    per_location = {}
    for row in connection.execute(
        "SELECT * FROM opencode_session_progress WHERE session_id GLOB ? "
        "AND substr(session_id,25)=?",
        (SCOPED_CHECKPOINT_PATTERN, ":" + session_id),
    ):
        provider = row["provider_id"]
        logical = checkpoints.get(provider)
        if logical is not None and all(
            row[key] == logical[key] for key in (*COUNTER_COLUMNS, "cost_usd", "observed_at")
        ):
            per_location.setdefault(row["session_id"], set()).add(provider)
    return any(providers == checkpoints.keys() for providers in per_location.values())


def _historical_events(connection, session_id):
    events = []
    for table in ("usage_events", "unpriced_usage_events"):
        for row in connection.execute(
            f"SELECT e.*,g.issue FROM {table} e "
            "LEFT JOIN coverage_gap_events g ON g.raw_id=e.raw_id "
            "WHERE e.session_id=? AND e.source=?",
            (session_id, SOURCE),
        ):
            stamp = parse_iso_time(row["occurred_at"])
            if stamp is None:
                return None
            events.append((stamp, table, dict(row)))
    return sorted(events, key=lambda item: item[0])


def _reported_cost(value):
    return Decimal(str(value)) if value is not None else None


def _checkpoints_match(reconstructed, checkpoints):
    if set(reconstructed) != set(checkpoints):
        return False
    for provider, (counters, cost) in reconstructed.items():
        checkpoint = checkpoints[provider]
        if counters != tuple(checkpoint[key] for key in COUNTER_COLUMNS):
            return False
        expected = checkpoint["cost_usd"]
        if (cost is None) != (expected is None):
            return False
        if cost is not None and not isclose(cost, expected, rel_tol=1e-12, abs_tol=1e-12):
            return False
    return True


def repair_provider_switch(connection, pricing, session_id, checkpoints):
    """Replay the legacy deltas, preserving raw IDs and unknown histories.

    A legacy row's hash contains the provider and gross counters. Recompute
    each hash from the retained incremental observations, compare the end
    totals to all saved provider checkpoints, and only then replace deltas.
    """
    if not _checkpoints_share_source(connection, session_id, checkpoints):
        return None
    events = _historical_events(connection, session_id)
    if not events or len({stamp for stamp, _, _ in events}) != len(events):
        return None
    per_provider = {}
    repairs = []
    earlier = ZERO_COUNTS
    earlier_cost = earlier_reasoning = None
    for stamp, table, event in events:
        raw = event["raw_id"]
        prefix = raw.split(":", 1)[0]
        baseline = prefix == "opencode-baseline" and event["issue"] == BASELINE_ISSUE
        if not baseline and (prefix != "opencode-delta" or event["issue"] != DELTA_ISSUE):
            # Older formats, granular overlap, or another evidence path.
            return None
        incremental = (event["input_tokens"] - event["cached_input_tokens"],
                       event["cached_input_tokens"], event["cache_write_tokens"], event["output_tokens"])
        if min(incremental) < 0:
            return None
        matches = []
        for provider in checkpoints:
            prior = per_provider.get(provider)
            if baseline != (prior is None):
                continue
            totals = incremental if prior is None else tuple(a + b for a, b in zip(prior[0], incremental))
            if raw == stable_id(prefix, session_id, provider, int(stamp.timestamp() * 1000), totals):
                matches.append((provider, totals, prior))
        if len(matches) != 1:
            return None
        provider, totals, prior = matches[0]
        delta = tuple(now - then for now, then in zip(totals, earlier))
        observed_cost = _reported_cost(event["cost_usd"])
        total_cost = observed_cost
        if prior is not None:
            total_cost = observed_cost + prior[1] if observed_cost is not None and prior[1] is not None else None
        if min(delta) < 0 or (total_cost is not None and earlier_cost is not None and total_cost < earlier_cost):
            return None
        if per_provider:
            cost = total_cost - earlier_cost if total_cost is not None and earlier_cost is not None else None
            reasoning = event["reasoning_tokens"]
            if not delta[3] or earlier_reasoning is None or reasoning is None or reasoning < earlier_reasoning:
                reasoning = None
            else:
                reasoning -= earlier_reasoning
            # Same-provider increments are already correct. Only rewrite the
            # older row when switching changed its counters or reported cost.
            if delta != incremental or (cost is None) != (observed_cost is None) or (
                cost is not None and not isclose(cost, observed_cost, rel_tol=1e-12, abs_tol=1e-12)
            ):
                repairs.append((stamp, table, event, delta, cost, reasoning))
        earlier, earlier_cost, earlier_reasoning = totals, total_cost, event["reasoning_tokens"]
        per_provider[provider] = totals, total_cost
    if not _checkpoints_match(per_provider, checkpoints):
        return None

    # Validate all new prices before changing any rows. Unpriced observations
    # can be corrected without a price and promoted by the normal ingest path.
    ready = []
    for stamp, table, event, delta, cost, reasoning in repairs:
        fresh, cached, writes, output = delta
        if not any(delta) and not cost:
            ready.append((event["raw_id"], None, {}))
            continue
        changes = dict(input_tokens=fresh + cached, cached_input_tokens=cached,
                       cache_write_tokens=writes, output_tokens=output,
                       reasoning_tokens=reasoning, cost_usd=None if cost is None else float(cost))
        if table == "usage_events":
            try:
                changes["computed_cost_usd"] = float(pricing.compute(
                    model_key=event["model_key"], occurred_at=stamp, input_tokens=fresh + cached,
                    cached_input_tokens=cached, cache_write_tokens=writes, cache_write_1h_tokens=0,
                    output_tokens=output,
                ))
            except (UnpricedModelError, ValueError):
                return None
        ready.append((event["raw_id"], table, changes))
    for raw, table, changes in ready:
        if table is None:
            delete_event(connection, raw)
        else:
            setters = ",".join(f"{key}=?" for key in changes)
            connection.execute(f"UPDATE {table} SET {setters} WHERE raw_id=?", (*changes.values(), raw))
            connection.execute("UPDATE coverage_gap_events SET issue=? WHERE raw_id=?", (DELTA_ISSUE, raw))
    return len(ready)
