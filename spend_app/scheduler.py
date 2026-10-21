from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from spend_app.aggregate import aggregate_summary_cached, data_clock, recently_requested_summaries
from spend_app.config import Settings
from spend_app.db import connect, initialize, prune_ingest_runs
from spend_app.freshness import pass_finished, pass_started
from spend_app.pricing import PricingEngine
from spend_app.providers import (
    ProviderSpec,
    REGISTRY,
    ingest_muse_local,
    ingest_anthropic_admin,
    ingest_antigravity_local,
    ingest_claude_local,
    ingest_codex_local,
    ingest_cursor_admin,
    ingest_cursor_csv,
    ingest_cursor_local,
    ingest_cursor_usage,
    ingest_grok_local,
    ingest_opencode_local,
    ingest_openai_admin,
    ingest_traycer_local,
    ingest_zcode_local,
    iter_admin_ingest,
    iter_local_ingest,
)
from spend_app.quotas import nudge_quota_lanes, poll_activity, poll_quotas
from spend_app.subscriptions import materialize_subscription_days

# Re-exported so existing tests can monkeypatch scheduler.ingest_* aliases.
# The local/admin loops resolve these at call time via scheduler_alias.
_ = (
    ingest_anthropic_admin,
    ingest_antigravity_local,
    ingest_claude_local,
    ingest_codex_local,
    ingest_cursor_admin,
    ingest_cursor_csv,
    ingest_cursor_local,
    ingest_cursor_usage,
    ingest_grok_local,
    ingest_opencode_local,
    ingest_openai_admin,
    ingest_traycer_local,
    ingest_zcode_local,
)


_ = (ingest_muse_local,)

# Local sources that read a remote service rather than files on this computer.
# They run on their own job, so a slow or unreachable provider never holds the
# local transcript sources (and every figure derived from them) behind it.
NETWORK_LOCAL_SOURCES = frozenset({"cursor_usage_service"})

_SOURCE_TO_QUOTA_PROVIDER: dict[str, str] = {
    "codex_local": "codex",
    "claude_local": "claude-code",
    "antigravity_local": "antigravity",
    "grok_local": "grok",
    "opencode_local": "opencode",
    "cursor_local": "grok",
    "muse_local": "muse",
}


def _failed_sources(database_path, keys) -> list[str]:
    """Sources in this pass whose latest recorded run failed."""
    if not keys:
        return []
    try:
        with connect(database_path) as connection:
            return [
                key for key in keys
                if (connection.execute(
                    "SELECT status FROM ingest_runs WHERE source=? ORDER BY id DESC LIMIT 1", (key,)
                ).fetchone() or ["skipped"])[0] == "failed"
            ]
    except Exception:
        return []


def _ingest_callable(spec: ProviderSpec):
    alias = spec.scheduler_alias
    if alias:
        fn = globals().get(alias)
        if fn is not None:
            return fn
    return spec.ingest


def _run_ingest_specs(jobs: list[tuple[ProviderSpec, dict]], settings=None, pricing=None, window=None, timings=None, should_continue=None) -> None:
    errors: list[BaseException] = []
    for spec, kwargs in jobs:
        if should_continue is not None and not should_continue():
            break
        ingest = _ingest_callable(spec)
        if ingest is None:
            continue
        began = time.perf_counter()
        try:
            if settings is None:
                result = ingest(**kwargs)
            else:
                from spend_app.connections import execute
                result = execute(settings, pricing, spec, ingest, window)
                if isinstance(result, dict) and ((result.get("eventsWritten") or 0) + (result.get("unpricedEventsWritten") or 0)) > 0:
                    try:
                        quota_provider = _SOURCE_TO_QUOTA_PROVIDER.get(spec.key)
                        if quota_provider:
                            nudge_quota_lanes(settings.database_path, quota_provider)
                        from spend_app.setup_wizard import maybe_run_configured_spoke_sync
                        from spend_app.sync import STREAM_BROKER, clear_hub_proxy_cache
                        maybe_run_configured_spoke_sync(settings, pricing)
                        clear_hub_proxy_cache()
                        STREAM_BROKER.publish({"type": "ingest", "source": spec.key})
                    except Exception:
                        pass
            if isinstance(result, dict) and result.get("status") == "failed" and spec.stability != "experimental":
                errors.append(RuntimeError(f"{spec.key} reported failed collection"))
        except Exception as exc:
            from spend_app.adapters.common import failed_result, public_error
            failed_result(database_path=settings.database_path if settings else kwargs["database_path"], source=spec.key, reason=public_error(exc))
            if spec.stability == "experimental":
                continue
            errors.append(exc)
        finally:
            if timings is not None:
                timings[spec.key] = time.perf_counter() - began
    if errors:
        raise RuntimeError(f"{len(errors)} ingest job(s) failed") from errors[0]


class HistoryDemand:
    """Keep historical collection active while a dashboard is being used."""

    def __init__(self, *, clock=time.monotonic, ttl_seconds=120):
        self._clock = clock
        self._ttl = ttl_seconds
        self._until = float("-inf")
        self._lock = threading.Lock()

    def active(self) -> bool:
        with self._lock:
            return self._clock() < self._until

    def touch(self) -> bool:
        with self._lock:
            now = self._clock()
            activated = now >= self._until
            self._until = now + self._ttl
            return activated


def create_scheduler(settings: Settings, pricing: PricingEngine, *, history_active=None) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    collect_history = history_active or (lambda: True)

    local_ingest_interval = {
        "seconds": settings.local_ingest_interval_seconds,
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": max(5, settings.local_ingest_interval_seconds * 2),
    }

    def local_ingest_jobs() -> None:
        is_central = os.getenv("BURNRATE_CENTRAL_SERVER", "").strip().lower() in {"1", "true", "yes"}
        from spend_app.sync import STREAM_BROKER
        from spend_app.setup_wizard import maybe_run_configured_spoke_sync

        if not collect_history():
            # The HUD still syncs quota/activity snapshots, without opening
            # transcript stores or rebuilding historical summaries.
            if not is_central:
                maybe_run_configured_spoke_sync(settings, pricing)
            return
        errors: list[BaseException] = []
        timings: dict[str, float] = {}
        began = time.perf_counter()
        pass_started()
        if not is_central:
            try:
                _run_ingest_specs(
                    [
                        (spec, {})
                        for spec in REGISTRY
                        if spec.kind in {"local", "manual"} and spec.key not in NETWORK_LOCAL_SOURCES
                    ],
                    settings,
                    pricing,
                    timings=timings,
                    should_continue=collect_history,
                )
            except Exception as exc:
                errors.append(exc)
        pass_finished(timings, _failed_sources(settings.database_path, timings), time.perf_counter() - began)
        try:
            with connect(settings.database_path) as connection:
                prune_ingest_runs(connection)
        except Exception as exc:
            errors.append(exc)
        warm_summaries()
        if not is_central:
            maybe_run_configured_spoke_sync(settings, pricing)
        STREAM_BROKER.publish({"type": "ingest"})
        if errors:
            raise RuntimeError(f"{len(errors)} local ingest job(s) failed") from errors[0]

    def warm_summaries() -> None:
        # Compute the summaries viewers are currently watching right after the
        # cycle that could have changed them, so their next poll is a memo hit.
        is_central = os.getenv("BURNRATE_CENTRAL_SERVER", "").strip().lower() in {"1", "true", "yes"}
        pending = recently_requested_summaries()
        if is_central and ("1d", "all") not in pending:
            pending = [("1d", "all"), *pending]
        if not pending:
            return
        clock = data_clock(settings.database_path, settings.local_ingest_interval_seconds)
        for window_key, tool in pending:
            try:
                aggregate_summary_cached(
                    database_path=settings.database_path,
                    pricing=pricing,
                    window_key=window_key,
                    tool=tool,
                    timezone=settings.timezone,
                    cache_threshold=settings.cache_hit_threshold,
                    cadence_seconds=settings.local_ingest_interval_seconds,
                    now=clock,
                )
            except Exception:
                continue

    scheduler.add_job(
        local_ingest_jobs,
        "interval",
        **local_ingest_interval,
        id="local-ingest",
        next_run_time=datetime.now(UTC) + timedelta(seconds=1),
    )

    def network_ingest_jobs() -> None:
        if not collect_history():
            return
        if os.getenv("BURNRATE_CENTRAL_SERVER", "").strip().lower() in {"1", "true", "yes"}:
            return
        # Each adapter keeps its own adaptive cadence; this tick only offers it a turn.
        _run_ingest_specs(
            [(spec, {}) for spec in REGISTRY if spec.kind == "local" and spec.key in NETWORK_LOCAL_SOURCES],
            settings,
            pricing,
        )

    scheduler.add_job(
        network_ingest_jobs,
        "interval",
        **local_ingest_interval,
        id="network-ingest",
        next_run_time=datetime.now(UTC) + timedelta(seconds=2),
    )

    def admin_jobs() -> None:
        if not collect_history():
            return
        if os.getenv("BURNRATE_CENTRAL_SERVER", "").strip().lower() in {"1", "true", "yes"}:
            return
        from spend_app.adapters.report_pages import reporting_window
        start, end = reporting_window(datetime.now(UTC))
        _run_ingest_specs([(spec, {}) for spec in REGISTRY.admin_ingest()], settings, pricing, {"start": start, "end": end})

    scheduler.add_job(
        admin_jobs,
        "interval",
        minutes=settings.admin_ingest_interval_minutes,
        id="provider-admin",
        coalesce=True,
        max_instances=1,
    )

    def subscription_job() -> None:
        initialize(settings.database_path)
        # Calendar decisions use the CONFIGURED timezone, never the system
        # date (Release A C15/A02).
        from zoneinfo import ZoneInfo

        from spend_app.timeutil import month_start as _month_start

        now_utc = datetime.now(UTC)
        today = now_utc.astimezone(ZoneInfo(settings.timezone)).date()
        month_begin = _month_start(now_utc, ZoneInfo(settings.timezone)).astimezone(ZoneInfo(settings.timezone)).date()
        with connect(settings.database_path) as connection:
            materialize_subscription_days(
                connection,
                start=month_begin,
                end=today + timedelta(days=40),
            )

    scheduler.add_job(
        subscription_job,
        "interval",
        minutes=settings.admin_ingest_interval_minutes,
        id="subscriptions",
        coalesce=True,
        max_instances=1,
    )

    poller_opts = {"coalesce": True, "max_instances": 1}
    scheduler.add_job(
        poll_quotas,
        "interval",
        seconds=settings.quota_poll_seconds,
        kwargs={"database_path": settings.database_path},
        id="quota-poll",
        next_run_time=datetime.now(UTC) + timedelta(seconds=3),
        **poller_opts,
    )
    scheduler.add_job(
        poll_activity,
        "interval",
        seconds=settings.activity_poll_seconds,
        kwargs={"database_path": settings.database_path},
        id="activity",
        next_run_time=datetime.now(UTC) + timedelta(seconds=1),
        **poller_opts,
    )
    from spend_app.attention_store import register
    register(scheduler, settings, pricing)
    from spend_app.capacity_forecast import register_forecast
    register_forecast(scheduler, settings)
    return scheduler
