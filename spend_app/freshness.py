"""Is this computer's usage current? One local, read-only answer.

The dashboard on a spoke shows the hub's figures, so the hub's status cannot
say that a source on *this* computer stopped collecting, that a quota meter is
holding an old reading, or that this computer stopped syncing. This report
answers those questions from local state only and is never proxied to the hub.

It also carries the ingest heartbeat the watchdog uses to restart a service
whose collection loop has stalled while its web server still answers.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime

from spend_app import __version__

LOG = logging.getLogger("burnrate.ingest")

# A quota reading older than this is "held": the meter shows a value that the
# lane has not confirmed recently (429 back-off, endpoint answering without
# usage, collector failing). The slowest healthy idle cadence is 5 minutes.
LANE_HELD_AFTER_SECONDS = 15 * 60
# No finished local ingest pass for this long means collection has stalled.
INGEST_STALLED_AFTER_SECONDS = 10 * 60
# The first pass after a start re-reads every store (about 5 minutes on a busy
# machine); it gets longer so the watchdog never restarts a slow cold start.
FIRST_PASS_STALLED_AFTER_SECONDS = 30 * 60
# A spoke that has not reached its hub for this long is out of sync.
SYNC_STALE_AFTER_SECONDS = 10 * 60
# Sources not attempted for this long were removed or disabled; not an alarm.
RETIRED_AFTER_SECONDS = 24 * 3600
HUB_VERSION_TTL_SECONDS = 600

SOURCE_NAMES = {
    "codex_local": "Codex",
    "claude_local": "Claude Code",
    "opencode_local": "OpenCode",
    "zcode_local": "ZCode",
    "grok_local": "Grok",
    "traycer_local": "Traycer",
    "antigravity_local": "Antigravity",
    "muse_local": "Muse",
    "openai_admin": "OpenAI admin",
    "anthropic_admin": "Anthropic admin",
    "openrouter": "OpenRouter",
}

# Quota lanes by provider, as the HUD names them.
LANE_NAMES = {
    "claude-code": "Claude",
    "codex": "Codex",
    "antigravity": "Antigravity",
    "grok": "Grok",
    "muse": "Muse",
    "opencode": "Z.ai",
    "opencode-go": "OpenCode Go",
    "openrouter": "OpenRouter",
}

_STARTED_MONO = time.monotonic()
_COLLECTION_RESUMED_MONO: float | None = None
_LOCK = threading.Lock()
_HEARTBEAT: dict = {"startedAt": None, "finishedAt": None, "seconds": None, "slowest": [], "failed": [], "passes": 0}
_HUB_VERSION: dict = {"checkedMono": None, "version": None, "refreshing": False}


def reset_state() -> None:
    global _STARTED_MONO, _COLLECTION_RESUMED_MONO
    with _LOCK:
        _STARTED_MONO = time.monotonic()
        _COLLECTION_RESUMED_MONO = None
        _HEARTBEAT.update(startedAt=None, finishedAt=None, seconds=None, slowest=[], failed=[], passes=0)
        _HUB_VERSION.update(checkedMono=None, version=None, refreshing=False)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def pass_started(now: datetime | None = None) -> None:
    with _LOCK:
        _HEARTBEAT["startedAt"] = _iso(now or datetime.now(UTC))


def collection_resumed() -> None:
    """Start the watchdog grace period when a viewer resumes idle collection."""
    global _COLLECTION_RESUMED_MONO
    with _LOCK:
        started = _parse(_HEARTBEAT["startedAt"])
        finished = _parse(_HEARTBEAT["finishedAt"])
        # Renewed demand must never excuse a pass that is already stuck.
        if started and (finished is None or started > finished):
            return
        _COLLECTION_RESUMED_MONO = time.monotonic()


def pass_finished(timings: dict[str, float], failed: list[str], seconds: float, now: datetime | None = None) -> None:
    """Record a finished local ingest pass and log one compact timing line."""
    slowest = sorted(((key, round(value, 1)) for key, value in timings.items() if value >= 0.5), key=lambda item: -item[1])[:4]
    with _LOCK:
        _HEARTBEAT.update(finishedAt=_iso(now or datetime.now(UTC)), seconds=round(seconds, 1),
                          slowest=[list(item) for item in slowest], failed=sorted(failed),
                          passes=_HEARTBEAT["passes"] + 1)
    detail = ", ".join(f"{key} {value}s" for key, value in slowest) or "all sources under 0.5s"
    if failed:
        detail += "; failed: " + ", ".join(sorted(failed))
    LOG.info("local ingest pass %.1fs (%s)", seconds, detail)


def heartbeat(now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    with _LOCK:
        beat = dict(_HEARTBEAT)
        uptime = time.monotonic() - _STARTED_MONO
        resumed = _COLLECTION_RESUMED_MONO
    started, finished = _parse(beat["startedAt"]), _parse(beat["finishedAt"])
    running = bool(started and (finished is None or started > finished))
    reference = finished or None
    age = (now - reference).total_seconds() if reference else None
    # Measure from the last finish or process start, allowing a new grace
    # period when a viewer resumes collection after it was deferred.
    quiet_for = age if age is not None else uptime
    if resumed is not None:
        quiet_for = min(quiet_for, max(0, time.monotonic() - resumed))
    limit = INGEST_STALLED_AFTER_SECONDS if beat["passes"] else FIRST_PASS_STALLED_AFTER_SECONDS
    return {
        "lastPassAt": beat["finishedAt"],
        "lastPassAgeSeconds": None if age is None else max(0, int(age)),
        "lastPassSeconds": beat["seconds"],
        "running": running,
        "runningSeconds": int((now - started).total_seconds()) if running and started else None,
        "slowest": beat["slowest"],
        "failed": beat["failed"],
        "uptimeSeconds": int(uptime),
        "passes": beat["passes"],
        "stalled": quiet_for > limit,
    }


def _source_issues(connection, now: datetime) -> list[dict]:
    from spend_app.diagnostics import collection_stale_after, safe_reason

    issues = []
    from spend_app.providers import RETIRED_SOURCES

    sources = [row[0] for row in connection.execute(
        "SELECT DISTINCT source FROM ingest_runs WHERE source NOT LIKE 'sync:%'") if row[0] not in RETIRED_SOURCES]
    for source in sorted(sources):
        latest = connection.execute(
            "SELECT status, finished_at, started_at, error FROM ingest_runs WHERE source=? ORDER BY id DESC LIMIT 1",
            (source,),
        ).fetchone()
        if latest is None or latest[0] in {"skipped", "running"}:
            continue
        attempted = _parse(latest[1] or latest[2])
        if attempted is None or (now - attempted).total_seconds() > RETIRED_AFTER_SECONDS:
            continue
        success = _parse(connection.execute(
            "SELECT MAX(finished_at) FROM ingest_runs WHERE source=? AND status IN ('success','partial')",
            (source,),
        ).fetchone()[0])
        age = None if success is None else max(0, int((now - success).total_seconds()))
        name = SOURCE_NAMES.get(source, source)
        if latest[0] == "failed":
            issues.append({"kind": "source", "key": source, "name": name, "short": name, "state": "failed", "severity": "warn",
                           "ageSeconds": age, "detail": safe_reason(latest[3], source) or "The latest collection failed."})
        elif age is None or age > collection_stale_after(source):
            issues.append({"kind": "source", "key": source, "name": name, "short": name, "state": "stale", "severity": "warn",
                           "ageSeconds": age, "detail": "No successful collection since the last pass."})
    return issues


def _lane_issues(connection, now: datetime) -> list[dict]:
    issues = []
    rows = connection.execute(
        "SELECT q.provider_key, q.limit_key, q.label, q.unit, q.resets_at, q.polled_at FROM quotas q "
        "JOIN (SELECT provider_key, limit_key, MAX(polled_at) AS polled FROM quotas "
        "      WHERE source NOT LIKE 'sync:%' GROUP BY provider_key, limit_key) latest "
        "ON latest.provider_key=q.provider_key AND latest.limit_key=q.limit_key AND latest.polled=q.polled_at "
        "WHERE q.source NOT LIKE 'sync:%'"
    ).fetchall()
    seen = set()
    for provider, limit, label, unit, resets_at, polled_at in rows:
        # Cursor is no longer polled; an old reading is not a held lane.
        if (provider, limit) in seen or unit == "unavailable" or provider == "cursor":
            continue
        seen.add((provider, limit))
        polled = _parse(polled_at)
        reset = _parse(resets_at)
        if polled is None or (reset is not None and reset <= now):
            continue  # a reset window is masked on the meter, not held
        age = int((now - polled).total_seconds())
        if age <= LANE_HELD_AFTER_SECONDS:
            continue
        name = str(label or limit).split(" — ", 1)[0]
        # The lane's newest unavailable sample says why polls stopped confirming it.
        why = connection.execute(
            "SELECT label FROM quotas WHERE provider_key=? AND unit='unavailable' AND polled_at>? "
            "AND source NOT LIKE 'sync:%' ORDER BY polled_at DESC LIMIT 1",
            (provider, polled_at),
        ).fetchone()
        reason = str(why[0]).split(" — ", 1)[1] if why and " — " in str(why[0]) else None
        from spend_app.claude_accounts import provider_meta as claude_account_meta

        lane_name = LANE_NAMES.get(provider) or (claude_account_meta(provider) or {}).get("shortName") or provider
        short = f"{lane_name} {str(limit).replace('-', ' ')}"
        issues.append({"kind": "lane", "key": f"{provider}:{limit}", "name": name, "short": short, "state": "held", "severity": "warn",
                       "ageSeconds": age, "detail": reason or "The meter shows the last confirmed reading; newer polls returned no value."})
    return sorted(issues, key=lambda item: -item["ageSeconds"])


def _machines(connection, now: datetime) -> list[dict]:
    machines = []
    for source, finished in connection.execute(
        "SELECT source, MAX(finished_at) FROM ingest_runs WHERE source LIKE 'sync:%' AND status IN ('success','partial') GROUP BY source"
    ):
        seen = _parse(finished)
        age = None if seen is None else max(0, int((now - seen).total_seconds()))
        machines.append({"key": source[5:], "lastSeenAt": finished, "ageSeconds": age,
                         "state": "online" if age is not None and age <= SYNC_STALE_AFTER_SECONDS else "quiet"})
    return sorted(machines, key=lambda item: item["key"])


def _sync_state(state: dict, now: datetime, heartbeat_state: dict) -> tuple[dict | None, list[dict]]:
    if state.get("role") != "spoke":
        return None, []
    spoke = state.get("spokeConfig") or {}
    if not spoke.get("enabled"):
        return None, []
    last = _parse(spoke.get("lastSyncAt"))
    age = None if last is None else max(0, int((now - last).total_seconds()))
    error = bool(spoke.get("lastSyncError"))
    sync = {"role": "spoke", "lastSyncAt": spoke.get("lastSyncAt"), "ageSeconds": age, "failing": error}
    issues = []
    # Sync runs after each ingest pass; judge its age only once a pass could have run.
    settled = heartbeat_state["uptimeSeconds"] > SYNC_STALE_AFTER_SECONDS
    if error or (settled and (age is None or age > SYNC_STALE_AFTER_SECONDS)):
        issues.append({"kind": "sync", "key": "hub", "name": "Hub sync", "short": "Hub sync", "state": "failed" if error else "stale",
                       "severity": "alarm" if age is None or age > SYNC_STALE_AFTER_SECONDS else "warn", "ageSeconds": age,
                       "detail": "This computer could not reach its hub; figures from here are not reaching the dashboard."
                       if error else "This computer has not synced with its hub recently."})
    return sync, issues


def _refresh_hub_version(settings) -> None:
    version = None
    try:
        from spend_app.sync import fetch_from_configured_hub

        payload = fetch_from_configured_hub(settings, "/api/spend", ttl_seconds=HUB_VERSION_TTL_SECONDS)
        if isinstance(payload, dict) and isinstance(payload.get("version"), str):
            version = payload["version"][:40]
    except Exception:
        version = None
    with _LOCK:
        _HUB_VERSION.update(checkedMono=time.monotonic(), version=version, refreshing=False)


def hub_version(settings, *, background: bool = True) -> str | None:
    """Cached hub version; refreshed off the request path at most every 10 minutes."""
    with _LOCK:
        checked = _HUB_VERSION["checkedMono"]
        due = checked is None or time.monotonic() - checked >= HUB_VERSION_TTL_SECONDS
        start = due and not _HUB_VERSION["refreshing"]
        if start:
            _HUB_VERSION["refreshing"] = True
        version = _HUB_VERSION["version"]
    if start:
        if background:
            threading.Thread(target=_refresh_hub_version, args=(settings,), name="burnrate-hub-version", daemon=True).start()
        else:
            _refresh_hub_version(settings)
            with _LOCK:
                version = _HUB_VERSION["version"]
    return version


def report(settings, now: datetime | None = None, *, background: bool = True,
           history_active: bool = True, include_history: bool = True) -> dict:
    """This computer's collection, quota and sync state.

    Collection is unconditional, so a stalled heartbeat is always reported as
    an alarm — nothing masks it any more. ``history_active`` is accepted for
    compatibility and ignored.
    """
    from spend_app.db import connect, initialize
    from spend_app.setup_wizard import read_setup_state

    now = now or datetime.now(UTC)
    beat = heartbeat(now)
    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        sources = _source_issues(connection, now) if include_history else []
        lanes = _lane_issues(connection, now)
        machines = _machines(connection, now)
        state = read_setup_state(connection)
    sync, sync_issues = _sync_state(state, now, beat)
    issues: list[dict] = []
    if include_history and beat["stalled"]:
        age = beat["lastPassAgeSeconds"]
        issues.append({"kind": "ingest", "key": "local-ingest", "name": "Collection", "short": "Collection", "state": "stalled",
                       "severity": "alarm", "ageSeconds": age,
                       "detail": "No local collection pass has finished recently; the watchdog restarts the service."})
    if include_history and beat["passes"] < 2 and not beat["stalled"]:
        # The first pass after a start re-reads every store and can take
        # minutes, so sources it reached early, or has not reached yet, look
        # old until the next (normal-length) pass. A pass that never finishes
        # is caught by the stalled check above.
        sources = [item for item in sources if item["state"] != "stale"]
        issues.append({"kind": "ingest", "key": "local-ingest", "name": "Collection", "short": "Collection",
                       "state": "warming", "severity": "info", "ageSeconds": beat["uptimeSeconds"],
                       "detail": "Collection is catching up after the service started."})
    issues += sync_issues + sources + lanes
    hub = None
    if sync is not None:
        remote = hub_version(settings, background=background)
        hub = {"version": remote, "matches": None if remote is None else remote == __version__}
        if remote is not None and remote != __version__:
            # Informational: figures stay current, but the hub lacks this build's fixes.
            issues.append({"kind": "version", "key": "hub", "name": "Hub version", "short": "Hub version", "state": "mismatch", "severity": "info",
                           "ageSeconds": None, "detail": f"Hub runs {remote}; this computer runs {__version__}."})
    severities = {item["severity"] for item in issues}
    severity = "alarm" if "alarm" in severities else "warn" if "warn" in severities else "ok"
    return {
        "generatedAt": _iso(now),
        "version": __version__,
        "status": severity,
        "issues": issues,
        "ingest": beat,
        "sync": sync,
        "hub": hub,
        "machines": machines,
    }
