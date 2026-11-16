"""Multi-profile connection state store and v1 -> v2 migration.

Persists v2 connection state under ``app_meta["connections.v2"]``:
    {
        "version": 2,
        "profiles": {provider: {profileKey: {...record...}}},
        "dismissed": {provider: {candidateId: iso}},
        "requests": {...},
        "legacy": [...],
        "cleanup": [...],
    }

``connections.v1`` is left untouched in ``app_meta`` for rollback. Read-only
paths compute the migrated v2 view in memory without writing.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from spend_app import claude_accounts, opencode_profiles
from spend_app.connection_paths import LocationError, _resolve, identity

KEY = "connections.v2"
V1_KEY = "connections.v1"
MIGRATED_KEY = "profiles.migrated.v1"

DEFAULT_TOOL_KEYS: dict[str, str] = {
    "claude_local": "claude-code",
    "codex_local": "codex",
    "opencode_local": "opencode-go",
    "zcode_local": "zcode",
    "grok_local": "grok",
    "antigravity_local": "antigravity",
    "muse_local": "muse",
}


class FolderInUse(ValueError):
    """Raised when a resolved folder already belongs to another profile."""


def supports_profiles(provider: str) -> bool:
    """True when ``provider`` is a single-key local adapter supporting profiles."""
    return provider in DEFAULT_TOOL_KEYS


@dataclass(frozen=True)
class Profile:
    provider: str
    key: str
    name: str
    location: str | None
    origin: str
    enabled: bool
    revision: int
    account_digest: str | None
    tool_key: str | None


def profile_tool_key(provider: str, key: str) -> str | None:
    if key == "default":
        return DEFAULT_TOOL_KEYS.get(provider)
    return key


def profile_key_for_folder(
    provider: str, folder: Path, existing: Iterable[str] = ()
) -> str:
    """Derive a collision-free profile key from a folder path."""
    path = Path(folder)
    if provider == "claude_local":
        base = f"{DEFAULT_TOOL_KEYS['claude_local']}-{claude_accounts.slug_for(path)}"
    elif provider == "opencode_local":
        base = f"{DEFAULT_TOOL_KEYS['opencode_local']}-{opencode_profiles.slug_for(path)}"
    else:
        tool_prefix = DEFAULT_TOOL_KEYS.get(provider)
        if not tool_prefix:
            raise ValueError(f"Provider {provider!r} does not support multiple profiles")
        name = path.name.lower().lstrip(".")
        for sep in ("-", "_"):
            prefix = tool_prefix + sep
            if name.startswith(prefix):
                name = name[len(prefix) :]
                break
        slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")[:24].strip("-") or "profile"
        base = f"{tool_prefix}-{slug}"

    used = set(existing) | {"default", *DEFAULT_TOOL_KEYS.values()}
    if base not in used:
        return base
    counter = 2
    while f"{base}-{counter}" in used:
        counter += 1
    return f"{base}-{counter}"


def _resolve_soft(raw: str | Path) -> Path:
    path = Path(raw).expanduser()
    try:
        return _resolve(path, strict=False)
    except (OSError, ValueError):
        return path


def _folder_identities(raw: str | Path | None, provider: str | None = None) -> set[str]:
    """Return normalized identities for ``raw`` and its provider profile root."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return set()
    resolved = _resolve_soft(raw)
    ids = {identity(resolved)}
    name = resolved.name.lower()
    if provider in (None, "claude_local") and name in {"projects", "transcripts"}:
        if resolved.parent != resolved:
            ids.add(identity(resolved.parent))
    if provider in (None, "codex_local") and name in {"sessions", "archived_sessions"}:
        if resolved.parent != resolved:
            ids.add(identity(resolved.parent))
    if provider in (None, "opencode_local") and name.endswith((".db", ".sqlite", ".json")):
        if resolved.parent != resolved:
            ids.add(identity(resolved.parent))
            if (
                resolved.parent.name.lower() == "opencode"
                and resolved.parent.parent != resolved.parent
                and resolved.parent.parent.name.lower().startswith(".opencode-")
            ):
                ids.add(identity(resolved.parent.parent))
    if provider in (None, "zcode_local") and len(resolved.parts) >= 3:
        if tuple(part.lower() for part in resolved.parts[-3:]) == ("cli", "db", "db.sqlite"):
            ids.add(identity(resolved.parents[2]))
    return ids


def assert_folder_free(
    state: dict,
    provider: str,
    folder: str | Path,
    exclude_key: str | None = None,
) -> None:
    """Raise ``FolderInUse`` when ``folder`` already belongs to another profile."""
    target_ids = _folder_identities(folder, provider)
    if not target_ids:
        return

    raw_profiles = (state or {}).get("profiles") or {}
    for prov, provider_map in raw_profiles.items():
        if not isinstance(provider_map, dict):
            continue
        for key, record in provider_map.items():
            if prov == provider and exclude_key is not None and key == exclude_key:
                continue
            if not isinstance(record, dict):
                continue
            loc = record.get("location")
            if not loc:
                continue
            if target_ids & _folder_identities(loc, prov):
                raise FolderInUse("This folder is already used by another profile.")

    raw_bindings = (state or {}).get("bindings") or {}
    for prov, record in raw_bindings.items():
        if prov in raw_profiles and isinstance(raw_profiles[prov], dict) and "default" in raw_profiles[prov]:
            continue
        if prov == provider and exclude_key == "default":
            continue
        if not isinstance(record, dict):
            continue
        loc = record.get("location")
        if not loc:
            continue
        if target_ids & _folder_identities(loc, prov):
            raise FolderInUse("This folder is already used by another profile.")


def empty_state() -> dict:
    return {
        "version": 2,
        "profiles": {},
        "dismissed": {},
        "requests": {},
        "legacy": [],
        "cleanup": [],
    }


def _validate_binding_record(record: object) -> None:
    if (
        not isinstance(record, dict)
        or type(record.get("enabled")) is not bool
        or type(record.get("revision")) is not int
    ):
        raise LocationError(
            "Connection binding metadata is corrupted; defaults were not enabled."
        )


def decode(raw: str | None) -> dict:
    state = json.loads(raw) if raw is not None else empty_state()
    if not isinstance(state, dict) or state.get("version") != 2:
        raise LocationError("Connection settings require a newer BURNRATE version.")
    if (
        not isinstance(state.get("profiles"), dict)
        or not isinstance(state.get("dismissed"), dict)
        or not isinstance(state.get("requests"), dict)
        or not isinstance(state.get("legacy"), list)
        or not isinstance(state.get("cleanup"), list)
    ):
        raise LocationError(
            "Connection settings are corrupted; restore or repair them before collecting."
        )
    for provider_profiles in state["profiles"].values():
        if not isinstance(provider_profiles, dict):
            raise LocationError(
                "Connection settings are corrupted; restore or repair them before collecting."
            )
        for record in provider_profiles.values():
            _validate_binding_record(record)
    if "bindings" in state:
        if not isinstance(state["bindings"], dict):
            raise LocationError(
                "Connection settings are corrupted; restore or repair them before collecting."
            )
        for binding in state["bindings"].values():
            _validate_binding_record(binding)
    cleaned = {k: v for k, v in state.items() if k != "bindings"}
    return cleaned


def encode(state: dict) -> str:
    persisted = {k: v for k, v in state.items() if k != "bindings"}
    raw = json.dumps(persisted, ensure_ascii=False)
    decode(raw)
    return raw


def with_bindings_view(state: dict) -> dict:
    """Attach the derived, never-persisted ``bindings`` compatibility view."""
    view = dict(state)
    bindings: dict[str, dict] = {}
    for provider, provider_profiles in (state.get("profiles") or {}).items():
        if isinstance(provider_profiles, dict) and isinstance(
            provider_profiles.get("default"), dict
        ):
            bindings[provider] = provider_profiles["default"]
    view["bindings"] = bindings
    return view


def sync_bindings_view(state: dict, initial_bindings: dict | None = None) -> dict:
    """Propagate ``state["bindings"]`` mutations into ``state["profiles"]`` and strip ``bindings``."""
    bindings = state.get("bindings")
    if isinstance(bindings, dict):
        profiles_map = state.setdefault("profiles", {})
        if initial_bindings is None:
            for provider, record in bindings.items():
                if isinstance(record, dict):
                    provider_profiles = profiles_map.setdefault(provider, {})
                    merged = dict(record)
                    merged.setdefault("source", provider)
                    merged.setdefault("name", "Default")
                    merged.setdefault("origin", "default")
                    merged.setdefault("toolKey", profile_tool_key(provider, "default"))
                    provider_profiles["default"] = merged
        elif bindings != initial_bindings:
            for provider, record in bindings.items():
                if provider not in initial_bindings or initial_bindings[provider] != record:
                    if isinstance(record, dict):
                        provider_profiles = profiles_map.setdefault(provider, {})
                        merged = dict(record)
                        merged.setdefault("source", provider)
                        merged.setdefault("name", "Default")
                        merged.setdefault("origin", "default")
                        merged.setdefault(
                            "toolKey", profile_tool_key(provider, "default")
                        )
                        provider_profiles["default"] = merged
            for provider in set(initial_bindings) - set(bindings):
                provider_profiles = profiles_map.get(provider)
                if isinstance(provider_profiles, dict):
                    provider_profiles.pop("default", None)
    return {k: v for k, v in state.items() if k != "bindings"}


def _decode_v1(raw: str | None, *, allow_missing_version: bool = False) -> dict | None:
    if raw is None:
        return None
    state = json.loads(raw)
    if not isinstance(state, dict):
        raise LocationError("Connection settings require a newer BURNRATE version.")
    version = state.get("version")
    if version != 1 and not (allow_missing_version and version is None):
        raise LocationError("Connection settings require a newer BURNRATE version.")
    if (
        not isinstance(state.get("bindings"), dict)
        or not isinstance(state.get("legacy", []), list)
    ):
        raise LocationError(
            "Connection settings are corrupted; restore or repair them before collecting."
        )
    if "requests" in state and not isinstance(state["requests"], dict):
        raise LocationError(
            "Connection settings are corrupted; restore or repair them before collecting."
        )
    if "cleanup" in state and not isinstance(state["cleanup"], list):
        raise LocationError(
            "Connection settings are corrupted; restore or repair them before collecting."
        )
    for binding in state["bindings"].values():
        _validate_binding_record(binding)
    return state


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _claude_folder_from_location(location: str | None) -> Path | None:
    if not isinstance(location, str) or not location.strip():
        return None
    path = Path(location.strip()).expanduser()
    if path.name.lower() in {"projects", "transcripts"} and path.parent != path:
        return path.parent
    return path


def _claude_digest_for_location(
    location: str | None, *, is_default: bool = False
) -> str | None:
    folder = _claude_folder_from_location(location)
    if folder is None:
        return None
    candidates = [folder / ".claude.json"]
    if is_default and folder.name == ".claude" and folder.parent != folder:
        candidates.append(folder.parent / ".claude.json")
    for candidate in candidates:
        try:
            data = claude_accounts.read_state(candidate)
        except OSError:
            continue
        uid = claude_accounts.account_uuid(data)
        if uid:
            return _sha256_hex(uid)
    return None


def _location_matches(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    try:
        return identity(a) == identity(b)
    except Exception:
        return False


def migrate_v1_to_v2(
    v1_state: dict | None,
    legacy_sources: Iterable[str] = (),
    *,
    discover: bool = True,
) -> dict:
    """Convert a v1 connection state (and optional discovered profiles) into v2."""
    state = empty_state()
    if v1_state is not None:
        v1_bindings = v1_state.get("bindings", {})
        state["requests"] = copy.deepcopy(v1_state.get("requests") or {})
        state["cleanup"] = list(v1_state.get("cleanup") or [])
        merged_legacy = list(v1_state.get("legacy") or [])
    else:
        v1_bindings = {}
        merged_legacy = []

    for src in legacy_sources:
        if src not in merged_legacy:
            merged_legacy.append(src)
    state["legacy"] = merged_legacy

    alt_binding: dict | None = None
    for provider, binding in v1_bindings.items():
        if provider == "claude_local_alt":
            alt_binding = copy.deepcopy(binding)
            continue
        record = copy.deepcopy(binding)
        record["source"] = provider
        record["name"] = record.get("name") or "Default"
        record["origin"] = "default"
        record["toolKey"] = profile_tool_key(provider, "default")
        if provider == "claude_local":
            record["accountDigest"] = _claude_digest_for_location(
                record.get("location"), is_default=True
            )
        else:
            record.setdefault("accountDigest", None)
        state["profiles"].setdefault(provider, {})["default"] = record

    if alt_binding is not None:
        claude_profiles = state["profiles"].setdefault("claude_local", {})
        loc = alt_binding.get("location")
        folder = _claude_folder_from_location(loc) or Path(".claude-alt")
        key = profile_key_for_folder(
            "claude_local", folder, existing=claude_profiles.keys()
        )
        slug = claude_accounts.slug_for(folder)
        fallback_name = (
            f"Profile {slug}" if slug.isdigit() else claude_accounts.label_for(slug)
        )
        record = copy.deepcopy(alt_binding)
        record["source"] = "claude_local"
        record["bindingId"] = f"claude_local:{key}"
        record["name"] = alt_binding.get("name") or fallback_name
        record["origin"] = "manual"
        record["toolKey"] = key
        record["accountDigest"] = _claude_digest_for_location(loc, is_default=False)
        claude_profiles[key] = record

    if discover:
        for account in claude_accounts.extra_accounts():
            claude_profiles = state["profiles"].setdefault("claude_local", {})
            digest = _sha256_hex(account.account_uuid)
            acc_location = str(account.config_dir)
            duplicate = any(
                (digest and existing.get("accountDigest") == digest)
                or _location_matches(existing.get("location"), acc_location)
                or _location_matches(
                    str(_claude_folder_from_location(existing.get("location")) or ""),
                    acc_location,
                )
                for existing in claude_profiles.values()
            )
            if duplicate:
                continue
            key = account.key
            if key in claude_profiles:
                key = profile_key_for_folder(
                    "claude_local", account.config_dir, existing=claude_profiles.keys()
                )
            claude_profiles[key] = {
                "source": "claude_local",
                "bindingId": f"claude_local:{key}",
                "name": account.label,
                "location": acc_location,
                "origin": "detected",
                "mode": "detected",
                "enabled": True,
                "revision": 1,
                "state": "awaiting_ingest",
                "detail": "Detected Claude Code profile.",
                "lastVerification": None,
                "lastImport": None,
                "importRevision": None,
                "capabilities": ["usage"],
                "accountDigest": digest,
                "toolKey": key,
            }

        for prof in opencode_profiles.extra_profiles():
            oc_profiles = state["profiles"].setdefault("opencode_local", {})
            prof_location = str(prof.database)
            duplicate = any(
                _location_matches(existing.get("location"), prof_location)
                for existing in oc_profiles.values()
            )
            if duplicate:
                continue
            key = prof.key
            if key in oc_profiles:
                key = profile_key_for_folder(
                    "opencode_local", prof.data_dir, existing=oc_profiles.keys()
                )
            oc_profiles[key] = {
                "source": "opencode_local",
                "bindingId": f"opencode_local:{key}",
                "name": prof.label,
                "location": prof_location,
                "origin": "detected",
                "mode": "detected",
                "enabled": True,
                "revision": 1,
                "state": "awaiting_ingest",
                "detail": "Detected OpenCode profile.",
                "lastVerification": None,
                "lastImport": None,
                "importRevision": None,
                "capabilities": ["usage"],
                "accountDigest": None,
                "toolKey": key,
            }

    return state


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _read_legacy_sources(connection: sqlite3.Connection) -> list[str]:
    if not _table_exists(connection, "ingest_runs"):
        return []
    return [
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT source FROM ingest_runs WHERE status IN ('success','partial')"
        )
    ]


def load(connection: sqlite3.Connection) -> dict:
    """Return v2 state from ``connection``; migrates v1 in memory on read-only DBs."""
    if not _table_exists(connection, "app_meta"):
        return empty_state()
    row = connection.execute(
        "SELECT value FROM app_meta WHERE key=?", (KEY,)
    ).fetchone()
    if row is not None:
        return decode(row[0])
    migrated_row = connection.execute(
        "SELECT value FROM app_meta WHERE key=?", (MIGRATED_KEY,)
    ).fetchone()
    if migrated_row is not None:
        return empty_state()
    v1_row = connection.execute(
        "SELECT value FROM app_meta WHERE key=?", (V1_KEY,)
    ).fetchone()
    v1_state = _decode_v1(v1_row[0]) if v1_row is not None else None
    legacy_sources = _read_legacy_sources(connection)
    return migrate_v1_to_v2(v1_state, legacy_sources, discover=True)


def _meta_rows_by_key(connection: sqlite3.Connection, *keys: str) -> tuple[dict[str, str | None], list[str | None]]:
    rows = list(
        connection.execute(
            f"SELECT key,value FROM app_meta WHERE key IN ({','.join('?' for _ in keys)})",
            keys,
        )
    )
    by_key: dict[str, str | None] = {}
    unkeyed: list[str | None] = []
    for row in rows:
        if isinstance(row, dict):
            if "key" in row:
                by_key[row["key"]] = row.get("value")
            else:
                unkeyed.append(row.get("value"))
        elif isinstance(row, sqlite3.Row):
            row_keys = row.keys()
            if "key" in row_keys:
                by_key[row["key"]] = row["value"]
            else:
                unkeyed.append(row["value"])
        else:
            by_key[row[0]] = row[1]
    return by_key, unkeyed


def load_config_if_present(connection: sqlite3.Connection) -> dict | None:
    """Load connection config for read-only consumers without filesystem discovery.

    Returns v2 state with the derived ``bindings`` view if either ``connections.v2``
    or ``connections.v1`` is stored in ``app_meta``, else ``None``.
    """
    by_key, _ = _meta_rows_by_key(connection, KEY, V1_KEY)
    if KEY in by_key:
        return with_bindings_view(decode(by_key[KEY]))
    if V1_KEY not in by_key:
        return None
    v1_state = _decode_v1(by_key[V1_KEY], allow_missing_version=True)
    return with_bindings_view(migrate_v1_to_v2(v1_state, discover=False))


def load_bindings_map(connection: sqlite3.Connection) -> dict[str, dict]:
    """Return ``{provider: binding}`` from v2 or v1 without touching the filesystem."""
    by_key, unkeyed = _meta_rows_by_key(connection, KEY, V1_KEY)
    candidates: list[str | None] = []
    if KEY in by_key:
        candidates.append(by_key[KEY])
    if V1_KEY in by_key:
        candidates.append(by_key[V1_KEY])
    candidates.extend(unkeyed)
    for raw in candidates:
        if not raw:
            return {}
        state = json.loads(raw)
        if not isinstance(state, dict):
            return {}
        if isinstance(state.get("profiles"), dict):
            return {
                provider: records["default"]
                for provider, records in state["profiles"].items()
                if isinstance(records, dict) and isinstance(records.get("default"), dict)
            }
        if isinstance(state.get("bindings"), dict):
            return {
                provider: binding
                for provider, binding in state["bindings"].items()
                if isinstance(binding, dict)
            }
        return {}
    return {}


def default_binding_from_records(records: dict[str, str], provider: str) -> dict | None:
    """Extract a provider's default binding from raw ``app_meta`` records."""
    raw_v2 = records.get(KEY)
    if raw_v2 is not None:
        conn_data = json.loads(raw_v2)
        if not isinstance(conn_data, dict):
            raise ValueError("Invalid connection metadata")
        if "profiles" in conn_data:
            profiles_map = conn_data["profiles"]
            if not isinstance(profiles_map, dict):
                raise ValueError("Invalid connection metadata")
            provider_map = profiles_map.get(provider)
            if provider_map is None:
                return None
            if not isinstance(provider_map, dict):
                raise ValueError("Invalid connection metadata")
            return provider_map.get("default")
        if "bindings" in conn_data:
            bindings_map = conn_data["bindings"]
            if not isinstance(bindings_map, dict):
                raise ValueError("Invalid connection metadata")
            return bindings_map.get(provider)
        return None
    raw_v1 = records.get(V1_KEY)
    if raw_v1 is not None:
        conn_data = json.loads(raw_v1)
        if not isinstance(conn_data, dict):
            raise ValueError("Invalid connection metadata")
        bindings_map = conn_data.get("bindings", {})
        if not isinstance(bindings_map, dict):
            raise ValueError("Invalid connection metadata")
        return bindings_map.get(provider)
    return None


def ensure_migrated(connection: sqlite3.Connection) -> dict:
    """Persist v1 -> v2 migration inside the caller's write transaction, idempotently."""
    v2_row = connection.execute(
        "SELECT value FROM app_meta WHERE key=?", (KEY,)
    ).fetchone()
    migrated_row = connection.execute(
        "SELECT value FROM app_meta WHERE key=?", (MIGRATED_KEY,)
    ).fetchone()
    if v2_row is not None:
        state = decode(v2_row[0])
        if migrated_row is None:
            connection.execute(
                "INSERT OR IGNORE INTO app_meta(key,value) VALUES(?,?)",
                (MIGRATED_KEY, "1"),
            )
        return state
    if migrated_row is not None:
        state = empty_state()
        connection.execute(
            "INSERT OR IGNORE INTO app_meta(key,value) VALUES(?,?)",
            (KEY, encode(state)),
        )
        return state

    v1_row = connection.execute(
        "SELECT value FROM app_meta WHERE key=?", (V1_KEY,)
    ).fetchone()
    v1_state = _decode_v1(v1_row[0]) if v1_row is not None else None
    legacy_sources = _read_legacy_sources(connection)
    state = migrate_v1_to_v2(v1_state, legacy_sources, discover=True)
    connection.execute(
        "INSERT INTO app_meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (MIGRATED_KEY, "1"),
    )
    connection.execute(
        "INSERT INTO app_meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (KEY, encode(state)),
    )
    return state


def profiles(state: dict, provider: str | None = None) -> list[Profile]:
    """Return ``Profile`` objects from a decoded v2 ``state``."""
    raw_profiles = state.get("profiles") or {}
    if provider is not None:
        items = [(provider, raw_profiles.get(provider) or {})]
    else:
        items = list(raw_profiles.items())
    result: list[Profile] = []
    for prov, provider_map in items:
        if not isinstance(provider_map, dict):
            continue
        for key, record in provider_map.items():
            if not isinstance(record, dict):
                continue
            tool_key = (
                record["toolKey"]
                if "toolKey" in record
                else profile_tool_key(prov, key)
            )
            result.append(
                Profile(
                    provider=prov,
                    key=key,
                    name=str(record.get("name") or ("Default" if key == "default" else key)),
                    location=record.get("location"),
                    origin=str(
                        record.get("origin") or ("default" if key == "default" else "manual")
                    ),
                    enabled=bool(record.get("enabled", False)),
                    revision=int(record.get("revision", 0)),
                    account_digest=record.get("accountDigest"),
                    tool_key=tool_key,
                )
            )
    return result


def enabled_profiles(database_path: str | Path) -> list[Profile]:
    """Open a read-only connection and return all enabled ``Profile`` entries."""
    path = Path(database_path)
    try:
        path.stat()
    except FileNotFoundError:
        return []
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)) as db:
        state = load(db)
    return [profile for profile in profiles(state) if profile.enabled]


__all__ = (
    "DEFAULT_TOOL_KEYS",
    "FolderInUse",
    "KEY",
    "MIGRATED_KEY",
    "Profile",
    "V1_KEY",
    "assert_folder_free",
    "decode",
    "default_binding_from_records",
    "empty_state",
    "enabled_profiles",
    "encode",
    "ensure_migrated",
    "load",
    "load_bindings_map",
    "load_config_if_present",
    "migrate_v1_to_v2",
    "profile_key_for_folder",
    "profile_tool_key",
    "profiles",
    "supports_profiles",
    "sync_bindings_view",
    "with_bindings_view",
)
