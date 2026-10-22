"""OpenCode Go: its own usage tool, a derived price card and a quota lane."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import spend_app.limits as limits
from spend_app.adapters.opencode_schema import parse_message
from spend_app.aggregate import _capacity_from_rows
from spend_app.pricing import PricingEngine
from spend_app.quotas import opencode_go_quota_samples

ROOT = Path(__file__).resolve().parents[1]
WHEN = datetime(2026, 10, 22, 17, 35, tzinfo=UTC)


def message(provider: str, model: str) -> dict:
    # Shape of an OpenCode 2 session_message row, reduced to usage metadata.
    return {"id": "msg_1", "sessionID": "ses_1", "role": "assistant",
            "model": {"id": model, "providerID": provider},
            "time": {"created": int(WHEN.timestamp() * 1000), "completed": int(WHEN.timestamp() * 1000) + 500},
            "cost": 0.00423225,
            "tokens": {"input": 25463, "output": 399, "reasoning": 273, "cache": {"read": 3200, "write": 0}}}


def test_go_messages_are_their_own_tool_and_bill_reasoning_as_output():
    go = parse_message(message("opencode-go", "deepseek-v4.1-flash"), role="assistant", format="v2").row
    assert (go.tool_key, go.model_key) == ("opencode-go", "opencode-go:deepseek-v4.1-flash")
    assert go.output_tokens == 399 + 273 and go.reasoning_tokens == 273
    zai = parse_message(message("zai-coding-plan", "glm-5.3"), role="assistant", format="v2").row
    assert (zai.tool_key, zai.model_key, zai.output_tokens) == ("opencode", "opencode:glm-5.3", 399)


def test_go_card_reproduces_the_cost_opencode_recorded():
    row = parse_message(message("opencode-go", "deepseek-v4.1-flash"), role="assistant", format="v2").row
    pricing = PricingEngine.load(ROOT / "pricing")
    value = pricing.compute(model_key=row.model_key, occurred_at=row.occurred_at, input_tokens=row.input_tokens,
                            cached_input_tokens=row.cached_input_tokens, cache_write_tokens=row.cache_write_tokens,
                            output_tokens=row.output_tokens)
    assert float(value) == pytest.approx(0.00423225, abs=1e-12)


def write_opencode_db(path: Path, rows: list[tuple]) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE credential (id TEXT, integration_id TEXT, label TEXT, value TEXT, connector_id TEXT, "
                   "method_id TEXT, active INTEGER, time_created INTEGER, time_updated INTEGER)")
        db.executemany("INSERT INTO credential(id, integration_id, value, active, time_created) VALUES(?,?,?,?,?)", rows)


def test_console_key_comes_from_opencodes_own_credential_store(tmp_path, monkeypatch):
    database = tmp_path / "opencode.db"
    write_opencode_db(database, [
        ("a", "opencode", json.dumps({"type": "key", "key": "old"}), 0, 3),
        ("b", "opencode", json.dumps({"type": "key", "key": "console"}), None, 2),
        ("c", "zai-coding-plan", json.dumps({"type": "key", "key": "zai"}), None, 1),
    ])
    monkeypatch.setenv("OPENCODE_DB", str(database))
    assert limits._opencode_credential("opencode") == "console"
    assert limits._opencode_credential("zai-coding-plan") == "zai"
    write_opencode_db(other := tmp_path / "other.db", [
        ("d", "opencode", json.dumps({"type": "key", "key": "x", "metadata": {"server": "https://example.test"}}), 1, 1)])
    monkeypatch.setenv("OPENCODE_DB", str(other))
    assert limits._opencode_credential("opencode") is None, "a self-hosted console key is not sent to opencode.ai"


class FakeResponse:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def test_go_usage_becomes_three_windows_and_no_subscription_writes_nothing(monkeypatch):
    monkeypatch.setenv("BURNRATE_ENABLE_OPENCODE_GO_QUOTA", "1")
    monkeypatch.setattr(limits, "_opencode_go_keys", lambda: ["console", "go-only"])
    sent = []
    usage = {"usage": {"rolling": {"status": "ok", "percent": 12.5, "resetsAt": "2026-10-22T22:27:21.000Z"},
                       "weekly": {"status": "ok", "percent": 40, "resetsAt": "2026-10-26T00:00:00.000Z"},
                       "monthly": {"status": "ok", "percent": 7, "resetsAt": "2026-11-22T17:13:19.000Z"}}}

    def fake_get(url, *, headers, **kwargs):
        sent.append(headers["Authorization"])
        return FakeResponse(403, {}) if headers["Authorization"] == "Bearer console" else FakeResponse(200, usage)

    monkeypatch.setattr(limits.httpx, "get", fake_get)
    payload = limits._opencode_go_limits_uncached()
    assert sent == ["Bearer console", "Bearer go-only"], "a Go-only key is tried when the Console key has no Go"
    samples = opencode_go_quota_samples(payload, source="opencode_go_usage_endpoint")
    assert [(s.limit_key, s.pct, s.resets_at) for s in samples] == [
        ("5h", 12.5, "2026-10-22T22:27:21Z"), ("weekly", 40.0, "2026-10-26T00:00:00Z"), ("monthly", 7.0, "2026-11-22T17:13:19Z")]

    monkeypatch.setattr(limits.httpx, "get", lambda url, *, headers, **kwargs: FakeResponse(403, {}))
    assert opencode_go_quota_samples(limits._opencode_go_limits_uncached(), source="opencode_go_usage_endpoint") == []
    monkeypatch.setenv("BURNRATE_ENABLE_OPENCODE_GO_QUOTA", "0")
    assert limits._opencode_go_limits_uncached()["status"] == "unavailable"


def test_go_card_is_named_and_orders_its_windows():
    rows = [{"id": i, "provider_key": "opencode-go", "limit_key": key, "label": f"OpenCode Go {key}", "used": None,
             "allowance": None, "unit": "pct", "pct": pct, "resets_at": "2026-11-01T00:00:00Z", "source": "opencode_go_usage_endpoint",
             "is_payg": 0, "polled_at": "2026-10-22T17:40:00Z"}
            for i, (key, pct) in enumerate((("monthly", 7.0), ("5h", 12.5), ("weekly", 40.0)))]
    card = next(c for c in _capacity_from_rows(rows, now=WHEN, month_to_date={}) if c["providerKey"] == "opencode-go")
    assert (card["providerName"], card["plan"]) == ("OpenCode Go", "Go")
    assert [row["limitKey"] for row in card["rows"]] == ["5h", "weekly", "monthly"]
