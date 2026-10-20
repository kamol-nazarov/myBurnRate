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
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

PRIMARY_KEY = "claude-code"
PREFIX = "claude-code-"
EXTRA_DIRS_ENV = "BURNRATE_CLAUDE_ACCOUNT_DIRS"
DISCOVERY_TTL_SECONDS = 60

_LOCK = threading.Lock()
_DISCOVERED: dict = {"at": None, "key": None, "accounts": ()}


@dataclass(frozen=True)
class ClaudeAccount:
    key: str
    label: str
    config_dir: Path
    state_path: Path
    credentials_path: Path
    account_uuid: str
    plan: str | None


def reset_cache() -> None:
    with _LOCK:
        _DISCOVERED.update(at=None, key=None, accounts=())


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
    found = []
    try:
        for entry in sorted(home.glob(".claude-*")):
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
                                      credentials_path=directory / ".credentials.json", account_uuid=uuid,
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


def account_for_key(key: str) -> ClaudeAccount | None:
    if not str(key).startswith(PREFIX):
        return None
    return next((account for account in extra_accounts() if account.key == key), None)


def is_account_key(key: object) -> bool:
    return isinstance(key, str) and key.startswith(PREFIX)


def provider_meta(key: str) -> dict | None:
    """Capacity-card metadata for an extra account lane (also for synced rows from another computer)."""
    if not is_account_key(key):
        return None
    account = account_for_key(key)
    label = account.label if account else label_for(key[len(PREFIX):])
    return {
        "providerKey": key,
        "providerName": f"Claude Code · {label}",
        "shortName": f"Claude {label}",
        "accountLabel": label,
        "plan": (account.plan if account else None) or "Claude",
        "isPayg": False,
        "toolKey": PRIMARY_KEY,
    }
