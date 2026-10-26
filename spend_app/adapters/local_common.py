"""Shared read-only helpers for local provider adapters.

Files opened here are provider-owned stores. SQLite uses URI ``mode=ro`` and
``PRAGMA query_only=ON``. JSONL/text is opened ``O_RDONLY``. Adapters never
write provider files and never persist prompt, response, or tool content.

Official vs observed, live-session rules, and per-source paths are documented
on each adapter module.
"""

from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote


def number(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)) and math.isfinite(value):
        return max(0, int(value))
    return 0


def optional_number(value: object) -> int | None:
    """A reported count, or None when the source omitted the field.

    ``number`` coerces missing values to 0 for fields where absence means
    nothing was consumed. Reasoning detail is optional telemetry: a missing
    field must stay NULL rather than be stored as a measured zero.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return max(0, int(value))
    return None


def positive_cost(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
        return float(value)
    return None


def folder_name(value: object) -> str | None:
    """The last folder of a path written on any OS.

    A session's working folder can come from another computer through the hub,
    so ``C:\\Dev\\app`` and ``/home/me/app`` both name ``app`` on every platform.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    name = value.strip().replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return None if not name or name.endswith(":") else name


_REPO_ROOTS: dict[str, str | None] = {}
_REPO_MARKERS = ("/.t3/worktrees/", "/orca/workspaces/")


def _repo_root(text: str) -> str | None:
    """The git repository a folder on this computer belongs to (a worktree's main repository)."""
    if text in _REPO_ROOTS:
        return _REPO_ROOTS[text]
    found = None
    try:
        start = Path(text)
        home = Path.home()
        if start.is_absolute():
            for candidate in (start, *start.parents)[:16]:
                if candidate == home or candidate == candidate.parent:
                    break
                marker = candidate / ".git"
                if marker.is_dir():
                    found = str(candidate)
                    break
                if marker.is_file():
                    pointer = marker.read_text(encoding="utf-8", errors="replace").strip()
                    gitdir = pointer.split(":", 1)[1].strip().replace("\\", "/") if pointer.startswith("gitdir:") else ""
                    at = gitdir.lower().find("/.git/worktrees/")
                    found = gitdir[:at] if at > 0 else str(candidate)
                    break
    except (OSError, ValueError):
        found = None
    if len(_REPO_ROOTS) > 4096:
        _REPO_ROOTS.clear()
    _REPO_ROOTS[text] = found
    return found


def project_name(value: object) -> str | None:
    """The project a session ran in: its repository when known, else its folder.

    Worktrees count toward their repository: T3 (``.t3/worktrees/<repo>/…``),
    Claude Code (``<repo>/.claude/worktrees/…``), Orca
    (``orca/workspaces/<repo>/…``) and any git worktree on this computer. A
    subfolder of a repository counts toward the repository. A path from
    another computer that matches none of these keeps its last folder name.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("\\", "/").rstrip("/")
    lowered = text.lower()
    for marker in _REPO_MARKERS:
        at = lowered.find(marker)
        if at >= 0:
            repo = text[at + len(marker):].split("/", 1)[0]
            if repo:
                return repo
    at = lowered.find("/.claude/worktrees/")
    if at > 0:
        return folder_name(text[:at])
    return folder_name(_repo_root(value.strip()) or text)


def sqlite_read_only(path: Path) -> sqlite3.Connection:
    from spend_app.connection_paths import confined
    return _read_only_connection(confined(path))


def store_read_only(path: Path) -> sqlite3.Connection:
    """Read-only connection to BURNRATE's own database, never a provider store.

    Provider files are confined to the approved source location; the app's
    database lives elsewhere by design, so confining it would always fail.
    """
    return _read_only_connection(Path(path))


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri_path = quote(path.resolve().as_posix(), safe="/:.")
    connection = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True, timeout=5)
    connection.execute("PRAGMA query_only=ON")
    return connection


def open_text_read_only(path: Path):
    """Open a provider JSONL/text file without write or truncate access."""
    from spend_app.connection_paths import confined, readonly_fd

    return os.fdopen(readonly_fd(confined(path)), "r", encoding="utf-8", errors="replace")


def open_binary_read_only(path: Path):
    """Open a provider file for a bounded binary tail without write access."""
    from spend_app.connection_paths import confined, readonly_fd

    return os.fdopen(readonly_fd(confined(path)), "rb")


def parse_iso_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def parse_millis(value: object) -> datetime | None:
    milliseconds = number(value)
    if milliseconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(frozen=True)
class TokenClassification:
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int
    unclassified_tokens: int
    telemetry_complete: bool


def classify_traycer_usage(usage: dict) -> TokenClassification:
    input_tokens = number(usage.get("inputTokens"))
    output_tokens = number(usage.get("outputTokens"))
    cache_read = number(usage.get("cacheReadInputTokens"))
    cache_write = number(usage.get("cacheCreationInputTokens"))
    total_tokens = number(usage.get("totalTokens"))
    context_tokens = number(usage.get("contextTokens"))

    if cache_read + cache_write <= input_tokens and abs(total_tokens - (input_tokens + output_tokens)) <= 8:
        return TokenClassification(
            # Normalized storage excludes cache writes from input_tokens;
            # cache_write_tokens is a separate component.
            input_tokens=max(0, input_tokens - cache_write),
            cached_input_tokens=cache_read,
            cache_write_tokens=cache_write,
            output_tokens=output_tokens,
            unclassified_tokens=0,
            telemetry_complete=True,
        )

    additive_input = input_tokens + cache_read + cache_write
    additive_total = additive_input + output_tokens
    if (
        abs(total_tokens - additive_total) <= 8
        or (context_tokens > 0 and abs(context_tokens - additive_input) <= 8)
        or (cache_read + cache_write > input_tokens and total_tokens >= additive_total)
    ):
        return TokenClassification(
            input_tokens=input_tokens + cache_read,
            cached_input_tokens=cache_read,
            cache_write_tokens=cache_write,
            output_tokens=output_tokens,
            unclassified_tokens=0,
            telemetry_complete=True,
        )

    return TokenClassification(
        input_tokens=0,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=0,
        unclassified_tokens=context_tokens or total_tokens,
        telemetry_complete=False,
    )
