"""A second Claude Code account (``~/.claude-<name>``) gets its own quota lane."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spend_app import claude_accounts
from spend_app.claude_accounts import ClaudeAccount, discover, provider_meta

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _state(directory: Path, uuid: str | None, *, tier: str = "default_claude_max_20x", usage: dict | None = None,
           fetched: datetime = NOW, cached_uuid: str | None = None, name: str = ".claude.json") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    document: dict = {}
    if uuid:
        document["oauthAccount"] = {"accountUuid": uuid, "organizationRateLimitTier": tier,
                                    "emailAddress": "someone@example.com"}
    if usage is not None:
        document["cachedUsageUtilization"] = {"fetchedAtMs": int(fetched.timestamp() * 1000),
                                              "accountUuid": cached_uuid or uuid, "utilization": usage}
    path = directory / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


USAGE = {
    "five_hour": {"utilization": 12, "resets_at": "2026-10-19T15:00:00+00:00"},
    "seven_day": {"utilization": 40, "resets_at": "2026-10-23T08:00:00+00:00"},
}


@pytest.fixture(autouse=True)
def _fresh_discovery():
    claude_accounts.reset_cache()
    yield
    claude_accounts.reset_cache()


def test_discovers_signed_in_profiles_other_than_the_default_account(tmp_path: Path) -> None:
    home = tmp_path
    _state(home, "personal-uuid")  # ~/.claude.json describes the default profile
    (home / ".claude").mkdir()
    _state(home / ".claude-work", "work-uuid")
    _state(home / ".claude-copy", "personal-uuid")  # same account as the default lane
    _state(home / ".claude-empty", None)  # never signed in
    (home / ".claude-notes.txt").write_text("not a profile", encoding="utf-8")
    accounts = discover(home=home, environ={})
    assert [(a.key, a.label, a.plan) for a in accounts] == [("claude-code-work", "Work", "Claude Max")]
    assert accounts[0].state_path == home / ".claude-work" / ".claude.json"


def test_extra_directories_can_be_named_explicitly(tmp_path: Path) -> None:
    _state(tmp_path, "personal-uuid")
    _state(tmp_path / "profiles" / "Client A", "client-uuid", tier="default_claude_pro")
    accounts = discover(home=tmp_path, environ={claude_accounts.EXTRA_DIRS_ENV: str(tmp_path / "profiles" / "Client A")})
    assert [(a.key, a.label, a.plan) for a in accounts] == [("claude-code-client-a", "Client A", "Claude Pro")]


def test_card_metadata_never_exposes_the_account_email(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(claude_accounts, "extra_accounts", lambda: ())
    meta = provider_meta("claude-code-work")
    assert meta == {"providerKey": "claude-code-work", "providerName": "Claude Code · Work", "shortName": "Claude Work",
                    "accountLabel": "Work", "plan": "Claude", "isPayg": False, "toolKey": "claude-code"}
    assert provider_meta("claude-code") is None and provider_meta("codex") is None


def test_cached_usage_is_read_while_it_belongs_to_the_account(tmp_path: Path) -> None:
    from spend_app.limits import claude_cached_usage

    path = _state(tmp_path / ".claude-work", "work-uuid", usage=USAGE, fetched=NOW - timedelta(minutes=3))
    payload = claude_cached_usage(path, key="claude-code-work", plan="Claude Max", now=NOW)
    assert payload["status"] == "exact" and payload["key"] == "claude-code-work"
    assert [(w["key"], w["usedPct"], w["resetAt"]) for w in payload["windows"]] == [
        ("5h", 12.0, "2026-10-19T15:00:00+00:00"), ("weekly", 40.0, "2026-10-23T08:00:00+00:00")]
    # An idle profile's older reading is still its latest one.
    later = claude_cached_usage(path, key="claude-code-work", now=NOW + timedelta(hours=1))
    assert later["observedAt"] == payload["observedAt"] and later["windows"][0]["usedPct"] == 12.0
    with pytest.raises(RuntimeError, match="stale"):
        claude_cached_usage(path, key="claude-code-work", now=NOW + timedelta(days=8))
    other = _state(tmp_path / ".claude-other", "work-uuid", usage=USAGE, cached_uuid="someone-else")
    with pytest.raises(RuntimeError, match="different account"):
        claude_cached_usage(other, key="claude-code-other", now=NOW)


def test_a_window_whose_reset_has_passed_reads_as_reset(tmp_path: Path) -> None:
    from spend_app.limits import claude_cached_usage

    path = _state(tmp_path / ".claude-work", "work-uuid", usage=USAGE, fetched=NOW)
    windows = {w["key"]: w for w in claude_cached_usage(path, key="claude-code-work", now=NOW + timedelta(days=5))["windows"]}
    # The 5-hour window starts with the next message, so it has no reset yet.
    assert (windows["5h"]["usedPct"], windows["5h"]["remainingPct"], windows["5h"]["resetAt"]) == (0.0, 100.0, None)
    # The weekly window keeps its cadence.
    assert (windows["weekly"]["usedPct"], windows["weekly"]["resetAt"]) == (0.0, "2026-10-30T08:00:00Z")


def test_a_profile_without_a_cached_reading_has_no_meter_and_no_credential_read(tmp_path: Path, monkeypatch) -> None:
    from spend_app import limits

    path = _state(tmp_path / ".claude-work", "work-uuid")
    (path.parent / ".credentials.json").write_text('{"claudeAiOauth": {"accessToken": "secret"}}', encoding="utf-8")
    account = ClaudeAccount(key="claude-code-work", label="Work", config_dir=path.parent, state_path=path,
                            account_uuid="work-uuid", plan="Claude Max")
    real_read_text = Path.read_text
    opened = []
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: opened.append(self.name) or real_read_text(self, *a, **k))
    payload, source = limits.claude_account_limits(account, now=NOW)
    assert source == "claude_code_cache"
    assert payload["status"] == "unavailable" and payload["windows"] == []
    assert payload["detail"] == limits.CLAUDE_NO_READING_DETAIL
    assert ".credentials.json" not in opened


def test_account_lane_is_polled_stored_and_shown_as_its_own_card(tmp_path: Path, monkeypatch) -> None:
    from spend_app.aggregate import _capacity_from_rows
    from spend_app.db import connect, initialize
    from spend_app.quotas import QuotaLaneScheduler, _claude_account_collector, poll_quotas

    current = datetime.now(UTC)
    usage = {"five_hour": {"utilization": 12, "resets_at": _iso(current + timedelta(hours=3))},
             "seven_day": {"utilization": 40, "resets_at": _iso(current + timedelta(days=4))}}
    path = _state(tmp_path / ".claude-work", "work-uuid", usage=usage, fetched=current)
    account = ClaudeAccount(key="claude-code-work", label="Work", config_dir=path.parent, state_path=path,
                            account_uuid="work-uuid", plan="Claude Max")
    monkeypatch.setattr(claude_accounts, "extra_accounts", lambda: (account,))
    database = tmp_path / "spend.db"
    initialize(database)
    poll_quotas(database, collectors={account.key: _claude_account_collector(account)}, now=lambda: _iso(NOW))
    with connect(database) as connection:
        rows = [dict(row) for row in connection.execute("SELECT * FROM quotas WHERE provider_key='claude-code-work'")]
    assert {(r["limit_key"], r["label"], r["pct"], r["source"]) for r in rows} == {
        ("5h", "Claude 5-hour window", 12.0, "claude_code_cache"),
        ("weekly", "Claude weekly window", 40.0, "claude_code_cache"),
    }
    cards = _capacity_from_rows(rows, now=NOW, month_to_date={})
    card = next(card for card in cards if card["providerKey"] == "claude-code-work")
    assert card["providerName"] == "Claude Code · Work" and card["shortName"] == "Claude Work"
    assert card["toolKey"] == "claude-code" and card["plan"] == "Claude Max" and card["accountLabel"] == "Work"
    assert [row["limitKey"] for row in card["rows"]] == ["5h", "weekly"]
    # The account shares Claude's polling budget instead of the 15 s default.
    assert QuotaLaneScheduler().lane_cadence("claude-code-work") == QuotaLaneScheduler().lane_cadence("claude-code")


def test_spoke_shows_an_account_lane_the_hub_does_not_have_yet(tmp_path: Path, monkeypatch) -> None:
    from spend_app.db import connect, initialize, upsert_quota
    from spend_app.sync import _overlay_fresher_local_quotas

    monkeypatch.setattr(claude_accounts, "extra_accounts", lambda: ())
    database = tmp_path / "spend.db"
    initialize(database)
    polled = _iso(datetime.now(UTC))
    resets = _iso(datetime.now(UTC) + timedelta(days=2))
    with connect(database) as connection:
        for limit, pct in (("5h", 3.0), ("weekly", 21.0)):
            upsert_quota(connection, provider_key="claude-code-work", limit_key=limit, label=f"Claude Work {limit}",
                         used=None, allowance=None, unit="pct", pct=pct, resets_at=resets, source="claude_code_cache",
                         polled_at=polled, is_payg=False)
    hub = [{"providerKey": "codex", "providerName": "Codex", "plan": "ChatGPT Pro", "isPayg": False, "rows": []}]
    merged = _overlay_fresher_local_quotas(database, hub)
    assert [card["providerKey"] for card in merged] == ["codex", "claude-code-work"]
    work = merged[1]
    assert work["providerName"] == "Claude Code · Work" and work["primaryPct"] == 3.0
    assert {row["limitKey"]: row["pct"] for row in work["rows"]} == {"5h": 3.0, "weekly": 21.0}


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")
