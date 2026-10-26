"""``burnrate demo``: a made-up month that looks like a real one."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from spend_app import demo
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.db import connect

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 22, 18, 30, tzinfo=UTC)


def _settings(database: Path) -> Settings:
    return Settings(
        database_path=database,
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="America/New_York",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )


def test_seed_prices_every_row_and_spreads_them_over_two_computers(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    info = demo.seed(database, now=NOW)
    with connect(database) as connection:
        events = connection.execute("SELECT COUNT(*), COUNT(DISTINCT project) FROM usage_events").fetchone()
        unpriced = connection.execute("SELECT COUNT(*) FROM unpriced_usage_events").fetchone()[0]
        laptop = connection.execute(
            "SELECT COUNT(*) FROM usage_events WHERE raw_id LIKE ?", (f"m:{info['laptop']}:%",)
        ).fetchone()[0]
        running = connection.execute("SELECT COUNT(*) FROM agent_runs WHERE state='running'").fetchone()[0]
    assert events[0] > 1000 and events[1] == len(demo.PROJECTS)
    assert unpriced == 0
    assert 0 < laptop < events[0]
    assert running == len(demo.AGENTS)


def test_the_same_seed_draws_the_same_month(tmp_path: Path) -> None:
    totals = []
    for name in ("a", "b"):
        database = tmp_path / name / "spend.db"
        database.parent.mkdir()
        demo.seed(database, now=NOW)
        with connect(database) as connection:
            totals.append(connection.execute("SELECT COUNT(*), SUM(output_tokens) FROM usage_events").fetchone())
    assert totals[0] == totals[1]


def test_the_demo_hud_shows_meters_repos_and_a_week_worth_more_than_its_plans(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    demo.seed(database, now=NOW)
    client = TestClient(create_app(_settings(database), enable_scheduler=False, now=NOW))
    hud = client.get("/api/spend/hud", params={"machine": "all"}).json()
    meters = {(card["providerKey"], row["limitKey"]): row["pct"] for card in hud["capacity"] for row in card["rows"]}
    assert meters[("claude-code", "weekly")] == 61.0 and meters[("codex", "weekly")] == 38.0
    usage = client.get("/api/spend/hud/usage", params={"machine": "all"}).json()
    assert usage["repos"]["rows"] and usage["navigation"]["todayUsd"] > 0
    week = client.get("/api/spend/share/week", params={"machine": "all"}).json()
    assert week["usd"] > week["planUsd"] > 0
    assert week["tools"][0]["name"] == "Claude Code"
    setup = client.get("/api/setup/state").json()["setup"]
    assert setup["completed"] and setup["computerName"] == "Studio PC"


def test_demo_mode_reads_and_moves_nothing_on_this_computer(tmp_path: Path, monkeypatch) -> None:
    from spend_app import quotas, setup_wizard

    monkeypatch.setenv("BURNRATE_DEMO", "1")
    monkeypatch.setattr(quotas, "poll_quotas", lambda **_: (_ for _ in ()).throw(AssertionError("polled")))
    database = tmp_path / "spend.db"
    demo.seed(database, now=NOW)
    client = TestClient(create_app(_settings(database), enable_scheduler=False, now=NOW))
    assert [item["kind"] for item in setup_wizard.detect_network_addresses(17377)] == ["local"]
    assert client.post("/api/hud/native", json={"action": "resize"}).json() == {"ok": False, "reason": "demo"}
    assert client.post("/api/spend/quotas/refresh").json()["providersPolled"] == 0
