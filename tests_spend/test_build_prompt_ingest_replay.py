"""Re-running ingest on identical input must write zero new rows."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from spend_app.adapters.anthropic_admin import ingest as ingest_anthropic_admin
from spend_app.adapters.claude_local import ingest as ingest_claude
from spend_app.adapters.claude_local import reset_file_cache as reset_claude_cache
from spend_app.adapters.codex_local import ingest as ingest_codex
from spend_app.adapters.codex_local import reset_file_cache as reset_codex_cache
from spend_app.adapters.openai_admin import ingest as ingest_openai_admin
from spend_app.db import connect
from spend_app.pricing import PricingEngine
from tests_spend.test_build_prompt_harness import FIXTURES, ROOT


def _count(database: Path, table: str) -> int:
    with connect(database) as connection:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_codex_local_replay_writes_zero_new_rows(tmp_path: Path) -> None:
    session = tmp_path / "rollout.jsonl"
    session.write_text(
        (FIXTURES / "build_prompt_codex_session.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    reset_codex_cache()
    first = ingest_codex(database_path=database, pricing=pricing, session_glob=str(session))
    reset_codex_cache()
    second = ingest_codex(database_path=database, pricing=pricing, session_glob=str(session))
    assert first["status"] in {"success", "partial"}
    assert first["eventsWritten"] >= 1
    assert second["eventsWritten"] == 0
    assert _count(database, "usage_events") == first["eventsWritten"]


def test_claude_local_replay_writes_zero_new_rows(tmp_path: Path) -> None:
    session = tmp_path / "claude.jsonl"
    session.write_text(
        (FIXTURES / "build_prompt_claude_session.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    reset_claude_cache()
    first = ingest_claude(database_path=database, pricing=pricing, session_glob=str(session))
    reset_claude_cache()
    second = ingest_claude(database_path=database, pricing=pricing, session_glob=str(session))
    assert first["eventsWritten"] >= 1
    assert second["eventsWritten"] == 0
    assert _count(database, "usage_events") == first["eventsWritten"]


def _openai_client() -> httpx.Client:
    usage = json.loads((FIXTURES.parent / "fixtures" / "openai_usage.json").read_text(encoding="utf-8"))
    costs = json.loads((FIXTURES.parent / "fixtures" / "openai_costs.json").read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/organization/usage/completions"):
            return httpx.Response(200, json=usage)
        if path.endswith("/organization/costs"):
            return httpx.Response(200, json=costs)
        return httpx.Response(404, json={"error": {"message": path}})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_openai_admin_fixture_replay_writes_zero_new_rows(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    start = datetime.fromtimestamp(1788048000, UTC)
    end = datetime.fromtimestamp(1788134400, UTC)
    first = ingest_openai_admin(
        database_path=database,
        pricing=pricing,
        admin_key="test-admin-key",
        start=start,
        end=end,
        client=_openai_client(),
    )
    second = ingest_openai_admin(
        database_path=database,
        pricing=pricing,
        admin_key="test-admin-key",
        start=start,
        end=end,
        client=_openai_client(),
    )
    assert first["status"] == "success"
    assert first["eventsWritten"] >= 1
    assert second["eventsWritten"] == 0
    assert _count(database, "usage_events") == first["eventsWritten"]
    assert _count(database, "provider_cost_buckets") == first["costBucketsWritten"]


def _anthropic_client() -> httpx.Client:
    usage = json.loads((FIXTURES.parent / "fixtures" / "anthropic_usage.json").read_text(encoding="utf-8"))
    costs = json.loads((FIXTURES.parent / "fixtures" / "anthropic_costs.json").read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "usage_report" in path:
            return httpx.Response(200, json=usage)
        if "cost_report" in path:
            return httpx.Response(200, json=costs)
        return httpx.Response(404, json={"error": {"message": path}})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_anthropic_admin_fixture_replay_writes_zero_new_rows(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    start = datetime(2026, 8, 30, tzinfo=UTC)
    end = datetime(2026, 8, 31, tzinfo=UTC)
    first = ingest_anthropic_admin(
        database_path=database,
        pricing=pricing,
        admin_key="test-admin-key",
        start=start,
        end=end,
        client=_anthropic_client(),
    )
    second = ingest_anthropic_admin(
        database_path=database,
        pricing=pricing,
        admin_key="test-admin-key",
        start=start,
        end=end,
        client=_anthropic_client(),
    )
    assert first["status"] in {"success", "partial"}
    assert first["eventsWritten"] >= 1
    assert second["eventsWritten"] == 0
    assert _count(database, "usage_events") == first["eventsWritten"]


def test_missing_admin_credentials_are_unavailable_not_zero(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    openai = ingest_openai_admin(
        database_path=database,
        pricing=pricing,
        admin_key=None,
        start=datetime(2026, 8, 30, tzinfo=UTC),
        end=datetime(2026, 8, 31, tzinfo=UTC),
        client=httpx.Client(
            transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError("network")))
        ),
    )
    anthropic = ingest_anthropic_admin(
        database_path=database,
        pricing=pricing,
        admin_key=None,
        start=datetime(2026, 8, 30, tzinfo=UTC),
        end=datetime(2026, 8, 31, tzinfo=UTC),
        client=httpx.Client(
            transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError("network")))
        ),
    )
    assert openai["status"] == "skipped"
    assert anthropic["status"] == "skipped"
    assert openai["eventsWritten"] == 0
    assert anthropic["eventsWritten"] == 0
    assert _count(database, "usage_events") == 0
