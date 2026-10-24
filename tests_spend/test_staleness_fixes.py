"""Staleness regressions: collection must not depend on viewers, HUD payloads
must carry their data age, and the run-dry forecast must keep working from
measured usage when the quota poller cannot answer."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spend_app import aggregate, freshness
from spend_app.api import create_app
from spend_app.capacity_forecast import apply_forecasts, load_pace_samples, usage_forecast
from spend_app.config import Settings
from spend_app.db import UsageEvent, connect, initialize, upsert_quota, upsert_usage_event
from spend_app.hud import render_hud
from spend_app.hud_data import DATA_STALE_AFTER_SECONDS, lane_usage_pace, live_snapshot, recorded_usage
from spend_app.pricing import PricingEngine
from spend_app.scheduler import create_scheduler

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)
TZ = "UTC"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "spend.db",
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone=TZ,
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def isolated_sources(monkeypatch, tmp_path):
    """Replace source boundaries before constructing provider arguments."""
    from spend_app import providers

    home = tmp_path / "source-home"
    home.mkdir()
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "CODEX_HOME",
                "CLAUDE_CONFIG_DIR", "GROK_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(key, str(home))
    monkeypatch.setattr(providers, "_home", lambda: home)
    monkeypatch.setattr(providers, "grok_coverage_start", lambda *_args: None)

    def no_background_start(*_args, **_kwargs):
        raise AssertionError("scheduler fixtures must not start background workers")

    monkeypatch.setattr(
        "apscheduler.schedulers.background.BackgroundScheduler.start", no_background_start
    )


def _monitored_sources(monkeypatch):
    from spend_app.connections import empty_state
    from spend_app.providers import REGISTRY

    state = empty_state()
    state["legacy"] = [spec.key for spec in REGISTRY.local_ingest()]
    monkeypatch.setattr("spend_app.connections.Store.read", lambda self: state)
    monkeypatch.setattr(
        "spend_app.connections.execute",
        lambda settings, pricing, spec, ingest, window=None: (
            ingest(database_path=settings.database_path)
            if spec.key in state["legacy"]
            else {"status": "skipped"}
        ),
    )


LOCAL_INGEST_ALIASES = (
    "codex", "claude", "traycer", "opencode", "zcode", "grok", "antigravity", "muse",
)


def test_local_ingest_collects_with_no_viewers(tmp_path: Path, monkeypatch) -> None:
    """A dead or absent viewer must never stop usage collection."""
    _monitored_sources(monkeypatch)
    calls: list[str] = []

    def recorder(name):
        def run(**_kwargs):
            calls.append(name)
            return {"status": "success"}

        return run

    for name in LOCAL_INGEST_ALIASES:
        monkeypatch.setattr(f"spend_app.scheduler.ingest_{name}_local", recorder(name))
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    freshness.reset_state()
    scheduler = create_scheduler(
        settings, PricingEngine.load(ROOT / "pricing"), history_active=lambda: False
    )
    jobs = {job.id: job for job in scheduler.get_jobs()}
    jobs["local-ingest"].func()
    assert calls, "collection must run even when no viewer has asked for data"
    assert freshness.heartbeat()["passes"] == 1


def test_stalled_collection_is_an_alarm_even_with_no_viewers(tmp_path: Path) -> None:
    """The heartbeat is never masked: a stalled loop stays an alarm."""
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    freshness.reset_state()
    freshness.pass_finished({}, [], 1.0, NOW - timedelta(minutes=30))
    report = freshness.report(settings, NOW, history_active=False)
    ingest = [item for item in report["issues"] if item["kind"] == "ingest"]
    assert ingest and ingest[0]["state"] == "stalled" and ingest[0]["severity"] == "alarm"
    assert report["ingest"]["stalled"] is True
    assert "deferred" not in report["ingest"]


def _record_pass(connection, finished: datetime, status: str = "success") -> None:
    connection.execute(
        "INSERT INTO ingest_runs(source, started_at, finished_at, status, events_written) "
        "VALUES('claude_local', ?, ?, ?, 1)",
        (_iso(finished - timedelta(seconds=2)), _iso(finished), status),
    )


def test_hud_payloads_report_their_data_age(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        _record_pass(connection, NOW - timedelta(seconds=DATA_STALE_AFTER_SECONDS + 100))
    live = live_snapshot(settings, now=NOW)
    assert live["lastIngestAt"] == _iso(NOW - timedelta(seconds=DATA_STALE_AFTER_SECONDS + 100))
    assert live["lastIngestAgeSeconds"] >= DATA_STALE_AFTER_SECONDS
    assert live["stale"] is True
    pricing = PricingEngine.load(ROOT / "pricing")
    usage = recorded_usage(settings, pricing, now=NOW)
    assert usage["stale"] is True and usage["lastIngestAgeSeconds"] >= DATA_STALE_AFTER_SECONDS

    with connect(settings.database_path) as connection:
        _record_pass(connection, NOW - timedelta(seconds=5))
    assert live_snapshot(settings, now=NOW)["stale"] is False
    assert recorded_usage(settings, pricing, now=NOW)["stale"] is False


def test_hud_api_surfaces_data_age(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings, enable_scheduler=False, now=NOW))
    with connect(settings.database_path) as connection:
        _record_pass(connection, NOW - timedelta(seconds=30))
    live = client.get("/api/spend/hud").json()
    assert {"lastIngestAt", "lastIngestAgeSeconds", "stale"} <= set(live)
    assert live["stale"] is False
    usage = client.get("/api/spend/hud/usage").json()
    assert {"lastIngestAt", "lastIngestAgeSeconds", "stale"} <= set(usage)


def test_hud_polls_are_bounded_and_their_age_is_visible() -> None:
    html = render_hud()
    assert "AbortController" in html and "FETCH_TIMEOUT_MS" in html
    assert "paintUpdated" in html and "DATA STALE" in html
    assert 'id="hud-updated-foot"' in html
    # The indicator is driven by the server-reported data age, and a dead
    # usage poll alarms even while the live-meter poll still answers.
    assert "noteDataAge" in html and "lastIngestAgeSeconds" in html
    assert "USAGE_LATE_MS" in html and "DATA_STALE_SECONDS" in html


def test_old_hub_payloads_get_a_truthful_age_stamp() -> None:
    """A b17 Hub predates the data-age fields; the spoke must not hide it."""
    from spend_app.api import _stamp_hub_age

    old = _iso(NOW - timedelta(minutes=12))
    live = _stamp_hub_age(
        {"generatedAt": _iso(NOW), "capacity": [{"rows": [{"polledAt": old}]}]},
        now=NOW,
    )
    assert live["lastIngestAt"] == old
    assert live["lastIngestAgeSeconds"] == 720
    assert live["stale"] is True and live["dataSource"] == "hub"

    usage = _stamp_hub_age(
        {"generatedAt": _iso(NOW), "historyUpdatedAt": _iso(NOW - timedelta(seconds=5))},
        now=NOW,
        stamp=_iso(NOW - timedelta(seconds=5)),
    )
    assert usage["lastIngestAgeSeconds"] == 5 and usage["stale"] is False

    already = {"lastIngestAgeSeconds": 1, "stale": False}
    assert _stamp_hub_age(already, now=NOW) is already


def test_hud_indicator_alarms_on_stale_data_not_only_on_a_live_poll() -> None:
    html = render_hud()
    block = (
        "        const FEED_LATE_MS"
        + html.split("        const FEED_LATE_MS", 1)[1].split("        // Stat tips", 1)[0]
    )
    harness = f"""
const nodes = {{
  "hud-updated-foot": {{textContent: "", classList: {{toggle(name, on) {{ this.stale = on; }}}}, stale: false}},
  "hud-status-pill": {{dataset: {{}}}},
  "hud-status-text": {{textContent: ""}},
  "hud-mini-alert": {{hidden: true, dataset: {{}}, textContent: ""}},
}};
const $ = id => nodes[id];
const ago = s => s < 60 ? Math.round(s) + "s" : Math.round(s / 60) + "m";
{block}
feedHealth.live = Date.now();
feedHealth.usage = Date.now();
noteDataAge({{lastIngestAgeSeconds: 5, generatedAt: new Date().toISOString()}});
paintUpdated();
const fresh = {{
  text: nodes["hud-updated-foot"].textContent,
  stale: nodes["hud-updated-foot"].classList.stale,
  pill: nodes["hud-status-pill"].dataset.state || "",
}};
feedHealth.dataAt = Date.now() - 11 * 60 * 1000;
paintUpdated();
const staleData = {{
  text: nodes["hud-updated-foot"].textContent,
  stale: nodes["hud-updated-foot"].classList.stale,
  pill: nodes["hud-status-pill"].dataset.state,
  mini: nodes["hud-mini-alert"].textContent,
}};
feedHealth.dataAt = Date.now() - 2000;
feedHealth.usage = Date.now() - 60000;
paintUpdated();
const deadUsage = {{stale: nodes["hud-updated-foot"].classList.stale, pill: nodes["hud-status-pill"].dataset.state}};
console.log(JSON.stringify({{fresh, staleData, deadUsage}}));
"""
    result = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True,
        encoding="utf-8", check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip())
    assert report["fresh"] == {"text": "Updated 5s ago", "stale": False, "pill": ""}
    assert report["staleData"]["stale"] is True
    assert report["staleData"]["pill"] == "stale"
    assert report["staleData"]["mini"] == "! DATA STALE"
    assert "11m" in report["staleData"]["text"]
    assert report["deadUsage"]["stale"] is True
    assert report["deadUsage"]["pill"] == "stale"


def test_usage_forecast_projects_from_measured_tokens() -> None:
    reset = NOW + timedelta(hours=4)
    # 25% of the window is gone and 100k tokens are measured: the implied
    # limit is 400k, so 10k/h burns the remaining 300k in 30 hours — well
    # past the reset, i.e. comfortable.
    safe = usage_forecast(
        tokens=100_000, tokens_per_hour=10_000, pct=25.0, allowance=None,
        unit="pct", resets_at=_iso(reset), now=NOW,
    )
    assert safe["basis"] == "usage" and safe["tokensPerHour"] == 10_000
    assert safe["hoursRemaining"] == 30.0
    assert safe["burnPctPerHour"] == 2.5
    assert safe["state"] == "safe"

    tight = usage_forecast(
        tokens=100_000, tokens_per_hour=350_000, pct=25.0, allowance=None,
        unit="pct", resets_at=_iso(reset), now=NOW,
    )
    assert tight["state"] == "tight" and tight["hoursRemaining"] < 1.0

    # A provider-reported token allowance is used as-is.
    by_allowance = usage_forecast(
        tokens=100_000, tokens_per_hour=50_000, pct=25.0, allowance=500_000,
        unit="tokens", resets_at=_iso(NOW + timedelta(hours=20)), now=NOW,
    )
    assert by_allowance["hoursRemaining"] == 8.0

    assert usage_forecast(tokens=100, tokens_per_hour=0, pct=50, allowance=None,
                          unit="pct", resets_at=_iso(reset), now=NOW) is None
    assert usage_forecast(tokens=100, tokens_per_hour=10, pct=50, allowance=None,
                          unit="pct", resets_at=_iso(NOW - timedelta(hours=1)), now=NOW) is None


def _seed_event(connection, *, when: datetime, tokens: int, raw_id: str) -> None:
    upsert_usage_event(
        connection,
        UsageEvent(
            source="claude_local",
            tool_key="claude-code",
            model_key="claude-opus-5",
            occurred_at=_iso(when),
            session_id="sess",
            project="Proj",
            input_tokens=tokens,
            cached_input_tokens=0,
            cache_write_tokens=0,
            cache_write_1h_tokens=0,
            output_tokens=0,
            reasoning_tokens=None,
            cost_usd=None,
            computed_cost_usd=0.0,
            raw_id=raw_id,
            ingested_at=_iso(when + timedelta(seconds=30)),
            is_exact=True,
        ),
    )


def _claude_lane(connection, *, pct: float = 25.0, polled: datetime | None = None,
                 resets_in_hours: float = 4) -> None:
    upsert_quota(
        connection,
        provider_key="claude-code",
        limit_key="5h",
        label="Claude Code 5-hour window",
        used=None,
        allowance=None,
        unit="pct",
        pct=pct,
        resets_at=_iso(NOW + timedelta(hours=resets_in_hours)),
        source="claude_statusline",
        polled_at=_iso(polled or NOW - timedelta(minutes=5)),
        is_payg=False,
    )


def _capacity(connection):
    return aggregate._capacity_from_rows(
        aggregate._load_quota_rows(connection, now=NOW), now=NOW, month_to_date={}
    )


def test_idle_last_hour_does_not_forecast_a_run_dry(tmp_path: Path) -> None:
    """A quiet hour is zero burn; the window average must not keep alarming."""
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        # The window opened two hours ago, so its only usage is idle-time old.
        _claude_lane(connection, resets_in_hours=3)
        _seed_event(connection, when=NOW - timedelta(minutes=90), tokens=5000, raw_id="old")
        pace = lane_usage_pace(connection, _capacity(connection), now=NOW)
    assert pace[("claude-code", "5h")]["tokens"] == 5000
    assert pace[("claude-code", "5h")]["tokensPerHour"] == 0
    capacity = _capacity_and_apply(settings)
    assert _claude_row(capacity)["forecast"] is None


def test_forecast_falls_back_to_measured_burn_when_quota_pace_is_dead(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    # Window [NOW-1h, NOW+4h): two events before the hour mark are inside,
    # one before the window started must not count.
    with connect(settings.database_path) as connection:
        _claude_lane(connection)
        _seed_event(connection, when=NOW - timedelta(minutes=50), tokens=1000, raw_id="e1")
        _seed_event(connection, when=NOW - timedelta(minutes=50), tokens=1000, raw_id="e2")
        _seed_event(connection, when=NOW - timedelta(minutes=10), tokens=1000, raw_id="e3")
        _seed_event(connection, when=NOW - timedelta(minutes=10), tokens=1000, raw_id="e4")
        _seed_event(connection, when=NOW - timedelta(minutes=90), tokens=9999, raw_id="outside")
        pace = lane_usage_pace(connection, _capacity(connection), now=NOW)
    assert pace[("claude-code", "5h")]["tokens"] == 4000
    assert pace[("claude-code", "5h")]["tokensPerHour"] == 4000

    capacity = _capacity_and_apply(settings)
    row = _claude_row(capacity)
    assert row["forecast"] is not None
    assert row["forecast"]["basis"] == "usage"
    # Implied limit 4000/0.25 = 16k tokens; 12k left at 4k/h = 3 hours,
    # one hour before the 4-hour reset: tight.
    assert row["forecast"]["hoursRemaining"] == 3.0
    assert row["forecast"]["state"] == "tight"


def _capacity_and_apply(settings: Settings) -> list[dict]:
    with connect(settings.database_path) as connection:
        capacity = _capacity(connection)
        apply_forecasts(
            capacity,
            load_pace_samples(connection, NOW),
            now=NOW,
            active=set(),
            usage_pace=lane_usage_pace(connection, capacity, now=NOW),
        )
    return capacity


def _claude_row(capacity: list[dict]) -> dict:
    card = next(item for item in capacity if item.get("providerKey") == "claude-code")
    return next(row for row in card["rows"] if row.get("limitKey") == "5h")


def test_fresh_quota_pace_still_wins_over_measured_fallback(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        _claude_lane(connection)
        _seed_event(connection, when=NOW - timedelta(minutes=10), tokens=1000, raw_id="e1")
        reset = _iso(NOW + timedelta(hours=4))
        connection.executemany(
            "INSERT INTO quota_pace_samples(provider_key, limit_key, pct, resets_at, sampled_at) "
            "VALUES('claude-code','5h',?,?,?)",
            [
                (25.0, reset, _iso(NOW - timedelta(minutes=3))),
                (26.0, reset, _iso(NOW - timedelta(minutes=1))),
            ],
        )
    capacity = _capacity_and_apply(settings)
    forecast = _claude_row(capacity)["forecast"]
    assert forecast is not None and "basis" not in forecast
    assert forecast["burnPctPerHour"] == 30.0


def test_usage_pace_crosses_window_boundaries_correctly(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        # A window that reset a minute ago: nothing is open, no pace.
        upsert_quota(
            connection,
            provider_key="claude-code",
            limit_key="5h",
            label="Claude Code 5-hour window",
            used=None, allowance=None, unit="pct", pct=80.0,
            resets_at=_iso(NOW - timedelta(minutes=1)),
            source="claude_statusline",
            polled_at=_iso(NOW - timedelta(minutes=5)),
            is_payg=False,
        )
        _seed_event(connection, when=NOW - timedelta(minutes=30), tokens=1000, raw_id="e1")
        pace = lane_usage_pace(connection, _capacity(connection), now=NOW)
    assert pace == {}
