"""Synthetic backend regressions for the native usage surface."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from spend_app.api import create_app
from spend_app.db import (
    UnpricedUsageEvent,
    UsageEvent,
    connect,
    initialize,
    upsert_unpriced_event,
    upsert_usage_event,
)
from spend_app.native_usage import period_start, usage_report
from spend_app.pricing import PricingEngine
from tests_spend.test_api import make_settings

ROOT = Path(__file__).resolve().parents[1]
PRICING = PricingEngine.load(ROOT / "pricing")


def _stamp(moment):
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _priced(
    connection,
    *,
    raw_id,
    occurred_at,
    tool="codex",
    model="gpt-5.6-sol",
    source="codex_local",
    input_tokens=1000,
    cached=200,
    writes=50,
    write_1h=0,
    output=100,
    reasoning=30,
    reported_cost=999.0,
):
    upsert_usage_event(
        connection,
        UsageEvent(
            source=source,
            tool_key=tool,
            model_key=model,
            occurred_at=_stamp(occurred_at),
            session_id=None,
            project=None,
            input_tokens=input_tokens,
            cached_input_tokens=cached,
            cache_write_tokens=writes,
            cache_write_1h_tokens=write_1h,
            output_tokens=output,
            reasoning_tokens=reasoning,
            cost_usd=reported_cost,
            computed_cost_usd=reported_cost,
            raw_id=raw_id,
            ingested_at=_stamp(occurred_at),
            is_exact=True,
        ),
    )


def _unpriced(
    connection,
    *,
    raw_id,
    occurred_at,
    telemetry_complete=False,
    tokens=77,
    model="unknown-model",
):
    upsert_unpriced_event(
        connection,
        UnpricedUsageEvent(
            source="synthetic",
            tool_key="codex",
            model_key=model,
            occurred_at=_stamp(occurred_at),
            session_id=None,
            project=None,
            input_tokens=tokens,
            cached_input_tokens=0,
            cache_write_tokens=0,
            cache_write_1h_tokens=0,
            output_tokens=0,
            reasoning_tokens=0,
            unclassified_tokens=0,
            telemetry_complete=telemetry_complete,
            cost_usd=500.0,
            raw_id=raw_id,
            ingested_at=_stamp(occurred_at),
        ),
    )


def test_report_counts_token_classes_and_does_not_sum_reported_cost_or_plans(tmp_path):
    settings = make_settings(tmp_path / "usage.db")
    initialize(settings.database_path)
    now = datetime(2026, 11, 13, 15, tzinfo=UTC)
    occurred = now - timedelta(minutes=1)
    with connect(settings.database_path) as connection:
        _priced(connection, raw_id="local", occurred_at=occurred)
        _priced(
            connection,
            raw_id="org",
            occurred_at=occurred,
            source="openai_admin",
            tool="codex",
            model="gpt-5.6-sol",
            input_tokens=500,
            cached=100,
            writes=0,
            output=20,
            reasoning=7,
        )
        # Persist a large subscription separately; reference usage value must
        # remain a token-price calculation and never include plan cost.
        from spend_app.plan_service import apply_mutation

        apply_mutation(
            connection,
            {
                "operation": "add",
                "request_id": "native-plan-1",
                "values": {
                    "name": "Synthetic",
                    "tool_key": "codex",
                    "amount_usd": "9999",
                    "cadence": "monthly",
                    "start_date": "2026-11-01",
                    "end_date": None,
                },
            },
            now.date(),
        )

    report = usage_report(settings, PRICING, now=now, period="all")
    tracked = report["total"]
    org = report["organizationTotal"]
    local_expected = PRICING.compute(
        model_key="gpt-5.6-sol",
        occurred_at=occurred,
        input_tokens=1000,
        cached_input_tokens=200,
        cache_write_tokens=50,
        output_tokens=100,
    )
    org_expected = PRICING.compute(
        model_key="gpt-5.6-sol",
        occurred_at=occurred,
        input_tokens=500,
        cached_input_tokens=100,
        cache_write_tokens=0,
        output_tokens=20,
    )
    assert tracked["input"] == 800
    assert tracked["cached"] == 200
    assert tracked["writes"] == 50
    assert tracked["output"] == 100
    assert tracked["reasoning"] == 30
    assert tracked["reasoningComplete"] is True
    assert Decimal(str(tracked["valueUsd"])) == local_expected
    assert org["input"] == 400 and org["output"] == 20 and org["reasoning"] == 7
    assert org["reasoningComplete"] is True
    assert Decimal(str(org["valueUsd"])) == org_expected
    assert tracked["valueUsd"] < 9999
    assert len(report["tools"]) == 2
    assert {item["organization"] for item in report["tools"]} == {False, True}


def test_unpriced_incomplete_and_zero_usage_are_distinguishable(tmp_path):
    settings = make_settings(tmp_path / "quality.db")
    initialize(settings.database_path)
    now = datetime(2026, 11, 13, 15, tzinfo=UTC)
    with connect(settings.database_path) as connection:
        _priced(
            connection,
            raw_id="zero",
            occurred_at=now - timedelta(minutes=1),
            input_tokens=0,
            cached=0,
            writes=0,
            output=0,
            reasoning=0,
            reported_cost=0,
        )
        _unpriced(
            connection,
            raw_id="unpriced",
            occurred_at=now - timedelta(minutes=1),
            telemetry_complete=True,
        )
        _unpriced(
            connection,
            raw_id="incomplete",
            occurred_at=now - timedelta(minutes=1),
            telemetry_complete=False,
            tokens=5,
        )

    total = usage_report(settings, PRICING, now=now, period="all")["total"]
    assert total["events"] == 3
    assert total["pricedEvents"] == 1
    assert total["unpricedEvents"] == 2
    assert total["valueUsd"] == 0
    assert total["complete"] is False
    assert total["tokens"] == 82


def test_period_ranges_use_local_day_and_month_across_dst_and_keep_all_history(
    tmp_path,
):
    zone = "America/New_York"
    now = datetime(2026, 3, 8, 7, 30, tzinfo=UTC)  # spring-forward day
    assert period_start("today", now, zone) == datetime(2026, 3, 8, 5, tzinfo=UTC)
    assert period_start("month", now, zone) == datetime(2026, 3, 1, 5, tzinfo=UTC)
    assert period_start("all", now, zone) == datetime(1970, 1, 1, tzinfo=UTC)

    settings = make_settings(tmp_path / "history.db")
    initialize(settings.database_path)
    old = datetime(2025, 12, 1, tzinfo=UTC)
    current = datetime(2026, 3, 8, 6, tzinfo=UTC)
    with connect(settings.database_path) as connection:
        _priced(connection, raw_id="old", occurred_at=old)
        _priced(connection, raw_id="current", occurred_at=current)
    report = usage_report(settings, PRICING, now=now, period="all")
    assert report["total"]["events"] == 2
    assert report["from"] == "1970-01-01T00:00:00Z"


def test_imported_machine_rows_are_in_all_computers_report_and_corrections_invalidate(
    tmp_path,
):
    from spend_app.sync import apply_sync_push

    settings = make_settings(tmp_path / "machines.db")
    initialize(settings.database_path)
    now = datetime(2026, 11, 13, 15, tzinfo=UTC)
    body = {
        "usageEvents": [
            {
                "raw_id": "event-1",
                "source": "codex_local",
                "tool_key": "codex",
                "model_key": "gpt-5.6-sol",
                "occurred_at": "2026-11-13T14:00:00Z",
                "input_tokens": 1000,
                "cached_input_tokens": 100,
                "cache_write_tokens": 0,
                "output_tokens": 20,
                "ingested_at": "2026-11-13T14:10:00Z",
            }
        ],
        "unpricedEvents": [],
        "sessions": [],
        "quotas": [],
        "machine": {"name": "Synthetic laptop", "os": "test"},
    }
    apply_sync_push(
        settings.database_path, PRICING, machine_id="machine-test", body=body, now=now
    )
    all_report = usage_report(settings, PRICING, now=now, period="all", machine="all")
    per_machine = usage_report(
        settings, PRICING, now=now, period="all", machine="machine-test"
    )
    assert all_report["scope"] == "all-computers"
    assert all_report["total"]["events"] == 1
    assert per_machine["total"]["events"] == 1
    assert any(machine["id"] == "machine-test" for machine in all_report["machines"])

    corrected = {
        **body,
        "usageEvents": [{**body["usageEvents"][0], "model_key": "gpt-5.6-mini"}],
    }
    apply_sync_push(
        settings.database_path,
        PRICING,
        machine_id="machine-test",
        body=corrected,
        now=now,
    )
    revised = usage_report(settings, PRICING, now=now, period="all", machine="all")
    assert revised["tools"][0]["models"][0]["key"] == "gpt-5.6-mini"
    assert revised["total"]["valueUsd"] != all_report["total"]["valueUsd"]


def test_usage_api_hub_proxy_uses_machine_bearer_and_spoke_fallback_is_labeled(
    tmp_path, monkeypatch
):
    from spend_app import sync
    from spend_app.setup_wizard import read_setup_state, write_setup_state

    hub_settings = make_settings(tmp_path / "hub.db")
    spoke_settings = make_settings(tmp_path / "spoke.db")
    initialize(hub_settings.database_path)
    initialize(spoke_settings.database_path)
    now = datetime.now(UTC)
    with connect(hub_settings.database_path) as connection:
        _priced(connection, raw_id="hub-local", occurred_at=now - timedelta(minutes=1))
    token = sync.register_machine(
        hub_settings.database_path, name="Spoke", os_name="test"
    )["token"]
    hub_client = TestClient(create_app(hub_settings, enable_scheduler=False))
    spoke_client = TestClient(create_app(spoke_settings, enable_scheduler=False))
    with connect(spoke_settings.database_path) as connection:
        state = read_setup_state(connection)
        state["role"] = "spoke"
        state["spokeConfig"] = {
            "hubUrl": "https://hub.invalid",
            "token": token,
            "enabled": True,
        }
        state["account"] = {"machineId": "machine-test"}
        write_setup_state(connection, state)

    class RecordingHub:
        def __init__(self):
            self.headers = []

        def get(self, path, *, params, headers):
            self.headers.append(headers)
            return hub_client.get(path, params=params, headers=headers)

    recording_hub = RecordingHub()
    monkeypatch.setattr(
        sync, "_HUB_TEST_CLIENTS", {"https://hub.invalid": recording_hub}
    )
    sync.clear_hub_proxy_cache()
    response = spoke_client.get(
        "/api/spend/usage", params={"period": "all", "machine": "all"}
    )
    assert response.status_code == 200, response.text
    assert recording_hub.headers == [{"Authorization": f"Bearer {token}"}]
    assert response.json()["scope"] == "all-computers"
    assert response.json()["total"]["events"] == 1

    class OfflineHub:
        def get(self, *args, **kwargs):
            raise OSError("synthetic offline")

    monkeypatch.setattr(
        sync, "_HUB_TEST_CLIENTS", {"https://hub.invalid": OfflineHub()}
    )
    sync.clear_hub_proxy_cache()
    with connect(spoke_settings.database_path) as connection:
        _priced(
            connection, raw_id="spoke-local", occurred_at=now - timedelta(minutes=1)
        )
    fallback = spoke_client.get(
        "/api/spend/usage", params={"period": "all", "machine": "all"}
    )
    assert fallback.status_code == 200, fallback.text
    assert fallback.json()["scope"] == "local-fallback"
    assert "this computer" in fallback.json()["warning"]
    assert fallback.json()["total"]["events"] == 1
    sync.clear_hub_proxy_cache()
    hub_client.close()
    spoke_client.close()


def test_pairing_code_fallback_uses_own_id_and_excludes_stale_hub_rows(
    tmp_path, monkeypatch
):
    from spend_app import sync
    from spend_app.api import create_app
    from spend_app.db import connect, initialize
    from spend_app.setup_wizard import read_setup_state

    hub_settings = make_settings(tmp_path / "pair-hub.db")
    spoke_settings = make_settings(tmp_path / "pair-spoke.db")
    initialize(hub_settings.database_path)
    initialize(spoke_settings.database_path)
    hub_app = create_app(hub_settings, enable_scheduler=False)
    spoke_app = create_app(spoke_settings, enable_scheduler=False)
    hub_client, spoke_client = TestClient(hub_app), TestClient(spoke_app)
    headers = {"X-BURNRATE-Request": "1"}
    hub_url = "https://pairing-hub.invalid"
    try:
        paired = hub_client.post(
            "/api/setup/pair-hub",
            headers=headers,
            json={"hubUrl": hub_url, "name": "Travel computer"},
        )
        assert paired.status_code == 200, paired.text
        paired_id = paired.json()["machine"]["id"]
        code = paired.json()["pairingCode"]

        now = datetime.now(UTC)
        with connect(spoke_settings.database_path) as connection:
            _priced(
                connection,
                raw_id="my-local-event",
                occurred_at=now - timedelta(minutes=10),
            )
        # This imported row is stale data left from when this DB was a Hub.
        apply_sync = sync.apply_sync_push
        apply_sync(
            spoke_settings.database_path,
            PRICING,
            machine_id="old-paired-machine",
            body={
                "usageEvents": [
                    {
                        "raw_id": "retained-hub-copy",
                        "source": "codex_local",
                        "tool_key": "codex",
                        "model_key": "gpt-5.6-sol",
                        "occurred_at": _stamp(now - timedelta(minutes=8)),
                        "input_tokens": 2_000,
                        "cached_input_tokens": 0,
                        "cache_write_tokens": 0,
                        "output_tokens": 20,
                        "ingested_at": _stamp(now - timedelta(minutes=8)),
                    }
                ],
                "unpricedEvents": [],
                "sessions": [],
                "quotas": [],
            },
            now=now,
        )
        connected = spoke_client.post(
            "/api/setup/connect-spoke",
            headers=headers,
            json={
                "pairingCode": code,
                "computerName": "Travel computer",
                "testSync": False,
            },
        )
        assert connected.status_code == 200, connected.text
        with connect(spoke_settings.database_path) as connection:
            setup = read_setup_state(connection)
        assert setup["account"]["machineId"] == paired_id

        class OfflineHub:
            def get(self, *args, **kwargs):
                raise OSError("synthetic hub outage")

        monkeypatch.setattr(sync, "_HUB_TEST_CLIENTS", {hub_url: OfflineHub()})
        sync.clear_hub_proxy_cache()
        own = spoke_client.get(
            "/api/spend/usage", params={"period": "all", "machine": paired_id}
        )
        assert own.status_code == 200, own.text
        assert own.json()["scope"] == "local-fallback"
        assert own.json()["total"]["events"] == 1
        assert [machine["id"] for machine in own.json()["machines"]] == [paired_id]

        all_local = spoke_client.get(
            "/api/spend/usage", params={"period": "all", "machine": "all"}
        )
        assert all_local.status_code == 200, all_local.text
        assert all_local.json()["scope"] == "local-fallback"
        assert all_local.json()["total"]["events"] == 1
        assert all_local.json()["total"]["input"] == 800

        hub_local = spoke_client.get(
            "/api/spend/usage", params={"period": "all", "machine": "local"}
        )
        assert hub_local.status_code == 200, hub_local.text
        assert hub_local.json()["scope"] == "unavailable"
        assert hub_local.json()["total"]["events"] == 0

        stale_remote = spoke_client.get(
            "/api/spend/usage",
            params={"period": "all", "machine": "old-paired-machine"},
        )
        assert stale_remote.status_code == 200, stale_remote.text
        assert stale_remote.json()["scope"] == "unavailable"
        assert stale_remote.json()["total"]["events"] == 0
    finally:
        sync.clear_hub_proxy_cache()
        hub_client.close()
        spoke_client.close()


def test_configured_br_m_admin_token_precedes_machine_token_validation(
    tmp_path, monkeypatch
):
    from spend_app.sync import register_machine, revoke_machine

    admin_token = "br_m_admin-custom"
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", admin_token)
    settings = make_settings(tmp_path / "configured-machine-prefix-token.db")
    initialize(settings.database_path)
    registration = register_machine(
        settings.database_path, name="Synthetic peer", os_name="test"
    )
    client = TestClient(create_app(settings, enable_scheduler=False))
    endpoint = "/api/spend/usage?period=all&machine=all"
    try:
        admin = client.get(endpoint, headers={"Authorization": f"Bearer {admin_token}"})
        assert admin.status_code == 200, admin.text

        fabricated = client.get(
            endpoint,
            headers={"Authorization": "Bearer br_m_unregistered-test"},
        )
        assert fabricated.status_code == 401

        valid = client.get(
            endpoint, headers={"Authorization": f"Bearer {registration['token']}"}
        )
        assert valid.status_code == 200, valid.text

        revoke_machine(settings.database_path, machine_id=registration["machine"]["id"])
        revoked = client.get(
            endpoint, headers={"Authorization": f"Bearer {registration['token']}"}
        )
        assert revoked.status_code == 401
    finally:
        client.close()
