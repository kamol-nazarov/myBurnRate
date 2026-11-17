"""Null vs zero and ≈ placement from the original prompt."""

from __future__ import annotations

from pathlib import Path

from tests_spend.test_build_prompt_harness import (
    CONTRACT,
    client_for,
    complete_world,
    gap_world,
)


def test_unpriced_model_is_none_never_zero_or_guessed(tmp_path: Path, monkeypatch) -> None:
    database, _pricing = gap_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = client.get("/api/spend/summary", params={"window": "1d"}).json()
    unlisted = next(model for model in body["models"] if model["key"] == "opencode:unlisted-model")
    assert unlisted["tokens"] == 1_050
    assert unlisted["value"] is None
    assert unlisted["value"] != 0
    assert body["totals"]["trackedValue"] is None
    health = client.get("/api/spend/health").json()
    assert "opencode:unlisted-model" in health["pricingGaps"]


def test_unavailable_quota_is_not_a_fake_zero_percent(tmp_path: Path, monkeypatch) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = client.get("/api/spend/summary", params={"window": "1d"}).json()
    assert not any(card["providerKey"] == "cursor" for card in body["capacity"])
    openrouter = next(card for card in body["capacity"] if card["providerKey"] == "openrouter")
    other = openrouter["rows"][0]
    assert other["pct"] is None
    assert other["used"] != 0 or other["allowance"] is None
    assert other["allowance"] is None
    assert other["pct"] != 0
    assert openrouter["isPayg"] is True


def test_payg_openrouter_never_reports_a_zero_quota_bar(tmp_path: Path, monkeypatch) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = client.get("/api/spend/summary", params={"window": "1d"}).json()
    openrouter = next(card for card in body["capacity"] if card["providerKey"] == "openrouter")
    assert openrouter["isPayg"] is True
    assert openrouter["peakPct"] is None
    assert openrouter["peakPct"] != 0
    row = openrouter["rows"][0]
    assert row["pct"] is None
    assert row["allowance"] is None
    assert openrouter["monthToDateUsd"] is not None


def test_approx_marker_is_exactly_the_published_rate_set() -> None:
    exact = set(CONTRACT["exactTools"])
    derived = set(CONTRACT["derivedTools"])
    assert exact == {"codex", "claude-code"}
    assert derived == {"cursor", "opencode", "grok", "openrouter"}


def test_factual_zero_quota_stays_zero_and_is_distinct_from_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    database, _pricing = complete_world(tmp_path)
    client = client_for(database, monkeypatch)
    body = client.get("/api/spend/summary", params={"window": "1d"}).json()
    zai = next(card for card in body["capacity"] if card["providerKey"] == "opencode")
    five = next(row for row in zai["rows"] if row["limitKey"] in {"5h", "five_hour"} or "5-hour" in row["label"])
    assert five["pct"] == 0 or five["pct"] == 0.0
    assert five["used"] == 0 or five["used"] == 0.0
    assert five["allowance"] == 30000
    openrouter = next(card for card in body["capacity"] if card["providerKey"] == "openrouter")
    other = openrouter["rows"][0]
    assert other["pct"] is None
    assert five["pct"] != other["pct"]
