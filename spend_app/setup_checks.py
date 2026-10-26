"""What the setup wizard shows: one honest row per HUD card.

Each row says whether the tool's usage is being read and whether its plan
meter has a current reading, and what to do when it does not. The module also
owns the three switches for meters that reuse another app's sign-in, the plan
suggestions taken from what the meters already report, and the per-source
progress of the first collection. Everything here reads local state; only
``write_consent`` changes anything (the per-user ``.env`` and the process
environment).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from spend_app.db import connect

# Meters that reuse another application's sign-in. They are on by default for
# a new install (see config.load_user_env); the wizard shows them so the
# choice is visible and can be turned off.
CONSENT_SWITCHES = (
    {
        "key": "claude",
        "env": ("BURNRATE_ENABLE_CLAUDE_OAUTH_USAGE", "BURNRATE_CLAUDE_OAUTH_REFRESH"),
        "label": "Read Claude's plan usage with Claude Code's sign-in",
        "detail": "When Claude Code has no fresh reading of its own, BURNRATE asks Anthropic for your plan usage "
                  "with Claude Code's credentials, and renews them when they expire.",
        "lanes": ("claude-code", "claude-code-*"),
    },
    {
        "key": "zai",
        "env": ("BURNRATE_ENABLE_ZAI_QUOTA",),
        "label": "Read Z.AI credits with your Coding Plan key",
        "detail": "Uses the Z.AI key that OpenCode, ZCode or glm-acp-agent already stores to ask Z.AI for your "
                  "5-hour and weekly credits.",
        "lanes": ("opencode",),
    },
    {
        "key": "opencode_go",
        "env": ("BURNRATE_ENABLE_OPENCODE_GO_QUOTA",),
        "label": "Read OpenCode Go usage with OpenCode's sign-in",
        "detail": "Uses the OpenCode Console key each OpenCode profile stores to ask opencode.ai for its Go usage.",
        "lanes": ("opencode-go", "opencode-go-*"),
    },
)

# The usage source behind each built-in plan lane.
LANE_USAGE_SOURCES = {
    "claude-code": "claude_local",
    "codex": "codex_local",
    "grok": "grok_local",
    "opencode": "opencode_local",
    "opencode-go": "opencode_local",
    "antigravity": "antigravity_local",
    "muse": "muse_local",
}

# Names as the HUD shows them, in the HUD's usual order.
LANE_NAMES = {
    "claude-code": "Claude",
    "codex": "Codex",
    "antigravity": "Antigravity",
    "grok": "Grok",
    "opencode": "Z.AI",
    "opencode-go": "OpenCode Go",
    "muse": "Muse",
}

# What to do when a meter has no current reading.
FIX_HINTS = {
    "antigravity": "Open Antigravity. Its meter is read from the app while it runs.",
    "codex": "Finish a Codex turn. The meter comes from Codex's own session log.",
    "grok": "Run Grok CLI or Traycer once. The meter comes from their local snapshot.",
    "muse": "Sign in to Muse Code. The meter uses its saved session.",
    "opencode": "Check the Z.AI Coding Plan key that OpenCode or ZCode uses.",
    "opencode-go": "Sign in to OpenCode with your Console account.",
    "claude-code": "Use Claude Code once so it records a fresh reading.",
}

USAGE_STATES = {
    "healthy": ("ok", "Reading usage"),
    "partial": ("ok", "Reading usage"),
    "stale": ("warn", "Not read recently"),
    "reading": ("ok", "Reading now"),
    "failed": ("bad", "The last read failed"),
    "detected_without_history": ("waiting", "Found · no usage yet"),
    "not_checked": ("waiting", "Found · not read yet"),
    "not_detected": ("missing", "Not installed"),
    "configuration_missing": ("missing", "Not turned on"),
}

# A reading older than this is shown as held (matches freshness.py).
HELD_AFTER_SECONDS = 15 * 60

# Typical monthly prices, used only to prefill a suggestion the user confirms.
PLAN_PRICES = (
    ("claude max 20x", 200.0),
    ("claude max 5x", 100.0),
    ("claude max", 100.0),
    ("claude pro", 20.0),
    ("chatgpt pro", 200.0),
    ("chatgpt plus", 20.0),
    ("chatgpt team", 30.0),
    ("supergrok heavy", 300.0),
    ("supergrok", 30.0),
    ("google ai ultra", 249.99),
    ("google ai pro", 19.99),
    ("opencode go plus", 40.0),
    ("opencode go", 10.0),
    ("z.ai coding plan", 10.0),
)
# Words that tie a plan saved under "custom" to a plan lane.
PLAN_NAME_WORDS = {
    "claude-code": ("claude",),
    "codex": ("chatgpt", "codex"),
    "grok": ("grok",),
    "opencode": ("z.ai", "zai", "glm"),
    "opencode-go": ("opencode go",),
    "antigravity": ("antigravity", "google ai"),
    "muse": ("muse",),
}
DEFAULT_PLANS = {
    "codex": "ChatGPT Plus",
    "grok": "SuperGrok",
    "opencode": "Z.AI Coding Plan",
    "opencode-go": "OpenCode Go",
    "antigravity": "Google AI Pro",
    "muse": "Muse Code Power Usage",
}
REASON_SEPARATOR = " — "


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def consent_state(environ=None) -> list[dict]:
    env = os.environ if environ is None else environ
    return [
        {
            "key": switch["key"],
            "label": switch["label"],
            "detail": switch["detail"],
            # The first variable is the switch; the rest follow it.
            "enabled": _truthy(env.get(switch["env"][0])),
            "lanes": list(switch["lanes"]),
        }
        for switch in CONSENT_SWITCHES
    ]


def consent_key_for_lane(lane: str) -> str | None:
    for switch in CONSENT_SWITCHES:
        for pattern in switch["lanes"]:
            if lane == pattern or (pattern.endswith("*") and lane.startswith(pattern[:-1])):
                return switch["key"]
    return None


def write_consent(changes: dict, *, env_file: Path | None = None) -> tuple[list[dict], str | None]:
    """Apply switch changes now and keep them in the per-user ``.env``."""
    from dotenv import set_key

    from spend_app.config import default_app_dir

    known = {switch["key"]: switch for switch in CONSENT_SWITCHES}
    unknown = [key for key in changes if key not in known]
    if unknown:
        raise ValueError("Unknown setting: " + ", ".join(sorted(unknown)))
    path = Path(env_file) if env_file is not None else default_app_dir() / ".env"
    warning = None
    for key, enabled in changes.items():
        value = "1" if bool(enabled) else "0"
        for name in known[key]["env"]:
            os.environ[name] = value
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch(exist_ok=True)
                set_key(str(path), name, value, quote_mode="never")
            except OSError:
                warning = "Saved for this session only: the settings file could not be written."
    return consent_state(), warning


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _ago(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h ago"
    return f"{hours // 24} days ago"


def _limit_text(limit_key: str) -> str:
    names = {"5h": "5-hour window", "weekly": "weekly window", "monthly": "monthly window"}
    scope, _, window = str(limit_key).rpartition("-")
    text = names.get(limit_key) or names.get(window, str(limit_key).replace("-", " "))
    prefix = {"gemini": "Gemini ", "3p": "Claude/GPT "}.get(scope, "")
    return prefix + text


def lane_name(lane: str) -> str:
    from spend_app.aggregate import provider_meta

    meta = provider_meta(lane) or {}
    return LANE_NAMES.get(lane) or meta.get("shortName") or meta.get("providerName") or lane


def _usage(harness: dict | None) -> dict:
    if harness is None:
        return {"state": "missing", "text": "Not installed"}
    state, text = USAGE_STATES.get(harness.get("state"), ("waiting", "Found · not read yet"))
    if state == "missing" and harness.get("detected"):
        state, text = "waiting", "Found · not read yet"
    read_at = _parse(harness.get("lastSuccess"))
    if harness.get("state") in {"stale", "failed", "reading"} and read_at is not None:
        text += " · last read " + _ago((datetime.now(UTC) - read_at).total_seconds())
    return {"state": state, "text": text, "path": harness.get("path") or ""}


# Reasons that mean "nothing to read right now", not a broken meter.
IDLE_REASONS = ("reports usage only for a few hours after you use it",)


def _meter(lane: str, rows: list[dict], now: datetime, consent_on: bool | None) -> dict:
    readings = [
        row for row in rows
        if row["unit"] != "unavailable" and (row["pct"] is not None or row["used"] is not None)
        and (_parse(row["resets_at"]) is None or _parse(row["resets_at"]) > now)
    ]
    # Extra Claude accounts and OpenCode profiles share their default lane's fix.
    base = next((key for key in ("claude-code", "opencode-go") if lane.startswith(key + "-")), lane)
    hint = FIX_HINTS.get(base)
    if consent_on is False:
        hint = "Turn on the switch below to read this meter."
    if readings:
        newest = max((_parse(row["polled_at"]) for row in readings if row["polled_at"]), default=None)
        top = max(readings, key=lambda row: row["pct"] if row["pct"] is not None else -1)
        value = f"{round(top['pct'])}% of the {_limit_text(top['limit_key'])}" if top["pct"] is not None else "Reading"
        age = (now - newest).total_seconds() if newest else None
        if age is not None and age > HELD_AFTER_SECONDS:
            reason = next((str(row["label"]).split(REASON_SEPARATOR, 1)[1] for row in rows
                           if row["unit"] == "unavailable" and REASON_SEPARATOR in str(row["label"])), None)
            return {"state": "held", "text": f"{value} · last reading {_ago(age)}",
                    "detail": reason or "Newer polls returned no value, so the last reading is shown.", "fix": hint}
        return {"state": "ok", "text": value, "detail": None, "fix": None}
    reason = next((str(row["label"]).split(REASON_SEPARATOR, 1)[1] for row in rows
                   if row["unit"] == "unavailable" and REASON_SEPARATOR in str(row["label"])), None)
    if rows and not reason:
        reason = "The window reset; waiting for a new reading."
    if reason and any(phrase in reason for phrase in IDLE_REASONS):
        return {"state": "idle", "text": "Not used recently", "detail": reason, "fix": None}
    return {"state": "unavailable", "text": "No current reading", "detail": reason or "No reading yet.", "fix": hint}


def _latest_local_quotas(connection) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in connection.execute(
        """
        SELECT q.provider_key, q.limit_key, q.label, q.unit, q.pct, q.used, q.resets_at, q.polled_at
        FROM quotas q
        JOIN (
            SELECT provider_key, limit_key, MAX(id) AS max_id
            FROM quotas WHERE source NOT LIKE 'sync:%'
            GROUP BY provider_key, limit_key
        ) latest ON q.id = latest.max_id
        """
    ):
        grouped.setdefault(row["provider_key"], []).append(dict(row))
    return grouped


def lane_rows(settings, harnesses: list[dict], *, now: datetime | None = None) -> dict:
    """One row per plan card on this computer, plus usage-only tools and tools not found."""
    from spend_app.aggregate import provider_meta
    from spend_app.quotas import BALANCE_SOURCES, REQUIRED_LIMITS, default_quota_collectors

    moment = now or datetime.now(UTC)
    by_source = {item["source"]: item for item in harnesses}
    consent = {item["key"]: item["enabled"] for item in consent_state()}
    try:
        lanes = [key for key in default_quota_collectors(settings.database_path) if key not in BALANCE_SOURCES]
    except Exception:
        lanes = [key for key in REQUIRED_LIMITS if key not in BALANCE_SOURCES]
    with connect(Path(settings.database_path)) as connection:
        quotas = _latest_local_quotas(connection)
    order = list(LANE_NAMES)
    lanes = sorted(
        (lane for lane in lanes if lane in quotas or lane in REQUIRED_LIMITS),
        key=lambda lane: (order.index(lane) if lane in order else len(order), lane),
    )
    rows = []
    for lane in lanes:
        meta = provider_meta(lane) or {}
        consent_key = consent_key_for_lane(lane)
        source = LANE_USAGE_SOURCES.get(lane)
        if source:
            usage = _usage(by_source.get(source))
        elif lane.startswith("opencode-go-"):
            usage = {"state": "shared", "text": "Sessions in this profile are not counted yet"}
        else:
            usage = {"state": "shared", "text": "Usage is counted with the default profile"}
        rows.append({
            "key": lane,
            "name": lane_name(lane),
            "plan": meta.get("plan"),
            "tool": meta.get("toolKey") or lane,
            "usage": usage,
            "meter": _meter(lane, quotas.get(lane, []), moment, consent.get(consent_key) if consent_key else None),
            "consent": consent_key,
        })
    lane_sources = set(LANE_USAGE_SOURCES.values())
    usage_only = [
        {"key": item["source"], "name": item["name"], "tool": item["tool"], "usage": _usage(item)}
        for item in harnesses if item.get("detected") and item["source"] not in lane_sources
    ]
    not_found = [item["name"] for item in harnesses if not item.get("detected")]
    return {"lanes": rows, "usageOnly": usage_only, "notFound": not_found}


def _plan_tool(lane: str) -> str:
    if lane.startswith("claude-code"):
        return "claude-code"
    if lane.startswith("opencode-go"):
        return "opencode-go"
    return lane


def _price(name: str) -> float | None:
    lowered = name.lower()
    return next((price for prefix, price in PLAN_PRICES if lowered.startswith(prefix)), None)


def _cached_plan(prefix: str) -> str | None:
    """A plan name a recent quota poll already reported (no new I/O)."""
    try:
        from spend_app.limits import _CACHE, _CACHE_LOCK

        with _CACHE_LOCK:
            entries = [value for key, value in _CACHE.items() if key.startswith(prefix)]
    except Exception:
        return None
    for _stamp, payload in entries:
        plan = payload.get("plan") if isinstance(payload, dict) else None
        if isinstance(plan, str) and plan and "unknown" not in plan.lower():
            return plan
    return None


def _claude_tier_plan(lane: str) -> str | None:
    from spend_app import claude_accounts

    if lane == claude_accounts.PRIMARY_KEY:
        path = claude_accounts.primary_state_path(Path.home(), os.environ)
    else:
        account = claude_accounts.account_for_key(lane)
        path = account.state_path if account else None
    account_info = claude_accounts.read_state(path).get("oauthAccount") if path else None
    tier = str((account_info or {}).get("organizationRateLimitTier") or "").lower()
    if "max" in tier:
        return "Claude Max 20x" if "20x" in tier else "Claude Max 5x"
    if "pro" in tier:
        return "Claude Pro"
    return None


def _plan_hint(lane: str) -> tuple[str, float | None, str]:
    from spend_app.aggregate import provider_meta

    tool = _plan_tool(lane)
    name, basis = None, "Typical price · check yours"
    if tool == "claude-code":
        name = _claude_tier_plan(lane)
        basis = "From the account tier Claude Code records" if name else basis
        name = name or "Claude Pro"
    elif tool == "codex":
        name = _cached_plan("codex_quota_poll:")
        basis = "From Codex's own rate-limit record" if name else basis
    elif tool == "grok":
        name = _cached_plan("grok_quota_poll")
        basis = "From Grok's billing snapshot" if name else basis
    name = name or DEFAULT_PLANS.get(tool, lane_name(lane))
    price = _price(name)
    # A second account's plan is its own row: "Claude Max 20x (Work)".
    label = (provider_meta(lane) or {}).get("accountLabel")
    if label:
        name = f"{name} ({label})"
    return name, price, basis


def plan_suggestions(connection, rows: list[dict], timezone_name: str) -> dict:
    """Plans already tracked, and one suggestion per measured plan card that has no plan yet."""
    from spend_app.plan_service import effective_terms

    try:
        today = datetime.now(UTC).astimezone(ZoneInfo(timezone_name)).date().isoformat()
    except Exception:
        today = datetime.now(UTC).date().isoformat()
    active = [
        {"tool_key": plan["tool_key"], "name": plan["name"], "amount_usd": float(plan["amount_usd"]),
         "cadence": plan["cadence"]}
        for plan in effective_terms(connection)
        if not plan.get("end_date") or plan["end_date"] >= today
    ]
    tracked: dict[str, int] = {}
    for plan in active:
        tool = plan["tool_key"]
        if tool not in PLAN_NAME_WORDS:
            lowered = plan["name"].lower()
            tool = next((key for key, words in PLAN_NAME_WORDS.items() if any(word in lowered for word in words)), tool)
        tracked[tool] = tracked.get(tool, 0) + 1
    measured: dict[str, list[str]] = {}
    for row in rows:
        if row["meter"]["state"] in {"ok", "held"}:
            measured.setdefault(_plan_tool(row["key"]), []).append(row["key"])
    suggestions = []
    for tool, lanes in measured.items():
        missing = len(lanes) - tracked.get(tool, 0)
        for lane in lanes[len(lanes) - missing:] if missing > 0 else []:
            name, price, basis = _plan_hint(lane)
            suggestions.append({"lane": lane, "tool_key": tool, "name": name, "amount_usd": price,
                                "cadence": "monthly", "basis": basis})
    return {"active": active, "suggestions": suggestions}


TOOL_LABELS = {
    "claude-code": "Claude", "codex": "Codex", "grok": "Grok", "opencode": "Z.AI", "opencode-go": "OpenCode Go",
    "antigravity": "Antigravity", "muse": "Muse",
}


def plan_issues(plans_payload: dict | None) -> list[dict]:
    """Active plans that are not compared with any tool's usage, and the fix for each."""
    issues = []
    for plan in (plans_payload or {}).get("plans") or []:
        if plan.get("end_date"):
            continue  # already has a last active date
        term = next((term for term in reversed(plan.get("terms") or []) if term.get("status") == "active"), None)
        if term is None:
            continue
        base = {"planId": plan["id"], "version": plan["version"], "termId": term.get("id"), "name": term["name"],
                "toolKey": term["tool_key"], "amountUsd": str(term["amount_usd"])}
        if term["tool_key"] == "cursor":
            issues.append({**base, "issue": "Cursor is no longer tracked, so this plan is not compared with any usage.",
                           "fix": "end", "fixLabel": "End this plan"})
            continue
        if term["tool_key"] == "custom":
            lowered = term["name"].lower()
            tool = next((key for key, words in PLAN_NAME_WORDS.items() if any(word in lowered for word in words)), None)
            if tool:
                label = TOOL_LABELS.get(tool, tool)
                issues.append({**base, "issue": f"Saved as a custom plan, so it is not compared with {label} usage.",
                               "fix": "link", "toolKeyFix": tool, "fixLabel": f"Link to {label}"})
    return issues


def collection_progress(settings) -> dict:
    """The collection loop's heartbeat and each local source's latest run."""
    from spend_app.freshness import SOURCE_NAMES, heartbeat
    from spend_app.providers import REGISTRY, RETIRED_SOURCES

    local = {spec.key for spec in REGISTRY if spec.kind in {"local", "manual"}}
    with connect(Path(settings.database_path)) as connection:
        latest = connection.execute(
            """
            SELECT r.source, r.status, r.started_at, r.finished_at, r.events_written
            FROM ingest_runs r
            JOIN (SELECT source, MAX(id) AS max_id FROM ingest_runs GROUP BY source) last ON r.id = last.max_id
            """
        ).fetchall()
    sources = [
        {"source": row["source"], "name": SOURCE_NAMES.get(row["source"], row["source"]), "status": row["status"],
         "startedAt": row["started_at"], "finishedAt": row["finished_at"], "events": int(row["events_written"] or 0)}
        for row in latest if row["source"] in local and row["source"] not in RETIRED_SOURCES
    ]
    sources.sort(key=lambda item: item["name"].lower())
    return {"heartbeat": heartbeat(), "sources": sources}
