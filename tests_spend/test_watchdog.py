"""Watchdog decisions: when to restart the dashboard, and when to leave it alone."""

from __future__ import annotations

import json
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spend_app import watchdog

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BURNRATE_DATA_ROOT", str(tmp_path / "data"))
    (tmp_path / "data" / "run").mkdir(parents=True)
    return tmp_path / "data"


def _getter(health=None, freshness=None):
    def get(url, timeout, token=None):
        if url.endswith("/healthz"):
            if isinstance(health, Exception):
                raise health
            return {"status": "ok"}
        if isinstance(freshness, Exception):
            raise freshness
        return freshness
    return get


def test_probe_separates_down_unresponsive_stalled_and_healthy() -> None:
    assert watchdog.probe(get=_getter(freshness={"ingest": {"stalled": False}})) == (True, "ok")
    healthy, reason = watchdog.probe(get=_getter(health=ConnectionRefusedError()))
    assert not healthy and reason.startswith("down")
    healthy, reason = watchdog.probe(get=_getter(freshness=TimeoutError()))
    assert not healthy and reason.startswith("unresponsive")
    healthy, reason = watchdog.probe(get=_getter(freshness={"ingest": {"stalled": True, "lastPassAgeSeconds": 900}}))
    assert not healthy and reason == "collection stalled (last pass 900s ago)"
    # A build without the freshness route (or a token it cannot present) is judged by health alone.
    missing = urllib.error.HTTPError("u", 404, "missing", None, None)
    assert watchdog.probe(get=_getter(freshness=missing))[0] is True


def _run(probe_results, *, restart_ok=True, **kwargs):
    results = iter(probe_results)
    calls = []

    def probe_fn(port, token=None):
        return next(results)

    def restart_fn():
        calls.append(1)
        return restart_ok, "Restarted"

    outcome = watchdog.check_once(port=17331, sleep=lambda _s: None, probe_fn=probe_fn, restart_fn=restart_fn, now=NOW, **kwargs)
    return outcome, len(calls)


def test_healthy_and_transient_problems_do_not_restart() -> None:
    assert _run([(True, "ok")]) == ("ok", 0)
    assert _run([(False, "down"), (True, "ok")]) == ("recovered", 0)


def test_confirmed_failure_restarts_and_records_it(_data_root: Path) -> None:
    assert _run([(False, "collection stalled"), (False, "collection stalled")]) == ("restarted", 1)
    state = json.loads((_data_root / "run" / "watchdog.json").read_text(encoding="utf-8"))
    assert state["lastReason"] == "collection stalled" and len(state["restarts"]) == 1
    assert _run([(False, "down"), (False, "down")], restart_ok=False) == ("failed", 1)


def test_explicit_stop_is_respected(_data_root: Path) -> None:
    (_data_root / "run" / "stopped").write_text("2026-10-19T11:00:00Z", encoding="utf-8")
    assert _run([(False, "down"), (False, "down")]) == ("stopped", 0)


def test_restarts_are_rate_limited(_data_root: Path) -> None:
    recent = [(NOW - timedelta(minutes=minutes)).isoformat() for minutes in (5, 20, 40)]
    old = (NOW - timedelta(hours=2)).isoformat()
    (_data_root / "run" / "watchdog.json").write_text(json.dumps({"restarts": [old, *recent]}), encoding="utf-8")
    assert _run([(False, "down"), (False, "down")]) == ("rate-limited", 0)
    assert watchdog.recent_restarts({"restarts": [old, *recent]}, NOW) == recent


def test_dry_run_never_restarts() -> None:
    assert _run([(False, "down"), (False, "down")], dry_run=True) == ("dry-run", 0)


def test_restart_goes_through_the_lifecycle_script() -> None:
    seen = {}

    class Completed:
        returncode = 0
        stdout = "Restarted BURNRATE dashboard"
        stderr = ""

    def run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return Completed()

    ok, output = watchdog.restart(run=run)
    assert ok and "Restarted" in output
    assert seen["args"][-1].endswith("restart-burnrate.ps1")
    assert "-NonInteractive" in seen["args"]
    assert seen["kwargs"]["creationflags"] in {0, watchdog.CREATE_NO_WINDOW}
