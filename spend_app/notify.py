"""Desktop notifications from the running service.

Four kinds, each sent once per episode:

- ``threshold``: a plan window passes its alert level (90% unless set
  otherwise in the forecast preferences),
- ``dry``: a window's forecast runs out before the window resets,
- ``held``: a meter stuck on an old reading for over an hour,
- ``sync``: hub sync failing for over 15 minutes.

A window's episode lasts until it resets; a held meter's or a failing sync's
lasts until it recovers, so the next stretch notifies again. Quiet hours (the
forecast preferences) hold back everything except a run-out due within two
hours; a held-back notification is sent when they end if it still applies.

The service shows them as Windows toasts under BURNRATE's Start-menu app id,
so they arrive with no dashboard open. The desktop service turns them on
(``BURNRATE_DESKTOP_NOTIFICATIONS=1``); the CLI and tests leave them off.
Setup's Check step has a switch per kind.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from spend_app.timeutil import parse_utc

ENV = "BURNRATE_DESKTOP_NOTIFICATIONS"
STATE_KEY = "desktop_notifications.v1"
HELD_AFTER_SECONDS = 3600
SYNC_FAILING_AFTER_SECONDS = 15 * 60
DRY_MIN_PCT = 50
URGENT_HOURS = 2
APP_ID = "dev.burnrate.desktop"
# Windows PowerShell's own id, for a computer without BURNRATE's shortcut.
FALLBACK_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
# Meters that stop updating for an ordinary reason say so; they are not stuck.
QUIET_REASONS = ("isn't running", "reports usage only for a few hours after you use it")

KINDS = (
    ("threshold", "A plan passes its alert level"),
    ("dry", "A plan will run out before it resets"),
    ("held", "A meter stops updating for over an hour"),
    ("sync", "Hub sync keeps failing"),
)

WINDOW_WORDS = {"5h": "5-hour window", "weekly": "weekly window", "monthly": "monthly window"}

log = logging.getLogger(__name__)


def enabled() -> bool:
    return (os.getenv(ENV, "").strip().lower() in {"1", "true", "yes"}
            and os.getenv("BURNRATE_CENTRAL_SERVER", "").strip().lower() not in {"1", "true", "yes"})


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _span(seconds: float) -> str:
    hours = seconds / 3600
    if hours < 1:
        return f"{max(1, int(round(seconds / 60)))} min"
    if hours < 48:
        return f"{int(round(hours))} h"
    return f"{int(round(hours / 24))} days"


def _pct(value) -> str:
    return f"{int(round(float(value)))}%"


# ---------- state ----------

def _blank() -> dict:
    return {"kinds": {key: True for key, _label in KINDS}, "fired": {}}


def load_state(connection) -> dict:
    state = _blank()
    row = connection.execute("SELECT value FROM app_meta WHERE key=?", (STATE_KEY,)).fetchone()
    try:
        saved = json.loads(row[0]) if row else {}
    except (TypeError, ValueError):
        saved = {}
    if isinstance(saved, dict):
        kinds = saved.get("kinds")
        if isinstance(kinds, dict):
            state["kinds"].update({key: bool(kinds[key]) for key, _label in KINDS if key in kinds})
        if isinstance(saved.get("fired"), dict):
            state["fired"] = {str(k): str(v) for k, v in saved["fired"].items()}
    return state


def save_state(connection, state: dict) -> None:
    connection.execute(
        "INSERT INTO app_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (STATE_KEY, json.dumps(state, sort_keys=True, separators=(",", ":"))),
    )


def public_settings(database_path) -> dict:
    from spend_app.db import connect, initialize

    initialize(database_path)
    with connect(database_path) as connection:
        state = load_state(connection)
    return {"available": enabled() and os.name == "nt",
            "kinds": [{"key": key, "label": label, "on": state["kinds"][key]} for key, label in KINDS]}


def save_kinds(database_path, changes: dict) -> dict:
    from spend_app.db import connect, initialize

    known = {key for key, _label in KINDS}
    unknown = set(changes) - known
    if unknown:
        raise ValueError(f"Unknown notification: {sorted(unknown)[0]}")
    initialize(database_path)
    with connect(database_path) as connection:
        state = load_state(connection)
        state["kinds"].update({key: bool(value) for key, value in changes.items()})
        save_state(connection, state)
    return public_settings(database_path)


# ---------- conditions ----------

def _window_text(card: dict, row: dict) -> str:
    limit = str(row.get("limitKey") or "")
    if limit in WINDOW_WORDS:
        return WINDOW_WORDS[limit]
    label = str(row.get("label") or limit).split(" — ", 1)[0].replace(" remaining", "")
    provider = str(card.get("providerName") or "")
    return label[len(provider):].strip() if provider and label.startswith(provider + " ") else label


def plan_conditions(cards: list[dict], preferences: dict, now: datetime) -> list[dict]:
    """One condition per window that has passed its level or will run out."""
    conditions = []
    overrides = preferences.get("overrides") or {}
    for card in cards:
        if card.get("isPayg"):
            continue
        provider = str(card.get("providerKey") or "")
        name = card.get("shortName") or card.get("providerName") or provider
        level = int(overrides.get(provider, preferences.get("threshold", 90)))
        for row in card.get("rows") or []:
            reset = parse_utc(row.get("resetsAt"))
            if row.get("pct") is None or row.get("isPayg") or reset is None or reset <= now:
                continue
            pct = float(row["pct"])
            forecast = row.get("forecast") if isinstance(row.get("forecast"), dict) else {}
            projected = parse_utc(forecast.get("projectedRunDry"))
            runs_dry = (forecast.get("state") in {"tight", "dry"} and projected is not None
                        and projected < reset and pct >= DRY_MIN_PCT)
            over = pct >= level
            if not (over or runs_dry):
                continue
            episode = f"{provider}|{row.get('limitKey')}|{_iso(reset)}"
            window = _window_text(card, row)
            resets = f"Resets in {_span((reset - now).total_seconds())}."
            dry_line = ""
            if runs_dry:
                left = max(0.0, (projected - now).total_seconds())
                early = (reset - projected).total_seconds()
                dry_line = f"At the current pace it runs out in {_span(left)}, {_span(early)} before it resets."
            conditions.append({
                "kinds": [kind for kind, on in (("threshold", over), ("dry", runs_dry)) if on],
                "keys": [f"{kind}|{episode}" for kind, on in (("threshold", over), ("dry", runs_dry)) if on],
                "titles": {"threshold": f"{name}: {window} at {_pct(pct)}",
                           "dry": f"{name}: {window} will run out before it resets"},
                "body": " ".join(part for part in (f"{_pct(pct)} used." if runs_dry else "", dry_line, resets) if part),
                "urgent": runs_dry and projected is not None and (projected - now) <= timedelta(hours=URGENT_HOURS),
                "until": _iso(reset),
            })
    return conditions


def health_conditions(freshness: dict) -> list[dict]:
    conditions = []
    for issue in freshness.get("issues") or []:
        age = issue.get("ageSeconds")
        detail = str(issue.get("detail") or "")
        if issue.get("kind") == "lane" and age is not None and age > HELD_AFTER_SECONDS:
            if any(reason in detail for reason in QUIET_REASONS):
                continue
            conditions.append({"kinds": ["held"], "keys": [f"held|{issue.get('key')}"],
                               "titles": {"held": f"{issue.get('short') or issue.get('name')} meter has not updated for {_span(age)}"},
                               "body": f"{detail} Open setup's Check step to see the fix.", "urgent": False})
        elif issue.get("kind") == "sync" and issue.get("state") == "failed" and (age is None or age > SYNC_FAILING_AFTER_SECONDS):
            since = f" Last sync {_span(age)} ago." if age is not None else ""
            conditions.append({"kinds": ["sync"], "keys": ["sync|hub"], "titles": {"sync": "Hub sync is failing"},
                               "body": detail + since, "urgent": False})
    return conditions


# ---------- engine ----------

def _prune(fired: dict, current: set[str], now: datetime) -> dict:
    kept = {}
    for key, at in fired.items():
        kind, _, rest = key.partition("|")
        if kind in {"held", "sync"}:
            if key in current:  # the episode ends when the meter or sync recovers
                kept[key] = at
            continue
        reset = parse_utc(rest.rsplit("|", 1)[-1])
        if reset is not None and now - reset < timedelta(days=1):
            kept[key] = at
    return kept


def decide(conditions: list[dict], state: dict, *, now: datetime, quiet: bool) -> list[dict]:
    """The notifications to send now; marks them fired in ``state``."""
    fired = _prune(state["fired"], {key for c in conditions for key in c["keys"]}, now)
    due = []
    for condition in conditions:
        fresh = [(kind, key) for kind, key in zip(condition["kinds"], condition["keys"])
                 if key not in fired and state["kinds"].get(kind, True)]
        if not fresh or (quiet and not condition.get("urgent")):
            continue
        # A run-out is the stronger news when both arrive together.
        lead = "dry" if any(kind == "dry" for kind, _key in fresh) else fresh[0][0]
        due.append({"kind": lead, "title": condition["titles"][lead], "body": condition["body"],
                    "tag": hashlib.sha1(fresh[0][1].encode()).hexdigest()[:16]})
        for _kind, key in fresh:
            fired[key] = _iso(now)
    state["fired"] = fired
    return due


def _cards(settings, now: datetime) -> list[dict]:
    from spend_app.hud_data import live_snapshot
    from spend_app.sync import fetch_from_configured_hub

    # This computer's own meters carry their forecasts; plans read only on
    # another computer come from the hub's copy.
    local = live_snapshot(settings, now=now)["capacity"]
    have = {card.get("providerKey") for card in local if any(r.get("pct") is not None for r in card.get("rows") or [])}
    hub = fetch_from_configured_hub(settings, "/api/spend/hud", {"machine": "all"}, stale_seconds=300) or {}
    return [*local, *(card for card in hub.get("capacity") or [] if isinstance(card, dict) and card.get("providerKey") not in have)]


def check(settings, *, now: datetime | None = None, send=None) -> list[dict]:
    from spend_app.capacity_forecast import in_quiet_hours, load_state as forecast_state
    from spend_app.db import connect, initialize
    from spend_app.freshness import report

    now = now or datetime.now(UTC)
    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        preferences = forecast_state(connection)["preferences"]
    conditions = plan_conditions(_cards(settings, now), preferences, now)
    conditions += health_conditions(report(settings, now, include_history=False))
    quiet = in_quiet_hours(now, settings.timezone, preferences["quietStart"], preferences["quietEnd"])
    with connect(settings.database_path) as connection:
        state = load_state(connection)
        due = decide(conditions, state, now=now, quiet=quiet)
        save_state(connection, state)
    for note in due:
        try:
            (send or show_toast)(note["title"], note["body"], tag=note["tag"])
            log.info("notified: %s", note["title"])
        except Exception as error:  # a toast that fails never stops the check
            log.warning("notification failed: %s", error)
    return due


def notify_job(settings) -> None:
    if enabled():
        check(settings)


def register(scheduler, settings) -> None:
    scheduler.add_job(notify_job, "interval", seconds=60, kwargs={"settings": settings},
                      id="desktop-notifications", coalesce=True, max_instances=1)


# ---------- delivery ----------

_TOAST_SCRIPT = """
$ErrorActionPreference = 'Stop'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
function Esc([string]$text) { [Security.SecurityElement]::Escape($text) }
$xml = '<toast activationType="protocol" launch="' + (Esc $env:BURNRATE_TOAST_URL) + '"><visual><binding template="ToastGeneric"><text>' +
  (Esc $env:BURNRATE_TOAST_TITLE) + '</text><text>' + (Esc $env:BURNRATE_TOAST_BODY) + '</text></binding></visual></toast>'
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$toast = New-Object Windows.UI.Notifications.ToastNotification $doc
$toast.Tag = $env:BURNRATE_TOAST_TAG
$toast.Group = 'burnrate'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:BURNRATE_TOAST_APP).Show($toast)
"""


def _app_id() -> str:
    shortcut = Path(os.getenv("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "BURNRATE.lnk"
    return APP_ID if shortcut.is_file() else FALLBACK_APP_ID


def show_toast(title: str, body: str, *, tag: str = "burnrate", run=subprocess.run) -> None:
    """Show a Windows toast; the text travels in environment variables, never in the script."""
    if os.name != "nt":
        raise OSError("Desktop notifications need Windows")
    shell = Path(os.getenv("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    port = os.getenv("BURNRATE_PORT", "17331")
    env = {**os.environ, "BURNRATE_TOAST_TITLE": title[:200], "BURNRATE_TOAST_BODY": body[:600],
           "BURNRATE_TOAST_TAG": tag[:16], "BURNRATE_TOAST_APP": _app_id(),
           "BURNRATE_TOAST_URL": f"http://127.0.0.1:{port}/setup?step=4"}
    encoded = base64.b64encode(_TOAST_SCRIPT.encode("utf-16-le")).decode("ascii")
    completed = run([str(shell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                    env=env, capture_output=True, text=True, timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if completed.returncode != 0:
        raise OSError((completed.stderr or completed.stdout or "toast failed").strip()[-300:])
