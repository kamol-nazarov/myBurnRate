"""Atomic, exact-ID compatibility for repaired event parsers."""
from collections import defaultdict
from dataclasses import dataclass, replace

from spend_app.adapters.common import stable_id
from spend_app.adapters.usage_repair import delete_event, read_meta, write_meta


@dataclass(frozen=True)
class Observation:
    row: object
    aliases: tuple
    revision: float
    format: str
    authority: int = 0
    components: tuple = ()
    field_ranks: tuple = ()
    field_conflicts: tuple = ()
    request_envelopes: tuple = ()


def stored_event(connection, raw_id):
    for table in ("usage_events", "unpriced_usage_events"):
        row = connection.execute(f"SELECT * FROM {table} WHERE raw_id=?", (raw_id,)).fetchone()
        if row:
            return dict(row)
    return None


def _identity_key(source, canonical):
    return "usage.identity.v1:" + stable_id(source, canonical)


def _alias_key(source, alias):
    return "usage.alias.v1:" + stable_id(source, alias)


def _claude_fallback_key(raw_id):
    return _identity_key("claude_local", stable_id("claude-fallback-v2", raw_id))


def _bounded_claude_envelope(evidence):
    """Keep enough observations to recognize incompatible provider or request IDs."""
    by_provider = defaultdict(list)
    for item in sorted(evidence, key=lambda item: (item[0] or "", item[2] or "", item[1])):
        by_provider[item[0]].append(item)
    providers = sorted(provider for provider in by_provider if provider is not None)[:2]
    return [list(item) for provider in (None, *providers)
            for item in by_provider.get(provider, ())[:3]]


def _link_claude_requests(connection, observations, issues):
    """Link a missing request ID only through its observed assistant envelope."""
    source = "claude_local"
    envelopes = defaultdict(set)
    for item in observations:
        for scope, provider, fallback, explicit in item.request_envelopes:
            envelopes[scope].add((provider, fallback, explicit))

    candidates = defaultdict(set)
    fallback_scopes = defaultdict(set)
    blocked = set()
    for scope, current in envelopes.items():
        key = _identity_key(source, scope)
        stored = read_meta(connection, key, [])
        seen = {tuple(item) for item in stored} | current
        providers = {provider for provider, _, _ in seen if provider is not None}
        known_requests = [(provider, explicit) for provider, _, explicit in seen if explicit is not None]
        for provider, fallback, explicit in seen:
            if explicit is not None:
                continue
            fallback_scopes[fallback].add(scope)
            for other_provider, request in known_requests:
                if provider == other_provider or (len(providers) <= 1 and None in (provider, other_provider)):
                    candidates[fallback].add((scope, request))
                elif None in (provider, other_provider):
                    blocked.add(fallback)
        state = _bounded_claude_envelope(seen)
        if state != stored:
            write_meta(connection, key, state)

    redirects = {}
    for fallback, scopes in fallback_scopes.items():
        # The fallback key predates scoped UUIDs. If different envelopes
        # contributed to that same row, one match cannot identify the whole row.
        scope_key = _claude_fallback_key(fallback)
        previous = read_meta(connection, scope_key, {})
        known_scopes = sorted(scopes | set(previous.get("scopes", ())))[:2]
        options = candidates[fallback]
        explicit = next(iter(options))[1] if len(options) == 1 else None
        owner = read_meta(connection, _alias_key(source, fallback), None) if options else None
        ambiguous = (fallback in blocked or previous.get("ambiguous") or len(options) > 1
                     or bool(options) and (len(known_scopes) != 1 or owner not in (None, fallback, explicit)))
        state = {"scopes": known_scopes, "ambiguous": bool(ambiguous)}
        if state != previous:
            write_meta(connection, scope_key, state)
        if ambiguous:
            blocked.add(fallback)
            issues.append("claude_envelope_request_ambiguity")
        elif explicit:
            redirects[fallback] = explicit

    # A formerly proven link is durable. Contrary evidence prevents the same
    # fallback from rewriting either explicit request or reviving a third row.
    normalized, former_ids = [], defaultdict(list)
    for item in observations:
        canonical = item.row.raw_id
        if item.row.source == source:
            explicit = redirects.get(canonical) or read_meta(connection, _alias_key(source, canonical), None)
            if explicit and explicit != canonical:
                if canonical in blocked or read_meta(connection, _claude_fallback_key(canonical), {}).get("ambiguous"):
                    issues.append("claude_envelope_request_ambiguity")
                    continue
                redirects[canonical] = explicit
                item = replace(item, row=replace(item.row, raw_id=explicit))
        normalized.append(item)
    for fallback, explicit in redirects.items():
        if fallback != explicit:
            former_ids[explicit].append(fallback)
    return normalized, former_ids


def reconcile(connection, observations, issues=None, *, claude_models=None):
    issues = issues if issues is not None else []
    linked = []
    for observation in observations:
        raw_id = observation.row.raw_id
        if observation.row.source == "codex_local" and raw_id.startswith("codex-counter:"):
            owner = read_meta(connection, "usage.alias.v1:" + stable_id("codex_local", raw_id), None)
            if isinstance(owner, str) and owner.startswith("codex-request:"):
                observation = replace(observation, row=replace(observation.row, raw_id=owner),
                    aliases=observation.aliases + (raw_id,),
                    components=observation.components + (("request_equivalent_counter", raw_id),))
        linked.append(observation)
    observations = guard_counter_fragments(connection, linked, issues)
    observations, claude_former_ids = _link_claude_requests(connection, observations, issues)
    grouped = {}
    claude_groups = {}
    for observation in observations:
        canonical = observation.row.raw_id
        if observation.row.source == "claude_local":
            claude_groups.setdefault(canonical, []).append(observation)
            continue
        previous = grouped.get(canonical)
        aliases = tuple(dict.fromkeys((previous.aliases if previous else ()) + observation.aliases))
        rank = (observation.revision, observation.row.telemetry_complete, observation.authority)
        old_rank = (previous.revision, previous.row.telemetry_complete, previous.authority) if previous else None
        if previous and rank == old_rank and replace(observation.row, project=None, session_id=None) != replace(previous.row, project=None, session_id=None):
            issues.append("conflicting_equal_revision_copies")
        if not previous or rank > old_rank or rank == old_rank and observation.row.source == "codex_local" and dict(observation.components).get("request_equivalent_counter"):
            grouped[canonical] = replace(observation, aliases=aliases)
        else:
            grouped[canonical] = replace(previous, aliases=aliases)
    from spend_app.adapters.claude_records import merge_observations
    grouped.update({key: merge_observations(group, issues=issues) for key, group in claude_groups.items()})
    rows = []
    for canonical, observation in grouped.items():
        source = observation.row.source
        namespace = source
        allowed_sources = {source}
        key = _identity_key(namespace, canonical)
        record = read_meta(connection, key, None)
        target = record["raw_id"] if record else canonical
        found = []
        former_ids = claude_former_ids.get(canonical, [])
        if source == "claude_local":
            existing = stored_event(connection, target)
            saved = [(record, existing)]
            for former_id in former_ids:
                previous_record = read_meta(connection, _identity_key(source, former_id), None)
                previous_target = previous_record["raw_id"] if previous_record else former_id
                previous_event = stored_event(connection, previous_target)
                if previous_event and previous_event["source"] == source:
                    found.append(previous_target)
                    if previous_target != target:
                        saved.append((previous_record, previous_event))
            observation = merge_observations([observation], saved=saved, issues=issues)
        rank = [observation.revision, int(observation.row.telemetry_complete), observation.authority]
        if source != "claude_local" and record and rank < record["rank"]:
            issues.append("older_usage_revision_ignored")
            continue
        for alias in dict.fromkeys((canonical, *former_ids, *observation.aliases)):
            owner_key = _alias_key(namespace, alias)
            owner = read_meta(connection, owner_key, None)
            if owner is not None and owner != canonical:
                proven_counter = source == "codex_local" and dict(observation.components).get("request_equivalent_counter") == alias
                if not ((proven_counter or alias in former_ids) and owner == alias):
                    if proven_counter:
                        issues.append("codex_counter_request_ownership_conflict")
                    continue
                old_identity = read_meta(connection, _identity_key(source, owner), None)
                if old_identity:
                    old_target = old_identity["raw_id"]
                    old_event = stored_event(connection, old_target)
                    if old_event and old_event["source"] == source:
                        found.append(old_target)
            existing = stored_event(connection, alias)
            if existing and existing["source"] in allowed_sources:
                found.append(alias)
            write_meta(connection, owner_key, canonical)
        if not record and found:
            # Keep a stored 0.3.0 ID instead of minting another accepted row.
            target = found[0]
        for alias in found:
            if alias != target:
                delete_event(connection, alias)
        row = replace(observation.row, raw_id=target)
        rows.append(row)
        components = dict(record.get("components", {})) if record else {}
        components.update(dict(observation.components))
        if source == "claude_local":
            components = dict(observation.components)
            if claude_models is not None:
                model_rank = dict(observation.field_ranks).get("model")
                if model_rank is None and row.model_key != "unknown":
                    model_rank = (observation.revision, observation.authority)
                previous = claude_models.get(row.session_id)
                if model_rank is not None and (previous is None or model_rank >= previous[0]):
                    claude_models[row.session_id] = (model_rank, row.model_key)
        write_meta(connection, key, {"raw_id": target, "rank": rank, **({"components": components} if components else {}),
            **({"field_ranks": dict(observation.field_ranks), "field_conflicts": dict(observation.field_conflicts)} if source == "claude_local" else {})})
    return rows


def guard_counter_fragments(connection, observations, issues):
    """Do not layer truncated cumulative prefixes over accepted intervals.

    Complete copied rollouts have identical adjacent intervals. When a fragment
    starts inside another accepted interval, counter identity proves overlap
    but not its historical per-model split. Preserve accepted history and make
    this limitation explicit instead of fabricating a granular allocation.
    """
    observations = list(observations)
    states, accepted = {}, []
    grouped = {}
    for observation in observations:
        scope = dict(observation.components).get("counter_scope")
        if scope:
            grouped.setdefault("codex.counter-ranges.v1:" + scope, []).append(observation)
    replacements, ignored = {}, set()
    for key, group in grouped.items():
        ranges = states.setdefault(key, read_meta(connection, key, {}))
        for item in group:
            details = dict(item.components)
            alias = details.get("request_equivalent_counter")
            if alias in ranges and ranges[alias] == [details["counter_start"], details["counter_end"]]:
                ranges[item.row.raw_id] = ranges.pop(alias)
        for old_id, span in list(ranges.items()):
            cursor, chain = span[0], []
            while cursor < span[1]:
                choices = {}
                for observation in group:
                    details = dict(observation.components)
                    interval = [details["counter_start"], details["counter_end"]]
                    if interval == span or interval[0] != cursor or interval[1] > span[1]:
                        continue
                    previous = choices.get(interval[1])
                    if previous is None or observation.revision > previous.revision:
                        choices[interval[1]] = observation
                if len(choices) != 1:
                    break
                cursor, observation = next(iter(choices.items()))
                chain.append(observation)
            if cursor != span[1] or len(chain) < 2:
                continue
            identity_key = "usage.identity.v1:" + stable_id("codex_local", old_id)
            record = read_meta(connection, identity_key, None)
            stored = stored_event(connection, record["raw_id"] if record else old_id)
            if stored is None:
                continue
            processed = lambda row: row.input_tokens + row.output_tokens + row.cache_write_tokens + row.unclassified_tokens
            if (sum(processed(item.row) for item in chain) != span[1] - span[0]
                    or any(chain[i].row.occurred_at > chain[i+1].row.occurred_at for i in range(len(chain)-1))
                    or stored.get("telemetry_complete", True) and not all(item.row.telemetry_complete for item in chain)):
                continue
            # The complete chain refines one proven counter interval. Its final
            # endpoint keeps the old canonical/0.3.0 ID; preceding endpoints
            # supply the uncovered pieces, never another baseline on top.
            del ranges[old_id]
            for item in chain:
                details = dict(item.components)
                ranges[item.row.raw_id] = [details["counter_start"], details["counter_end"]]
                rank = read_meta(connection, "usage.identity.v1:" + stable_id("codex_local", item.row.raw_id), None)
                replacements[id(item)] = replace(item, revision=max(item.revision, rank["rank"][0] if rank else item.revision),
                                                 authority=details["counter_start"])
            for item in group:
                details = dict(item.components)
                if [details["counter_start"], details["counter_end"]] == span:
                    ignored.add(id(item))
    observations = [replacements.get(id(item), item) for item in observations if id(item) not in ignored]
    observations.sort(key=lambda item: (dict(item.components).get("counter_scope", ""), dict(item.components).get("counter_end", 0), -dict(item.components).get("counter_start", 0)))
    for observation in observations:
        components = dict(observation.components)
        scope = components.get("counter_scope")
        if not scope:
            accepted.append(observation)
            continue
        key = "codex.counter-ranges.v1:" + scope
        ranges = states[key]
        start, end = components["counter_start"], components["counter_end"]
        if start == 0 and not ranges and not components.get("counter_reset"):
            from spend_app.adapters.local_common import parse_iso_time
            earlier_legacy = False
            for table in ("usage_events", "unpriced_usage_events"):
                for old in connection.execute(f"SELECT * FROM {table} WHERE source=? AND session_id=?", ("codex_local", observation.row.session_id)):
                    when = parse_iso_time(old["occurred_at"])
                    if old["raw_id"].startswith("codex-local:") and when and when < observation.row.occurred_at:
                        earlier_legacy = True
            if earlier_legacy:
                issues.append("codex_legacy_fragment_requires_full_replay")
                continue
        interval = [start, end]
        overlap = any(max(start, known[0]) < min(end, known[1]) and interval != known for known in ranges.values())
        if overlap:
            issues.append("codex_overlapping_counter_fragment_requires_complete_history")
            continue
        ranges[observation.row.raw_id] = interval
        accepted.append(observation)
    for key, ranges in states.items():
        write_meta(connection, key, ranges)
    return accepted
