from datetime import UTC, datetime
from pathlib import Path

from spend_app.adapters.opencode_local import ingest as ingest_opencode
from spend_app.aggregate import aggregate_health
from spend_app.db import connect
from spend_app.pricing import PricingEngine
from spend_app.quotas import poll_activity, poll_quotas, unavailable_samples
from tests_spend.test_local_provider_adapters import write_opencode_fixture
from tests_spend.test_quota_pollers import ACTIVITY, fixture_collectors


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
START = datetime(2026, 8, 30, tzinfo=UTC)
END = datetime(2026, 8, 31, tzinfo=UTC)
NOW = datetime(2026, 8, 30, 23, tzinfo=UTC)


def _money_zeros(payload: dict) -> list[tuple[str, float]]:
    found: list[tuple[str, float]] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        elif isinstance(node, (int, float)) and not isinstance(node, bool) and float(node) == 0.0:
            leaf = path.rsplit(".", 1)[-1]
            if leaf in {"value", "amountUsd", "monthlyEquivalent", "todayUsd", "burnRatePerDay", "planCost"}:
                found.append((path, float(node)))

    walk(payload, "")
    return found


def test_s02_02_admin_missing_keys_are_unavailable_not_zero(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    from spend_app.adapters.anthropic_admin import ingest as ingest_anthropic
    from spend_app.adapters.openai_admin import ingest as ingest_openai

    openai = ingest_openai(
        database_path=database,
        pricing=pricing,
        admin_key=None,
        start=START,
        end=END,
    )
    anthropic = ingest_anthropic(
        database_path=database,
        pricing=pricing,
        admin_key=None,
        start=START,
        end=END,
    )
    assert openai["status"] == "skipped"
    assert anthropic["status"] == "skipped"
    health = aggregate_health(database_path=database, now=NOW, timezone="UTC")
    by_source = {row["source"]: row for row in health["ingest"]}
    for source in ("openai_admin", "anthropic_admin"):
        item = by_source[source]
        assert item["status"] == "unavailable"
        assert "credential missing" in (item["error"] or "")
    assert _money_zeros(health) == []


def test_s02_07_opencode_fixture_replay_writes_zero(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    source = write_opencode_fixture(tmp_path / "opencode.db")
    pricing = PricingEngine.load(ROOT / "pricing")
    first = ingest_opencode(database_path=database, pricing=pricing, source_database=source)
    second = ingest_opencode(database_path=database, pricing=pricing, source_database=source)
    assert first["eventsWritten"] > 0
    assert second["eventsWritten"] == 0


def test_s02_11_agent_runs_unique_id_second_poll_writes_zero(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    first = poll_activity(database, collector=lambda: ACTIVITY, now=lambda: "2026-09-15T12:00:00Z")
    second = poll_activity(database, collector=lambda: ACTIVITY, now=lambda: "2026-09-15T12:01:00Z")
    assert first["new"] == 2
    assert second["new"] == 0
    with connect(database) as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_runs'"
        ).fetchone()[0]
        count = connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
        ids = [row[0] for row in connection.execute("SELECT id FROM agent_runs")]
    assert "id TEXT PRIMARY KEY" in sql.replace("\n", " ")
    assert count == 2
    assert sorted(ids) == ["traycer:chat-1", "traycer:chat-2"]


def test_s02_10_unavailable_quota_is_not_zero_percent(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    from spend_app.db import initialize

    initialize(database)
    collectors = {
        "claude-code": lambda: unavailable_samples(
            "claude-code", "Claude quota source is unavailable.", source="claude_oauth_usage"
        )
    }
    poll_quotas(database, collectors=collectors, now=lambda: "2026-09-15T12:00:00Z")
    with connect(database) as connection:
        rows = connection.execute(
            "SELECT used, pct, unit, label FROM quotas WHERE provider_key='claude-code'"
        ).fetchall()
    assert rows
    for used, pct, unit, label in rows:
        assert used is None
        assert pct is None
        assert unit == "unavailable"
        assert "Claude quota source is unavailable." in label


def test_s02_16_readme_lists_backfill_choices_and_partial_exit() -> None:
    docs = (ROOT / "docs" / "troubleshooting.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    surface = docs + "\n" + readme
    for choice in (
        "codex-local",
        "claude-local",
        "traycer-local",
        "opencode-local",
        "openai-admin",
        "anthropic-admin",
    ):
        assert choice in surface
    assert "finishes the run as `failed`" not in surface
    assert "CLI-only" in surface
    assert "partial" in surface
    assert "exit `2`" in surface or "Exit `2`" in surface or "`2` only" in surface
