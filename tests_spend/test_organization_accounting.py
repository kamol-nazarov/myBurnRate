"""Organization usage and billed charges with synthetic provider transports."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from spend_app.adapters.openai_admin import ingest
from spend_app.aggregate import _cost_buckets, aggregate_health, usage_projection
from spend_app.db import (
    ProviderCostBucket,
    UsageEvent,
    connect,
    initialize,
    upsert_cost_bucket,
    upsert_usage_event,
)
from spend_app.native_usage import usage_report
from spend_app.organization import organization_scope, scoped_raw_id
from spend_app.pricing import PricingEngine
from spend_app.sync import (
    apply_sync_push,
    export_sync_batch,
    read_sync_cursor,
    sync_once,
)
from spend_app.timeutil import iso_utc
from tests_spend.test_api import make_settings

START = datetime(2026, 11, 14, 12, tzinfo=UTC)
END = START + timedelta(hours=1)
MODEL = "gpt-6-astra"


def _openai_bucket(*, input_tokens=300_000, requests=3, model=MODEL):
    result = {
        "input_tokens": input_tokens,
        "output_tokens": 0,
        "model": model,
        "service_tier": "default",
    }
    if requests is not None:
        result["num_model_requests"] = requests
    return {
        "data": [
            {
                "start_time": int(START.timestamp()),
                "end_time": int(END.timestamp()),
                "results": [result],
            }
        ],
        "has_more": False,
    }


@pytest.mark.parametrize(
    "tokens,requests,expected",
    [(300_000, 3, None), (300_000, None, None), (300_000, 1, 6), (272_000, 3, 2.72)],
)
def test_request_tier_from_admin_bucket(tmp_path, tokens, requests, expected):
    settings = make_settings(tmp_path / "org.db")
    pricing = PricingEngine.load(settings.pricing_path)
    payload = _openai_bucket(input_tokens=tokens, requests=requests)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        outcome = ingest(
            database_path=settings.database_path,
            pricing=pricing,
            admin_key="synthetic",
            start=START,
            end=END,
            client=client,
        )

    with connect(settings.database_path) as connection:
        (projected,) = usage_projection(connection, pricing, START, END)
        record = connection.execute(
            "SELECT computed_cost_usd, is_exact FROM usage_events"
        ).fetchone()
    org = usage_report(settings, pricing, now=END)["organizationTotal"]
    assert org["tokens"] == projected["input_tokens"] == tokens
    assert org["valueUsd"] == expected
    if expected is None:
        assert record is None
        assert projected["spend"] is None
        assert org["unpricedEvents"] == 1
        assert outcome["status"] == "partial"
    else:
        assert tuple(record) == (expected, 1)
        assert outcome["status"] == "success"


def test_legacy_unqualified_bucket_does_not_reprice_as_a_single_request(tmp_path):
    settings = make_settings(tmp_path / "org.db")
    pricing = PricingEngine.load(settings.pricing_path)
    initialize(settings.database_path)
    old_record = dict(
        source="openai_admin",
        tool_key="codex",
        model_key=MODEL,
        occurred_at=START.isoformat(),
        session_id=None,
        project=None,
        input_tokens=300_000,
        cached_input_tokens=0,
        cache_write_tokens=0,
        cache_write_1h_tokens=0,
        output_tokens=0,
        reasoning_tokens=None,
        cost_usd=None,
        computed_cost_usd=6,
        raw_id="old-bucket",
        ingested_at=START.isoformat(),
        is_exact=True,
    )
    with connect(settings.database_path) as connection:
        upsert_usage_event(connection, UsageEvent(**old_record))
    assert (
        usage_report(settings, pricing, now=END)["organizationTotal"]["valueUsd"]
        is None
    )
    three_requests = (
        pricing.compute(
            model_key=MODEL,
            occurred_at=START,
            input_tokens=100_000,
            cached_input_tokens=0,
            cache_write_tokens=0,
            output_tokens=0,
        )
        * 3
    )
    assert three_requests == 3

    hub = make_settings(tmp_path / "hub.db")
    apply_sync_push(
        hub.database_path,
        pricing,
        machine_id="spoke-test",
        body={"usageEvents": [old_record]},
        now=END,
    )
    with connect(hub.database_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0
        )
    report = usage_report(hub, pricing, now=END)["organizationTotal"]
    assert report["tokens"] == 300_000
    assert report["valueUsd"] is None

    batch, _ = export_sync_batch(settings.database_path, pricing=pricing)
    assert batch["usageEvents"] == []
    (staged,) = batch["unpricedEvents"]
    assert staged["input_tokens"] == 300_000
    assert staged["telemetry_complete"] is False
    assert staged["admin_telemetry_complete"] is True
    assert batch["usageChanges"]["changes"][0]["kind"] == "unpriced"
    _send(hub, pricing, "current-spoke", batch)
    with connect(hub.database_path) as connection:
        assert (
            connection.execute(
                "SELECT telemetry_complete FROM unpriced_usage_events WHERE raw_id='m:current-spoke:old-bucket'"
            ).fetchone()[0]
            == 1
        )


def _billed_amount(amount):
    start = START.replace(hour=0)
    end = start + timedelta(days=1)
    return {
        "data": [
            {
                "start_time": int(start.timestamp()),
                "end_time": int(end.timestamp()),
                "results": [{"amount": {"currency": "usd", "value": amount}}],
            }
        ],
        "has_more": False,
    }


def _collect_org(settings, pricing, *, organization, amount=7.50, tokens=100):
    usage = _openai_bucket(input_tokens=tokens)
    usage["data"][0]["results"][0]["output_tokens"] = 10

    def handler(request):
        snapshot = (
            _billed_amount(amount) if request.url.path.endswith("/costs") else usage
        )
        headers = {"openai-organization": organization} if organization else {}
        # An organization-like body field does not authenticate the source.
        return httpx.Response(
            200, json={**snapshot, "organization_id": "org-untrusted"}, headers=headers
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return ingest(
            database_path=settings.database_path,
            pricing=pricing,
            admin_key="synthetic",
            start=START,
            end=END + timedelta(days=1),
            client=client,
        )


def _send(hub, pricing, machine, payload):
    body = {**payload, "machine": {**payload.get("machine", {}), "name": machine}}
    return apply_sync_push(
        hub.database_path, pricing, machine_id=machine, body=body, now=END
    )


def _charges(settings, machine="all"):
    with connect(settings.database_path) as db:
        return _cost_buckets(
            db,
            START.replace(hour=0),
            START.replace(hour=0) + timedelta(days=1),
            machine,
        )


def test_verified_org_buckets_are_deduplicated_after_machine_selection(tmp_path):
    hub = make_settings(tmp_path / "hub.db")
    pricing = PricingEngine.load(hub.pricing_path)
    one = make_settings(tmp_path / "one.db")
    two = make_settings(tmp_path / "two.db")
    three = make_settings(tmp_path / "three.db")
    four = make_settings(tmp_path / "four.db")
    for settings, org in (
        (one, "org-alpha"),
        (two, "org-alpha"),
        (three, "org-beta"),
        (four, None),
    ):
        _collect_org(settings, pricing, organization=org)
    for settings, machine in ((one, "laptop"), (two, "desktop")):
        batch, _ = export_sync_batch(settings.database_path)
        _send(hub, pricing, machine, batch)
        _send(hub, pricing, machine, batch)  # duplicate network delivery

    report = usage_report(hub, pricing, now=END)
    assert report["organizationTotal"]["tokens"] == 110
    assert report["organizationTotal"]["events"] == 1
    assert report["organizationScopeUnknown"] == 0
    assert report["total"]["tokens"] == 0
    for machine in ("laptop", "desktop"):
        report = usage_report(hub, pricing, now=END, machine=machine)
        assert report["organizationTotal"]["tokens"] == 110
        assert _charges(hub, machine) == {"openai_admin": Decimal("7.50")}
    assert _charges(hub) == {"openai_admin": Decimal("7.50")}

    batch, _ = export_sync_batch(three.database_path)
    _send(hub, pricing, "second-org", batch)
    assert usage_report(hub, pricing, now=END)["organizationTotal"]["tokens"] == 220
    assert _charges(hub) == {"openai_admin": Decimal("15.00")}
    batch, _ = export_sync_batch(four.database_path)
    _send(hub, pricing, "legacy", batch)
    report = usage_report(hub, pricing, now=END)
    assert report["organizationTotal"]["tokens"] == 330
    assert report["organizationScopeUnknown"] == 1
    assert "may overlap" in report["organizationWarning"]
    with connect(hub.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 4
        assert len(usage_projection(db, pricing, START, END)) == 3
    assert "providerCosts" not in export_sync_batch(hub.database_path)[0]


def test_cost_correction_replaces_replays_across_origins_and_keeps_other_costs(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("spend_app.adapters.common.utc_now", lambda: iso_utc(START))
    spoke = make_settings(tmp_path / "spoke.db")
    other = make_settings(tmp_path / "other.db")
    hub = make_settings(tmp_path / "hub.db")
    pricing = PricingEngine.load(spoke.pricing_path)
    _collect_org(spoke, pricing, organization="org-alpha")
    _collect_org(other, pricing, organization="org-alpha")
    snapshot, cursor = export_sync_batch(spoke.database_path)
    other_snapshot, _ = export_sync_batch(other.database_path)
    _send(hub, pricing, "laptop", snapshot)
    _send(hub, pricing, "desktop", other_snapshot)
    # Re-reading the unchanged report advances no cost cursor.
    _collect_org(spoke, pricing, organization="org-alpha")
    assert (
        "providerCosts" not in export_sync_batch(spoke.database_path, cursor=cursor)[0]
    )
    monkeypatch.setattr(
        "spend_app.adapters.common.utc_now", lambda: iso_utc(START - timedelta(hours=1))
    )
    _collect_org(spoke, pricing, organization="org-alpha", amount=9.25)
    correction, next_cursor = export_sync_batch(spoke.database_path, cursor=cursor)
    assert next_cursor["provider_cost_seq"] > cursor["provider_cost_seq"]
    _send(hub, pricing, "laptop", correction)
    _send(hub, pricing, "laptop", snapshot)
    _send(hub, pricing, "desktop", other_snapshot)
    assert _charges(hub) == {"openai_admin": Decimal("9.25")}
    assert _charges(hub, "desktop") == {"openai_admin": Decimal("7.50")}
    assert _charges(hub, "laptop") == {"openai_admin": Decimal("9.25")}


def test_billed_charge_remains_known_when_reference_price_is_uncertain(tmp_path):
    settings = make_settings(tmp_path / "org.db")
    pricing = PricingEngine.load(settings.pricing_path)
    _collect_org(
        settings, pricing, organization="org-alpha", amount=7.5, tokens=300_000
    )
    assert _charges(settings) == {"openai_admin": Decimal("7.50")}
    assert (
        usage_report(settings, pricing, now=END)["organizationTotal"]["valueUsd"]
        is None
    )
    health = aggregate_health(
        database_path=settings.database_path, pricing=pricing, now=END
    )
    assert health["providerVsComputedVariancePct"] is None


def test_charge_variance_excludes_usage_for_an_unbilled_organization(tmp_path):
    settings = make_settings(tmp_path / "org.db")
    pricing = PricingEngine.load(settings.pricing_path)
    _collect_org(settings, pricing, organization="org-alpha", amount=7.5)
    health = aggregate_health(
        database_path=settings.database_path, pricing=pricing, now=END
    )
    assert health["providerVsComputedVariancePct"] == pytest.approx(99.98)
    _collect_org(
        settings, pricing, organization="org-beta", amount=None, tokens=200_000
    )
    health = aggregate_health(
        database_path=settings.database_path, pricing=pricing, now=END
    )
    assert health["providerVsComputedVariancePct"] == pytest.approx(99.98)


@pytest.mark.parametrize(
    "uncached,writes,expected", [(99_000, 1_000, 0.010025), (120_000, 10_000, None)]
)
def test_anthropic_bucket_preserves_cache_writes_and_optional_org_identity(
    tmp_path, uncached, writes, expected
):
    from spend_app.adapters.anthropic_admin import ingest as ingest_anthropic

    settings = make_settings(tmp_path / "anthropic.db")
    pricing = PricingEngine.load(settings.pricing_path)
    provider_id = "c457aaf4-e90c-4293-843c-ff6442c13172"

    def handler(request):
        if request.url.path.endswith("/organizations/me"):
            return httpx.Response(200, json={"id": provider_id, "type": "organization"})
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "starting_at": iso_utc(START),
                        "ending_at": iso_utc(END),
                        "results": [
                            {
                                "model": "claude-haiku-5-5",
                                "uncached_input_tokens": uncached,
                                "cache_creation": {"ephemeral_5m_input_tokens": writes},
                                "context_window": "0-200k",
                            }
                        ],
                    }
                ],
                "has_more": False,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        ingest_anthropic(
            database_path=settings.database_path,
            pricing=pricing,
            admin_key="synthetic",
            start=START,
            end=END,
            client=client,
        )
    payload = usage_report(settings, pricing, now=END)
    report = payload["organizationTotal"]
    assert report["tokens"] == uncached + writes
    assert report["writes"] == writes
    assert report["valueUsd"] == expected
    assert payload["organizationScopeUnknown"] == 0


def test_provider_cost_cursor_retries_legacy_ack_and_interrupted_batches(tmp_path):
    spoke = make_settings(tmp_path / "spoke.db")
    hub = make_settings(tmp_path / "hub.db")
    pricing = PricingEngine.load(spoke.pricing_path)
    scope = organization_scope("openai_admin", "org-alpha")
    start = START.replace(hour=0)
    amount = ProviderCostBucket(
        source="openai_admin",
        raw_id=scoped_raw_id("openai-cost:" + "a" * 64, scope),
        starting_at=iso_utc(start),
        ending_at=iso_utc(start + timedelta(days=1)),
        project_id=None,
        line_item=None,
        model_key=None,
        cost_usd=7.5,
        ingested_at=iso_utc(START),
        organization_scope=scope,
    )
    initialize(spoke.database_path)
    with connect(spoke.database_path) as db:
        upsert_cost_bucket(db, amount)
        upsert_cost_bucket(
            db, replace(amount, raw_id=scoped_raw_id("openai-cost:" + "b" * 64, scope))
        )

    url = "https://synthetic.invalid"

    class Transport:
        legacy = True
        fail_next = False

        def post(self, endpoint, json, headers):
            if self.fail_next:
                self.fail_next = False
                raise httpx.ReadError("synthetic interrupted stream")
            payload = dict(json)
            if self.legacy:
                payload.pop("providerCosts", None)
            result = _send(hub, pricing, "spoke", payload)
            return SimpleNamespace(status_code=200, json=lambda: result)

    transport = Transport()
    result = sync_once(
        spoke,
        pricing,
        hub_url=url,
        token="synthetic",
        client=transport,
        collect_local=False,
    )
    assert result["providerCostsPending"] is True
    assert read_sync_cursor(spoke.database_path, url).get("provider_cost_seq", 0) == 0

    # Save only the first acknowledged record, then simulate loss of the next
    # network request. The normal CLI entry point resumes from that position.
    first, cursor = export_sync_batch(spoke.database_path, limit=1)
    assert first["providerCosts"]["hasMore"] is True
    _send(hub, pricing, "spoke", first)
    from spend_app.sync import write_sync_cursor

    write_sync_cursor(spoke.database_path, url, cursor)
    transport.legacy = False
    transport.fail_next = True
    with pytest.raises(httpx.ReadError, match="interrupted"):
        sync_once(
            spoke,
            pricing,
            hub_url=url,
            token="synthetic",
            client=transport,
            collect_local=False,
        )
    assert (
        read_sync_cursor(spoke.database_path, url)["provider_cost_seq"]
        == cursor["provider_cost_seq"]
    )
    final = sync_once(
        spoke,
        pricing,
        hub_url=url,
        token="synthetic",
        client=transport,
        collect_local=False,
    )
    assert final["cursor"]["provider_cost_seq"] > cursor["provider_cost_seq"]
    assert _charges(hub) == {"openai_admin": Decimal("15.00")}
