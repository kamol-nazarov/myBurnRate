"""OpenCode profiles on this computer beyond the default one.

OpenCode keeps its data in ``$XDG_DATA_HOME/opencode`` (``~/.local/share/opencode``
by default). A second profile is OpenCode started with ``XDG_DATA_HOME``
pointing elsewhere, conventionally ``~/.opencode-<name>``, which then holds
``opencode/opencode.db``. Each extra profile signed in to a different OpenCode
Console account gets its own Go quota lane, ``opencode-go-<name>``, beside the
default ``opencode-go`` lane.

Only the profile's stored Console (or Go) key is read, to ask the Go usage
endpoint about that account; it is never surfaced or persisted. The lane is
named after the directory ("Profile 2" for ``~/.opencode-go-2``, "Work" for
``~/.opencode-work``). Directories with other names can be listed in
``BURNRATE_OPENCODE_PROFILE_DIRS`` (separated by the OS path separator).
"""

from __future__ import annotations

from contextlib import closing
import hashlib
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

PRIMARY_KEY = "opencode-go"
PREFIX = "opencode-go-"
EXTRA_DIRS_ENV = "BURNRATE_OPENCODE_PROFILE_DIRS"
DISCOVERY_TTL_SECONDS = 60

_LOCK = threading.Lock()
_DISCOVERED: dict = {"at": None, "key": None, "profiles": ()}


@dataclass(frozen=True)
class OpenCodeProfile:
    key: str
    label: str
    data_dir: Path
    database: Path


def reset_cache() -> None:
    with _LOCK:
        _DISCOVERED.update(at=None, key=None, profiles=())


def slug_for(directory: Path) -> str:
    name = directory.name.lower()
    for prefix in (".opencode-go-", ".opencode-"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    else:
        name = name.lstrip(".")
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")[:24].strip("-")
    return slug or "profile"


def label_for(slug: str) -> str:
    if slug.isdigit():
        return f"Profile {slug}"
    return " ".join(part.capitalize() for part in slug.split("-") if part) or "Profile"


def database_for(directory: Path) -> Path | None:
    """The profile's ``opencode.db``: under ``<XDG_DATA_HOME>/opencode`` or the data directory itself."""
    for candidate in (directory / "opencode" / "opencode.db", directory / "opencode.db"):
        if candidate.is_file():
            return candidate
    return None


def _candidates(home: Path, environ) -> list[Path]:
    from spend_app.connection_paths import is_junction

    found = []
    try:
        for entry in sorted(home.glob(".opencode-*")):
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


def _fingerprints(keys: list[str]) -> set[str]:
    # Compared in memory only, so the same account is not given two lanes.
    return {hashlib.sha256(key.encode("utf-8")).hexdigest() for key in keys}


def discover(home: Path | None = None, environ=None, *, keys_for=None, primary_database: Path | None = None) -> tuple[OpenCodeProfile, ...]:
    """Signed-in OpenCode profiles other than the default profile's account."""
    from spend_app.limits import _opencode_go_keys
    from spend_app.providers import default_opencode_database

    home = Path(home) if home is not None else Path.home()
    environ = os.environ if environ is None else environ
    keys_for = keys_for or (lambda database: _opencode_go_keys(database) if database is not None else _opencode_go_keys())
    primary = Path(primary_database) if primary_database is not None else default_opencode_database()
    seen = _fingerprints(keys_for(None))
    seen_keys = {PRIMARY_KEY}
    profiles: list[OpenCodeProfile] = []
    for directory in _candidates(home, environ):
        database = database_for(directory)
        if database is None:
            continue
        try:
            if database.resolve() == primary.resolve():
                continue
        except OSError:
            continue
        prints = _fingerprints(keys_for(database))
        if not prints or prints & seen:
            continue  # not signed in, or the same account as another lane
        slug = slug_for(directory)
        key = PREFIX + slug
        if key in seen_keys:
            continue
        seen |= prints
        seen_keys.add(key)
        profiles.append(OpenCodeProfile(key=key, label=label_for(slug), data_dir=directory, database=database))
    return tuple(profiles)


def extra_profiles() -> tuple[OpenCodeProfile, ...]:
    """Discovered profiles, re-scanned at most once a minute."""
    marker = (str(Path.home()), os.environ.get("XDG_DATA_HOME"), os.environ.get("OPENCODE_DB"), os.environ.get(EXTRA_DIRS_ENV))
    with _LOCK:
        fresh = _DISCOVERED["at"] is not None and time.monotonic() - _DISCOVERED["at"] < DISCOVERY_TTL_SECONDS
        if fresh and _DISCOVERED["key"] == marker:
            return _DISCOVERED["profiles"]
    try:
        profiles = discover()
    except Exception:
        profiles = ()
    with _LOCK:
        _DISCOVERED.update(at=time.monotonic(), key=marker, profiles=profiles)
    return profiles


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
            state = profile_store.load_config_if_present(db)
    except Exception:
        return []
    if not state:
        return []
    found = profile_store.profiles(state, provider="opencode_local")
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


def _database_from_location(location: str | None) -> Path | None:
    if not location:
        return None
    loc = Path(location).expanduser()
    if loc.is_dir():
        return database_for(loc) or (loc / "opencode.db")
    return loc


def profile_for_key(key: str, database_path: Path | str | None = None) -> OpenCodeProfile | None:
    if not is_profile_key(key, database_path=database_path):
        return None
    store_prof = next(
        (profile for profile in _store_profiles(database_path) if (profile.tool_key or profile.key) == key),
        None,
    )
    if store_prof is not None and store_prof.location:
        db_path = _database_from_location(store_prof.location)
        if db_path is not None:
            data_dir = db_path.parent.parent if db_path.parent.name == "opencode" else db_path.parent
            slug = key[len(PREFIX):] if key.startswith(PREFIX) else slug_for(data_dir)
            return OpenCodeProfile(
                key=key,
                label=store_prof.name or label_for(slug),
                data_dir=data_dir,
                database=db_path,
            )
    return next((profile for profile in extra_profiles() if profile.key == key), None)


def is_profile_key(key: object, database_path: Path | str | None = None) -> bool:
    if not isinstance(key, str) or not key or key in {PRIMARY_KEY, "opencode"}:
        return False
    if key.startswith(PREFIX):
        return True
    return any((profile.tool_key or profile.key) == key for profile in _store_profiles(database_path))


def provider_meta(key: str, database_path: Path | str | None = None) -> dict | None:
    """Capacity-card metadata for an extra profile lane (also for synced rows from another computer)."""
    if not is_profile_key(key, database_path=database_path):
        return None
    store_prof = next(
        (profile for profile in _store_profiles(database_path) if (profile.tool_key or profile.key) == key),
        None,
    )
    profile = profile_for_key(key, database_path=database_path)
    slug = key[len(PREFIX):] if key.startswith(PREFIX) else key
    label = (
        (store_prof.name if store_prof and store_prof.name else None)
        or (profile.label if profile else label_for(slug))
    )
    short = slug if (slug.isdigit() and label == label_for(slug)) else label
    meta = {
        "providerKey": key,
        "providerName": f"OpenCode Go · {label}",
        "shortName": f"OpenCode Go {short}",
        "accountLabel": label,
        "plan": "Go",
        "isPayg": False,
        "toolKey": PRIMARY_KEY,
    }
    sharing = _store_sharing_info(key, database_path)
    if sharing:
        meta.update(sharing)
    return meta
