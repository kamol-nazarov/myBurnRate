"""Which Claude Code account lane each transcript's session belongs to.

Claude Code writes every transcript under ``~/.claude/projects`` even when a
tool such as Traycer runs it with another account's config directory (its
``projects`` folder is a junction to the default one), and a transcript never
names the account. What does name it is ``<config dir>/session-env/<sessionId>/``,
which Claude Code creates in the config directory of the account that ran the
session. A session id found in a non-default lane's account folders is that
lane's; anything else stays on the default ``claude-code`` lane.

Claude Code prunes ``session-env`` entries after about 30 days, so every
attribution that a transcript was actually given is remembered in ``app_meta``
and merged back in: a transcript never flips back to the personal lane.
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

PRIMARY_KEY = "claude-code"
META_KEY = "claude.session_lanes.v1"
DISCOVERY_TTL_SECONDS = 60
MAX_SESSION_ENV_ENTRIES = 5_000

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_LOCK = threading.Lock()
_DISCOVERED: dict = {"at": None, "key": None, "found": {}, "lanes": frozenset()}


def reset_cache() -> None:
    with _LOCK:
        _DISCOVERED.update(at=None, key=None, found={}, lanes=frozenset())


def session_id_for_path(path: Path) -> str:
    """The session a transcript belongs to; subagent transcripts belong to their parent session."""
    path = Path(path)
    if path.parent.name == "subagents" and path.parent.parent.name:
        return path.parent.parent.name.lower()
    return path.stem.lower()


def _plain_dir(path: Path) -> bool:
    from spend_app.connection_paths import is_junction

    try:
        return not path.is_symlink() and not is_junction(path) and path.is_dir()
    except OSError:
        return False


def _session_ids(config_dir: Path) -> set[str]:
    """UUID-named session directories under ``<config_dir>/session-env`` (bounded, links ignored)."""
    root = Path(config_dir) / "session-env"
    if not _plain_dir(root):
        return set()
    found: set[str] = set()
    try:
        with os.scandir(root) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_SESSION_ENV_ENTRIES:
                    break
                name = entry.name.lower()
                if _UUID.match(name) and _plain_dir(root / entry.name):
                    found.add(name)
    except OSError:
        pass
    return found


def _lane_accounts(database_path: Path | str) -> list[tuple[str, str | None]]:
    """(tool_key, account uuid or None) for each enabled extra Claude lane worth attributing to."""
    from spend_app import claude_accounts, profile_store
    from spend_app.providers import REGISTRY

    spec = REGISTRY.get("claude_local")
    connection_spec = spec.connection if spec is not None else None
    # No try/except: an unreadable store must fail the pass, not drop attributions and flip rows back.
    profiles = [p for p in profile_store.enabled_profiles(database_path) if p.provider == "claude_local"]
    default = next((p for p in profiles if p.key == "default"), None)
    if not any(p.key != "default" for p in profiles):
        return []  # nothing but the default profile: no home-directory scan
    primary_state = claude_accounts.read_state(
        claude_accounts.primary_state_path(Path.home(), os.environ)
    )
    primary_uuid = claude_accounts.account_uuid(primary_state)
    default_digests = {profile_store._sha256_hex(primary_uuid)} if primary_uuid else set()
    if default is not None and default.account_digest:
        default_digests.add(default.account_digest)
    known_uuids = {account.account_uuid for account in claude_accounts.extra_accounts() if account.account_uuid}

    lanes: list[tuple[str, str | None]] = []
    seen_digests = set(default_digests)
    for profile in sorted(profiles, key=lambda p: p.key):
        tool_key = profile.tool_key or profile.key
        if profile.key == "default" or not tool_key or tool_key == PRIMARY_KEY:
            continue
        if connection_spec is None or not profile_store.location_unchanged(profile, connection_spec):
            continue
        folder = profile_store._claude_folder_from_location(profile.location)
        uuid = claude_accounts.account_uuid(claude_accounts.read_state(folder / ".claude.json")) if folder else None
        if uuid is None and profile.account_digest:
            uuid = next(
                (known for known in known_uuids if profile_store._sha256_hex(known) == profile.account_digest), None
            )
        digest = profile_store._sha256_hex(uuid) if uuid else profile.account_digest
        if not digest:
            continue
        if digest in seen_digests:
            continue  # shares limits with an earlier lane (or the default): its sessions stay there
        seen_digests.add(digest)
        lanes.append((tool_key, uuid))
    return lanes


def _discover(database_path: Path | str) -> tuple[dict[str, str], frozenset[str]]:
    from spend_app import claude_accounts

    primary_dir = Path(str(os.environ.get("CLAUDE_CONFIG_DIR") or "").strip() or Path.home() / ".claude").expanduser()
    primary_ids = _session_ids(primary_dir)
    claimed: dict[str, set[str]] = {}
    lanes = _lane_accounts(database_path)
    for tool_key, uuid in lanes:
        if not uuid:
            continue
        for folder in claude_accounts.account_folders(uuid):
            if folder.is_primary:
                continue
            for sid in _session_ids(folder.config_dir):
                claimed.setdefault(sid, set()).add(tool_key)
    found = {sid: next(iter(keys)) for sid, keys in claimed.items() if len(keys) == 1 and sid not in primary_ids}
    return found, frozenset(tool_key for tool_key, _ in lanes)


def read_remembered(connection) -> dict[str, str]:
    from spend_app.adapters.usage_repair import read_meta

    stored = read_meta(connection, META_KEY, {})
    return {k: v for k, v in stored.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(stored, dict) else {}


def remember(connection, attributed: dict[str, str]) -> None:
    """Persist ``{session id: lane}`` inside the caller's transaction (merged into what is stored)."""
    from spend_app.adapters.usage_repair import write_meta

    if not attributed:
        return
    stored = read_remembered(connection)
    merged = {**stored, **attributed}
    if merged != stored:
        write_meta(connection, META_KEY, merged)


def session_lanes(database_path: Path | str) -> dict[str, str]:
    """sessionId -> tool_key for sessions that ran under a non-default Claude lane's account.

    Discovery re-scans at most once a minute; remembered attributions are merged in and
    dropped (not deleted) while their lane is not an enabled, distinct-account lane.
    """
    from spend_app.db import connect

    marker = (str(database_path), str(Path.home()), os.environ.get("CLAUDE_CONFIG_DIR"))
    with _LOCK:
        fresh = _DISCOVERED["at"] is not None and time.monotonic() - _DISCOVERED["at"] < DISCOVERY_TTL_SECONDS
        cached = (_DISCOVERED["found"], _DISCOVERED["lanes"]) if fresh and _DISCOVERED["key"] == marker else None
    if cached is None:
        cached = _discover(database_path)
        with _LOCK:
            _DISCOVERED.update(at=time.monotonic(), key=marker, found=cached[0], lanes=cached[1])
    found, enabled = cached
    with connect(Path(database_path)) as connection:
        remembered = read_remembered(connection)
    merged = {sid: lane for sid, lane in remembered.items() if lane in enabled}
    merged.update(found)
    return merged
