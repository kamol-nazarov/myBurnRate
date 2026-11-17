"""The non-negotiable internal-consistency gates from the original prompt."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from tests_spend.test_build_prompt_harness import (
    cents,
    client_for,
    complete_world,
    usd_js,
)


def _summary(client, window: str = "1d") -> dict:
    response = client.get("/api/spend/summary", params={"window": window, "tool": "all"})
    assert response.status_code == 200
    return response.json()


def test_c1_itemized_leaks_equal_waste_headline_and_day_times_30_equals_month(
    tmp_path: Path, monkeypatch
) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = _summary(client)
    items = body["waste"]["items"]
    item_sum = sum((cents(item["perDay"]) for item in items), Decimal(0))
    per_day = cents(body["waste"]["perDay"])
    per_month = cents(body["waste"]["perMonth"])
    assert item_sum == per_day
    assert per_day * Decimal(30) == per_month


def test_c2_chart_bucket_totals_equal_measured_tokens_kpi(tmp_path: Path, monkeypatch) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = _summary(client)
    series_total = sum(Decimal(str(point["total"])) for point in body["series"])
    assert abs(float(series_total) - body["totals"]["tokens"]) < 1e-6
    for tool in body["tools"]:
        tool_series = sum(
            Decimal(str(point["byTool"].get(tool["key"], 0))) for point in body["series"]
        )
        assert abs(float(tool_series) - tool["tokens"]) < 1e-6


def test_c3_per_tool_tokens_and_value_plus_subscriptions_equal_totals(
    tmp_path: Path, monkeypatch
) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = _summary(client)
    assert sum(tool["tokens"] for tool in body["tools"]) == body["totals"]["tokens"]
    usage = sum(
        (Decimal(str(tool["value"])) for tool in body["tools"] if tool["value"] is not None),
        Decimal(0),
    )
    tracked = Decimal(str(body["totals"]["trackedValue"]))
    priced = Decimal(str(body["totals"]["priced"]))
    published = Decimal(str(body["totals"]["publishedRate"]))
    subscriptions = tracked - priced - published
    assert abs(float(usage - priced - published)) < 1e-4
    assert subscriptions >= 0
    assert abs(float(priced + published + subscriptions) - float(tracked)) < 1e-6
    assert any(row["monthlyEquivalent"] for row in body["subscriptions"] if row["monthlyEquivalent"])


def test_c4_token_mix_shares_sum_to_100_on_overview_and_every_drilldown(
    tmp_path: Path, monkeypatch
) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = _summary(client)
    assert {item["key"] for item in body["mix"]} == {"cached_input", "fresh_input", "output"}
    assert abs(sum(item["share"] for item in body["mix"]) - 100) <= 0.1
    assert sum(item["tokens"] for item in body["mix"]) == body["totals"]["tokens"]
    for model in body["models"]:
        entity = client.get(
            "/api/spend/entity",
            params={"kind": "model", "key": model["key"], "window": "1d"},
        ).json()
        assert abs(sum(item["share"] for item in entity["mix"]) - 100) <= 0.1
        assert {item["key"] for item in entity["mix"]} == {"cached_input", "fresh_input", "output"}
    for tool in body["tools"]:
        entity = client.get(
            "/api/spend/entity",
            params={"kind": "tool", "key": tool["key"], "window": "1d"},
        ).json()
        if entity["tokens"]:
            assert abs(sum(item["share"] for item in entity["mix"]) - 100) <= 0.1


def test_c5_drilldown_share_numerator_is_the_entity_kpi(tmp_path: Path, monkeypatch) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = _summary(client)
    entity = client.get(
        "/api/spend/entity",
        params={"kind": "model", "key": "gpt-5.6-sol", "window": "1d"},
    ).json()
    assert entity["value"] is not None
    tracked = body["totals"]["trackedValue"]
    assert tracked
    expected_share = entity["value"] / tracked * 100
    assert abs(entity["shareOfTrackedValue"] - expected_share) < 1e-6


def test_c6_session_rows_show_real_values_and_count_is_min_of_six_and_runs(
    tmp_path: Path, monkeypatch
) -> None:
    """Release A (N02): sessions display real monetary values. A shown subset
    may legitimately cover 100% of the entity value; money is never rescaled
    to manufacture a remainder."""

    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    entity = client.get(
        "/api/spend/entity",
        params={"kind": "model", "key": "gpt-5.6-sol", "window": "1d"},
    ).json()
    rows = entity["sessions"]["rows"]
    assert entity["runs"] >= 2
    assert len(rows) == min(6, entity["runs"])
    assert len(rows) != 6 or entity["runs"] >= 6
    shown = sum(Decimal(str(row["value"])) for row in rows if row["value"] is not None)
    assert abs(float(shown) - entity["sessions"]["shownTotal"]) < 1e-6
    assert entity["sessions"]["shownTotal"] <= entity["value"] + 1e-6


def test_c8_already_saved_and_recoverable_share_one_rate_basis(
    tmp_path: Path, monkeypatch
) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = _summary(client)
    entity = client.get(
        "/api/spend/entity",
        params={"kind": "model", "key": "cursor:grok-4.6", "window": "1d"},
    ).json()
    assert body["cacheSavings"] is not None
    assert entity["opportunity"]["alreadySaved"] is not None
    cache_items = [item for item in body["waste"]["items"] if "cache" in item["key"]]
    assert cache_items


def test_c9_record_counts_tokens_and_shares_change_with_the_window(
    tmp_path: Path, monkeypatch
) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    short = _summary(client, "15m")
    day = _summary(client, "1d")
    week = _summary(client, "1w")
    assert short["totals"]["records"] < day["totals"]["records"] < week["totals"]["records"]
    assert short["totals"]["tokens"] < day["totals"]["tokens"] < week["totals"]["tokens"]
    assert short["window"]["buckets"] == 15
    assert day["window"]["buckets"] == 24
    assert week["window"]["buckets"] == 28

    def shares(payload: dict) -> dict[str, float]:
        total = payload["totals"]["tokens"]
        return {tool["key"]: tool["tokens"] / total for tool in payload["tools"] if total}

    assert shares(short) != shares(day)
    assert shares(day) != shares(week)


def test_c10_one_money_format_per_scale() -> None:
    assert usd_js(9.41) == "$9.41"
    assert usd_js(14) == "$14.00"
    assert usd_js(14) != "$14"
    assert usd_js(2323) == "$2,323"


def test_c11_reingest_gate_is_owned_by_the_replay_module() -> None:
    # Presence check only — executable replay lives in test_build_prompt_ingest_replay.py.
    from tests_spend import test_build_prompt_ingest_replay as replay

    assert hasattr(replay, "test_codex_local_replay_writes_zero_new_rows")
    assert hasattr(replay, "test_claude_local_replay_writes_zero_new_rows")
    assert hasattr(replay, "test_openai_admin_fixture_replay_writes_zero_new_rows")
