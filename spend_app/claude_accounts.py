"""Claude Code accounts on this computer beyond the default profile.

Claude Code keeps one signed-in account per config directory: the default
profile (``~/.claude``, state in ``~/.claude.json``) and any directory chosen
with ``CLAUDE_CONFIG_DIR``, conventionally ``~/.claude-<name>`` (state in
``<dir>/.claude.json``). Each extra account gets its own quota lane,
``claude-code-<name>``, beside the default ``claude-code`` lane.

Only the account UUID, its plan tier and Claude Code's own cached usage are
read from the state file. The account's email address is never surfaced; the
lane is named after the directory ("Work" for ``~/.claude-work``).
Directories with other names can be listed in ``BURNRATE_CLAUDE_ACCOUNT_DIRS``
(separated by the OS path separator).

Other tools run Claude Code with a config directory of their own (Traycer keeps
one per profile under ``~/.traycer/host/harness-accounts/claude-code``). Those
folders never become lanes; they are only extra places to read an existing
account's data, e.g. a fresher cached usage reading (``account_folders``).
Further roots can be listed in ``BURNRATE_CLAUDE_HARNESS_DIRS``.
"""

from __future__ import annotations

from contextlib import closing
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

PRIMARY_KEY = "claude-code"
PREFIX = "claude-code-"
EXTRA_DIRS_ENV = "BURNRATE_CLAUDE_ACCOUNT_DIRS"
HARNESS_DIRS_ENV = "BURNRATE_CLAUDE_HARNESS_DIRS"  # extra harness roots, os.pathsep-separated
DISCOVERY_TTL_SECONDS = 60
MAX_HARNESS_CHILDREN = 128

_LOCK = threading.Lock()
_DISCOVERED: dict = {"at": None, "key": None, "accounts": ()}
_FOLDERS: dict = {"at": None, "key": None, "folders": ()}


@dataclass(frozen=True)
class ClaudeAccount:
    key: str
    label: str
    config_dir: Path
    state_path: Path
    account_uuid: str
    plan: str | None


@dataclass(frozen=True)
class AccountFolder:
    config_dir: Path
    state_path: Path  # primary profile: primary_state_path(home, environ); others: config_dir / ".claude.json"
    is_primary: bool


def reset_cache() -> None:
    with _LOCK:
        _DISCOVERED.update(at=None, key=None, accounts=())
        _FOLDERS.update(at=None, key=None, folders=())


def read_state(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def account_uuid(state: dict) -> str | None:
    account = state.get("oauthAccount")
    value = account.get("accountUuid") if isinstance(account, dict) else None
    return value if isinstance(value, str) and value else None


def plan_name(state: dict) -> str | None:
    account = state.get("oauthAccount")
    tier = str(account.get("organizationRateLimitTier") or "") if isinstance(account, dict) else ""
    tier = tier.lower()
    if "max" in tier:
        return "Claude Max"
    if "pro" in tier:
        return "Claude Pro"
    if "team" in tier:
        return "Claude Team"
    if "enterprise" in tier:
        return "Claude Enterprise"
    return None


def primary_state_path(home: Path, environ) -> Path:
    configured = str(environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    return Path(configured).expanduser() / ".claude.json" if configured else home / ".claude.json"


def slug_for(directory: Path) -> str:
    name = directory.name.lower()
    name = name[len(".claude-"):] if name.startswith(".claude-") else name.lstrip(".")
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")[:24].strip("-")
    return slug or "account"


def label_for(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.split("-") if part) or "Account"


def _candidates(home: Path, environ) -> list[Path]:
    from spend_app.connection_paths import is_junction

    found = []
    try:
        for entry in sorted(home.glob(".claude-*")):
            if entry.is_dir() and not entry.is_symlink() and not is_junction(entry):
                found.append(entry)
    except OSError:
        pass
    for raw in str(environ.get(EXTRA_DIRS_ENV) or "").split(os.pathsep):
        if raw.strip():
            path = Path(raw.strip()).expanduser()
            if path.is_dir() and not path.is_symlink() and not is_junction(path):
                found.append(path)
    return found


def discover(home: Path | None = None, environ=None) -> tuple[ClaudeAccount, ...]:
    """Signed-in Claude Code profiles other than the default account."""
    home = Path(home) if home is not None else Path.home()
    environ = os.environ if environ is None else environ
    primary = account_uuid(read_state(primary_state_path(home, environ)))
    primary_dir = Path(str(environ.get("CLAUDE_CONFIG_DIR") or "").strip() or home / ".claude").expanduser()
    accounts: list[ClaudeAccount] = []
    seen_uuids = {primary} if primary else set()
    seen_keys = {PRIMARY_KEY}
    for directory in _candidates(home, environ):
        try:
            if directory.resolve() == primary_dir.resolve():
                continue
        except OSError:
            continue
        state_path = directory / ".claude.json"
        state = read_state(state_path)
        uuid = account_uuid(state)
        if not uuid or uuid in seen_uuids:
            continue  # not signed in, or the same account as another lane
        slug = slug_for(directory)
        key = PREFIX + slug
        if key in seen_keys:
            continue
        seen_uuids.add(uuid)
        seen_keys.add(key)
        accounts.append(ClaudeAccount(key=key, label=label_for(slug), config_dir=directory, state_path=state_path,
                                      account_uuid=uuid,
                                      plan=plan_name(state)))
    return tuple(accounts)


def extra_accounts() -> tuple[ClaudeAccount, ...]:
    """Discovered accounts, re-scanned at most once a minute."""
    marker = (str(Path.home()), os.environ.get("CLAUDE_CONFIG_DIR"), os.environ.get(EXTRA_DIRS_ENV))
    with _LOCK:
        fresh = _DISCOVERED["at"] is not None and time.monotonic() - _DISCOVERED["at"] < DISCOVERY_TTL_SECONDS
        if fresh and _DISCOVERED["key"] == marker:
            return _DISCOVERED["accounts"]
    accounts = discover()
    with _LOCK:
        _DISCOVERED.update(at=time.monotonic(), key=marker, accounts=accounts)
    return accounts


def _plain_dir(path: Path) -> bool:
    from spend_app.connection_paths import is_junction

    try:
        return not path.is_symlink() and not is_junction(path) and path.is_dir()
    except OSError:
        return False


def _plain_state_file(path: Path) -> bool:
    from spend_app.connection_paths import is_junction

    try:
        return not path.is_symlink() and not is_junction(path) and path.is_file()
    except OSError:
        return False


def harness_roots(home: Path, environ) -> list[Path]:
    """Roots holding one Claude config folder per profile for tools that run Claude Code."""
    roots = [Path(home) / ".traycer" / "host" / "harness-accounts" / "claude-code"]
    for raw in str(environ.get(HARNESS_DIRS_ENV) or "").split(os.pathsep):
        if raw.strip():
            roots.append(Path(raw.strip()).expanduser())
    return [root for root in roots if _plain_dir(root)]


def _harness_children(root: Path) -> list[Path]:
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [root / name for name in names[:MAX_HARNESS_CHILDREN] if _plain_dir(root / name)]


def _scan_folders(home: Path, environ) -> tuple[tuple[AccountFolder, str], ...]:
    """Every folder with a regular state file signed into some account, as (folder, account uuid)."""
    primary_dir = Path(str(environ.get("CLAUDE_CONFIG_DIR") or "").strip() or home / ".claude").expanduser()
    candidates = [AccountFolder(primary_dir, primary_state_path(home, environ), True)]
    candidates += [AccountFolder(path, path / ".claude.json", False) for path in _candidates(home, environ)]
    for root in harness_roots(home, environ):
        candidates += [AccountFolder(path, path / ".claude.json", False) for path in _harness_children(root)]
    found: list[tuple[AccountFolder, str]] = []
    seen: set[str] = set()
    for folder in candidates:
        marker = _safe_identity(folder.config_dir)
        if marker is None or marker in seen:
            continue
        seen.add(marker)
        if not _plain_state_file(folder.state_path):
            continue
        uuid = account_uuid(read_state(folder.state_path))
        if uuid:
            found.append((folder, uuid))
    return tuple(found)


def account_folders(account_uuid: str, *, home: Path | None = None, environ=None) -> tuple[AccountFolder, ...]:
    """Every Claude Code config folder on this computer signed into ``account_uuid``.

    Covers the primary profile, ``~/.claude-*`` and ``BURNRATE_CLAUDE_ACCOUNT_DIRS`` folders and each folder under
    the harness roots. Symlinked or junctioned folders and state files are skipped. Re-scanned at most once a minute.
    """
    home = Path(home) if home is not None else Path.home()
    environ = os.environ if environ is None else environ
    marker = (str(home), environ.get("CLAUDE_CONFIG_DIR"), environ.get(EXTRA_DIRS_ENV), environ.get(HARNESS_DIRS_ENV))
    with _LOCK:
        fresh = _FOLDERS["at"] is not None and time.monotonic() - _FOLDERS["at"] < DISCOVERY_TTL_SECONDS
        scanned = _FOLDERS["folders"] if fresh and _FOLDERS["key"] == marker else None
    if scanned is None:
        scanned = _scan_folders(home, environ)
        with _LOCK:
            _FOLDERS.update(at=time.monotonic(), key=marker, folders=scanned)
    return tuple(folder for folder, uuid in scanned if uuid == account_uuid)


def _safe_identity(path: Path) -> str | None:
    from spend_app.connection_paths import identity

    try:
        return identity(path)
    except Exception:
        return None


def _store_profiles(database_path: Path | str | None = None, *, include_default: bool = False):
    from spend_app import profile_store

    raw = database_path if database_path is not None else os.environ.get("SPEND_DATABASE_PATH")
    if not raw:
        return []
    db_path = Path(raw)
    try:
        db_path.stat()
    except OSError:
        return []
    try:
        with closing(sqlite3.connect(db_path.absolute().as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            state = profile_store.load_config_if_present(db)
    except Exception:
        return []
    if not state:
        return []
    found = profile_store.profiles(state, provider="claude_local")
    if include_default:
        return found
    return [profile for profile in found if profile.key != "default"]


def _store_sharing_info(key: str, database_path: Path | str | None = None) -> dict | None:
    all_profiles = [p for p in _store_profiles(database_path, include_default=True) if p.enabled]
    if not all_profiles:
        return None
    ordered = sorted(all_profiles, key=lambda p: 0 if p.key == "default" else 1)
    seen: dict[str, tuple[str, str]] = {}
    for profile in ordered:
        tkey = profile.tool_key or (PRIMARY_KEY if profile.key == "default" else profile.key)
        digest = profile.account_digest
        if not digest:
            continue
        if digest in seen:
            if tkey == key:
                owner_key, owner_name = seen[digest]
                return {
                    "sharesLimitsWith": owner_key,
                    "sharesLimitsLabel": f"Shares limits with {owner_name}",
                }
        else:
            seen[digest] = (tkey, profile.name or ("Default" if profile.key == "default" else tkey))
    return None


def account_for_key(key: str, database_path: Path | str | None = None) -> ClaudeAccount | None:
    if not is_account_key(key, database_path=database_path):
        return None
    from spend_app import profile_store

    discovered = extra_accounts()
    matched = next((account for account in discovered if account.key == key), None)
    store_prof = next(
        (profile for profile in _store_profiles(database_path) if (profile.tool_key or profile.key) == key),
        None,
    )
    if store_prof is not None:
        folder = profile_store._claude_folder_from_location(store_prof.location)
        if matched is None and folder is not None:
            folder_id = _safe_identity(folder)
            if folder_id is not None:
                matched = next(
                    (account for account in discovered if _safe_identity(account.config_dir) == folder_id),
                    None,
                )
        if matched is not None:
            return ClaudeAccount(
                key=key,
                label=store_prof.name or matched.label,
                config_dir=matched.config_dir,
                state_path=matched.state_path,
                account_uuid=matched.account_uuid,
                plan=matched.plan,
            )
        if folder is not None:
            state_path = folder / ".claude.json"
            state = read_state(state_path) if state_path.is_file() else {}
            uid = account_uuid(state) or ""
            slug = key[len(PREFIX):] if key.startswith(PREFIX) else slug_for(folder)
            return ClaudeAccount(
                key=key,
                label=store_prof.name or label_for(slug),
                config_dir=folder,
                state_path=state_path,
                account_uuid=uid,
                plan=plan_name(state),
            )
    return matched


def is_account_key(key: object, database_path: Path | str | None = None) -> bool:
    if not isinstance(key, str) or not key or key == PRIMARY_KEY:
        return False
    if key.startswith(PREFIX):
        return True
    return any((profile.tool_key or profile.key) == key for profile in _store_profiles(database_path))


def provider_meta(key: str, database_path: Path | str | None = None) -> dict | None:
    """Capacity-card metadata for an extra account lane (also for synced rows from another computer)."""
    if not is_account_key(key, database_path=database_path):
        return None
    store_prof = next(
        (profile for profile in _store_profiles(database_path) if (profile.tool_key or profile.key) == key),
        None,
    )
    account = account_for_key(key, database_path=database_path)
    slug = key[len(PREFIX):] if key.startswith(PREFIX) else key
    label = (
        (store_prof.name if store_prof and store_prof.name else None)
        or (account.label if account else label_for(slug))
    )
    meta = {
        "providerKey": key,
        "providerName": f"Claude Code · {label}",
        "shortName": f"Claude {label}",
        "accountLabel": label,
        "plan": (account.plan if account else None) or "Claude",
        "isPayg": False,
        "toolKey": PRIMARY_KEY,
    }
    sharing = _store_sharing_info(key, database_path)
    if sharing:
        meta.update(sharing)
    return meta
