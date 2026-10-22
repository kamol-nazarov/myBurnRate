"""The HUD is the product: the dashboard is opt-in, and the HUD keeps its numbers."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from spend_app import sync as sync_module
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.db import connect, initialize
from spend_app.hud import render_hud
from spend_app.setup_wizard import write_setup_state

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 22, 16, tzinfo=UTC)


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(database_path=tmp_path / "hud.db", pricing_path=ROOT / "pricing",
                        anthropic_admin_key=None, openai_admin_key=None, timezone="UTC",
                        cache_hit_threshold=0.75, over_routing_token_ceiling=40000)
    initialize(settings.database_path)
    return settings


def complete_setup(settings: Settings) -> None:
    with connect(settings.database_path) as connection:
        write_setup_state(connection, {"completed": True, "role": "hub"})


def test_root_opens_the_hud_when_the_dashboard_is_off(tmp_path, monkeypatch):
    monkeypatch.delenv("BURNRATE_ENABLE_DASHBOARD", raising=False)
    settings = settings_for(tmp_path)
    complete_setup(settings)
    client = TestClient(create_app(settings, enable_scheduler=False, now=NOW))
    for route in ("/", "/?app=1"):
        page = client.get(route)
        assert page.status_code == 200
        assert 'location.replace("/hud")' in page.text
        assert 'invoke("hide_main")' in page.text
        assert "spend.js" not in page.text
    assert '<html lang="en" data-dashboard="off">' in client.get("/hud").text
    assert "Skip to </span>HUD →" in client.get("/setup").text


def test_unfinished_setup_still_opens_the_wizard(tmp_path, monkeypatch):
    monkeypatch.delenv("BURNRATE_ENABLE_DASHBOARD", raising=False)
    client = TestClient(create_app(settings_for(tmp_path), enable_scheduler=False, now=NOW))
    page = client.get("/?app=1").text
    assert 'id="wizard-stepper"' in page
    # The desktop app starts this window hidden, so the wizard shows it.
    assert 'invoke?.("show_dashboard")' in page


def test_dashboard_returns_with_its_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("BURNRATE_ENABLE_DASHBOARD", "1")
    settings = settings_for(tmp_path)
    complete_setup(settings)
    client = TestClient(create_app(settings, enable_scheduler=False, now=NOW))
    assert "/spend.js?v=" in client.get("/").text
    assert 'data-dashboard="off"' not in client.get("/hud").text.split(">", 2)[1]
    assert "Skip to </span>Dashboard →" in client.get("/setup").text


def test_hud_hides_dashboard_entry_points_when_off():
    html = render_hud()
    assert '<html lang="en" data-dashboard="off">' in html
    assert 'html[data-dashboard="off"] #hud-expand-btn { display: none; }' in html
    assert "if (!dashboardOn) return;" in html
    assert render_hud(dashboard=True).startswith("<!doctype html>\n<html lang=\"en\">")


def _spoke(tmp_path: Path, monkeypatch, hub) -> Settings:
    settings = settings_for(tmp_path)
    with connect(settings.database_path) as connection:
        write_setup_state(connection, {"role": "spoke", "spokeConfig": {
            "enabled": True, "hubUrl": "https://hub.test", "token": "spoke-token"}})
    monkeypatch.setitem(sync_module._HUB_TEST_CLIENTS, "https://hub.test", hub)
    sync_module.clear_hub_proxy_cache()
    return settings


class FakeResponse:
    def __init__(self, body: dict, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def json(self) -> dict:
        return json.loads(json.dumps(self._body))


def test_spoke_hud_serves_the_last_hub_copy_while_renewing_it(tmp_path, monkeypatch):
    calls = []
    release = threading.Event()

    class SlowHub:
        def get(self, path, params=None, headers=None):
            calls.append(path)
            if len(calls) > 1:
                release.wait(5)
            return FakeResponse({"capacity": [], "read": len(calls)})

    settings = _spoke(tmp_path, monkeypatch, SlowHub())
    fetch = sync_module.fetch_from_configured_hub
    assert fetch(settings, "/api/spend/hud", stale_seconds=300)["read"] == 1
    started = time.monotonic()
    # The copy is past its TTL: it is served at once while the Hub is slow.
    assert fetch(settings, "/api/spend/hud", ttl_seconds=0, stale_seconds=300)["read"] == 1
    assert fetch(settings, "/api/spend/hud", ttl_seconds=0, stale_seconds=300)["read"] == 1
    assert time.monotonic() - started < 1
    release.set()
    deadline = time.monotonic() + 5
    while sync_module._HUB_PROXY_RENEWING and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) == 2, "one background read renews the copy"
    assert fetch(settings, "/api/spend/hud", ttl_seconds=60, stale_seconds=300)["read"] == 2


def test_expired_hub_copy_still_covers_a_failed_hub_read(tmp_path, monkeypatch):
    responses = [FakeResponse({"capacity": [], "read": 1}), FakeResponse({}, status_code=503)]

    class FlakyHub:
        def get(self, path, params=None, headers=None):
            return responses.pop(0)

    settings = _spoke(tmp_path, monkeypatch, FlakyHub())
    fetch = sync_module.fetch_from_configured_hub
    assert fetch(settings, "/api/spend/hud")["read"] == 1
    # New local data expires the copy; the next read goes to the Hub, and a
    # failed read falls back to the last copy instead of a local-only view.
    sync_module.expire_hub_proxy_cache()
    assert fetch(settings, "/api/spend/hud")["read"] == 1
    assert not responses


def _hold_source() -> str:
    html = render_hud()
    return "        const heldReadings" + html.split("        const heldReadings", 1)[1].split("        const shownPeak", 1)[0]


def test_hud_keeps_the_last_reading_when_a_poll_returns_none():
    harness = """
let selectedMachine = "all";
let now = Date.parse("2026-10-22T12:00:00Z");
Date.now = () => now;
%s
const open = "2026-10-22T15:00:00Z", reset = "2026-10-22T11:00:00Z";
const row = (limitKey, pct, resetsAt, payg = false) => ({limitKey, pct, resetsAt, payg});
const plan = (key, rows) => ({key, name: key, rows});
const pick = (groups, key) => groups.find(group => group.key === key);
holdReadings([
  plan("claude-code", [row("5h", 6, open), row("weekly", 12, open)]),
  plan("claude-code-work", [row("5h", 3, open)]),
  plan("muse", [row("5h", 77, reset)]),
]);
const second = holdReadings([
  plan("claude-code", [row("5h", null, ""), row("weekly", 13, open)]),
  plan("muse", [row("5h", null, "")]),
]);
now += 16 * 60 * 1000;
const later = holdReadings([plan("claude-code", [row("5h", null, "")])]);
selectedMachine = "laptop";
const otherComputer = holdReadings([plan("claude-code", [row("5h", null, "")])]);
console.log(JSON.stringify({
  claude: pick(second, "claude-code").rows,
  work: pick(second, "claude-code-work") && pick(second, "claude-code-work").rows,
  muse: pick(second, "muse").rows,
  later: later.map(group => group.key),
  laterClaude: pick(later, "claude-code").rows[0],
  otherComputer: otherComputer[0].rows[0],
}));
""" % _hold_source()
    result = subprocess.run(["node", "-e", harness], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip())
    assert report["claude"][0] == {"limitKey": "5h", "pct": 6, "resetsAt": "2026-10-22T15:00:00Z", "payg": False, "held": True}
    assert report["claude"][1]["pct"] == 13 and "held" not in report["claude"][1]
    assert report["work"] == [{"limitKey": "5h", "pct": 3, "resetsAt": "2026-10-22T15:00:00Z", "payg": False, "held": True}]
    assert report["muse"][0]["pct"] is None, "a window that already reset is not held"
    assert report["later"] == ["claude-code"], "a card missing for 15 minutes is dropped"
    assert report["laterClaude"]["pct"] == 6
    assert report["otherComputer"]["pct"] is None, "readings are held per computer filter"
