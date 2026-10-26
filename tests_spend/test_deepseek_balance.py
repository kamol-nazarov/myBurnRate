"""DeepSeek: account balance from the key connected for it, shown like OpenRouter's."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

import spend_app.limits as limits
from spend_app.aggregate import _capacity_from_rows
from spend_app.integration_policy import PolicyDenied, assert_allowed_host
from spend_app.quotas import deepseek_quota_samples

WHEN = datetime(2026, 10, 26, 4, 0, tzinfo=UTC)


def reply(status: int, body: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=body or {}, request=httpx.Request("GET", limits.DEEPSEEK_BALANCE_URL))


def test_balance_reader_reads_the_usd_balance_and_keeps_the_key_out(monkeypatch) -> None:
    observed = {}

    def fake_get(url, **kwargs):
        observed.update(url=url, authorization=kwargs["headers"]["Authorization"],
                        trust_env=kwargs["trust_env"], follow_redirects=kwargs["follow_redirects"])
        return reply(200, {"is_available": True, "balance_infos": [
            {"currency": "CNY", "total_balance": "80.00", "granted_balance": "0.00", "topped_up_balance": "80.00"},
            {"currency": "USD", "total_balance": "12.50", "granted_balance": "2.50", "topped_up_balance": "10.00"}]})

    monkeypatch.setattr(limits.httpx, "get", fake_get)
    payload = limits._deepseek_balance_uncached("sk-fixture-secret")
    assert observed == {"url": "https://api.deepseek.com/user/balance", "authorization": "Bearer sk-fixture-secret",
                        "trust_env": False, "follow_redirects": False}
    assert payload["status"] == "exact"
    assert payload["windows"][0]["remainingUsd"] == 12.5
    assert "sk-fixture-secret" not in str(payload)
    [sample] = deepseek_quota_samples(payload)
    assert (sample.provider_key, sample.limit_key, sample.unit, sample.used, sample.is_payg) == (
        "deepseek", "balance", "usd", 12.5, True)


def test_rejected_key_and_cny_only_balance_explain_themselves(monkeypatch) -> None:
    monkeypatch.setattr(limits.httpx, "get", lambda url, **kwargs: reply(401))
    rejected = limits._deepseek_balance_uncached("sk-bad")
    assert rejected["status"] == "error" and "rejected the key" in rejected["detail"]
    [sample] = deepseek_quota_samples(rejected)
    assert sample.unit == "unavailable" and "rejected the key" in sample.reason

    monkeypatch.setattr(limits.httpx, "get", lambda url, **kwargs: reply(200, {"balance_infos": [
        {"currency": "CNY", "total_balance": "80.00"}]}))
    assert "CNY" in limits._deepseek_balance_uncached("sk-cny")["detail"]


def test_no_connected_key_means_no_card_and_no_request(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_BALANCE_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-another-tools-key")  # never borrowed

    def fake_get(url, **kwargs):
        raise AssertionError("no request without a connected DeepSeek key")

    monkeypatch.setattr(limits.httpx, "get", fake_get)
    payload = limits._deepseek_balance_uncached()
    assert payload["status"] == "absent"
    assert deepseek_quota_samples(payload) == []


def test_balance_reader_refuses_a_host_off_the_allowlist(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(limits, "DEEPSEEK_BALANCE_URL", "https://evil.example/user/balance")
    monkeypatch.setattr(limits.httpx, "get", lambda url, **kwargs: calls.append(url))
    payload = limits._deepseek_balance_uncached("sk-fixture-secret")
    assert calls == [] and payload["status"] == "error"
    assert_allowed_host("deepseek_balance", "api.deepseek.com")
    with pytest.raises(PolicyDenied):
        assert_allowed_host("deepseek_balance", "evil.example")


def test_balance_card_shows_funds_left_with_no_window() -> None:
    rows = [{"id": 1, "provider_key": "deepseek", "limit_key": "balance", "label": "DeepSeek funds remaining",
             "used": 12.5, "allowance": None, "unit": "usd", "pct": None, "resets_at": None,
             "source": "deepseek_balance_api", "is_payg": 1, "polled_at": "2026-10-26T03:59:00Z"}]
    cards = _capacity_from_rows(rows, now=WHEN, month_to_date={})
    card = next(c for c in cards if c["providerKey"] == "deepseek")
    assert (card["providerName"], card["isPayg"], card["fundsRemainingUsd"]) == ("DeepSeek", True, 12.5)
    assert (card["rows"][0]["etaLabel"], card["rows"][0]["pct"], card["rows"][0]["resetsAt"]) == ("available", None, None)
    # Balance lanes sort after the subscription meters.
    keys = [c["providerKey"] for c in cards]
    assert keys.index("deepseek") > keys.index("claude-code")


def test_spoke_shows_a_fresher_local_balance_over_the_hubs(tmp_path) -> None:
    from datetime import timedelta

    from spend_app.db import connect, initialize, upsert_quota
    from spend_app.sync import _overlay_fresher_local_quotas

    database = tmp_path / "spend.db"
    initialize(database)
    now = datetime.now(UTC)
    stamp = lambda moment: moment.isoformat().replace("+00:00", "Z")
    with connect(database) as connection:
        for provider, label, used in (("openrouter", "OpenRouter funds remaining", 41.25),
                                      ("deepseek", "DeepSeek funds remaining", 12.5)):
            upsert_quota(connection, provider_key=provider, limit_key="balance", label=label, used=used,
                         allowance=None, unit="usd", pct=None, resets_at=None, source=f"{provider}_api",
                         polled_at=stamp(now), is_payg=True)
    # The Hub still has an older OpenRouter balance and no DeepSeek card yet.
    hub = [{"providerKey": "openrouter", "providerName": "OpenRouter", "isPayg": True, "fundsRemainingUsd": 50.0,
            "rows": [{"limitKey": "balance", "label": "OpenRouter funds remaining", "used": 50.0, "unit": "usd",
                      "pct": None, "isPayg": True, "etaLabel": "available", "polledAt": stamp(now - timedelta(hours=1))}]}]
    merged = {card["providerKey"]: card for card in _overlay_fresher_local_quotas(database, hub)}
    assert merged["openrouter"]["fundsRemainingUsd"] == 41.25
    assert merged["openrouter"]["rows"][0]["etaLabel"] == "available"
    assert (merged["deepseek"]["providerName"], merged["deepseek"]["fundsRemainingUsd"]) == ("DeepSeek", 12.5)
