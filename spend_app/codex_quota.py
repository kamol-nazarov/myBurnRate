"""Main-pool Codex quota metadata. No credentials, usage accounting or probes."""
import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path

KEY = "codex.quota-observation.v1"
MAX_AGE_SECONDS = 6 * 60 * 60


def timestamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def observation(event, now):
    if not isinstance(event, dict):
        return None
    when = timestamp(event.get("timestamp"))
    payload = event.get("payload")
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    if when is None or when > now or not isinstance(limits, dict) or limits.get("limit_id") != "codex":
        return None
    # Credit-only updates and missing-ID legacy records cannot establish this pool.
    windows = [w for w in (limits.get("primary"), limits.get("secondary"))
               if isinstance(w, dict) and number(w.get("window_minutes")) and w["window_minutes"] == 10080]
    if len(windows) != 1:
        return None
    w = windows[0]
    if not number(w.get("used_percent")) or not 0 <= w["used_percent"] <= 100 or not number(w.get("resets_at")):
        return None
    try:
        reset = datetime.fromtimestamp(w["resets_at"], UTC)
    except (ValueError, OverflowError, OSError):
        return None
    if reset <= when or (reset - when).total_seconds() > 10080 * 60:
        return None
    # Retain only quota metadata, never transcript fields or credit documents.
    return when.timestamp(), {"limit_id": "codex", "plan_type": limits.get("plan_type") if isinstance(limits.get("plan_type"), str) else None, "primary": {key: w[key] for key in ("window_minutes", "used_percent", "resets_at")}}


def newest(previous, candidate):
    if candidate is None:
        return previous
    if previous and candidate[0] == previous[0] and candidate[1] != previous[1]:
        return candidate[0], {"ambiguous": True}
    return candidate if previous is None or candidate[0] > previous[0] else previous


def freshness_reason(observed_at, resets_at, now):
    observed, reset = timestamp(observed_at), timestamp(resets_at)
    if observed is None or reset is None or observed > now:
        return "Codex quota observation time is unavailable or invalid; wait for fresh main-pool telemetry."
    if reset <= now:
        return "Codex quota window expired; wait for a new main-pool snapshot. No new zero was inferred."
    if (now - observed).total_seconds() > MAX_AGE_SECONDS:
        return "Codex quota snapshot is older than six hours; wait for fresh main-pool telemetry."
    return None


def scope(root):
    return hashlib.sha256(str(Path(root).resolve()).replace("\\", "/").casefold().encode()).hexdigest()


def current_scope():
    from spend_app.providers import default_codex_glob
    return scope(default_codex_glob().replace("\\", "/").removesuffix("/**/*.jsonl"))


def meta_key(provider_key="codex"):
    return KEY if provider_key == "codex" else f"{KEY}:{provider_key}"


def _is_codex_sample(sample):
    provider_key = getattr(sample, "provider_key", None)
    if not isinstance(provider_key, str) or not provider_key:
        return False
    return (
        provider_key == "codex"
        or provider_key.startswith("codex-")
        or getattr(sample, "source", None) == "codex_local_telemetry"
        or getattr(sample, "pool_id", None) == "codex"
    )


def _codex_root(location):
    if not isinstance(location, str) or not location.strip():
        return None
    return Path(location.strip().replace("\\", "/").removesuffix("/**/*.jsonl")).expanduser()


def _is_codex_row(row):
    key = row.get("provider_key")
    if not isinstance(key, str) or not key:
        return False
    return (
        key == "codex"
        or key.startswith("codex-")
        or row.get("source") == "codex_local_telemetry"
    )


def save_metadata(connection, sample, polled_at):
    if not _is_codex_sample(sample):
        return
    value = {"poolId": sample.pool_id, "scope": sample.scope,
             "observedAt": sample.observed_at, "polledAt": polled_at,
             "pct": sample.pct, "resetsAt": sample.resets_at}
    connection.execute("INSERT INTO app_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (meta_key(sample.provider_key), json.dumps(value)))


def mask_expired_synced(rows, now):
    """Mask synced rows whose window already reset.

    A ``sync:`` row is another machine's last reading and local polls never
    overwrite it, so once its reset passes the percentage describes a window
    that no longer exists. No new zero is inferred.
    """
    for row in rows:
        if not str(row.get('source') or '').startswith('sync:') or row.get('is_payg') or row.get('unit') == 'unavailable':
            continue
        reset = timestamp(row.get('resets_at'))
        if reset is None or reset > now:
            continue
        name = str(row.get('label') or row.get('limit_key') or 'Quota window').split(' — ', 1)[0]
        reason = "Synced quota window reset; wait for a fresh reading. No new zero was inferred."
        row.update(pct=None, used=None, allowance=None, resets_at=None, unit='unavailable', label=f"{name} — {reason}")
    return rows


def read_rows(connection, *, now=None, rows=None):
    """Mask unproven/expired Codex rows and expired synced rows at read time, including pre-fix rows."""
    rows = [dict(row) for row in (connection.execute("SELECT * FROM quotas") if rows is None else rows)]
    now = now or datetime.now(UTC)
    mask_expired_synced(rows, now)
    non_sync_rows = [row for row in rows if not str(row.get('source') or '').startswith('sync:')]
    if not non_sync_rows:
        return rows

    from spend_app import connections, profile_store

    candidate_keys = list(dict.fromkeys(
        row['provider_key']
        for row in non_sync_rows
        if _is_codex_row(row)
    ))
    query_keys = [*dict.fromkeys(meta_key(k) for k in candidate_keys), profile_store.KEY, profile_store.V1_KEY]
    placeholders = ",".join("?" for _ in query_keys)
    records = dict(connection.execute(f"SELECT key,value FROM app_meta WHERE key IN ({placeholders})", tuple(query_keys)))

    has_v2_profiles = False
    extra_profiles = {}
    corrupt_conn = False
    try:
        binding = profile_store.default_binding_from_records(records, "codex_local")
        if binding is not None and not isinstance(binding, dict):
            raise ValueError("Invalid connection metadata")
        raw_v2 = records.get(profile_store.KEY)
        if raw_v2 is not None:
            conn_data = json.loads(raw_v2)
            if not isinstance(conn_data, dict):
                raise ValueError("Invalid connection metadata")
            if "profiles" in conn_data:
                profiles_map = conn_data["profiles"]
                if not isinstance(profiles_map, dict):
                    raise ValueError("Invalid connection metadata")
                has_v2_profiles = True
                codex_map = profiles_map.get("codex_local")
                if codex_map is not None:
                    if not isinstance(codex_map, dict):
                        raise ValueError("Invalid connection metadata")
                    for prof_key, record in codex_map.items():
                        if prof_key == "default" or not isinstance(record, dict):
                            continue
                        tool_key = record.get("toolKey") or prof_key
                        if isinstance(tool_key, str) and tool_key:
                            extra_profiles[tool_key] = record
    except (ValueError, TypeError, AttributeError):
        corrupt_conn = True
        binding = {"enabled": False}
        has_v2_profiles = True
        extra_profiles = {}

    extra_codex_keys = {
        row['provider_key']
        for row in non_sync_rows
        if row['provider_key'] in extra_profiles and row['provider_key'] not in candidate_keys
    }
    if extra_codex_keys:
        missing_meta_keys = tuple(meta_key(k) for k in sorted(extra_codex_keys))
        ph = ",".join("?" for _ in missing_meta_keys)
        records.update(dict(connection.execute(f"SELECT key,value FROM app_meta WHERE key IN ({ph})", missing_meta_keys)))

    all_codex_keys = set(candidate_keys) | extra_codex_keys
    if not all_codex_keys:
        return rows

    meta_by_key = {}
    for key in all_codex_keys:
        try:
            parsed = json.loads(records.get(meta_key(key), "{}"))
            meta_by_key[key] = parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            meta_by_key[key] = {}

    suppressed = connections.suppresses_default_profile("codex_local", binding)
    try:
        default_scope = current_scope() if not suppressed else None
    except (OSError, ValueError):
        default_scope = None

    for row in rows:
        key = row['provider_key']
        if key not in all_codex_keys:
            continue
        if str(row.get('source') or '').startswith('sync:'):
            continue
        meta = meta_by_key.get(key, {})
        reason = None
        if key == 'codex':
            if suppressed:
                reason = "Codex connection is disabled." if not binding.get("enabled") else "Managed Codex location grants usage only; local quota permission is not supported. No default profile was read."
            else:
                active_scope = default_scope
                require_scope = True
        else:
            if corrupt_conn:
                reason = "Codex connection is disabled."
                active_scope = None
                require_scope = True
            elif has_v2_profiles:
                profile_rec = extra_profiles.get(key)
                if profile_rec is None:
                    reason = "Codex profile was removed."
                    active_scope = None
                elif not profile_rec.get("enabled"):
                    reason = "Codex connection is disabled."
                    active_scope = None
                else:
                    root = _codex_root(profile_rec.get("location"))
                    try:
                        active_scope = scope(root) if root is not None else None
                    except (OSError, ValueError):
                        active_scope = None
                require_scope = True
            else:
                active_scope = meta.get('scope') if isinstance(meta.get('scope'), str) and meta.get('scope') else None
                require_scope = False

        if reason is None and row['pct'] is not None:
            scope_ok = (active_scope is not None and meta.get('scope') == active_scope) if require_scope else bool(meta.get('scope'))
            if (meta.get('poolId') != 'codex' or not scope_ok
                    or meta.get('pct') != row['pct'] or meta.get('resetsAt') != row['resets_at']
                    or meta.get('polledAt') != row['polled_at']):
                reason = "Main Codex quota identity is unverified; awaiting a new quota poll."
            else:
                row['observed_at'] = meta.get('observedAt')
                polled = timestamp(meta.get('polledAt'))
                observed = timestamp(meta.get('observedAt'))
                check_now = max(now, polled) if polled and observed and observed <= polled else now
                reason = freshness_reason(meta.get('observedAt'), row['resets_at'], check_now)
        if reason:
            row.update(pct=None, used=None, allowance=None, resets_at=None, unit='unavailable', label='Codex weekly window — ' + reason)
    return rows
