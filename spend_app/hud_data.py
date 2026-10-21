"""HUD reads: live meters first, bounded recorded-usage statistics separately."""

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
    return {"generatedAt": aggregate._iso(now), "historyUpdatedAt": last_import,
            "navigation": {"burnRatePerDay": float(burn) if burn is not None else None,
                           "todayUsd": float(today.usage_value) if today.records and today.usage_value is not None else None,
                           "todayTokens": int(today.tokens) if today.records else 0,
                           "tokensAvgPerDay": average, "todayComplete": today.complete if today.records else False},
            "totals": {"cacheReusePct": aggregate._cache_reuse(mix)}}
