"""Plain-text report behind "Copy diagnostics" in setup's Check step.

Versions, collection and meter freshness, recent failed runs and the tail of
the service logs. Credential-shaped text, e-mail addresses and the home folder
are removed before it leaves this module. Nothing is sent anywhere: the page
puts the text on the clipboard for the owner to paste where they choose.
"""

from __future__ import annotations

import platform
import re
import sys
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from spend_app import __version__
from spend_app.source_evidence import _CREDENTIAL_SHAPED

LOG_FILES = ("dashboard.log", "collector.log", "watchdog.log")
LOG_LINES = 40
LINE_CHARS = 400
FAILED_RUNS = 12

_SECRET_FIELD = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|passwd|authorization|cookie|session[_-]?key)"
    r"(\s*[\"']?\s*[:=]\s*[\"']?)(?:(?:bearer|basic)\s+)?[^\s\"',;}]+"
)
_EMAIL = re.compile(r"(?i)[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}")
_USER_DIR = re.compile(r"(?i)([A-Z]:[\\/]+Users[\\/]+)[^\\/\s\"']+")
_UNIX_HOME = re.compile(r"(/(?:home|Users)/)[^/\s\"']+")


def redact(text: str, home: Path | None = None) -> str:
    home_text = str(home or Path.home())
    for form in {home_text, home_text.replace("\\", "/"), home_text.replace("/", "\\"), home_text.replace("\\", "\\\\")}:
        if len(form) > 3:
            text = re.sub(re.escape(form), "~", text, flags=re.IGNORECASE)
    text = _CREDENTIAL_SHAPED.sub("[redacted]", text)
    text = _SECRET_FIELD.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", text)
    text = _EMAIL.sub("[email]", text)
    text = _USER_DIR.sub(r"\1[user]", text)
    return _UNIX_HOME.sub(r"\1[user]", text)


def _age(seconds) -> str:
    if seconds is None:
        return "never"
    seconds = int(seconds)
    if seconds < 120:
        return f"{seconds}s ago"
    if seconds < 7200:
        return f"{seconds // 60}m ago"
    if seconds < 172800:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _tail(path: Path, lines: int = LOG_LINES) -> list[str]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return [line.rstrip("\n")[:LINE_CHARS] for line in deque(handle, maxlen=lines)]
    except OSError:
        return []


def build_report(settings, *, now: datetime | None = None, log_dir: Path | None = None) -> str:
    from spend_app.build import build_id
    from spend_app.db import connect, initialize
    from spend_app.freshness import report
    from spend_app.setup_checks import consent_state
    from spend_app.setup_wizard import read_setup_state

    now = now or datetime.now(UTC)
    fresh = report(settings, now)
    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        setup = read_setup_state(connection)
        failed = connection.execute(
            "SELECT source, status, started_at, error FROM ingest_runs WHERE status='failed' "
            "ORDER BY id DESC LIMIT ?", (FAILED_RUNS,)
        ).fetchall()
        meters = connection.execute(
            "SELECT q.provider_key, q.limit_key, q.unit, q.pct, q.used, q.polled_at, q.source FROM quotas q "
            "JOIN (SELECT provider_key, limit_key, MAX(id) AS id FROM quotas GROUP BY provider_key, limit_key) latest "
            "ON latest.id = q.id ORDER BY q.provider_key, q.limit_key"
        ).fetchall()

    out = ["BURNRATE diagnostics", f"Generated {now.astimezone(UTC).replace(microsecond=0).isoformat()}", ""]
    hub = fresh.get("hub") or {}
    sync = fresh.get("sync") or {}
    out += [
        "## Versions",
        f"Build: {build_id()} (version {__version__})",
        f"Python {sys.version.split()[0]} on {platform.platform()}",
        f"Role: {setup.get('role') or 'standalone'}" + (" (setup finished)" if setup.get("completed") else " (setup not finished)"),
    ]
    if sync:
        out.append(f"Hub: {hub.get('build') or hub.get('version') or 'unknown'}"
                   f"{'' if hub.get('matches') is None else ' (same build)' if hub.get('matches') else ' (different build)'}; "
                   f"last sync {sync.get('lastSyncAt') or 'never'}{' — failing' if sync.get('failing') else ''}")
    beat = fresh.get("ingest") or {}
    out += ["", "## Collection",
            f"Status: {fresh.get('status')}; passes since start {beat.get('passes')}; last pass {_age(beat.get('lastPassAgeSeconds'))}"
            f"{'; STALLED' if beat.get('stalled') else ''}"]
    issues = fresh.get("issues") or []
    out += ["", "## Needs attention" if issues else "## Needs attention: nothing"]
    for item in issues:
        out.append(f"- [{item.get('severity')}] {item.get('name')}: {item.get('state')}"
                   f"{'' if item.get('ageSeconds') is None else ', ' + _age(item.get('ageSeconds'))} — {item.get('detail') or ''}")
    out += ["", "## Meters (newest reading per window)"]
    for provider, limit, unit, pct, used, polled_at, source in meters:
        value = f"{pct:.0f}%" if pct is not None else (f"{used} {unit}" if used is not None else unit)
        out.append(f"- {provider} {limit}: {value}, read {polled_at} via {source}")
    out += ["", "## Sign-in reuse switches"]
    out += [f"- {item['label']}: {'on' if item.get('enabled') else 'off'}" for item in consent_state()]
    out += ["", f"## Recent failed runs ({len(failed)})"]
    out += [f"- {started} {source}: {(error or status)[:LINE_CHARS]}" for source, status, started, error in failed]
    if log_dir is None:
        from spend_app.service import log_dir as service_log_dir

        try:
            log_dir = service_log_dir()
        except RuntimeError:
            log_dir = None
    for name in LOG_FILES:
        lines = _tail(log_dir / name) if log_dir is not None else []
        if lines:
            out += ["", f"## {name} (last {len(lines)} lines)", *lines]
    return redact("\n".join(out)) + "\n"
