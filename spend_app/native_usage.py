"""Compact usage projection for the native desktop client.

Reference value always comes from measured token classes and the effective
rate card. Reported charges, plans, balances and quotas are not added to it.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from spend_app import aggregate
from spend_app.db import connect
from spend_app.pricing import PricingEngine, UnpricedModelError
from spend_app.source_reconcile import reconcile_events
from spend_app.sync import list_machines
from spend_app.timeutil import EPOCH, iso_utc, parse_utc

PERIODS = {
    "today": "Today",
    "week": "Last 7 days",
    "month": "This month",
    "all": "All time",
}
# Organization feeds can overlap local sessions. Keep them visible, separate
# from the deduplicated tool total, until a shared event identity proves scope.
ORGANIZATION_SOURCES = frozenset({"openai_admin", "anthropic_admin"})


def period_start(period: str, now: datetime, timezone: str) -> datetime:
    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    if period == "today":
        return datetime.combine(local.date(), time.min, zone).astimezone(UTC)
    if period == "week":
        return now - timedelta(days=7)
    if period == "month":
        return datetime.combine(local.date().replace(day=1), time.min, zone).astimezone(
            UTC
        )
    if period == "all":
        return EPOCH
    raise ValueError("Choose today, week, month, or all")


def _empty() -> dict:
    return {
        "tokens": 0,
        "input": 0,
        "cached": 0,
        "writes": 0,
        "output": 0,
        "unclassified": 0,
        "events": 0,
        "pricedEvents": 0,
        "unpricedEvents": 0,
        "reasoning": 0,
        "reasoningEvents": 0,
        "knownValue": Decimal(0),
    }


def _add(target: dict, parts: dict, value: Decimal | None) -> None:
    for key in ("tokens", "input", "cached", "writes", "output", "unclassified"):
        target[key] += parts[key]
    target["events"] += 1
    if parts.get("reasoning") is not None:
        target["reasoning"] += parts["reasoning"]
        target["reasoningEvents"] += 1
    if value is None:
        target["unpricedEvents"] += 1
    else:
        target["pricedEvents"] += 1
        target["knownValue"] += value


def _public(parts: dict) -> dict:
    value = parts["knownValue"]
    return {key: val for key, val in parts.items() if key != "knownValue"} | {
        "valueUsd": float(value) if parts["pricedEvents"] else None,
        "complete": parts["unpricedEvents"] == 0,
        "reasoning": parts["reasoning"] if parts["reasoningEvents"] else None,
        "reasoningComplete": parts["reasoningEvents"] == parts["events"],
    }


def _build_series(
    samples: list[tuple[datetime, Decimal, int]],
    *,
    period: str,
    start: datetime,
    now: datetime,
    timezone: str,
) -> list[dict]:
    zone = ZoneInfo(timezone)
    if period == "all":
        earliest = min(
            (when for when, _, _ in samples), default=now - timedelta(days=14)
        )
        span_start = min(earliest, now - timedelta(days=1))
    else:
        span_start = start
    total_seconds = max(3600.0, (now - span_start).total_seconds())
    if period == "today":
        hours = max(4, int(total_seconds // 3600) + 1)
        bucket_count = min(24, hours)
    elif period == "week":
        bucket_count = 14
    elif period == "month":
        days = max(
            2,
            (now.astimezone(zone).date() - span_start.astimezone(zone).date()).days + 1,
        )
        bucket_count = min(31, max(7, days))
    else:
        bucket_count = 20

    step = total_seconds / bucket_count
    bucket_usd = [Decimal(0) for _ in range(bucket_count)]
    bucket_tok = [0 for _ in range(bucket_count)]
    for when, usd, tok in samples:
        offset = (when - span_start).total_seconds()
        idx = min(bucket_count - 1, max(0, int(offset / step)))
        bucket_usd[idx] += usd
        bucket_tok[idx] += tok

    points: list[dict] = []
    running_usd = Decimal(0)
    running_tok = 0
    for idx in range(bucket_count):
        point_end = span_start + timedelta(seconds=step * (idx + 1))
        local_end = point_end.astimezone(zone)
        if period == "today":
            label = local_end.strftime("%I:%M %p").lstrip("0")
        elif period in {"week", "month"}:
            label = local_end.strftime("%b %d").replace(" 0", " ")
        else:
            label = local_end.strftime("%b %d, %Y").replace(" 0", " ")
        running_usd += bucket_usd[idx]
        running_tok += bucket_tok[idx]
        points.append(
            {
                "label": label,
                "bucketUsd": float(bucket_usd[idx]),
                "cumulativeUsd": float(running_usd),
                "tokens": bucket_tok[idx],
                "cumulativeTokens": running_tok,
            }
        )
    return points


def _rows(connection, start: datetime, end: datetime, machine: str) -> list[dict]:
    # Existing loaders already preserve identities and half-open UTC ranges.
    priced = aggregate._load_events(connection, start, end, "all", machine)
    staged = aggregate._load_unpriced_events(connection, start, end, "all", machine)
    rows, _ = reconcile_events([*priced, *staged])
    return [item.row for item in rows]


def usage_report(
    settings,
    pricing: PricingEngine,
    *,
    now: datetime,
    period: str = "today",
    machine: str = "all",
) -> dict:
    now = now.astimezone(UTC)
    start = period_start(period, now, settings.timezone)
    with connect(settings.database_path) as connection:
        fingerprint = aggregate._range_fingerprint(connection, start, now, "all")
        machines = list_machines(connection, now=now)
        key = (
            str(settings.database_path.resolve()),
            id(pricing),
            period,
            machine,
            settings.timezone,
            iso_utc(start),
            iso_utc(now),
            fingerprint,
        )

    def compute():
        with connect(settings.database_path) as connection:
            rows = _rows(connection, start, now, machine)
        total, organizations = _empty(), _empty()
        tools: dict[str, dict] = {}
        models: dict[str, dict[str, dict]] = defaultdict(dict)
        timeline_samples: list[tuple[datetime, Decimal, int]] = []
        for row in rows:
            cached = max(0, int(row.get("cached_input_tokens") or 0))
            inp = max(0, int(row.get("input_tokens") or 0))
            writes = max(0, int(row.get("cache_write_tokens") or 0))
            output = max(0, int(row.get("output_tokens") or 0))
            unknown = max(0, int(row.get("unclassified_tokens") or 0))
            parts = {
                "input": max(0, inp - cached),
                "cached": cached,
                "writes": writes,
                "output": output,
                "unclassified": unknown,
                "tokens": inp + writes + output + unknown,
            }
            parts["reasoning"] = row.get("reasoning_tokens")
            when = parse_utc(row.get("occurred_at"))
            value = None
            if row.get("telemetry_complete", True) and when is not None:
                try:
                    value = pricing.compute(
                        model_key=row["model_key"],
                        occurred_at=when,
                        input_tokens=inp,
                        cached_input_tokens=cached,
                        cache_write_tokens=writes,
                        cache_write_1h_tokens=int(
                            row.get("cache_write_1h_tokens") or 0
                        ),
                        output_tokens=output,
                    )
                except (UnpricedModelError, ValueError):
                    pass
            org = row.get("source") in ORGANIZATION_SOURCES
            tool = ("api:" + row["source"]) if org else row["tool_key"]
            model = row["model_key"]
            group = tools.setdefault(tool, _empty())
            detail = models[tool].setdefault(model, _empty())
            _add(group, parts, value)
            _add(detail, parts, value)
            _add(organizations if org else total, parts, value)
            if not org and when is not None:
                timeline_samples.append((when, value or Decimal(0), parts["tokens"]))
        result = []
        for tool, parts in tools.items():
            org = tool.startswith("api:")
            name = {
                "api:openai_admin": "OpenAI organization API",
                "api:anthropic_admin": "Anthropic organization API",
            }.get(tool) or aggregate.tool_name(tool)
            result.append(
                {
                    "key": tool,
                    "name": name,
                    "organization": org,
                    **_public(parts),
                    "models": sorted(
                        [
                            {
                                "key": model,
                                "name": aggregate.display_model(model),
                                **_public(p),
                            }
                            for model, p in models[tool].items()
                        ],
                        key=lambda item: (-(item["valueUsd"] or 0), item["name"]),
                    ),
                }
            )
        result.sort(
            key=lambda item: (
                item["organization"],
                -(item["valueUsd"] or 0),
                item["name"],
            )
        )
        series = _build_series(
            timeline_samples,
            period=period,
            start=start,
            now=now,
            timezone=settings.timezone,
        )
        earliest = min((when for when, _, _ in timeline_samples), default=None)
        active_from = earliest if period == "all" else start
        return {
            "period": period,
            "periodLabel": PERIODS[period],
            "timezone": settings.timezone,
            "from": iso_utc(start),
            "to": iso_utc(now),
            "generatedAt": iso_utc(now),
            "machine": machine,
            "scope": "all-computers" if machine == "all" else "computer",
            "total": _public(total),
            "organizationTotal": _public(organizations),
            "tools": result,
            "series": series,
            "activeFrom": iso_utc(active_from) if active_from is not None else None,
            "rate": _burn_rate(total, active_from, now),
        }

    report = aggregate._memo("native_usage", key, compute)
    plans = _plan_comparison(settings, report, now=now, machine=machine)
    return {**report, "machines": machines, "plans": plans}


def _burn_rate(total: dict, active_from: datetime | None, now: datetime) -> dict:
    """Average reference value per hour and per day across the shown span.

    The span is the period itself (or first recorded use for All time), with a
    one-hour floor so an early-morning Today does not explode the rate. No
    priced usage stays unknown rather than becoming a measured zero rate.
    """
    complete = total["unpricedEvents"] == 0
    if active_from is None or not total["pricedEvents"]:
        rate_known = complete and total["events"] == 0 and active_from is not None
        zero = 0.0 if rate_known else None
        return {"perHourUsd": zero, "perDayUsd": zero, "spanHours": None, "complete": complete}
    seconds = max(3600.0, (now - active_from).total_seconds())
    value = total["knownValue"]
    hours = Decimal(str(seconds)) / Decimal(3600)
    return {
        "perHourUsd": float(value / hours),
        "perDayUsd": float(value / hours * 24),
        "spanHours": float(hours),
        "complete": complete,
    }


def _plan_comparison(settings, report: dict, *, now: datetime, machine: str) -> dict | None:
    """Reference value versus configured plan accrual over the SAME span.

    Plans are account-wide, so a single-computer view has no fair denominator
    and returns None. The ratio reuses the month-to-date rules: complete usage
    is a ratio, partly priced usage is a lower bound, otherwise unavailable.
    """
    if machine != "all" or not report.get("activeFrom"):
        return None
    active_from = parse_utc(report["activeFrom"])
    if active_from is None:
        return None
    local_today = now.astimezone(ZoneInfo(settings.timezone)).date()
    with connect(settings.database_path) as connection:
        accrual, _ = aggregate._subscription_cost(
            connection, active_from, now, settings.timezone, cap_date=local_today
        )
    total = report.get("total") or {}
    usage = Decimal(str(total.get("valueUsd") or 0))
    _, basis, multiple = aggregate.plan_value_comparison(
        usage, complete=bool(total.get("complete", True)), accrual_to_date=accrual
    )
    return {
        "accruedUsd": float(accrual),
        "multiple": multiple,
        "basis": basis,
        "method": "Reference value versus configured plan cost accrued over the same span.",
    }
