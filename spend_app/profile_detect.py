"""Bounded local profile detection for Claude Code, OpenCode, and Codex.

Scans only each provider's default folder, shallow home globs, and explicit
``BURNRATE_*`` profile-directory environment variables. Reads only identity
files (never transcripts or prompts) and never returns, persists, or logs
tokens or raw account identifiers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from spend_app import claude_accounts, opencode_profiles
from spend_app.connection_paths import _resolve, identity, is_junction

PROVIDERS: tuple[str, ...] = ("claude_local", "opencode_local", "codex_local")
CODEX_PREFIX = "codex-"
CODEX_EXTRA_DIRS_ENV = "BURNRATE_CODEX_PROFILE_DIRS"
MAX_CANDIDATES = 20
_MAX_SCAN_DIRS = MAX_CANDIDATES * 2
_MAX_SAMPLE_ENTRIES = 512
DISCOVERY_TTL_SECONDS = 60

_ENV_CACHE_KEYS: tuple[str, ...] = (
    "CLAUDE_CONFIG_DIR",
    claude_accounts.EXTRA_DIRS_ENV,
    "XDG_DATA_HOME",
    "OPENCODE_DB",
    "OPENCODE_API_KEY",
    opencode_profiles.EXTRA_DIRS_ENV,
    "CODEX_HOME",
    CODEX_EXTRA_DIRS_ENV,
)

_LOCK = threading.Lock()
_DISCOVERED: dict = {"at": None, "key": None, "candidates": ()}
_DIGEST_CACHE: dict[tuple, tuple[float, str | None]] = {}


@dataclass(frozen=True)
class Candidate:
    provider: str               # "claude_local" | "opencode_local" | "codex_local"
    folder: Path                # profile root (Claude: config dir; Codex: CODEX_HOME-style dir; OpenCode: data dir)
    location: str               # what the adapter reads
    key: str                    # "default" for the default folder, else the folder-derived key
    account_digest: str | None  # sha256 hex of the account id, or None
    plan: str | None
    has_sample: bool
    is_default: bool
    candidate_id: str           # stable: account_digest or sha256(resolved folder)


def reset_cache() -> None:
    with _LOCK:
        _DISCOVERED.update(at=None, key=None, candidates=())
        _DIGEST_CACHE.clear()


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _folder_id(resolved: Path) -> str:
    return _sha256_hex(identity(resolved))


def _resolve_folder(path: Path) -> Path | None:
    try:
        return _resolve(path, strict=True)
    except OSError:
        return None


def _is_safe_dir(path: Path) -> bool:
    try:
        return not path.is_symlink() and not is_junction(path) and path.is_dir()
    except OSError:
        return False


def _is_safe_file(path: Path) -> bool:
    try:
        return not path.is_symlink() and not is_junction(path) and path.is_file()
    except OSError:
        return False


def _has_jsonl_sample(root: Path, *, max_entries: int = _MAX_SAMPLE_ENTRIES) -> bool:
    if not _is_safe_dir(root):
        return False
    visited = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            scan = os.scandir(current)
        except OSError:
            continue
        with scan as entries:
            for entry in entries:
                visited += 1
                if visited > max_entries:
                    return False
                try:
                    if entry.is_symlink() or is_junction(entry.path):
                        continue
                    if entry.is_file(follow_symlinks=False):
                        if entry.name.lower().endswith(".jsonl"):
                            return True
                    elif entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                except OSError:
                    continue
    return False


def _provider_dirs(
    default_dir: Path,
    home: Path,
    environ,
    glob_pattern: str,
    extra_env: str,
) -> list[tuple[Path, Path, bool]]:
    found: list[tuple[Path, Path, bool]] = []
    seen_folders: set[str] = set()
    if _is_safe_dir(default_dir):
        resolved = _resolve_folder(default_dir)
        if resolved is not None:
            seen_folders.add(identity(resolved))
            found.append((default_dir, resolved, True))

    resolved_home = _resolve_folder(home)
    if resolved_home is not None:
        home_id = identity(resolved_home)
        try:
            entries = sorted(home.glob(glob_pattern))
        except OSError:
            entries = []
        for entry in entries:
            if not _is_safe_dir(entry):
                continue
            resolved = _resolve_folder(entry)
            if resolved is None or not resolved.is_relative_to(resolved_home):
                continue
            folder_id = identity(resolved)
            if folder_id == home_id or folder_id in seen_folders:
                continue
            seen_folders.add(folder_id)
            found.append((entry, resolved, False))
            if len(found) >= _MAX_SCAN_DIRS:
                return found

    for raw in str(environ.get(extra_env) or "").split(os.pathsep):
        text = raw.strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if not _is_safe_dir(path):
            continue
        resolved = _resolve_folder(path)
        if resolved is None:
            continue
        folder_id = identity(resolved)
        if folder_id in seen_folders:
            continue
        seen_folders.add(folder_id)
        found.append((path, resolved, False))
        if len(found) >= _MAX_SCAN_DIRS:
            break
    return found


def _codex_slug(directory: Path) -> str:
    name = directory.name.lower()
    name = name[len(".codex-"):] if name.startswith(".codex-") else name.lstrip(".")
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")[:24].strip("-")
    return slug or "profile"


def _codex_account_digest(auth_path: Path) -> str | None:
    if not _is_safe_file(auth_path):
        return None
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return None
    account_id = tokens.get("account_id")
    if not isinstance(account_id, str) or not account_id.strip():
        return None
    return _sha256_hex(account_id.strip())


def _opencode_key_digests(database: Path, environ, *, is_default: bool) -> list[str]:
    from spend_app.limits import _opencode_credential

    keys: list[str] = []
    if _is_safe_file(database):
        console_key = _opencode_credential("opencode", database)
        if console_key:
            keys.append(console_key)
        auth_path = database.parent / "auth.json"
        if _is_safe_file(auth_path):
            try:
                entry = (json.loads(auth_path.read_text(encoding="utf-8")) or {}).get("opencode-go") or {}
                if isinstance(entry, dict) and entry.get("type") == "api":
                    go_key = str(entry.get("key") or "").strip()
                    if go_key:
                        keys.append(go_key)
            except (OSError, ValueError, AttributeError):
                pass
    if is_default:
        env_key = str(environ.get("OPENCODE_API_KEY") or "").strip()
        if env_key:
            keys.append(env_key)
    digests: list[str] = []
    seen: set[str] = set()
    for key in keys:
        digest = _sha256_hex(key)
        if digest not in seen:
            seen.add(digest)
            digests.append(digest)
    return digests


def _normalize_location_path(folder: str | Path) -> Path:
    text = str(folder).strip()
    if text.endswith(("/**/*.jsonl", "\\**\\*.jsonl")):
        text = text[:-11]
    elif text.endswith("*.jsonl"):
        text = str(Path(text).parent)
    return Path(text).expanduser()


def _read_current_account_digest(
    provider: str,
    folder: Path,
    base_home: Path,
    env,
    *,
    is_default: bool | None,
) -> str | None:
    if provider == "claude_local":
        root = folder.parent if folder.name.lower() in {"projects", "transcripts"} and folder.parent != folder else folder
        try:
            if root.is_symlink() or is_junction(root):
                return None
        except OSError:
            return None
        configured = str(env.get("CLAUDE_CONFIG_DIR") or "").strip()
        default_dir = Path(configured).expanduser() if configured else base_home / ".claude"
        if is_default is not None:
            effective_default = is_default
        else:
            resolved_root = _resolve_folder(root)
            resolved_def = _resolve_folder(default_dir)
            if resolved_root is not None and resolved_def is not None:
                effective_default = identity(resolved_root) == identity(resolved_def)
            else:
                effective_default = identity(root) == identity(default_dir)
        if not effective_default and not _is_safe_dir(root):
            return None
        state_path = claude_accounts.primary_state_path(base_home, env) if effective_default else root / ".claude.json"
        if not _is_safe_file(state_path):
            return None
        state = claude_accounts.read_state(state_path)
        uid = claude_accounts.account_uuid(state)
        return _sha256_hex(uid) if uid else None

    if provider == "codex_local":
        root = folder.parent if folder.name.lower() in {"sessions", "archived_sessions"} and folder.parent != folder else folder
        if not _is_safe_dir(root):
            return None
        return _codex_account_digest(root / "auth.json")

    if provider == "opencode_local":
        if folder.suffix.lower() in {".db", ".sqlite"}:
            database = folder
            root = (
                folder.parent.parent
                if folder.parent.name.lower() == "opencode"
                and folder.parent.parent != folder.parent
                and folder.parent.parent.name.lower().startswith(".opencode-")
                else folder.parent
            )
        else:
            root = folder
            if not _is_safe_dir(root):
                return None
            database = opencode_profiles.database_for(root)
        if database is None or not _is_safe_dir(root) or not _is_safe_file(database):
            return None
        if database.parent != root and not _is_safe_dir(database.parent):
            return None
        if is_default is not None:
            effective_default = is_default
        else:
            configured_db = str(env.get("OPENCODE_DB") or "").strip()
            if configured_db:
                default_db = Path(configured_db).expanduser()
            else:
                xdg = str(env.get("XDG_DATA_HOME") or "").strip()
                base = Path(xdg).expanduser() if xdg else base_home / ".local" / "share"
                default_db = base / "opencode" / "opencode.db"
            resolved_db = _resolve_folder(database)
            resolved_def_db = _resolve_folder(default_db)
            if resolved_db is not None and resolved_def_db is not None:
                effective_default = identity(resolved_db) == identity(resolved_def_db)
            else:
                effective_default = identity(database) == identity(default_db)
        digests = _opencode_key_digests(database, env, is_default=effective_default)
        return digests[0] if digests else None

    return None


def current_account_digest(
    provider: str,
    folder: str | Path | None,
    *,
    is_default: bool | None = None,
    home=None,
    environ=None,
    use_cache: bool = True,
) -> str | None:
    """Return the current account digest on disk for ``provider`` at ``folder``.

    Read-only; reuses each provider's bounded identity reader and caches
    results for ``DISCOVERY_TTL_SECONDS`` (60 s) unless ``use_cache=False``.
    """
    if provider not in PROVIDERS or folder is None:
        return None
    if isinstance(folder, str) and not folder.strip():
        return None
    path = _normalize_location_path(folder)
    base_home = Path(home).expanduser() if home is not None else Path.home()
    env = os.environ if environ is None else environ
    cache_key = (
        provider,
        identity(path),
        is_default,
        str(base_home),
        tuple((key, str(env.get(key) or "")) for key in _ENV_CACHE_KEYS),
    )
    if use_cache:
        now = time.monotonic()
        with _LOCK:
            cached = _DIGEST_CACHE.get(cache_key)
            if cached is not None and now - cached[0] < DISCOVERY_TTL_SECONDS:
                return cached[1]

    digest = _read_current_account_digest(
        provider, path, base_home, env, is_default=is_default
    )
    with _LOCK:
        _DIGEST_CACHE[cache_key] = (time.monotonic(), digest)
    return digest


def _detect_claude(home: Path, environ) -> list[Candidate]:
    configured = str(environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    default_dir = Path(configured).expanduser() if configured else home / ".claude"
    dirs = _provider_dirs(default_dir, home, environ, ".claude-*", claude_accounts.EXTRA_DIRS_ENV)
    candidates: list[Candidate] = []
    seen_digests: set[str] = set()
    seen_keys: set[str] = set()
    for folder, resolved, is_default in dirs:
        state_path = claude_accounts.primary_state_path(home, environ) if is_default else folder / ".claude.json"
        state = claude_accounts.read_state(state_path) if _is_safe_file(state_path) else {}
        uuid = claude_accounts.account_uuid(state)
        account_digest = _sha256_hex(uuid) if uuid else None
        if account_digest is not None and account_digest in seen_digests:
            continue
        key = "default" if is_default else (claude_accounts.PREFIX + claude_accounts.slug_for(folder))
        if key in seen_keys:
            continue
        if account_digest is not None:
            seen_digests.add(account_digest)
        seen_keys.add(key)
        candidates.append(
            Candidate(
                provider="claude_local",
                folder=folder,
                location=str(folder / "projects"),
                key=key,
                account_digest=account_digest,
                plan=claude_accounts.plan_name(state),
                has_sample=_has_jsonl_sample(folder / "projects"),
                is_default=is_default,
                candidate_id=account_digest or _folder_id(resolved),
            )
        )
        if len(candidates) >= MAX_CANDIDATES:
            break
    return candidates


def _detect_opencode(home: Path, environ) -> list[Candidate]:
    configured_db = str(environ.get("OPENCODE_DB") or "").strip()
    if configured_db:
        default_db = Path(configured_db).expanduser()
        default_dir = default_db.parent
    else:
        xdg = str(environ.get("XDG_DATA_HOME") or "").strip()
        base = Path(xdg).expanduser() if xdg else home / ".local" / "share"
        default_dir = base / "opencode"
        default_db = default_dir / "opencode.db"

    dirs = _provider_dirs(default_dir, home, environ, ".opencode-*", opencode_profiles.EXTRA_DIRS_ENV)
    candidates: list[Candidate] = []
    seen_digests: set[str] = set()
    seen_databases: set[str] = set()
    seen_keys: set[str] = set()
    for folder, resolved, is_default in dirs:
        if is_default:
            database = default_db
            has_sample = _is_safe_file(database)
        else:
            database = opencode_profiles.database_for(folder)
            if database is None:
                continue
            if database.parent != folder and not _is_safe_dir(database.parent):
                continue
            if not _is_safe_file(database):
                continue
            has_sample = True
        resolved_db = _resolve_folder(database) if has_sample else None
        if resolved_db is not None:
            db_id = identity(resolved_db)
            if db_id in seen_databases:
                continue
        else:
            db_id = None
        digests = _opencode_key_digests(database, environ, is_default=is_default) if has_sample else []
        account_digest = digests[0] if digests else None
        if digests and any(digest in seen_digests for digest in digests):
            continue
        key = "default" if is_default else (opencode_profiles.PREFIX + opencode_profiles.slug_for(folder))
        if key in seen_keys:
            continue
        if db_id is not None:
            seen_databases.add(db_id)
        seen_digests.update(digests)
        seen_keys.add(key)
        candidates.append(
            Candidate(
                provider="opencode_local",
                folder=folder,
                location=str(database),
                key=key,
                account_digest=account_digest,
                plan=None,
                has_sample=has_sample,
                is_default=is_default,
                candidate_id=account_digest or _folder_id(resolved),
            )
        )
        if len(candidates) >= MAX_CANDIDATES:
            break
    return candidates


def _detect_codex(home: Path, environ) -> list[Candidate]:
    configured = str(environ.get("CODEX_HOME") or "").strip()
    default_dir = Path(configured).expanduser() if configured else home / ".codex"
    dirs = _provider_dirs(default_dir, home, environ, ".codex-*", CODEX_EXTRA_DIRS_ENV)
    candidates: list[Candidate] = []
    seen_digests: set[str] = set()
    seen_keys: set[str] = set()
    for folder, resolved, is_default in dirs:
        account_digest = _codex_account_digest(folder / "auth.json")
        if account_digest is not None and account_digest in seen_digests:
            continue
        key = "default" if is_default else (CODEX_PREFIX + _codex_slug(folder))
        if key in seen_keys:
            continue
        if account_digest is not None:
            seen_digests.add(account_digest)
        seen_keys.add(key)
        candidates.append(
            Candidate(
                provider="codex_local",
                folder=folder,
                location=str(folder / "sessions"),
                key=key,
                account_digest=account_digest,
                plan=None,
                has_sample=_has_jsonl_sample(folder / "sessions"),
                is_default=is_default,
                candidate_id=account_digest or _folder_id(resolved),
            )
        )
        if len(candidates) >= MAX_CANDIDATES:
            break
    return candidates


_DETECTORS = {
    "claude_local": _detect_claude,
    "opencode_local": _detect_opencode,
    "codex_local": _detect_codex,
}


def detect(provider: str, *, home=None, environ=None) -> list[Candidate]:
    detector = _DETECTORS.get(provider)
    if detector is None:
        raise ValueError(f"Unsupported provider for profile detection: {provider!r}")
    base_home = Path(home).expanduser() if home is not None else Path.home()
    env = os.environ if environ is None else environ
    return detector(base_home, env)


def detect_all(*, home=None, environ=None) -> list[Candidate]:
    base_home = Path(home).expanduser() if home is not None else Path.home()
    env = os.environ if environ is None else environ
    marker = (
        str(base_home),
        tuple((key, str(env.get(key) or "")) for key in _ENV_CACHE_KEYS),
    )
    now = time.monotonic()
    with _LOCK:
        at = _DISCOVERED["at"]
        if at is not None and now - at < DISCOVERY_TTL_SECONDS and _DISCOVERED["key"] == marker:
            return list(_DISCOVERED["candidates"])
    candidates: list[Candidate] = []
    for provider in PROVIDERS:
        candidates.extend(detect(provider, home=base_home, environ=env))
    snapshot = tuple(candidates)
    with _LOCK:
        _DISCOVERED.update(at=time.monotonic(), key=marker, candidates=snapshot)
    return list(snapshot)
