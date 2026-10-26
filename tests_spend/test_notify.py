"""Desktop notifications: once per episode, quiet hours, switches, and a toast that carries no script input."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from spend_app import notify
from spend_app.config import Settings
from spend_app.db import connect, initialize

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
PREFS = {"threshold": 90, "overrides": {}, "quietStart": "22:00", "quietEnd": "07:00"}


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def plan(pct: float, *, resets: datetime, forecast: dict | None = None, provider: str = "claude-code",
         limit: str = "weekly") -> dict:
    return {"providerKey": provider, "providerName": "Claude Code", "shortName": "Claude Code", "isPayg": False,
            "rows": [{"limitKey": limit, "label": "Claude weekly window", "pct": pct, "resetsAt": iso(resets),
                      "forecast": forecast or {}}]}


def fresh_state() -> dict:
    return notify._blank()


def test_a_window_notifies_once_and_again_after_it_resets() -> None:
    state = fresh_state()
    reset = NOW + timedelta(days=2)
    cards = [plan(91, resets=reset)]
    [note] = notify.decide(notify.plan_conditions(cards, PREFS, NOW), state, now=NOW, quiet=False)
    assert note["kind"] == "threshold"
    assert note["title"] == "Claude Code: weekly window at 91%"
    assert note["body"] == "Resets in 2 days."
    assert notify.decide(notify.plan_conditions([plan(95, resets=reset)], PREFS, NOW), state, now=NOW, quiet=False) == []
    later = reset + timedelta(hours=1)
    next_window = [plan(92, resets=later + timedelta(days=7))]
    assert len(notify.decide(notify.plan_conditions(next_window, PREFS, later), state, now=later, quiet=False)) == 1


def test_a_run_out_before_the_reset_leads_and_is_sent_once_with_the_level() -> None:
    state = fresh_state()
    reset = NOW + timedelta(hours=30)
    forecast = {"state": "dry", "projectedRunDry": iso(NOW + timedelta(hours=6))}
    [note] = notify.decide(notify.plan_conditions([plan(92, resets=reset, forecast=forecast)], PREFS, NOW),
                           state, now=NOW, quiet=False)
    assert note["kind"] == "dry"
    assert note["title"] == "Claude Code: weekly window will run out before it resets"
    assert note["body"] == "92% used. At the current pace it runs out in 6 h, 24 h before it resets. Resets in 30 h."
    assert sorted(key.split("|")[0] for key in state["fired"]) == ["dry", "threshold"]


def test_early_forecasts_wait_for_half_the_window() -> None:
    forecast = {"state": "tight", "projectedRunDry": iso(NOW + timedelta(hours=3))}
    assert notify.plan_conditions([plan(30, resets=NOW + timedelta(hours=10), forecast=forecast)], PREFS, NOW) == []
    [condition] = notify.plan_conditions([plan(60, resets=NOW + timedelta(hours=10), forecast=forecast)], PREFS, NOW)
    assert condition["kinds"] == ["dry"] and not condition["urgent"]


def test_quiet_hours_hold_back_all_but_an_urgent_run_out() -> None:
    state = fresh_state()
    reset = NOW + timedelta(hours=8)
    calm = notify.plan_conditions([plan(91, resets=reset)], PREFS, NOW)
    assert notify.decide(calm, state, now=NOW, quiet=True) == []
    assert state["fired"] == {}
    urgent = notify.plan_conditions([plan(95, resets=reset, provider="codex", forecast={
        "state": "tight", "projectedRunDry": iso(NOW + timedelta(minutes=50))})], PREFS, NOW)
    assert [note["kind"] for note in notify.decide(urgent, state, now=NOW, quiet=True)] == ["dry"]
    # The held-back one goes out when quiet hours end.
    assert len(notify.decide(calm, state, now=NOW, quiet=False)) == 1


def lane(key: str, age: int, detail: str = "Newer polls returned no value.") -> dict:
    return {"kind": "lane", "key": key, "name": key, "short": "Codex weekly", "state": "held", "severity": "warn",
            "ageSeconds": age, "detail": detail}


def test_a_held_meter_notifies_per_stretch_but_an_app_closed_on_purpose_does_not() -> None:
    state = fresh_state()
    closed = lane("antigravity:gemini-5h", 7200, "Antigravity isn't running. Its meter is read from the app while it is open.")
    report = {"issues": [lane("codex:weekly", 1800), closed]}
    assert notify.health_conditions(report) == []
    report = {"issues": [lane("codex:weekly", 3700), closed]}
    [note] = notify.decide(notify.health_conditions(report), state, now=NOW, quiet=False)
    assert note["title"] == "Codex weekly meter has not updated for 1 h"
    assert notify.decide(notify.health_conditions(report), state, now=NOW, quiet=False) == []
    # Recovered, then stuck again: a new stretch.
    assert notify.decide([], state, now=NOW, quiet=False) == [] and state["fired"] == {}
    assert len(notify.decide(notify.health_conditions(report), state, now=NOW, quiet=False)) == 1


def test_hub_sync_notifies_after_fifteen_minutes_of_failing() -> None:
    sync = {"kind": "sync", "key": "hub", "name": "Hub sync", "state": "failed", "ageSeconds": 600, "detail": "Could not reach the hub."}
    assert notify.health_conditions({"issues": [sync]}) == []
    [condition] = notify.health_conditions({"issues": [{**sync, "ageSeconds": 1200}]})
    assert condition["titles"]["sync"] == "Hub sync is failing"
    assert condition["body"] == "Could not reach the hub. Last sync 20 min ago."


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    value = Settings(database_path=tmp_path / "spend.db", pricing_path=ROOT / "pricing",
                     anthropic_admin_key=None, openai_admin_key=None, timezone="UTC",
                     cache_hit_threshold=0.75, over_routing_token_ceiling=40000)
    initialize(value.database_path)
    return value


def test_check_sends_what_is_due_and_respects_a_switched_off_kind(settings, monkeypatch) -> None:
    reset = NOW + timedelta(days=2)
    monkeypatch.setattr(notify, "_cards", lambda _settings, _now: [plan(93, resets=reset)])
    monkeypatch.setattr("spend_app.freshness.report", lambda *_a, **_k: {"issues": [lane("codex:weekly", 4000)]})
    notify.save_kinds(settings.database_path, {"held": False})
    sent = []
    due = notify.check(settings, now=NOW, send=lambda title, body, tag: sent.append(title))
    assert sent == ["Claude Code: weekly window at 93%"] == [note["title"] for note in due]
    assert notify.check(settings, now=NOW + timedelta(minutes=1), send=lambda *a, **k: sent.append("again")) == []
    shown = notify.public_settings(settings.database_path)
    assert {kind["key"]: kind["on"] for kind in shown["kinds"]} == {"threshold": True, "dry": True, "held": False, "sync": True}
    with pytest.raises(ValueError):
        notify.save_kinds(settings.database_path, {"everything": True})


def test_notifications_are_off_outside_the_desktop_service(monkeypatch) -> None:
    monkeypatch.delenv(notify.ENV, raising=False)
    assert not notify.enabled()
    monkeypatch.setenv(notify.ENV, "1")
    assert notify.enabled()
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    assert not notify.enabled()


def test_the_toast_text_travels_outside_the_script(monkeypatch) -> None:
    monkeypatch.setattr(notify.os, "name", "nt")
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    title = "Claude Code: weekly at 91%'; Remove-Item C:/ -Recurse #"
    notify.show_toast(title, "Resets <soon> & then", tag="abc", run=fake_run)
    [(args, kwargs)] = calls
    script = base64.b64decode(args[-1]).decode("utf-16-le")
    assert args[-2] == "-EncodedCommand" and "Remove-Item" not in script and "SecurityElement" in script
    assert kwargs["env"]["BURNRATE_TOAST_TITLE"] == title
    assert kwargs["env"]["BURNRATE_TOAST_URL"].endswith("/setup?step=4")
    with pytest.raises(OSError):
        notify.show_toast("x", "y", run=lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="denied"))
