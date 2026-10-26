"""A second OpenCode profile (``~/.opencode-<name>``) gets its own Go quota lane."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import spend_app.limits as limits
from spend_app import opencode_profiles
from spend_app.aggregate import _capacity_from_rows
from spend_app.opencode_profiles import OpenCodeProfile, discover, provider_meta
from spend_app.quotas import LANE_CADENCE, QuotaLaneScheduler, _opencode_profile_collector

WHEN = datetime(2026, 10, 26, 4, 0, tzinfo=UTC)


def console_db(path: Path, key: str | None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE credential (id TEXT, integration_id TEXT, label TEXT, value TEXT, connector_id TEXT, "
                   "method_id TEXT, active INTEGER, time_created INTEGER, time_updated INTEGER)")
        if key:
            db.execute("INSERT INTO credential(id, integration_id, value, active, time_created) VALUES(?,?,?,?,?)",
                       ("a", "opencode", json.dumps({"type": "key", "key": key}), 1, 1))
    return path


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    # The default profile lives under XDG_DATA_HOME, as OpenCode itself resolves it.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    console_db(tmp_path / "xdg" / "opencode" / "opencode.db", "default-console")
    return tmp_path


def test_discovers_signed_in_profiles_on_other_accounts(home: Path) -> None:
    console_db(home / ".opencode-go-2" / "opencode" / "opencode.db", "second-console")
    console_db(home / ".opencode-copy" / "opencode" / "opencode.db", "default-console")  # same account
    console_db(home / ".opencode-empty" / "opencode" / "opencode.db", None)  # never signed in
    (home / ".opencode-cache").mkdir()  # no database at all
    profiles = discover(home=home, environ={})
    assert [(p.key, p.label) for p in profiles] == [("opencode-go-2", "Profile 2")]
    assert profiles[0].database == home / ".opencode-go-2" / "opencode" / "opencode.db"


def test_extra_directories_can_be_named_explicitly(home: Path) -> None:
    console_db(home / "profiles" / "Client A" / "opencode.db", "client-console")
    profiles = discover(home=home, environ={opencode_profiles.EXTRA_DIRS_ENV: str(home / "profiles" / "Client A")})
    assert [(p.key, p.label) for p in profiles] == [("opencode-go-client-a", "Client A")]


def test_card_metadata_names_the_profile() -> None:
    assert provider_meta("opencode-go-2") == {
        "providerKey": "opencode-go-2", "providerName": "OpenCode Go · Profile 2", "shortName": "OpenCode Go 2",
        "accountLabel": "Profile 2", "plan": "Go", "isPayg": False, "toolKey": "opencode-go"}
    assert provider_meta("opencode-go-work")["shortName"] == "OpenCode Go Work"
    assert provider_meta("opencode-go") is None and provider_meta("opencode") is None


class FakeResponse:
    status_code = 200

    def json(self) -> dict:
        return {"usage": {"rolling": {"percent": 4, "resetsAt": "2026-10-26T08:00:00.000Z"},
                          "weekly": {"percent": 31, "resetsAt": "2026-10-30T00:00:00.000Z"},
                          "monthly": {"percent": 9, "resetsAt": "2026-11-20T00:00:00.000Z"}}}

    def raise_for_status(self) -> None:
        return None


def test_profile_lane_asks_with_its_own_key_only(home: Path, monkeypatch) -> None:
    monkeypatch.setenv("BURNRATE_ENABLE_OPENCODE_GO_QUOTA", "1")
    monkeypatch.setenv("OPENCODE_API_KEY", "default-env-key")  # belongs to the default profile
    database = console_db(home / ".opencode-go-2" / "opencode" / "opencode.db", "second-console")
    sent = []

    def fake_get(url, *, headers, **kwargs):
        sent.append(headers["Authorization"])
        return FakeResponse()

    monkeypatch.setattr(limits.httpx, "get", fake_get)
    profile = OpenCodeProfile(key="opencode-go-2", label="Profile 2", data_dir=database.parent.parent, database=database)
    samples = _opencode_profile_collector(profile)()
    assert sent == ["Bearer second-console"]
    assert [(s.provider_key, s.limit_key, s.pct) for s in samples] == [
        ("opencode-go-2", "5h", 4.0), ("opencode-go-2", "weekly", 31.0), ("opencode-go-2", "monthly", 9.0)]


def test_profile_card_orders_windows_like_the_default_lane() -> None:
    rows = [{"id": i, "provider_key": "opencode-go-2", "limit_key": key, "label": f"OpenCode Go {key}", "used": None,
             "allowance": None, "unit": "pct", "pct": pct, "resets_at": "2026-11-01T00:00:00Z",
             "source": "opencode_go_usage_endpoint", "is_payg": 0, "polled_at": "2026-10-26T03:59:00Z"}
            for i, (key, pct) in enumerate((("monthly", 7.0), ("5h", 4.0), ("weekly", 31.0)))]
    card = next(c for c in _capacity_from_rows(rows, now=WHEN, month_to_date={}) if c["providerKey"] == "opencode-go-2")
    assert (card["providerName"], card["shortName"], card["toolKey"]) == ("OpenCode Go · Profile 2", "OpenCode Go 2", "opencode-go")
    assert [row["limitKey"] for row in card["rows"]] == ["5h", "weekly", "monthly"]


def test_profile_lane_polls_on_the_go_cadence() -> None:
    assert QuotaLaneScheduler().lane_cadence("opencode-go-2") == LANE_CADENCE["opencode-go"]
