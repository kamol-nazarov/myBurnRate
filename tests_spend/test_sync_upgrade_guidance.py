"""A reachable legacy hub must not look fully synchronized while repairs wait."""

from datetime import UTC, datetime
from pathlib import Path

from spend_app import freshness, setup_wizard
from spend_app.config import Settings
from spend_app.db import connect, initialize
from spend_app.pricing import PricingEngine


def test_legacy_repair_warning_persists_and_clears_after_ack(tmp_path, monkeypatch):
    now = datetime(2026, 11, 9, 12, tzinfo=UTC)
    monkeypatch.setattr(setup_wizard, "utc_now", lambda: now.isoformat())
    monkeypatch.setattr(setup_wizard, "_default_computer_name", lambda: "Fixture")
    settings = Settings(database_path=tmp_path / "spend.db",
                        pricing_path=Path(__file__).resolve().parents[1] / "pricing",
                        anthropic_admin_key=None, openai_admin_key=None, timezone="UTC",
                        cache_hit_threshold=0.75, over_routing_token_ceiling=40_000)
    initialize(settings.database_path)
    state = setup_wizard.default_setup_state()
    state.update(role="spoke", computerName="Fixture")
    state["spokeConfig"].update(enabled=True, hubUrl="https://hub.test", token="fixture-token")
    with connect(settings.database_path) as connection:
        setup_wizard.write_setup_state(connection, state)
    replies = iter([{"ok": True, "eventsWritten": 0, "usageChangesPending": True},
                    {"ok": True, "eventsWritten": 1, "usageChangeSeq": 5}])
    monkeypatch.setattr(setup_wizard, "sync_once", lambda *a, **kw: next(replies))
    for pending in (True, False):
        setup_wizard.maybe_run_configured_spoke_sync(settings, PricingEngine([]))
        with connect(settings.database_path) as connection:
            saved = setup_wizard.read_setup_state(connection)
        assert saved["spokeConfig"]["usageChangesPending"] is pending
        assert saved["spokeConfig"]["lastSyncError"] is None
        sync, issues = freshness._sync_state(saved, now, {"uptimeSeconds": 10})
        assert sync["usageChangesPending"] is pending
        assert sync["failing"] is False
        assert bool(issues) is pending
        if pending:
            assert "Update the hub" in issues[0]["detail"]
            assert "queued" in issues[0]["detail"]
