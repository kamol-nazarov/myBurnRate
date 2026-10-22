"""HUD reads: live meters first, bounded recorded-usage statistics separately."""

import calendar
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from spend_app import aggregate
from spend_app.capacity_forecast import (
    active_provider_keys,
    apply_forecasts,
    load_pace_samples,
)
from spend_app.db import connect
from spend_app.sync import list_machines
from spend_app.timeutil import epoch_micros

# Telemetry that draws on each plan's quota windows. Extra Claude accounts
# have none: only the default profile's sessions are collected.
BURN_TOOL_KEYS = {
    "claude-code": ("claude-code",),
    "codex": ("codex",),
    "grok": ("grok",),
    "opencode": ("opencode", "zcode"),
    "opencode-go": ("opencode-go",),
    "antigravity": ("antigravity",),
    "muse": ("muse",),
}
# The plan already names the provider; model rows drop the repeat.
MODEL_PREFIXES = {
    "claude-code": "Claude ",
    "antigravity": "Antigravity ",
    "grok": "SuperGrok ",
    "muse": "Muse ",
    "opencode-go": "OpenCode Go ",
}
MODEL_WORDS = {"glm": "GLM", "gpt": "GPT", "oss": "OSS", "deepseek": "DeepSeek", "ai": "AI"}
WINDOW_LENGTHS = {"5h": timedelta(hours=5), "weekly": timedelta(days=7)}


def live_snapshot(settings, *, now: datetime, machine: str = "all") -> dict:
    with connect(settings.database_path) as connection:
        machines = list_machines(connection, now=now)
        capacity = aggregate._capacity_from_rows(
            aggregate._load_quota_rows(connection, now=now), now=now, month_to_date={}
        )
        apply_forecasts(capacity, load_pace_samples(connection, now), now=now,
                        active=active_provider_keys(connection, now))
        activity = aggregate._activity_from_rows(
            aggregate._load_activity_rows(connection, machine), {m["id"]: m["name"] for m in machines}
        )
    return {"generatedAt": aggregate._iso(now), "status": "live", "capacity": capacity,
            "activity": activity, "selectedMachine": machine}


def recorded_usage(settings, pricing, *, now: datetime, machine: str = "all") -> dict:
    zone = ZoneInfo(settings.timezone)
    local_now = now.astimezone(zone)
    today_start = datetime.combine(local_now.date(), time.min, zone).astimezone(UTC)
    with connect(settings.database_path) as connection:
        today = aggregate._tracked_value(connection, pricing, today_start, now, "all", settings.timezone,
                                         cap_date=local_now.date(), machine=machine)
        burn, average = aggregate._seven_day_pace(connection, pricing, now, settings.timezone, machine)
        mix = aggregate.TokenMix()
        for event in [*aggregate._load_events(connection, now - timedelta(days=1), now, "all", machine),
                      *aggregate._load_unpriced_events(connection, now - timedelta(days=1), now, "all", machine)]:
            mix.add_event(event)
        last_import = connection.execute(
            "SELECT MAX(finished_at) FROM ingest_runs WHERE status IN ('success','partial')"
        ).fetchone()[0]
        capacity = aggregate._capacity_from_rows(
            aggregate._load_quota_rows(connection, now=now), now=now, month_to_date={}
        )
        window_tokens = window_burn(connection, capacity, now=now, today_start=today_start, machine=machine)
    return {"generatedAt": aggregate._iso(now), "historyUpdatedAt": last_import,
            "navigation": {"burnRatePerDay": float(burn) if burn is not None else None,
                           "todayUsd": float(today.usage_value) if today.records and today.usage_value is not None else None,
                           "todayTokens": int(today.tokens) if today.records else 0,
                           "tokensAvgPerDay": average, "todayComplete": today.complete if today.records else False},
            "totals": {"cacheReusePct": aggregate._cache_reuse(mix)},
            "windowTokens": window_tokens}


def _window_start(limit_key: str, resets_at: str | None) -> datetime | None:
    """Start of the quota window that ends at ``resets_at``."""
    if not resets_at:
        return None
    try:
        reset = aggregate._parse(resets_at)
    except ValueError:
        return None
    span = limit_key.rsplit("-", 1)[-1]
    if span == "monthly":
        year, month = (reset.year - 1, 12) if reset.month == 1 else (reset.year, reset.month - 1)
        return reset.replace(year=year, month=month, day=min(reset.day, calendar.monthrange(year, month)[1]))
    length = WINDOW_LENGTHS.get(span)
    return reset - length if length else None


def _lane_takes(provider_key: str, limit_key: str):
    """Antigravity meters Gemini and the other models in separate lanes."""
    if provider_key == "antigravity" and limit_key.startswith("gemini-"):
        return lambda model: "gemini" in model
    if provider_key == "antigravity" and limit_key.startswith("3p-"):
        return lambda model: "gemini" not in model
    return lambda _model: True


def _pretty_slug(slug: str) -> str:
    """"deepseek-v4.1-flash" -> "DeepSeek V4.1 Flash"."""
    words = []
    for word in slug.replace("_", "-").split("-"):
        if not word:
            continue
        if word.lower() in MODEL_WORDS:
            words.append(MODEL_WORDS[word.lower()])
        elif word[0].isdigit() or (word[0] in "vV" and word[1:2].isdigit()):
            words.append(word.upper())
        else:
            words.append(word.capitalize())
    return " ".join(words)


def _model_name(provider_key: str, model_key: str) -> str:
    model_key = aggregate.reporting_model_key(model_key)
    name = aggregate.display_model(model_key)
    slug = model_key.split(":", 1)[-1]
    # display_model upper- or title-cases an uncurated slug; tidy it, keep the client ("OpenCode Go").
    generic = next((form for form in (slug.replace("-", " ").upper(), slug.replace("-", " ").title())
                    if name.endswith(form)), None)
    if model_key not in aggregate.MODEL_NAMES and (name == model_key or generic):
        client = "" if name == model_key else name[: -len(generic)].strip()
        name = " ".join(filter(None, [client, _pretty_slug(slug)]))
    prefix = MODEL_PREFIXES.get(provider_key)
    return name[len(prefix):] if prefix and name.startswith(prefix) and len(name) > len(prefix) else name


def window_burn(connection, capacity: list[dict], *, now: datetime, today_start: datetime,
                machine: str = "all") -> dict:
    """Measured tokens inside each open quota window, by plan and limit.

    Weekly lanes also carry today's tokens since local midnight by model.
    """
    lanes = []
    for card in capacity:
        provider_key = card.get("providerKey") or ""
        if provider_key not in BURN_TOOL_KEYS:
            continue
        for row in card.get("rows") or []:
            limit_key = row.get("limitKey") or ""
            start = _window_start(limit_key, row.get("resetsAt"))
            if start is not None and start <= now:
                lanes.append((provider_key, limit_key, start))
    if not lanes:
        return {}
    since = min(min(start for *_, start in lanes), today_start)
    fingerprint = tuple(
        tuple(connection.execute(
            f"SELECT COUNT(*), MAX(id), MAX(ingested_at) FROM {table} "
            "WHERE occurred_epoch_us IS NOT NULL AND occurred_epoch_us >= ?",
            (epoch_micros(since),),
        ).fetchone())
        for table in ("usage_events", "unpriced_usage_events")
    )
    key = (aggregate._connection_identity(connection), machine, tuple(lanes), today_start, fingerprint)
    return aggregate._memo(
        "hud_window_burn", key, lambda: _window_burn_uncached(connection, lanes, since, now, today_start, machine)
    )


def _window_burn_uncached(connection, lanes, since, now, today_start, machine) -> dict:
    tools = {tool for provider_key, *_ in lanes for tool in BURN_TOOL_KEYS[provider_key]}
    events: dict[str, list[tuple]] = {}
    for tool in sorted(tools):
        rows = [*aggregate._load_events(connection, since, now, tool, machine),
                *aggregate._load_unpriced_events(connection, since, now, tool, machine)]
        events[tool] = [
            (int(row["occurred_epoch_us"]), str(row.get("model_key") or "unknown"),
             int(row.get("cached_input_tokens") or 0), aggregate._fresh(row),
             int(row.get("cache_write_tokens") or 0), int(row.get("output_tokens") or 0))
            for row in rows
        ]
    today_us = epoch_micros(today_start)
    result: dict[str, dict] = {}
    for provider_key, limit_key, start in lanes:
        takes = _lane_takes(provider_key, limit_key)
        start_us = epoch_micros(start)
        cached = fresh = output = 0
        today_models: dict[str, int] = defaultdict(int)
        for tool in BURN_TOOL_KEYS[provider_key]:
            for when, model, cache_read, new_input, cache_write, out in events[tool]:
                if not takes(model):
                    continue
                if when >= start_us:
                    cached += cache_read
                    fresh += new_input + cache_write
                    output += out
                if when >= today_us:
                    today_models[_model_name(provider_key, model)] += cache_read + new_input + cache_write + out
        lane = {"since": aggregate._iso(start), "tokens": cached + fresh + output,
                "input": fresh, "cached": cached, "output": output}
        if limit_key.rsplit("-", 1)[-1] == "weekly":
            models = sorted(((name, tokens) for name, tokens in today_models.items() if tokens > 0),
                            key=lambda item: (-item[1], item[0]))
            lane["today"] = {"tokens": sum(tokens for _, tokens in models),
                             "models": [{"name": name, "tokens": tokens} for name, tokens in models]}
        result.setdefault(provider_key, {})[limit_key] = lane
    return result
