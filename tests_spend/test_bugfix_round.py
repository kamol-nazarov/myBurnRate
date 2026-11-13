"""Regressions for accounting and paired-computer failures found in the bug pass."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from spend_app.adapters.common import UsageRow, persist_rows
from spend_app.adapters.local_common import parse_iso_time
from spend_app.db import connect, initialize
from spend_app.plan_service import apply_mutation, list_plans
from spend_app.pricing import PricingEngine
from spend_app.timeutil import parse_utc

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def pricing():
    return PricingEngine.load(ROOT / "pricing")


def _values(**changes):
    return {
        "name": "Example plan",
        "tool_key": "codex",
        "amount_usd": "30",
        "cadence": "monthly",
        "start_date": "2026-11-01",
        "end_date": None,
    } | changes


def _mutation(database, operation, request_id, **fields):
    with connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        return apply_mutation(
            connection,
            {"operation": operation, "request_id": request_id, **fields},
            NOW.date(),
        )


@pytest.mark.parametrize("parser", [parse_utc, parse_iso_time])
@pytest.mark.parametrize(
    "stamp", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"]
)
def test_out_of_range_utc_normalization_is_quarantined(parser, stamp):
    assert parser(stamp) is None


def test_local_iso_parser_treats_zoneless_telemetry_as_utc(monkeypatch):
    # Simulate a non-UTC OS without changing its timezone: a naive datetime's
    # astimezone() must never be reached on the provider timestamp path.
    import spend_app.adapters.local_common as common

    class NonUtcDateTime(datetime):
        def astimezone(self, tz=None):
            if self.tzinfo is None:
                return self.replace(tzinfo=UTC) + timedelta(hours=5)
            return super().astimezone(tz)

    monkeypatch.setattr(common, "datetime", NonUtcDateTime)
    assert common.parse_iso_time("2026-11-13T12:00:00") == NOW


@pytest.mark.parametrize("bad_rate", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_nonfinite_price_cards_are_rejected(tmp_path, bad_rate):
    (tmp_path / "test.yaml").write_text(
        "provider: openai\nexactness: derived\nprices:\n"
        "  - model_key: test\n"
        f"    input_per_mtok: '{bad_rate}'\n"
        "    cached_input_per_mtok: 0\n    cache_write_per_mtok: 0\n"
        "    output_per_mtok: 1\n    effective_from: '2026-01-01T00:00:00Z'\n"
        "    source_url: https://developers.openai.com/api/docs/pricing/\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="input_per_mtok"):
        PricingEngine.load(tmp_path)


def test_explicit_free_one_hour_cache_rate_is_respected(pricing):
    card = replace(
        pricing.resolve("claude-opus-5", NOW),
        aliases=(),
        cache_write_per_mtok=Decimal(10),
        cache_write_1h_per_mtok=Decimal(0),
    )
    engine = PricingEngine([card])
    assert (
        engine.compute(
            model_key=card.model_key,
            occurred_at=NOW,
            input_tokens=0,
            cached_input_tokens=0,
            cache_write_tokens=100,
            cache_write_1h_tokens=100,
            output_tokens=0,
        )
        == 0
    )


@pytest.mark.parametrize(
    "field",
    [
        "input_tokens",
        "cached_input_tokens",
        "cache_write_tokens",
        "cache_write_1h_tokens",
        "output_tokens",
    ],
)
def test_negative_usage_is_quarantined_without_losing_valid_rows(
    tmp_path, pricing, field
):
    good = UsageRow(
        source="test",
        tool_key="codex",
        model_key="gpt-5.6-sol",
        occurred_at=NOW,
        session_id=None,
        project=None,
        input_tokens=100,
        cached_input_tokens=0,
        cache_write_tokens=0,
        cache_write_1h_tokens=0,
        output_tokens=10,
        reasoning_tokens=None,
        cost_usd=None,
        raw_id="valid",
    )
    bad = replace(good, raw_id="invalid", **{field: -1})
    database = tmp_path / "accounting.db"
    result = persist_rows(
        database_path=database, pricing=pricing, source="test", usage_rows=[good, bad]
    )
    assert result["status"] == "partial"
    assert result["quarantined"] == 1
    with connect(database) as connection:
        assert [
            r[0] for r in connection.execute("SELECT raw_id FROM usage_events")
        ] == ["valid"]


def test_correction_preview_uses_old_plan_end_for_before_cost(tmp_path):
    database = tmp_path / "plans.db"
    initialize(database)
    created = _mutation(database, "add", "preview-add", values=_values())
    _mutation(
        database,
        "end",
        "preview-end",
        plan_id=created["id"],
        expected_version=1,
        end_date="2026-11-05",
    )
    with connect(database) as connection:
        term_id = list_plans(connection, NOW.date())["plans"][0]["terms"][0]["id"]
    preview = _mutation(
        database,
        "correct",
        "preview-extend",
        plan_id=created["id"],
        expected_version=2,
        term_id=term_id,
        values=_values(end_date="2026-11-10"),
        preview=True,
    )
    assert Decimal(preview["before_usd"]) == Decimal(5)
    assert Decimal(preview["after_usd"]) == Decimal(10)


@pytest.fixture
def paired_clients(tmp_path, monkeypatch):
    from spend_app import sync
    from spend_app.api import create_app
    from spend_app.setup_wizard import read_setup_state, write_setup_state
    from tests_spend.test_api import make_settings

    hub = make_settings(tmp_path / "hub.db")
    spoke = make_settings(tmp_path / "spoke.db")
    hub_client = TestClient(create_app(hub, enable_scheduler=False))
    spoke_client = TestClient(create_app(spoke, enable_scheduler=False))
    token = sync.register_machine(hub.database_path, name="Laptop", os_name="win32")[
        "token"
    ]
    with connect(spoke.database_path) as connection:
        state = read_setup_state(connection)
        state["role"] = "spoke"
        state["spokeConfig"] = {
            "hubUrl": "https://hub.example.invalid",
            "token": token,
            "enabled": True,
        }
        write_setup_state(connection, state)
    monkeypatch.setattr(
        sync, "_HUB_TEST_CLIENTS", {"https://hub.example.invalid": hub_client}
    )
    sync.clear_hub_proxy_cache()
    yield hub, spoke, hub_client, spoke_client
    sync.clear_hub_proxy_cache()
    hub_client.close()
    spoke_client.close()


def _post(client, body):
    return client.post(
        "/api/subscriptions",
        headers={
            "Origin": "http://127.0.0.1",
            "Host": "127.0.0.1",
            "X-BURNRATE-Request": "1",
        },
        json=body,
    )


def test_spoke_edits_hub_only_plan_without_local_identity(paired_clients):
    hub, spoke, _, client = paired_clients
    plan = _mutation(hub.database_path, "add", "hub-only-add", values=_values())
    result = _post(
        client,
        {
            "operation": "end",
            "request_id": "hub-only-end",
            "plan_id": plan["id"],
            "expected_version": 1,
            "end_date": "2026-11-10",
        },
    )
    assert result.status_code == 200, result.text
    assert result.json() == {"id": plan["id"], "version": 2}
    with connect(hub.database_path) as connection:
        assert (
            list_plans(connection, NOW.date())["plans"][0]["end_date"] == "2026-11-10"
        )
    with connect(spoke.database_path) as connection:
        assert list_plans(connection, NOW.date())["plans"] == []


def test_spoke_add_returns_hub_identity_without_shadow_plan(paired_clients):
    hub, spoke, _, client = paired_clients
    _mutation(hub.database_path, "add", "hub-existing", values=_values())
    result = _post(
        client,
        {
            "operation": "add",
            "request_id": "spoke-add",
            "values": _values(name="Second"),
        },
    )
    assert result.status_code == 200, result.text
    assert result.json() == {"id": 2, "version": 1}
    with connect(spoke.database_path) as connection:
        assert list_plans(connection, NOW.date())["plans"] == []


def test_spoke_preserves_hub_conflict(paired_clients):
    hub, spoke, _, client = paired_clients
    plan = _mutation(hub.database_path, "add", "hub-conflict", values=_values())
    _mutation(
        hub.database_path,
        "end",
        "hub-new-version",
        plan_id=plan["id"],
        expected_version=1,
        end_date="2026-11-05",
    )
    _mutation(spoke.database_path, "add", "local-conflict", values=_values())
    result = _post(
        client,
        {
            "operation": "end",
            "request_id": "spoke-conflict",
            "plan_id": plan["id"],
            "expected_version": 1,
            "end_date": "2026-11-10",
        },
    )
    assert result.status_code == 409, result.text
    with connect(spoke.database_path) as connection:
        assert list_plans(connection, NOW.date())["plans"][0]["end_date"] is None


def test_spoke_offline_save_fails_without_committing_locally(
    paired_clients, monkeypatch
):
    _, spoke, _, client = paired_clients
    from spend_app import sync

    class OfflineHub:
        def post(self, *args, **kwargs):
            raise OSError("Offline")

    monkeypatch.setattr(
        sync, "_HUB_TEST_CLIENTS", {"https://hub.example.invalid": OfflineHub()}
    )
    result = _post(
        client, {"operation": "add", "request_id": "spoke-offline", "values": _values()}
    )
    assert result.status_code == 503, result.text
    with connect(spoke.database_path) as connection:
        assert list_plans(connection, NOW.date())["plans"] == []


def test_spoke_correction_preview_comes_from_hub(paired_clients):
    hub, _, _, client = paired_clients
    plan = _mutation(hub.database_path, "add", "hub-preview", values=_values())
    with connect(hub.database_path) as connection:
        term_id = list_plans(connection, NOW.date())["plans"][0]["terms"][0]["id"]
    body = {
        "operation": "correct",
        "request_id": "spoke-preview",
        "plan_id": plan["id"],
        "expected_version": 1,
        "term_id": term_id,
        "values": _values(amount_usd="60"),
        "preview": True,
    }
    result = _post(client, body)
    assert result.status_code == 200, result.text
    preview = result.json()
    assert "preview_token" in preview
    confirmed = _post(
        client,
        {
            **body,
            "request_id": "spoke-confirm",
            "preview": False,
            "confirmed": True,
            "preview_token": preview["preview_token"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json() == {"id": plan["id"], "version": 2}


@pytest.mark.parametrize("first_month", [1, 4, 7, 10])
def test_quarterly_plan_comparison_accrues_exact_calendar_quarter(first_month):
    from spend_app.plan_math import PlanTerms, compare

    start = datetime(2026, first_month, 1, tzinfo=UTC)
    end = (
        datetime(2026, first_month + 3, 1, tzinfo=UTC)
        if first_month < 10
        else datetime(2027, 1, 1, tzinfo=UTC)
    )
    result = compare(
        plans=[
            PlanTerms("codex", "Quarterly", Decimal(300), "quarterly", start.date())
        ],
        window=(start, end),
        zone=ZoneInfo("UTC"),
        usage_value=Decimal(300),
        usage_complete=True,
    )
    assert abs(result.accrued_cost - Decimal(300)) < Decimal("1e-23")


def test_ingest_retention_keeps_last_success_during_persistent_failure(tmp_path):
    from spend_app.db import prune_ingest_runs

    database = tmp_path / "retention.db"
    initialize(database)
    with connect(database) as connection:
        for day, status in [(1, "success"), (2, "failed"), (3, "failed")]:
            stamp = f"2026-11-{day:02}T00:00:00Z"
            connection.execute(
                "INSERT INTO ingest_runs(source,started_at,finished_at,status,events_written) VALUES('test',?,?,?,0)",
                (stamp, stamp, status),
            )
        prune_ingest_runs(connection, now="2026-11-13T12:00:00Z", keep_per_source=1)
        assert [
            r[0]
            for r in connection.execute("SELECT status FROM ingest_runs ORDER BY id")
        ] == ["success", "failed"]


def test_hud_settled_model_correction_moves_tokens_to_the_right_meter(tmp_path):
    from spend_app import aggregate
    from spend_app.hud_data import window_burn
    from tests_spend.test_hud_window_tokens import event, quota, settings_for

    aggregate.reset_memo()
    settings = settings_for(tmp_path)
    now = datetime(2026, 10, 20, 18, 30, tzinfo=UTC)
    capacity = [
        {
            "providerKey": "antigravity",
            "rows": [
                {"limitKey": key, "resetsAt": (now + timedelta(days=2)).isoformat()}
                for key in ("gemini-weekly", "3p-weekly")
            ],
        }
    ]
    with connect(settings.database_path) as connection:
        quota(connection, "antigravity", "gemini-weekly", now + timedelta(days=2))
        event(
            connection,
            "corrected",
            "antigravity",
            "antigravity:claude-test",
            now - timedelta(hours=3),
            fresh=100,
        )
        first = window_burn(
            connection, capacity, now=now, today_start=now.replace(hour=0, minute=0)
        )
        connection.execute(
            "UPDATE usage_events SET model_key='antigravity:gemini-test' WHERE raw_id='corrected'"
        )
        second = window_burn(
            connection, capacity, now=now, today_start=now.replace(hour=0, minute=0)
        )
    assert first["antigravity"]["gemini-weekly"]["tokens"] == 0
    assert second["antigravity"]["gemini-weekly"]["tokens"] == 100
    assert second["antigravity"]["3p-weekly"]["tokens"] == 0
