"""HUD reads: live meters first, bounded recorded-usage statistics separately."""

import calendar
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from spend_app import aggregate
from spend_app.capacity_forecast import (
    active_provider_keys,
    apply_balance_runway,
    apply_forecasts,
    load_pace_samples,
)
from spend_app.db import connect
from spend_app.sync import list_machines
from spend_app.timeutil import epoch_micros

# Telemetry that draws on each plan's quota windows. An extra Claude account
# or OpenCode profile records its sessions under its own card key.
BURN_TOOL_KEYS = {
    "claude-code": ("claude-code",),
    "codex": ("codex",),
    "grok": ("grok",),
    "opencode": ("opencode", "zcode"),
    "opencode-go": ("opencode-go",),
    "antigravity": ("antigravity",),
    "muse": ("muse",),
}


def _burn_tools(provider_key: str) -> tuple[str, ...] | None:
    """Tool keys whose usage burns a plan card; an extra profile's card has its own."""
    if provider_key in BURN_TOOL_KEYS:
        return BURN_TOOL_KEYS[provider_key]
    if provider_key.startswith(("claude-code-", "opencode-go-")):
        return (provider_key,)
    return None


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


def lane_usage_pace(connection, capacity: list[dict], *, now: datetime, machine: str = "all") -> dict[tuple[str, str], dict]:
    """Measured token burn inside each open 5h/weekly window, from local usage records.

    This is the Claude-Code-Usage-Monitor data model: burn velocity comes
    from the usage the harnesses actually wrote — never from the quota
    poller — so a held or failing quota lane cannot silence the forecast.
    Settled hours are memoized behind the same row fingerprint window_burn
    uses; only the latest hour is read on each poll.
    """
    lanes = []
    for card in capacity:
        provider_key = card.get("providerKey") or ""
        if _burn_tools(provider_key) is None or card.get("isPayg"):
            continue
        for row in card.get("rows") or []:
            limit_key = row.get("limitKey") or ""
            if limit_key.rsplit("-", 1)[-1] not in WINDOW_LENGTHS:
                continue
            start = _window_start(limit_key, row.get("resetsAt"))
            if start is not None and start <= now < aggregate._parse(row["resetsAt"]):
                lanes.append((provider_key, limit_key, start))
    if not lanes:
        return {}
    since = min(start for *_, start in lanes)
    tools = tuple(sorted({tool for provider_key, *_ in lanes for tool in _burn_tools(provider_key)}))
    cutoff = max(since, now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1))
    key = (aggregate._connection_identity(connection), "pace", machine, tools, since, cutoff,
           _usage_fingerprint(connection, since, cutoff))
    settled = aggregate._memo("hud_burn_rows", key, lambda: _burn_rows(connection, tools, since, cutoff, machine))
    recent = _burn_rows(connection, tools, cutoff, now, machine)
    rows_by_tool = {tool: settled[tool] + recent[tool] for tool in tools}
    hour_ago_us = epoch_micros(now - timedelta(hours=1))
    pace: dict[tuple[str, str], dict] = {}
    for provider_key, limit_key, start in lanes:
        takes = _lane_takes(provider_key, limit_key)
        start_us = epoch_micros(start)
        tokens = recent_tokens = 0
        for tool in _burn_tools(provider_key):
            for when, model, cache_read, new_input, out in rows_by_tool[tool]:
                if not takes(model) or when < start_us:
                    continue
                total = cache_read + new_input + out
                tokens += total
                if when >= hour_ago_us:
                    recent_tokens += total
        elapsed_hours = (now - start).total_seconds() / 3600
        # Last-hour velocity; a younger window falls back to its own average.
        # An idle last hour is zero burn, not the window's historical average,
        # so a quiet window cannot forecast a run-dry it is not heading for.
        if elapsed_hours >= 1:
            rate = float(recent_tokens)
        else:
            rate = tokens / elapsed_hours if elapsed_hours > 0 else 0.0
        pace[(provider_key, limit_key)] = {
            "tokens": tokens, "tokensPerHour": rate, "since": aggregate._iso(start),
        }
    return pace


# A HUD read whose newest successful ingest is older than this is labelled
# stale so the page can alarm instead of trusting held numbers.
DATA_STALE_AFTER_SECONDS = 600


def _last_success_stamp(connection) -> str | None:
    return connection.execute(
        "SELECT MAX(finished_at) FROM ingest_runs WHERE status IN ('success','partial')"
    ).fetchone()[0]


def live_snapshot(settings, *, now: datetime, machine: str = "all") -> dict:
    with connect(settings.database_path) as connection:
        machines = list_machines(connection, now=now)
        capacity = aggregate._capacity_from_rows(
            aggregate._load_quota_rows(connection, now=now), now=now, month_to_date={}
        )
        usage_pace = lane_usage_pace(connection, capacity, now=now, machine=machine)
        apply_forecasts(capacity, load_pace_samples(connection, now), now=now,
                        active=active_provider_keys(connection, now), usage_pace=usage_pace)
        apply_balance_runway(capacity, connection, now=now)
        last_ingest = _last_success_stamp(connection)
        activity = aggregate._activity_from_rows(
            aggregate._load_activity_rows(connection, machine), {m["id"]: m["name"] for m in machines}
        )
    last_ingest_age = None if last_ingest is None else max(
        0, int((now - aggregate._parse(last_ingest)).total_seconds()))
    return {"generatedAt": aggregate._iso(now), "status": "live", "capacity": capacity,
            "activity": activity, "selectedMachine": machine,
            "lastIngestAt": last_ingest, "lastIngestAgeSeconds": last_ingest_age,
            "stale": last_ingest_age is not None and last_ingest_age > DATA_STALE_AFTER_SECONDS}



def local_usage_machine(settings, machine: str) -> str:
    """Machine id to use when a spoke has to answer usage from its own database.

    Rows collected here are unprefixed, so they belong to ``local``. The hub
    knows this computer by its paired id. Asking this database for that id
    would match nothing and the HUD would show an empty day.
    """
    if machine in {"", "all", "local"}:
        return machine or "all"
    try:
        from spend_app.setup_wizard import read_setup_state

        with connect(settings.database_path) as connection:
            state = read_setup_state(connection)
    except Exception:
        return machine
    if state.get("role") != "spoke":
        return machine
    own = str((state.get("account") or {}).get("machineId") or "")
    return "local" if own and machine == own else machine


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
    last_import_age = None if last_import is None else max(
        0, int((now - aggregate._parse(last_import)).total_seconds()))
    return {"generatedAt": aggregate._iso(now), "historyUpdatedAt": last_import,
            "lastIngestAt": last_import, "lastIngestAgeSeconds": last_import_age,
            "stale": last_import_age is not None and last_import_age > DATA_STALE_AFTER_SECONDS,
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
    base = next((key for key in ("claude-code", "opencode-go") if provider_key.startswith(key + "-")), provider_key)
    prefix = MODEL_PREFIXES.get(provider_key) or MODEL_PREFIXES.get(base)
    return name[len(prefix):] if prefix and name.startswith(prefix) and len(name) > len(prefix) else name


def window_burn(connection, capacity: list[dict], *, now: datetime, today_start: datetime,
                machine: str = "all") -> dict:
    """Measured tokens inside each open quota window, by plan and limit.

    Weekly lanes also carry today's tokens since local midnight by model.
    """
    lanes = []
    for card in capacity:
        provider_key = card.get("providerKey") or ""
        if _burn_tools(provider_key) is None:
            continue
        for row in card.get("rows") or []:
            limit_key = row.get("limitKey") or ""
            start = _window_start(limit_key, row.get("resetsAt"))
            if start is not None and start <= now < aggregate._parse(row["resetsAt"]):
                lanes.append((provider_key, limit_key, start))
    if not lanes:
        return {}
    since = min(min(start for *_, start in lanes), today_start)
    tools = tuple(sorted({tool for provider_key, *_ in lanes for tool in _burn_tools(provider_key)}))
    # Usage before the last full hour rarely changes: reuse it until its own
    # fingerprint moves (a late spoke sync, a backfill) and read only the
    # latest hour or two on each poll.
    cutoff = max(since, now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1))
    key = (aggregate._connection_identity(connection), machine, tools, since, cutoff,
           _usage_fingerprint(connection, since, cutoff))
    settled = aggregate._memo("hud_burn_rows", key, lambda: _burn_rows(connection, tools, since, cutoff, machine))
    recent = _burn_rows(connection, tools, cutoff, now, machine)
    return _burn_lanes(lanes, {tool: settled[tool] + recent[tool] for tool in tools}, today_start)


def _usage_fingerprint(connection, start: datetime, end: datetime) -> tuple:
    """Identity of the rows in ``[start, end)``.

    Token sums are part of the key. A revision of an existing row does not
    move the count, the max id, or the ingest stamp, and the settled burn
    would otherwise keep the old tokens.
    """
    bounds = (epoch_micros(start), epoch_micros(end))
    usage = connection.execute(
        "SELECT COUNT(*), MAX(id), MAX(ingested_at), SUM(input_tokens), "
        "SUM(cached_input_tokens), SUM(cache_write_tokens), SUM(output_tokens) "
        "FROM usage_events "
        "WHERE occurred_epoch_us IS NOT NULL AND occurred_epoch_us >= ? AND occurred_epoch_us < ?",
        bounds,
    ).fetchone()
    unpriced = connection.execute(
        "SELECT COUNT(*), MAX(id), MAX(ingested_at), SUM(input_tokens), "
        "SUM(cached_input_tokens), SUM(cache_write_tokens), SUM(output_tokens) "
        "FROM unpriced_usage_events "
        "WHERE occurred_epoch_us IS NOT NULL AND occurred_epoch_us >= ? AND occurred_epoch_us < ?",
        bounds,
    ).fetchone()
    return tuple(usage), tuple(unpriced)


def _burn_rows(connection, tools, start: datetime, end: datetime, machine: str) -> dict[str, list[tuple]]:
    """(epoch_us, model, cache reads, fresh input + cache writes, output) per tool."""
    rows: dict[str, list[tuple]] = {}
    for tool in tools:
        events = [*aggregate._load_events(connection, start, end, tool, machine),
                  *aggregate._load_unpriced_events(connection, start, end, tool, machine)]
        rows[tool] = [
            (int(event["occurred_epoch_us"]), str(event.get("model_key") or "unknown"),
             int(event.get("cached_input_tokens") or 0),
             aggregate._fresh(event) + int(event.get("cache_write_tokens") or 0),
             int(event.get("output_tokens") or 0))
            for event in events
        ]
    return rows


def _burn_lanes(lanes, rows: dict[str, list[tuple]], today_start: datetime) -> dict:
    today_us = epoch_micros(today_start)
    result: dict[str, dict] = {}
    for provider_key, limit_key, start in lanes:
        takes = _lane_takes(provider_key, limit_key)
        start_us = epoch_micros(start)
        cached = fresh = output = 0
        today_by_model: dict[str, int] = defaultdict(int)
        for tool in _burn_tools(provider_key):
            for when, model, cache_read, new_input, out in rows[tool]:
                if not takes(model):
                    continue
                if when >= start_us:
                    cached += cache_read
                    fresh += new_input
                    output += out
                if when >= today_us:
                    today_by_model[model] += cache_read + new_input + out
        lane = {"since": aggregate._iso(start), "tokens": cached + fresh + output,
                "input": fresh, "cached": cached, "output": output}
        if limit_key.rsplit("-", 1)[-1] == "weekly":
            named: dict[str, int] = defaultdict(int)
            for model, tokens in today_by_model.items():
                named[_model_name(provider_key, model)] += tokens
            models = sorted(((name, tokens) for name, tokens in named.items() if tokens > 0),
                            key=lambda item: (-item[1], item[0]))
            lane["today"] = {"tokens": sum(tokens for _, tokens in models),
                             "models": [{"name": name, "tokens": tokens} for name, tokens in models]}
        result.setdefault(provider_key, {})[limit_key] = lane
    return result
