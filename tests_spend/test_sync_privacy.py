"""Privacy assertions using isolated stores and fake provider transports."""

from datetime import UTC, datetime
from pathlib import Path

from spend_app.db import connect, initialize, upsert_agent_run
from spend_app.integration_policy import LANES, authorize
from spend_app.limits import _muse_limits_uncached
from spend_app.pricing import PricingEngine
from spend_app.sync import apply_sync_push, export_sync_batch
from spend_app.sync_privacy import scrub_synced_activity_titles


def _record(connection, run_id: str, title: str, stamp: str):
    upsert_agent_run(connection, id=run_id, name=title, model_key="gpt-5", state="live",
                     started_at=stamp, last_seen_at=stamp)


def test_hub_sync_shares_a_fixed_tool_name_keeps_device_title_local(tmp_path):
    desktop = tmp_path / "desktop.db"
    hub = tmp_path / "hub.db"
    initialize(desktop)
    now = datetime.now(UTC)
    title = "Private Example activity title"
    with connect(desktop) as connection:
        _record(connection, "codex:local-run", title, now.isoformat())
    batch, _ = export_sync_batch(desktop)
    assert batch["agentRuns"][0]["name"] == "Codex"
    assert title not in str(batch)
    with connect(desktop) as connection:
        assert connection.execute("SELECT name FROM agent_runs").fetchone()[0] == title

    # Released desktops sent their local title. The upgraded hub normalizes
    # that field before SQLite persistence while preserving the run.
    batch["agentRuns"][0]["name"] = title
    apply_sync_push(hub, PricingEngine([]), machine_id="desktop", body=batch, now=now)
    with connect(hub) as connection:
        assert tuple(connection.execute("SELECT id,name FROM agent_runs").fetchone()) == (
            "m:desktop:codex:local-run", "Codex",
        )


def test_activity_title_migration_is_idempotent_and_scoped(tmp_path):
    database = tmp_path / "existing.db"
    initialize(database)
    stamp = datetime.now(UTC).isoformat()
    with connect(database) as connection:
        _record(connection, "m:desktop:grok:old-run", "Original remote title", stamp)
        _record(connection, "codex:local-run", "Original local title", stamp)
        connection.execute("DELETE FROM app_meta WHERE key='sync.activity_tool_labels.v1'")
    scrub_synced_activity_titles(database)
    scrub_synced_activity_titles(database)
    with connect(database) as connection:
        assert dict(connection.execute("SELECT id,name FROM agent_runs")) == {
            "m:desktop:grok:old-run": "Grok Build",
            "codex:local-run": "Original local title",
        }


def test_muse_switch_blocks_the_provider_transport_before_sign_in_read(monkeypatch):
    assert authorize("muse_quota_endpoint", environ={})[0] is True
    monkeypatch.setenv("BURNRATE_ENABLE_MUSE_QUOTA", "0")

    def unexpected(*args, **kwargs):
        raise AssertionError("A disabled meter tried to read sign-in or contact the provider.")

    monkeypatch.setattr(Path, "read_text", unexpected)
    monkeypatch.setattr("spend_app.limits.httpx.post", unexpected)
    response = _muse_limits_uncached()
    assert response["status"] == "unavailable"
    assert "BURNRATE_ENABLE_MUSE_QUOTA" in response["detail"]


def test_privacy_lists_all_provider_and_update_hosts():
    from spend_app import limits, native_update

    doc = (Path(__file__).resolve().parents[1] / "docs" / "privacy.md").read_text(encoding="utf-8")
    hosts = {host for lane in LANES.values() for host in lane.allowed_hosts}
    hosts.update({"github.com", *native_update._ASSET_HOSTS})
    for key, values in vars(limits).items():
        if key.endswith("_ALLOWED_HOSTS"):
            hosts.update(values)
    assert {host for host in hosts if f"`{host}`" not in doc} == set()


def test_pricing_audit_reads_the_store_with_query_only_enabled(tmp_path, monkeypatch):
    from spend_app import pricing_audit

    database = tmp_path / "audit.db"
    initialize(database)

    def observe(connection, pricing, *, now):
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        return {}

    monkeypatch.setattr(pricing_audit, "coverage_by_source", observe)
    result = pricing_audit.audit(database, PricingEngine([]), now=datetime.now(UTC))
    assert result["modelCount"] == 0
