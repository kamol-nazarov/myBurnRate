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

import hashlib
import os
import re
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
    found = []
    try:
        for entry in sorted(home.glob(".opencode-*")):
            if entry.is_dir() and not entry.is_symlink():
                found.append(entry)
    except OSError:
        pass
    for raw in str(environ.get(EXTRA_DIRS_ENV) or "").split(os.pathsep):
        if raw.strip():
            path = Path(raw.strip()).expanduser()
            if path.is_dir():
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


def profile_for_key(key: str) -> OpenCodeProfile | None:
    if not is_profile_key(key):
        return None
    return next((profile for profile in extra_profiles() if profile.key == key), None)


def is_profile_key(key: object) -> bool:
    return isinstance(key, str) and key.startswith(PREFIX)


def provider_meta(key: str) -> dict | None:
    """Capacity-card metadata for an extra profile lane (also for synced rows from another computer)."""
    if not is_profile_key(key):
        return None
    profile = profile_for_key(key)
    slug = key[len(PREFIX):]
    label = profile.label if profile else label_for(slug)
    short = slug if slug.isdigit() else label
    return {
        "providerKey": key,
        "providerName": f"OpenCode Go · {label}",
        "shortName": f"OpenCode Go {short}",
        "accountLabel": label,
        "plan": "Go",
        "isPayg": False,
        "toolKey": PRIMARY_KEY,
    }
