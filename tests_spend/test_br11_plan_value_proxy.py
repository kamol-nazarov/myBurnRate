"""Verify that a spoke can display an actual hub Plans & Value response."""

from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from spend_app import sync
from spend_app.connection_secrets import publish_spoke_credentials
from spend_app.db import connect, initialize
from spend_app.product_api import product_router
from spend_app.subscriptions import add_subscription
from tests_spend.test_api import make_settings

HUB_ORIGIN = "https://hub.test"
VALUE_PATH = "/api/subscriptions/value"
PERIOD = {"period": "last_month"}


def _client(database):
    initialize(database)
    settings = make_settings(database)
    app = FastAPI()
    app.include_router(product_router(settings))
    return TestClient(app)


def _add_plan(database, amount):
    with connect(database) as connection:
        add_subscription(
            connection,
            tool_key="codex",
            name="Codex plan",
            amount_usd=amount,
            cadence="monthly",
            start_date="2025-01-01",
            end_date=None,
        )


@pytest.fixture
def valuation_pair(tmp_path, monkeypatch):
    hub_database = tmp_path / "hub.db"
    spoke_database = tmp_path / "spoke.db"
    with _client(hub_database) as hub, _client(spoke_database) as spoke:
        monkeypatch.setenv("BURNRATE_HUB_URL", HUB_ORIGIN)

        def pair(_connection, state):
            state["role"] = "spoke"
            state["spokeConfig"]["enabled"] = True
            state["spokeConfig"]["hubUrl"] = HUB_ORIGIN

        publish_spoke_credentials(
            spoke_database, owner_session="br_s_synthetic_plan_value", apply_state=pair
        )
        sync.clear_hub_proxy_cache()
        yield hub, hub_database, spoke, spoke_database
    sync.clear_hub_proxy_cache()


@pytest.mark.parametrize("hub_has_plan", [True, False], ids=["codex-plan", "empty"])
def test_spoke_displays_hub_valuation(valuation_pair, monkeypatch, hub_has_plan):
    hub, hub_database, spoke, spoke_database = valuation_pair
    if hub_has_plan:
        _add_plan(hub_database, 200)
    else:
        # A valid empty hub report must take precedence over a local plan.
        _add_plan(spoke_database, 50)
    forwarded = []

    def fetch(path, *, params, headers):
        assert path == VALUE_PATH
        assert params == PERIOD
        assert headers["Authorization"] == "Bearer br_s_synthetic_plan_value"
        response = hub.get(path, params=params, headers=headers)
        forwarded.append(response.json())
        return response

    monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_ORIGIN, SimpleNamespace(get=fetch))
    response = spoke.get(VALUE_PATH, params=PERIOD)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert [response.json()] == forwarded
    if hub_has_plan:
        (group,) = response.json()["groups"]
        assert group["groupId"] == "tool:codex"
        assert Decimal(group["configuredCostUsd"]).quantize(Decimal("0.01")) == 200
    else:
        assert response.json()["groups"] == []


@pytest.mark.parametrize(
    "hub_payload",
    [
        {"plans": []},
        {"period": "last_month", "groups": {}, "unassigned": {}},
        {"period": "last_month", "groups": [], "unassigned": []},
        {"period": "this_month", "groups": [], "unassigned": {}},
        None,
    ],
    ids=[
        "plan-list",
        "invalid-groups",
        "invalid-unassigned",
        "wrong-period",
        "offline",
    ],
)
def test_spoke_falls_back_for_invalid_or_unavailable_valuation(
    valuation_pair, monkeypatch, hub_payload
):
    _, _, spoke, spoke_database = valuation_pair
    _add_plan(spoke_database, 50)

    def fetch(*args, **kwargs):
        if hub_payload is None:
            raise httpx.ConnectError("synthetic disconnection")
        return httpx.Response(200, json=hub_payload)

    monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_ORIGIN, SimpleNamespace(get=fetch))
    response = spoke.get(VALUE_PATH, params=PERIOD)
    assert response.status_code == 200
    assert response.json()["period"] == "last_month"
    (group,) = response.json()["groups"]
    assert Decimal(group["configuredCostUsd"]).quantize(Decimal("0.01")) == 50
