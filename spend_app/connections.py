"""Revisioned connection service; multi-profile bindings per registry source.

The existing app_meta table holds non-secret, versioned JSON under
``connections.v2``. No new schema, event identity, database, worker, or
scheduler is introduced.
"""
import copy
import hashlib
import json
import os
import re
import sqlite3
import threading
import unicodedata
import uuid
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from spend_app import claude_accounts, opencode_profiles, profile_detect, profile_store
from spend_app.connection_paths import (
    Inspector,
    LocationError,
    MissingLocation,
    _resolve,
    approved,
    identity,
)
from spend_app.db import connect
from spend_app.providers import (
    REGISTRY,
    default_grok_root,
    default_opencode_database,
    default_zcode_database,
)
from spend_app.redact import redact
from spend_app.timeutil import parse_utc

KEY = profile_store.KEY
# Provider APIs that report an account balance only, never usage events.
BALANCE_SOURCES = frozenset({"openrouter", "deepseek"})
_API_PROFILE_STATES = {
    "receiving_usage": "receiving",
    "balance_available": "receiving",
    "connected": "receiving",
    "awaiting_ingest": "added",
    "waiting_activity": "added",
}
_LOCKS = {}
_SOURCE_LOCKS = {}
_LOCK_GUARD = threading.Lock()


def lock_for(path):
    key = identity(Path(path).absolute())
    with _LOCK_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def source_lock_for(path, source, profile="default"):
    key = (identity(Path(path).absolute()), str(source), str(profile))
    with _LOCK_GUARD:
        return _SOURCE_LOCKS.setdefault(key, threading.RLock())


def now():
    return datetime.now(UTC).isoformat()


def empty_state():
    return profile_store.with_bindings_view(profile_store.empty_state())


def decode(raw):
    return profile_store.with_bindings_view(profile_store.decode(raw))


def _profile_record(state, source, profile="default"):
    record = (state.get("profiles") or {}).get(source, {}).get(profile)
    if record is None and profile == "default":
        record = (state.get("bindings") or {}).get(source)
    return record


def _set_profile_record(state, source, profile, record):
    state.setdefault("profiles", {}).setdefault(source, {})[profile] = record
    if profile == "default" and isinstance(state.get("bindings"), dict):
        state["bindings"][source] = record


class Store:
    def __init__(self, path):
        self.path = path

    def read(self):
        # Status reads do not create directories, switch WAL modes or write.
        try:
            Path(self.path).stat()
        except FileNotFoundError:
            return empty_state()
        with closing(sqlite3.connect(Path(self.path).absolute().as_uri() + "?mode=ro", uri=True)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_meta'").fetchone():
                return empty_state()
            return profile_store.with_bindings_view(profile_store.load(db))

    @contextmanager
    def transaction(self):
        with connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            v2_state = profile_store.ensure_migrated(db)
            state = profile_store.with_bindings_view(v2_state)
            initial_bindings = copy.deepcopy(state["bindings"])
            yield state
            persisted = profile_store.sync_bindings_view(state, initial_bindings)
            encoded = profile_store.encode(persisted)
            db.execute(
                "INSERT INTO app_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (KEY, encoded),
            )

    def bootstrap(self):
        # Called by normal application initialization, never by GET/discovery.
        with connect(self.path) as db:
            existing = {
                row[0]
                for row in db.execute(
                    "SELECT key FROM app_meta WHERE key IN (?,?)",
                    (KEY, profile_store.MIGRATED_KEY),
                )
            }
            if existing == {KEY, profile_store.MIGRATED_KEY}:
                return
            db.execute("BEGIN IMMEDIATE")
            profile_store.ensure_migrated(db)


class Conflict(LocationError):
    pass


def source_spec(source):
    spec = REGISTRY.get(source)
    if not spec or not spec.connection:
        raise LocationError("Choose a supported local source or documented provider API.")
    return spec


def default_location(spec, settings):
    meta = spec.connection
    value = str(meta.default())
    if meta.suffix:
        value = value.replace("\\", "/").removesuffix("/" + meta.suffix)
    return value


def external_key(spec, settings):
    name = spec.connection.credential_env
    attribute = {"OPENAI_ADMIN_KEY": "openai_admin_key", "ANTHROPIC_ADMIN_KEY": "anthropic_admin_key"}.get(name)
    return os.getenv(name) or (getattr(settings, attribute, None) if attribute else None) if name else None


def external(spec, settings):
    return bool(external_key(spec, settings) or (spec.connection.location_env and os.getenv(spec.connection.location_env)))


def location_conflict(spec, location):
    name = spec.connection.location_env
    raw = os.getenv(name) if name else None
    if not raw:
        return False
    base = Path(raw).expanduser()
    if spec.key == "opencode_local":
        base /= "opencode"
    base = base.resolve()
    chosen = Path(location).resolve()
    return not (chosen == base or chosen.is_relative_to(base))


def suppresses_default_profile(source, binding):
    """Whether a binding stops BURNRATE reading that tool's default profile for activity and quota.

    Connecting the default location reads exactly what collection reads anyway,
    so the tool keeps its activity and plan meter. A custom location, or a
    connection the user disabled, still suppresses them.
    """
    if not binding:
        return False
    spec = REGISTRY.get(source)
    if not binding.get("enabled") or spec is None or spec.connection is None or spec.connection.shape == "api":
        return True
    try:
        return identity(binding.get("location") or "") != identity(default_location(spec, None))
    except Exception:
        return True


def eligible(spec, state, profile="default"):
    record = _profile_record(state, spec.key, profile)
    if record:
        return record["enabled"]
    if profile != "default":
        return False
    return spec.enabled_by_default or spec.key in state.get("legacy", [])


def format_home_location(raw: str | Path | None) -> str:
    """Format ``raw`` relative to ``~`` when inside ``Path.home()``, else full path."""
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    if text == "~":
        return "~"
    if text.startswith(("~/", "~\\")):
        tail = text[2:].replace("\\", "/").lstrip("/")
        return f"~/{tail}" if tail else "~"
    home = Path.home()
    path = Path(text).expanduser()
    try:
        if path == home:
            return "~"
        if path.is_relative_to(home):
            rel = path.relative_to(home).as_posix()
            return f"~/{rel}" if rel != "." else "~"
        resolved_home = _resolve(home, strict=False)
        resolved_path = _resolve(path, strict=False)
        if resolved_path == resolved_home:
            return "~"
        if resolved_path.is_relative_to(resolved_home):
            rel = resolved_path.relative_to(resolved_home).as_posix()
            return f"~/{rel}" if rel != "." else "~"
    except (OSError, ValueError):
        pass
    return text


def _safe_is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _safe_is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _default_folder_info(spec, settings) -> tuple[bool, Path, str]:
    key = spec.key
    if key == "claude_local":
        configured = str(os.getenv("CLAUDE_CONFIG_DIR") or "").strip()
        folder = Path(configured).expanduser() if configured else Path.home() / ".claude"
        exists = _safe_is_dir(folder)
        projects = folder / "projects"
        loc = str(projects if _safe_is_dir(projects) else folder)
        return exists, folder, loc
    if key == "codex_local":
        configured = str(os.getenv("CODEX_HOME") or "").strip()
        folder = Path(configured).expanduser() if configured else Path.home() / ".codex"
        exists = _safe_is_dir(folder)
        sessions = folder / "sessions"
        loc = str(sessions if _safe_is_dir(sessions) else folder)
        return exists, folder, loc
    if key == "opencode_local":
        db_path = default_opencode_database()
        exists = _safe_is_file(db_path) or _safe_is_dir(db_path.parent)
        return exists, db_path.parent, str(db_path)
    if key == "zcode_local":
        db_path = default_zcode_database()
        root = Path.home() / ".zcode"
        exists = _safe_is_file(db_path) or _safe_is_dir(root)
        return exists, root, str(db_path)
    if key == "grok_local":
        root = default_grok_root()
        return _safe_is_dir(root), root, str(root)
    if key == "traycer_local":
        root = Path(default_location(spec, settings)).expanduser()
        db_path = root / "app-assets" / "app-assets.db"
        return (_safe_is_file(db_path) or _safe_is_dir(root)), root, str(root)
    root = Path(default_location(spec, settings)).expanduser()
    return _safe_is_dir(root), root, str(root)


def _profile_folder_for(provider: str, location: str | Path) -> Path:
    path = Path(location).expanduser()
    name = path.name.lower()
    if provider == "claude_local" and name in {"projects", "transcripts"} and path.parent != path:
        return path.parent
    if provider == "codex_local" and name in {"sessions", "archived_sessions"} and path.parent != path:
        return path.parent
    if provider == "opencode_local" and name.endswith((".db", ".sqlite", ".json")) and path.parent != path:
        if path.parent.name.lower() == "opencode" and path.parent.parent != path.parent and path.parent.parent.name.lower().startswith(".opencode-"):
            return path.parent.parent
        return path.parent
    if provider == "zcode_local" and len(path.parts) >= 3:
        if tuple(part.lower() for part in path.parts[-3:]) == ("cli", "db", "db.sqlite"):
            return path.parents[2]
    return path


def _folder_label(provider: str, folder: Path) -> str:
    if provider == "claude_local":
        slug = claude_accounts.slug_for(folder)
        return f"Profile {slug}" if slug.isdigit() else claude_accounts.label_for(slug)
    if provider == "opencode_local":
        return opencode_profiles.label_for(opencode_profiles.slug_for(folder))
    tool_prefix = profile_store.DEFAULT_TOOL_KEYS.get(provider, "")
    name = folder.name.lower().lstrip(".")
    if tool_prefix:
        for sep in ("-", "_"):
            prefix = tool_prefix + sep
            if name.startswith(prefix):
                name = name[len(prefix):]
                break
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")[:24].strip("-") or "profile"
    if slug.isdigit():
        return f"Profile {slug}"
    return " ".join(part.capitalize() for part in slug.split("-") if part) or "Profile"


def _validate_profile_name(raw, *, required: bool, fallback: str | None = None) -> str:
    if raw is None:
        if required:
            raise LocationError("Enter a profile name (1 to 40 characters).")
        base = (fallback or "Profile").strip()
        cleaned = "".join(ch for ch in base if unicodedata.category(ch) != "Cc").strip()
        return cleaned[:40].strip() or "Profile"
    if not isinstance(raw, str):
        raise LocationError("Enter a profile name (1 to 40 characters).")
    if any(unicodedata.category(ch) == "Cc" for ch in raw):
        raise LocationError("Profile name cannot contain control characters.")
    trimmed = raw.strip()
    if not trimmed or len(trimmed) > 40:
        raise LocationError("Profile name must be 1 to 40 characters.")
    return trimmed


def _identity_for_folder(
    provider: str,
    location: str | Path,
    *,
    is_default: bool = False,
    candidates: list[profile_detect.Candidate] | None = None,
) -> tuple[str | None, str | None]:
    loc_ids = profile_store._folder_identities(location, provider)
    if candidates:
        for cand in candidates:
            if cand.provider != provider:
                continue
            cand_ids = (
                profile_store._folder_identities(cand.folder, provider)
                | profile_store._folder_identities(cand.location, provider)
            )
            if loc_ids & cand_ids:
                return cand.account_digest, cand.plan
    folder = _profile_folder_for(provider, location)
    if provider == "claude_local":
        state_path = (
            claude_accounts.primary_state_path(Path.home(), os.environ)
            if is_default
            else folder / ".claude.json"
        )
        if profile_detect._is_safe_file(state_path):
            data = claude_accounts.read_state(state_path)
            uid = claude_accounts.account_uuid(data)
            return (profile_detect._sha256_hex(uid) if uid else None), claude_accounts.plan_name(data)
        return None, None
    if provider == "codex_local":
        return profile_detect._codex_account_digest(folder / "auth.json"), None
    if provider == "opencode_local":
        path = Path(location).expanduser()
        database = path if path.suffix.lower() in {".db", ".sqlite"} else opencode_profiles.database_for(folder)
        if database is not None and profile_detect._is_safe_file(database):
            digests = profile_detect._opencode_key_digests(database, os.environ, is_default=is_default)
            return (digests[0] if digests else None), None
    return None, None


def _with_virtual_defaults(
    state: dict,
    settings,
    candidates_by_provider: dict[str, list[profile_detect.Candidate]] | None = None,
) -> dict:
    """Return a copy of ``state`` with existing unpersisted default folders attached."""
    view = {**state, "profiles": {k: dict(v) for k, v in (state.get("profiles") or {}).items() if isinstance(v, dict)}}
    for spec in REGISTRY:
        if not spec.connection or not profile_store.supports_profiles(spec.key):
            continue
        prov_map = view["profiles"].setdefault(spec.key, {})
        if "default" in prov_map:
            continue
        exists, _folder, loc = _default_folder_info(spec, settings)
        if not exists:
            continue
        cands = (candidates_by_provider or {}).get(spec.key) or []
        default_cand = next((c for c in cands if c.is_default), None)
        prov_map["default"] = {
            "source": spec.key,
            "name": "Default",
            "location": loc,
            "origin": "default",
            "enabled": True,
            "revision": 0,
            "accountDigest": default_cand.account_digest if default_cand else None,
            "toolKey": profile_store.profile_tool_key(spec.key, "default"),
        }
    return view


def _recent_ingest_tool_keys(database_path, *, hours: int = 24) -> set[str]:
    if not database_path:
        return set()
    db_path = Path(database_path)
    try:
        db_path.stat()
    except OSError:
        return set()
    cutoff_dt = datetime.now(UTC) - timedelta(hours=hours)
    sql_cutoff = (cutoff_dt - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    try:
        with closing(sqlite3.connect(db_path.absolute().as_uri() + "?mode=ro", uri=True)) as db:
            rows = db.execute(
                "SELECT tool_key, MAX(ingested_at) FROM ("
                "SELECT tool_key, ingested_at FROM usage_events WHERE ingested_at >= ? "
                "UNION ALL "
                "SELECT tool_key, ingested_at FROM unpriced_usage_events WHERE ingested_at >= ?"
                ") GROUP BY tool_key",
                (sql_cutoff, sql_cutoff),
            ).fetchall()
    except sqlite3.Error:
        return set()
    recent: set[str] = set()
    for tool_key, max_ts in rows:
        if not tool_key or not max_ts:
            continue
        parsed = parse_utc(str(max_ts))
        if parsed is not None and parsed >= cutoff_dt:
            recent.add(str(tool_key))
    return recent


def _public_record(record: dict) -> dict:
    return {k: v for k, v in record.items() if k not in {"credentialRef", "accountDigest"}}


class Service:
    def __init__(self, settings, store=None, inspector=None, vault=None, probe=None):
        self.settings = settings
        self.store = store or Store(settings.database_path)
        self.inspector = inspector or Inspector()
        from spend_app.connection_secrets import Vault, test_provider
        self.vault = vault or Vault(settings.database_path)
        self.probe = probe or test_provider
        self.lock = lock_for(settings.database_path)

    def list(self):
        state = self.store.read()
        recent_keys = _recent_ingest_tool_keys(getattr(self.store, "path", None))
        detected_candidates = profile_detect.detect_all()
        candidates_by_provider: dict[str, list[profile_detect.Candidate]] = {}
        for cand in detected_candidates:
            candidates_by_provider.setdefault(cand.provider, []).append(cand)

        rows = []
        for spec in REGISTRY:
            meta = spec.connection
            if not meta:
                if spec.kind == "local":
                    rows.append({
                        "source": spec.key,
                        "name": spec.key.replace("_", " "),
                        "kind": "external",
                        "experimental": spec.stability == "experimental",
                        "revision": 0,
                        "location": "",
                        "capabilities": sorted(spec.capabilities),
                        "capabilityNote": "This existing local-service integration has no supported custom-file contract. Configure it outside this wizard.",
                        "credentialHelp": "",
                        "externallyManaged": True,
                        "state": "externally_managed",
                        "detail": "The wizard does not borrow native credentials, call private services to discover sources, or configure this integration.",
                        "lastVerification": None,
                        "lastImport": None,
                        "importRevision": None,
                        "supportsProfiles": False,
                        "profiles": [],
                        "detected": [],
                    })
                continue

            binding = _profile_record(state, spec.key, "default")
            monitored = eligible(spec, state) and meta.shape != "api"
            row = {
                "source": spec.key,
                "name": meta.name,
                "kind": meta.shape,
                "experimental": spec.stability == "experimental",
                "revision": 0,
                "location": "" if meta.shape == "api" else default_location(spec, self.settings),
                "capabilities": ["balance"] if spec.key in BALANCE_SOURCES else (["usage", "provider charges"] if meta.shape == "api" else ["usage"]),
                "capabilityNote": "Connecting a tool's default location keeps its activity and plan meter. A custom location collects usage only. Pricing availability is separate.",
                "credentialHelp": meta.credential_help,
                "externallyManaged": external(spec, self.settings),
                "state": "already_monitored" if monitored else "not_connected",
                "detail": "Existing collection settings." if monitored else "Choose a location or provider to verify and connect.",
                "lastVerification": None,
                "lastImport": None,
                "importRevision": None,
            }
            if binding:
                row.update({k: v for k, v in binding.items() if k not in {"credentialRef", "name", "accountDigest"}})
                row["credentialConfigured"] = bool(binding.get("credentialRef"))
            if row["externallyManaged"]:
                row.update(state="externally_managed", detail="Configured outside BURNRATE. Change that explicit setting before managing this source here.")
            if binding and meta.location_env:
                row["externallyManaged"] = False
                row["externalSetting"] = meta.location_env if os.getenv(meta.location_env) else None

            supports_multi = profile_store.supports_profiles(spec.key)
            row["supportsProfiles"] = supports_multi

            if meta.shape == "api":
                if row["externallyManaged"]:
                    api_prof_state = "externally_managed"
                elif binding and not binding.get("enabled"):
                    api_prof_state = "disabled"
                else:
                    raw_api_state = str(row["state"])
                    api_prof_state = _API_PROFILE_STATES.get(raw_api_state, raw_api_state)
                row["profiles"] = [
                    {
                        "key": "default",
                        "name": str((binding or {}).get("name") or "Default"),
                        "location": "",
                        "origin": str((binding or {}).get("origin") or "default"),
                        "state": api_prof_state,
                        "revision": int((binding or {}).get("revision", 0)),
                        "plan": None,
                        "sharesLimitsWith": None,
                    }
                ]
                row["detected"] = []
            elif not supports_multi:
                exists, _def_folder, def_loc = _default_folder_info(spec, self.settings)
                traycer_keys = spec.tool_keys or (spec.key.removesuffix("_local"),)
                if row["externallyManaged"]:
                    single_state = "externally_managed"
                elif binding and not binding.get("enabled"):
                    single_state = "disabled"
                elif binding and binding.get("state") == "needs_attention":
                    single_state = "error"
                elif any(tk in recent_keys for tk in traycer_keys):
                    single_state = "receiving"
                elif binding or exists:
                    single_state = "added"
                else:
                    single_state = "not_found"
                loc = (binding or {}).get("location") or def_loc
                row["profiles"] = [
                    {
                        "key": "default",
                        "name": str((binding or {}).get("name") or "Default"),
                        "location": format_home_location(loc),
                        "origin": str((binding or {}).get("origin") or "default"),
                        "state": single_state,
                        "revision": int((binding or {}).get("revision", 0)),
                        "plan": None,
                        "sharesLimitsWith": None,
                    }
                ]
                row["detected"] = []
            else:
                prov_profiles = (state.get("profiles") or {}).get(spec.key) or {}
                prov_candidates = candidates_by_provider.get(spec.key, [])
                default_cand = next((c for c in prov_candidates if c.is_default), None)

                entries: list[tuple[str, dict, bool, bool]] = []
                if "default" in prov_profiles and isinstance(prov_profiles["default"], dict):
                    entries.append(("default", prov_profiles["default"], False, True))
                else:
                    def_exists, _def_folder, def_loc = _default_folder_info(spec, self.settings)
                    virtual_default = {
                        "name": "Default",
                        "location": def_loc,
                        "origin": "default",
                        "enabled": True,
                        "revision": 0,
                        "accountDigest": default_cand.account_digest if default_cand else None,
                        "plan": default_cand.plan if default_cand else None,
                        "toolKey": profile_store.profile_tool_key(spec.key, "default"),
                    }
                    entries.append(("default", virtual_default, True, def_exists))

                for k in sorted(k for k in prov_profiles if k != "default"):
                    rec = prov_profiles[k]
                    if isinstance(rec, dict):
                        entries.append((k, rec, False, True))

                primary_by_digest: dict[str, str] = {}
                added_digests: dict[str, str] = {}
                added_folder_ids: set[str] = set()
                profiles_out: list[dict] = []

                for prof_key, rec, is_virtual, folder_exists in entries:
                    tool_key = rec.get("toolKey") or profile_store.profile_tool_key(spec.key, prof_key)
                    rec_loc = rec.get("location") or ""

                    matched_cand = None
                    if prof_key == "default" and default_cand is not None:
                        matched_cand = default_cand
                    elif rec_loc:
                        rec_ids = profile_store._folder_identities(rec_loc, spec.key)
                        for cand in prov_candidates:
                            cand_ids = (
                                profile_store._folder_identities(cand.folder, spec.key)
                                | profile_store._folder_identities(cand.location, spec.key)
                            )
                            if rec_ids & cand_ids:
                                matched_cand = cand
                                break

                    digest = rec.get("accountDigest") or (matched_cand.account_digest if matched_cand else None)
                    plan = rec.get("plan") or (matched_cand.plan if matched_cand else None)

                    if prof_key == "default" and row["externallyManaged"]:
                        prof_state = "externally_managed"
                    elif not is_virtual and not rec.get("enabled"):
                        prof_state = "disabled"
                    elif not is_virtual and rec.get("state") == "needs_attention":
                        prof_state = "error"
                    elif is_virtual and not folder_exists:
                        prof_state = "not_found"
                    elif tool_key and tool_key in recent_keys:
                        prof_state = "receiving"
                    else:
                        prof_state = "added"

                    if digest:
                        if digest not in primary_by_digest:
                            primary_by_digest[digest] = prof_key
                            shares = None
                        else:
                            shares = primary_by_digest[digest]
                        added_digests.setdefault(digest, prof_key)
                    else:
                        shares = None

                    if rec_loc and (not is_virtual or folder_exists):
                        added_folder_ids |= profile_store._folder_identities(rec_loc, spec.key)

                    profiles_out.append(
                        {
                            "key": prof_key,
                            "name": str(rec.get("name") or ("Default" if prof_key == "default" else prof_key)),
                            "location": format_home_location(rec_loc),
                            "origin": str(rec.get("origin") or ("default" if prof_key == "default" else "manual")),
                            "state": prof_state,
                            "revision": int(rec.get("revision", 0)),
                            "plan": plan,
                            "sharesLimitsWith": shares,
                        }
                    )

                dismissed = (state.get("dismissed") or {}).get(spec.key) or {}
                detected_out: list[dict] = []
                for cand in prov_candidates:
                    if cand.is_default:
                        continue
                    if cand.candidate_id in dismissed:
                        continue
                    cand_ids = (
                        profile_store._folder_identities(cand.folder, spec.key)
                        | profile_store._folder_identities(cand.location, spec.key)
                    )
                    if cand_ids & added_folder_ids:
                        continue
                    same_as = added_digests.get(cand.account_digest) if cand.account_digest else None
                    detected_out.append(
                        {
                            "candidateId": cand.candidate_id,
                            "folder": format_home_location(cand.folder),
                            "plan": cand.plan,
                            "sameAccountAs": same_as,
                        }
                    )

                row["profiles"] = profiles_out
                row["detected"] = detected_out

            rows.append(row)
        return {
            "connections": rows,
            "vaultAvailable": self.vault.available(),
            "computerScope": "Locations are on the computer running BURNRATE.",
        }

    def verify(self, body):
        spec = source_spec(body.get("source"))
        if spec.connection.shape == "api":
            raise LocationError("Use Test and connect for a provider API.")
        path = self.inspector.normalize(body.get("location"), spec.connection)
        return {"source": spec.key, "location": path, **self.inspector.inspect(spec.key, path, spec.connection), "lastVerification": now()}

    def discover(self):
        profile_detect.reset_cache()
        rows = self.list()["connections"]
        for row in rows:
            if row["kind"] in {"api", "external"}:
                continue
            spec = source_spec(row["source"])
            if spec.connection.manual_only:
                row["candidates"] = []
                row["detail"] = "Select an already populated supported cache manually. BURNRATE does not run or enroll its producer."
                continue
            # Default and managed location may differ: expose both, never guess.
            candidates = list({identity(path): path for path in [default_location(spec, self.settings), row["location"]]}.values())
            row["candidates"] = []
            for path in candidates:
                try:
                    result = self.verify({"source": spec.key, "location": path})
                except LocationError as exc:
                    result = {"location": path, "state": "needs_attention", "detail": str(exc)}
                row["candidates"].append(result)
        return {"connections": [r for r in rows if r["kind"] not in {"api", "external"} and not source_spec(r["source"]).connection.manual_only]}

    def _resolve_candidate(self, provider: str, candidate_id: str) -> profile_detect.Candidate | None:
        if provider not in profile_detect.PROVIDERS:
            return None
        for cand in profile_detect.detect_all():
            if cand.provider == provider and cand.candidate_id == candidate_id:
                return cand
        for cand in profile_detect.detect(provider):
            if cand.candidate_id == candidate_id:
                return cand
        return None

    def _assert_account_free(
        self,
        state: dict,
        provider: str,
        digest: str | None,
        exclude_key: str | None = None,
    ) -> None:
        if not digest:
            return
        prov_profiles = (state.get("profiles") or {}).get(provider) or {}
        for key, record in prov_profiles.items():
            if exclude_key is not None and key == exclude_key:
                continue
            if isinstance(record, dict) and record.get("accountDigest") == digest:
                raise Conflict("This account is already added under another profile.")

    def mutate(self, body, profile=None):
        if not isinstance(body, dict):
            raise LocationError("Expected a connection object.")
        spec = source_spec(body.get("source"))
        op = body.get("operation", "connect")
        valid_ops = {
            "connect",
            "disable",
            "reconnect",
            "recheck",
            "add_profile",
            "rename_profile",
            "remove_profile",
            "dismiss_candidate",
        }
        if op not in valid_ops:
            raise LocationError("Unsupported connection operation.")

        request_id = body.get("requestId")
        try:
            uuid.UUID(request_id)
        except (ValueError, TypeError, AttributeError):
            raise LocationError("A request identifier is required.") from None

        if op in {"add_profile", "dismiss_candidate"}:
            if not profile_store.supports_profiles(spec.key):
                raise LocationError("Multiple profiles are not supported for this source.")
            if op == "add_profile":
                return self._mutate_add_profile(spec, body, request_id)
            return self._mutate_dismiss_candidate(spec, body, request_id)

        target_profile = profile if profile is not None else (body.get("profile") if body.get("profile") is not None else "default")
        if not isinstance(target_profile, str) or not target_profile.strip():
            raise LocationError("Invalid profile identifier.")
        target_profile = target_profile.strip()
        if target_profile != "default" and not profile_store.supports_profiles(spec.key):
            raise LocationError("Multiple profiles are not supported for this source.")

        revision = body.get("revision")
        if type(revision) is not int or revision < 0:
            raise LocationError("A current binding revision is required.")

        if op == "rename_profile":
            return self._mutate_rename_profile(spec, target_profile, body, request_id, revision)
        if op == "remove_profile":
            return self._mutate_remove_profile(spec, target_profile, body, request_id, revision)

        # Never hash/store a secret. Retries identify the original operation;
        # replacement requires a NEW request identifier.
        public_body = {k: body.get(k) for k in ("source", "operation", "revision", "location", "mode")}
        if target_profile != "default":
            public_body["profile"] = target_profile
        if body.get("name") is not None:
            public_body["name"] = body.get("name")
        fingerprint = hashlib.sha256(json.dumps(public_body, sort_keys=True).encode()).hexdigest()
        new_ref = None
        with source_lock_for(self.settings.database_path, spec.key, target_profile), self.lock:
            state = self.store.read()
            receipt = state["requests"].get(request_id)
            if receipt:
                if receipt["fingerprint"] != fingerprint:
                    raise Conflict("Request identifier already used. Reload connections.")
                return receipt["result"]
            previous = _profile_record(state, spec.key, target_profile)
            if (previous or {}).get("revision", 0) != revision:
                raise Conflict("Connection changed. Reload before saving.")
            if external(spec, self.settings) and not spec.connection.location_env:
                raise Conflict("This source has an external override. Change it outside BURNRATE first; no settings were rewritten.")
            if body.get("mode", "manual") not in {"auto", "manual", "api"}:
                raise LocationError("Choose auto, manual or API mode.")
            if spec.connection.shape == "api" and body.get("mode") == "auto":
                raise LocationError("Provider APIs cannot be automatically connected.")
            detected_candidates = profile_detect.detect_all() if spec.key in profile_detect.PROVIDERS else []
            cands_by_prov = {spec.key: [c for c in detected_candidates if c.provider == spec.key]}
            computed_digest = (previous or {}).get("accountDigest")
            computed_plan = (previous or {}).get("plan")
            if op == "disable":
                default_loc = (
                    default_location(spec, self.settings)
                    if spec.connection.shape != "api" and target_profile == "default"
                    else (body.get("location") or "")
                )
                record = copy.deepcopy(previous or {"source": spec.key, "location": default_loc, "mode": "manual"})
                record.update(enabled=False, state="disabled", detail="Disconnected. Existing measured history is preserved.", credentialRef=None)
            elif spec.connection.shape == "api":
                if not self.vault.available():
                    raise LocationError("Secure Windows credential storage is unavailable. Local connections still work.")
                key = body.get("secret")
                if not key and previous and previous.get("credentialRef"):
                    key = self.vault.get(previous["credentialRef"])
                if not isinstance(key, str) or not key.strip() or len(key) > 4096 or any(c.isspace() for c in key):
                    raise LocationError("Enter the required provider key.")
                self.probe(spec.key, key)
                new_ref = "burnrate-" + uuid.uuid4().hex
                # Journal the owned reference before vault I/O. A process exit
                # between vault and metadata commit leaves a cleanup entry.
                with self.store.transaction() as pending:
                    pending["cleanup"].append(new_ref)
                try:
                    self.vault.put(key, new_ref)
                except Exception:
                    self._cleanup_ref(new_ref)
                    raise
                record = {"source": spec.key, "location": "", "mode": "api", "enabled": True,
                          "credentialRef": new_ref, "state": "awaiting_ingest", "detail": "Credential verified; awaiting scheduled collection.", "lastVerification": now(), "capabilities": ["balance"] if spec.key in BALANCE_SOURCES else ["usage", "provider charges"]}
            else:
                location = body.get("location") or (previous or {}).get("location")
                if op == "recheck" and previous:
                    location = previous["location"]
                try:
                    checked = self.verify({"source": spec.key, "location": location})
                except LocationError as exc:
                    if op != "recheck" or not previous:
                        raise
                    with self.store.transaction() as current:
                        record = _profile_record(current, spec.key, target_profile)
                        if not record or record["revision"] != revision:
                            raise Conflict("Connection changed. Reload before rechecking.")
                        record.update(state="needs_attention" if record["enabled"] else "disabled", detail=redact(str(exc), 1000), lastVerification=now())
                        _set_profile_record(current, spec.key, target_profile, record)
                    raise
                if target_profile == "default" and location_conflict(spec, checked["location"]):
                    raise Conflict("Selected location conflicts with " + spec.connection.location_env + ". Resolve the external setting first.")
                effective_state = _with_virtual_defaults(state, self.settings, cands_by_prov)
                profile_store.assert_folder_free(
                    effective_state, spec.key, checked["location"], exclude_key=target_profile
                )
                digest_found, plan_found = _identity_for_folder(
                    spec.key,
                    checked["location"],
                    is_default=(target_profile == "default"),
                    candidates=detected_candidates,
                )
                if digest_found:
                    computed_digest = digest_found
                if plan_found:
                    computed_plan = plan_found
                self._assert_account_free(
                    effective_state, spec.key, computed_digest, exclude_key=target_profile
                )
                record = {**checked, "mode": body.get("mode", "manual"), "enabled": True,
                          "state": "awaiting_ingest", "detail": "Saved, awaiting first import." if checked["usableSample"] else checked["detail"], "capabilities": ["usage"]}
                if op == "recheck" and previous and not previous["enabled"]:
                    record.update(enabled=False, state="disabled", detail="Location verified; connection remains disabled. Use Reconnect to collect again.")
                # No new authority engine: custom Grok/Traycer combinations cannot
                # reuse the existing default-log cutoff safely.
                other = "traycer_local" if spec.key == "grok_local" else "grok_local"
                if spec.key in {"grok_local", "traycer_local"} and target_profile == "default":
                    peer = REGISTRY.get(other)
                    peer_binding = _profile_record(state, other, "default")
                    custom = identity(record["location"]) != identity(default_location(spec, self.settings))
                    peer_custom = peer_binding and identity(peer_binding["location"]) != identity(default_location(peer, self.settings))
                    # An environment-selected Grok profile is not evidence that
                    # the default Traycer store belongs to that profile.
                    custom = custom or bool(os.getenv("GROK_HOME"))
                    if eligible(peer, state) and (custom or peer_custom):
                        raise Conflict("Custom Grok and Traycer locations cannot be monitored together. Disable the overlapping source first.")
            fallback_name = (previous or {}).get("name") or (
                "Default" if target_profile == "default" else target_profile
            )
            name = _validate_profile_name(body.get("name"), required=False, fallback=fallback_name)
            origin = (previous or {}).get("origin") or (
                "default" if target_profile == "default" else "manual"
            )
            binding_id = spec.key if target_profile == "default" else f"{spec.key}:{target_profile}"
            record.update(
                revision=revision + 1,
                bindingId=binding_id,
                name=name,
                origin=origin,
                accountDigest=computed_digest,
                toolKey=profile_store.profile_tool_key(spec.key, target_profile),
                lastImport=None,
                importRevision=None,
            )
            if computed_plan:
                record["plan"] = computed_plan
            if previous and previous.get("cutoverAt"):
                record["cutoverAt"] = previous["cutoverAt"]
            try:
                with self.store.transaction() as current:
                    current_record = _profile_record(current, spec.key, target_profile)
                    if (current_record or {}).get("revision", 0) != revision:
                        raise Conflict("Connection changed while verifying. Reload before saving.")
                    if spec.connection.shape != "api":
                        current_effective = _with_virtual_defaults(current, self.settings, cands_by_prov)
                        profile_store.assert_folder_free(
                            current_effective, spec.key, record["location"], exclude_key=target_profile
                        )
                        self._assert_account_free(
                            current_effective, spec.key, computed_digest, exclude_key=target_profile
                        )
                    _set_profile_record(current, spec.key, target_profile, record)
                    if new_ref:
                        current["cleanup"] = [r for r in current["cleanup"] if r != new_ref]
                    public = _public_record(record)
                    result = {"connection": public, "requestId": request_id}
                    current["requests"][request_id] = {"fingerprint": fingerprint, "result": result}
                    old_ref = (previous or {}).get("credentialRef")
                    if old_ref and old_ref != new_ref:
                        current["cleanup"].append(old_ref)
            except Exception:
                if new_ref:
                    self._cleanup_ref(new_ref)
                raise
            self.cleanup()
            return result

    def _mutate_add_profile(self, spec, body: dict, request_id: str) -> dict:
        candidate_id = body.get("candidateId")
        raw_location = body.get("location")
        if candidate_id is not None and (not isinstance(candidate_id, str) or not candidate_id.strip()):
            raise LocationError("A candidate identifier is required.")
        candidate_id = candidate_id.strip() if isinstance(candidate_id, str) else None
        if candidate_id is None and (not isinstance(raw_location, str) or not raw_location.strip()):
            raise LocationError("Choose a detected profile or enter a local folder.")
        if body.get("mode", "manual") not in {"auto", "manual", "detected"}:
            raise LocationError("Choose auto, manual or detected mode.")

        public_body = {
            "source": spec.key,
            "operation": "add_profile",
            "candidateId": candidate_id,
            "location": raw_location,
            "name": body.get("name"),
        }
        fingerprint = hashlib.sha256(json.dumps(public_body, sort_keys=True).encode()).hexdigest()

        with source_lock_for(self.settings.database_path, spec.key, "add"), self.lock:
            state = self.store.read()
            receipt = state["requests"].get(request_id)
            if receipt:
                if receipt["fingerprint"] != fingerprint:
                    raise Conflict("Request identifier already used. Reload connections.")
                return receipt["result"]

            candidate = None
            digest = None
            plan = None
            if candidate_id is not None:
                candidate = self._resolve_candidate(spec.key, candidate_id)
                if candidate is None:
                    raise LocationError("Detected profile was not found. Refresh discovery and try again.")
                verify_loc = candidate.location if Path(candidate.location).exists() else str(candidate.folder)
                digest = candidate.account_digest
                plan = candidate.plan
                folder_for_label = candidate.folder
            else:
                verify_loc = raw_location
                folder_for_label = None

            checked = self.verify({"source": spec.key, "location": verify_loc})
            folder_path = (
                folder_for_label
                if folder_for_label is not None
                else _profile_folder_for(spec.key, checked["location"])
            )
            if digest is None:
                digest, detected_plan = _identity_for_folder(
                    spec.key,
                    checked["location"],
                    is_default=False,
                    candidates=profile_detect.detect_all() if spec.key in profile_detect.PROVIDERS else None,
                )
                if plan is None:
                    plan = detected_plan

            name = _validate_profile_name(
                body.get("name"),
                required=False,
                fallback=_folder_label(spec.key, folder_path),
            )

            all_cands = profile_detect.detect_all()
            cands_by_prov: dict[str, list[profile_detect.Candidate]] = {}
            for cand in all_cands:
                cands_by_prov.setdefault(cand.provider, []).append(cand)
            effective_state = _with_virtual_defaults(state, self.settings, cands_by_prov)
            profile_store.assert_folder_free(effective_state, spec.key, folder_path)
            if identity(folder_path) != identity(checked["location"]):
                profile_store.assert_folder_free(effective_state, spec.key, checked["location"])
            self._assert_account_free(effective_state, spec.key, digest)

            with self.store.transaction() as current:
                current_effective = _with_virtual_defaults(current, self.settings, cands_by_prov)
                profile_store.assert_folder_free(current_effective, spec.key, folder_path)
                if identity(folder_path) != identity(checked["location"]):
                    profile_store.assert_folder_free(current_effective, spec.key, checked["location"])
                self._assert_account_free(current_effective, spec.key, digest)

                prov_profiles = current.setdefault("profiles", {}).setdefault(spec.key, {})
                existing_keys = set(prov_profiles.keys()) | {"default"}
                new_key = profile_store.profile_key_for_folder(
                    spec.key, folder_path, existing=existing_keys
                )
                origin = "detected" if candidate is not None else "manual"
                record = {
                    **checked,
                    "source": spec.key,
                    "bindingId": f"{spec.key}:{new_key}",
                    "name": name,
                    "origin": origin,
                    "mode": "detected" if candidate is not None else body.get("mode", "manual"),
                    "enabled": True,
                    "revision": 1,
                    "state": "awaiting_ingest",
                    "detail": (
                        "Saved, awaiting first import."
                        if checked.get("usableSample")
                        else checked.get("detail")
                    ),
                    "lastImport": None,
                    "importRevision": None,
                    "capabilities": ["usage"],
                    "accountDigest": digest,
                    "toolKey": profile_store.profile_tool_key(spec.key, new_key),
                }
                if plan:
                    record["plan"] = plan
                _set_profile_record(current, spec.key, new_key, record)
                if candidate_id is not None:
                    dismissed = (current.get("dismissed") or {}).get(spec.key)
                    if isinstance(dismissed, dict):
                        dismissed.pop(candidate_id, None)
                public = {**_public_record(record), "key": new_key}
                result = {"connection": public, "requestId": request_id}
                current["requests"][request_id] = {"fingerprint": fingerprint, "result": result}
            return result

    def _mutate_rename_profile(
        self, spec, target_profile: str, body: dict, request_id: str, revision: int
    ) -> dict:
        name = _validate_profile_name(body.get("name"), required=True)
        public_body = {
            "source": spec.key,
            "operation": "rename_profile",
            "profile": target_profile,
            "revision": revision,
            "name": name,
        }
        fingerprint = hashlib.sha256(json.dumps(public_body, sort_keys=True).encode()).hexdigest()

        with source_lock_for(self.settings.database_path, spec.key, target_profile), self.lock:
            state = self.store.read()
            receipt = state["requests"].get(request_id)
            if receipt:
                if receipt["fingerprint"] != fingerprint:
                    raise Conflict("Request identifier already used. Reload connections.")
                return receipt["result"]

            previous = _profile_record(state, spec.key, target_profile)
            if previous is None:
                if revision != 0:
                    raise Conflict("Connection changed. Reload before saving.")
                if target_profile != "default":
                    raise LocationError("Profile was not found.")
                exists, _def_folder, def_loc = _default_folder_info(spec, self.settings)
                if not exists:
                    raise LocationError("Default profile folder was not found.")
                cands = profile_detect.detect_all() if spec.key in profile_detect.PROVIDERS else []
                digest, plan = _identity_for_folder(spec.key, def_loc, is_default=True, candidates=cands)
                base_record = {
                    "source": spec.key,
                    "bindingId": spec.key,
                    "name": name,
                    "location": def_loc,
                    "origin": "default",
                    "mode": "manual",
                    "enabled": True,
                    "revision": 1,
                    "state": "awaiting_ingest",
                    "detail": "Default location monitored.",
                    "lastVerification": None,
                    "lastImport": None,
                    "importRevision": None,
                    "capabilities": ["usage"],
                    "accountDigest": digest,
                    "toolKey": profile_store.profile_tool_key(spec.key, "default"),
                }
                if plan:
                    base_record["plan"] = plan
            else:
                if previous.get("revision", 0) != revision:
                    raise Conflict("Connection changed. Reload before saving.")
                base_record = copy.deepcopy(previous)
                base_record["name"] = name
                base_record["revision"] = revision + 1

            with self.store.transaction() as current:
                current_rec = _profile_record(current, spec.key, target_profile)
                if (current_rec or {}).get("revision", 0) != revision:
                    raise Conflict("Connection changed. Reload before saving.")
                _set_profile_record(current, spec.key, target_profile, base_record)
                public = {**_public_record(base_record), "key": target_profile}
                result = {"connection": public, "requestId": request_id}
                current["requests"][request_id] = {"fingerprint": fingerprint, "result": result}
            return result

    def _mutate_remove_profile(
        self, spec, target_profile: str, body: dict, request_id: str, revision: int
    ) -> dict:
        public_body = {
            "source": spec.key,
            "operation": "remove_profile",
            "profile": target_profile,
            "revision": revision,
        }
        fingerprint = hashlib.sha256(json.dumps(public_body, sort_keys=True).encode()).hexdigest()

        with source_lock_for(self.settings.database_path, spec.key, target_profile), self.lock:
            state = self.store.read()
            receipt = state["requests"].get(request_id)
            if receipt:
                if receipt["fingerprint"] != fingerprint:
                    raise Conflict("Request identifier already used. Reload connections.")
                return receipt["result"]

            previous = _profile_record(state, spec.key, target_profile)
            if previous is None:
                if revision != 0:
                    raise Conflict("Connection changed. Reload before saving.")
                if target_profile != "default":
                    raise LocationError("Profile was not found.")
            elif previous.get("revision", 0) != revision:
                raise Conflict("Connection changed. Reload before saving.")

            if target_profile == "default":
                if external(spec, self.settings) and not spec.connection.location_env:
                    raise Conflict("This source has an external override. Change it outside BURNRATE first; no settings were rewritten.")
                if previous is None:
                    default_loc = "" if spec.connection.shape == "api" else default_location(spec, self.settings)
                    record = {
                        "source": spec.key,
                        "bindingId": spec.key,
                        "name": "Default",
                        "location": default_loc,
                        "origin": "default",
                        "mode": "api" if spec.connection.shape == "api" else "manual",
                        "enabled": False,
                        "revision": 1,
                        "state": "disabled",
                        "detail": "Disconnected. Existing measured history is preserved.",
                        "credentialRef": None,
                        "lastVerification": None,
                        "lastImport": None,
                        "importRevision": None,
                        "capabilities": ["balance"] if spec.key in BALANCE_SOURCES else (["usage", "provider charges"] if spec.connection.shape == "api" else ["usage"]),
                        "accountDigest": None,
                        "toolKey": profile_store.profile_tool_key(spec.key, "default"),
                    }
                else:
                    record = copy.deepcopy(previous)
                    record.update(
                        enabled=False,
                        state="disabled",
                        detail="Disconnected. Existing measured history is preserved.",
                        credentialRef=None,
                        revision=revision + 1,
                    )
                with self.store.transaction() as current:
                    current_rec = _profile_record(current, spec.key, "default")
                    if (current_rec or {}).get("revision", 0) != revision:
                        raise Conflict("Connection changed. Reload before saving.")
                    old_ref = (current_rec or {}).get("credentialRef")
                    if old_ref:
                        current["cleanup"].append(old_ref)
                    _set_profile_record(current, spec.key, "default", record)
                    public = {**_public_record(record), "key": "default"}
                    result = {"connection": public, "disabled": True, "requestId": request_id}
                    current["requests"][request_id] = {"fingerprint": fingerprint, "result": result}
                self.cleanup()
                return result

            with self.store.transaction() as current:
                current_rec = _profile_record(current, spec.key, target_profile)
                if current_rec is None:
                    raise LocationError("Profile was not found.")
                if current_rec.get("revision", 0) != revision:
                    raise Conflict("Connection changed. Reload before saving.")
                old_ref = current_rec.get("credentialRef")
                if old_ref:
                    current["cleanup"].append(old_ref)
                prov_profiles = (current.get("profiles") or {}).get(spec.key)
                if isinstance(prov_profiles, dict):
                    prov_profiles.pop(target_profile, None)
                result = {
                    "removed": True,
                    "source": spec.key,
                    "profile": target_profile,
                    "requestId": request_id,
                }
                current["requests"][request_id] = {"fingerprint": fingerprint, "result": result}
            self.cleanup()
            return result

    def _mutate_dismiss_candidate(self, spec, body: dict, request_id: str) -> dict:
        raw_cid = body.get("candidateId")
        if not isinstance(raw_cid, str) or not raw_cid.strip() or len(raw_cid.strip()) > 128:
            raise LocationError("A candidate identifier is required.")
        candidate_id = raw_cid.strip()
        public_body = {
            "source": spec.key,
            "operation": "dismiss_candidate",
            "candidateId": candidate_id,
        }
        fingerprint = hashlib.sha256(json.dumps(public_body, sort_keys=True).encode()).hexdigest()

        with source_lock_for(self.settings.database_path, spec.key, "dismiss"), self.lock:
            state = self.store.read()
            receipt = state["requests"].get(request_id)
            if receipt:
                if receipt["fingerprint"] != fingerprint:
                    raise Conflict("Request identifier already used. Reload connections.")
                return receipt["result"]
            with self.store.transaction() as current:
                current.setdefault("dismissed", {}).setdefault(spec.key, {})[candidate_id] = now()
                result = {
                    "dismissed": True,
                    "source": spec.key,
                    "candidateId": candidate_id,
                    "requestId": request_id,
                }
                current["requests"][request_id] = {"fingerprint": fingerprint, "result": result}
            return result

    def _cleanup_ref(self, ref):
        try:
            self.vault.delete(ref)
        except Exception:
            with self.store.transaction() as state:
                if ref not in state["cleanup"]:
                    state["cleanup"].append(ref)

    def _protected_v1_refs(self):
        path = getattr(self.store, "path", None)
        if not path:
            return set()
        try:
            Path(path).stat()
        except OSError:
            return set()
        with closing(sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True)) as db:
            return profile_store.v1_credential_refs(db)

    def cleanup(self):
        # Leave Vault references still named in frozen connections.v1 intact so
        # rollback to v1 works; those secrets are retired when v1 is removed.
        cleanup_refs = self.store.read().get("cleanup", [])
        if not cleanup_refs:
            return
        protected = self._protected_v1_refs()
        for ref in cleanup_refs:
            if ref not in protected:
                try:
                    self.vault.delete(ref)
                except Exception:
                    continue
            with self.store.transaction() as state:
                state["cleanup"] = [r for r in state["cleanup"] if r != ref]

    def completion(self, source, revision, result, profile="default"):
        with self.store.transaction() as state:
            record = _profile_record(state, source, profile)
            if not record or not record["enabled"] or record["revision"] != revision:
                return
            status = result.get("status")
            if status in {"success", "partial"}:
                accepted = result.get("eventsAccepted", result.get("eventsSeen", 0))
                if not accepted and result.get("issues"):
                    record.update(state="needs_attention", detail=redact("Source metadata was read, but usage could not be accepted safely: " + ", ".join(str(i) for i in result["issues"]), 1000))
                    _set_profile_record(state, source, profile, record)
                    return
                seen = accepted > 0 or record["state"] == "receiving_usage"
                record.update(lastImport=now(), importRevision=revision,
                              state=("balance_available" if source in BALANCE_SOURCES else "receiving_usage") if (seen or source in BALANCE_SOURCES) else "waiting_activity",
                              detail=("Balance updated; usage events are a separate connection." if source in BALANCE_SOURCES else "Import completed. Missing pricing and quota do not disconnect usage.") if (seen or source in BALANCE_SOURCES) else "Readable; waiting for usage. No paid turn is required to finish setup.")
                if result.get("issues"):
                    record["detail"] = redact(record["detail"] + " Some records need attention: " + "; ".join(redact(str(issue)).replace("_", " ") for issue in sorted(set(result["issues"]))[:3]) + ". Review the provider compatibility guidance before changing the source.", 1000)
            else:
                record.update(state="needs_attention", detail="Import unavailable. Recheck location, permissions or provider access; collection will retry on its normal cadence.")
            _set_profile_record(state, source, profile, record)


def managed_job(settings, pricing, spec, window=None, profile="default"):
    """Resolve at execution time, not when the scheduler was constructed."""
    service = Service(settings)
    state = service.store.read()
    binding = _profile_record(state, spec.key, profile)
    if binding and not binding["enabled"]:
        return service, binding, None
    if profile != "default":
        if not binding:
            return service, None, None
    elif not eligible(spec, state) and not (spec.connection and external(spec, settings)):
        return service, None, None
    if spec.key in {"grok_local", "traycer_local"} and profile == "default":
        peers = [REGISTRY.get(key) for key in ("grok_local", "traycer_local")]
        custom = bool(os.getenv("GROK_HOME")) or any(
            _profile_record(state, peer.key, "default")
            and identity(_profile_record(state, peer.key, "default")["location"]) != identity(default_location(peer, settings))
            for peer in peers)
        if custom and all(eligible(peer, state) for peer in peers):
            raise Conflict("Selected Grok/Traycer profiles have no proven shared authority. Disable one overlapping source first.")
    if spec.key == "traycer_local" or binding and spec.connection and spec.connection.shape != "api":
        kwargs = {"database_path": settings.database_path, "pricing": pricing}
        if spec.key == "traycer_local":
            from spend_app.providers import default_grok_log, default_traycer_glob, grok_coverage_start
            kwargs["database_glob"] = default_traycer_glob()
            kwargs["grok_covered_from"] = grok_coverage_start(default_grok_log(), settings.database_path) if eligible(REGISTRY.get("grok_local"), state) else None
    else:
        kwargs = spec.build_ingest_kwargs(settings, pricing, **(window or {}))
    if binding and spec.connection:
        meta = spec.connection
        if not binding["enabled"] and (profile != "default" or not external(spec, settings)):
            return service, binding, None
        if profile != "default" or not external(spec, settings) or meta.location_env:
            if meta.shape == "api":
                kwargs[meta.argument] = service.vault.get(binding["credentialRef"])
            else:
                path = service.inspector.normalize(binding["location"], meta)
                if profile == "default" and location_conflict(spec, path):
                    raise Conflict("Saved location conflicts with " + meta.location_env + "; collection was not redirected.")
                if identity(path) != identity(binding["location"]):
                    raise LocationError("Saved location moved. Recheck before collecting.")
                service.inspector.inspect(spec.key, path, meta)
                if meta.suffix and Path(path).is_dir() and meta.argument != "import_path":
                    kwargs[meta.argument] = str(Path(path) / meta.suffix)
                else:
                    kwargs[meta.argument] = Path(path) if meta.argument not in {"session_glob", "database_glob"} else path
    if spec.skip_if and not binding and spec.skip_if(settings):
        return service, binding, None
    return service, binding, kwargs


def execute(settings, pricing, spec, ingest, window=None, profile="default"):
    # Saves serialize with the existing job per source, so a successful save
    # cannot be followed by an obsolete collector starting or reporting healthy,
    # while independent sources and quota/activity readers are not starved by
    # one slow local transcript scan.
    with source_lock_for(settings.database_path, spec.key, profile):
        try:
            with lock_for(settings.database_path):
                service = Service(settings)
                service.cleanup()
                binding = _profile_record(service.store.read(), spec.key, profile)
                service, binding, kwargs = managed_job(settings, pricing, spec, window, profile=profile)
                if kwargs is None:
                    return {"status": "skipped", "eventsSeen": 0}
                root = binding["location"] if binding and spec.connection.shape != "api" else None
                if not binding and spec.connection and spec.connection.shape != "api" and not spec.connection.manual_only:
                    root = service.inspector.normalize(default_location(spec, settings), spec.connection)
            with approved(root, binding["revision"] if binding else 0, binding.get("patterns") if binding else None):
                result = ingest(**kwargs)
        except MissingLocation:
            if not binding and spec.connection and not external(spec, settings):
                from spend_app.adapters.common import skipped_result
                return skipped_result(database_path=settings.database_path, source=spec.key,
                    reason="Default local source was not detected. Install or connect this optional harness when needed.")
            if binding:
                with lock_for(settings.database_path):
                    service.completion(spec.key, binding["revision"], {"status": "failed"}, profile=profile)
            raise LocationError("Configured source is missing or moved. Recheck its saved location or explicit environment path.") from None
        except Exception:
            if binding:
                with lock_for(settings.database_path):
                    service.completion(spec.key, binding["revision"], {"status": "failed"}, profile=profile)
                raise LocationError("Connected source import failed. Recheck its location, permissions or provider access.") from None
            raise
        if binding:
            with lock_for(settings.database_path):
                service.completion(spec.key, binding["revision"], result, profile=profile)
        return result


def serialize_collection(function):
    from functools import wraps
    @wraps(function)
    def wrapped(database_path, *args, **kwargs):
        with lock_for(database_path):
            return function(database_path, *args, **kwargs)
    return wrapped
