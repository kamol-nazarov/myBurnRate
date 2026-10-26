"""Balance runway: days an OpenRouter or DeepSeek balance lasts at the recent spend."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from spend_app.capacity_forecast import apply_balance_runway, balance_points, balance_runway, record_balance_sample
from spend_app.db import connect, initialize, upsert_quota

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def card(provider: str, used: float) -> dict:
    return {"providerKey": provider, "isPayg": True,
            "rows": [{"limitKey": "balance", "unit": "usd", "used": used, "pct": None, "isPayg": True}]}


def balance(connection, provider: str, used: float, at: datetime, source: str = "openrouter_credits_api") -> None:
    upsert_quota(connection, provider_key=provider, limit_key="balance", label="funds remaining", used=used,
                 allowance=None, unit="usd", pct=None, resets_at=None, source=source, polled_at=stamp(at), is_payg=True)


def test_runway_counts_falls_as_spend_and_ignores_a_top_up() -> None:
    day = timedelta(days=1)
    start = NOW - 4 * day
    # $50 → $44 → top-up to $64 → $58 over four days: $12 spent, $3/day.
    points = [(start, 50.0), (start + day, 44.0), (start + 2 * day, 64.0), (NOW, 58.0)]
    runway = balance_runway(points, remaining=58.0)
    assert (runway["state"], runway["dailyUsd"], runway["basisDays"]) == ("estimate", 3.0, 4.0)
    assert runway["days"] == 19.3
    assert runway["line"] == "about 19 days at your current spend ($3.00/day)"
    assert runway["runsOutAt"].startswith("2026-11-14")


def test_runway_waits_for_a_day_and_says_when_nothing_was_spent() -> None:
    learning = balance_runway([(NOW - timedelta(hours=5), 41.0), (NOW, 40.0)], remaining=40.0)
    assert (learning["state"], learning["days"]) == ("learning", None)
    idle = balance_runway([(NOW - timedelta(days=3), 41.0), (NOW, 41.0)], remaining=41.0)
    assert (idle["state"], idle["days"], idle["line"]) == ("idle", None, "No spend in the last 3 days.")


def test_hourly_samples_give_an_unchanged_balance_its_history(tmp_path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    with connect(database) as connection:
        for hour in range(0, 49):
            # The quotas table only rewrites polled_at for an unchanged value;
            # the samples keep how long it has been watched.
            assert record_balance_sample(connection, provider_key="openrouter", source="openrouter_credits_api",
                                         usd=41.0, sampled_at=stamp(NOW - timedelta(hours=48 - hour)))
        assert not record_balance_sample(connection, provider_key="openrouter", source="openrouter_credits_api",
                                         usd=41.0, sampled_at=stamp(NOW - timedelta(minutes=20)))
        balance(connection, "openrouter", 41.0, NOW)
        cards = [card("openrouter", 41.0)]
        apply_balance_runway(cards, connection, now=NOW)
    assert cards[0]["fundsRunway"]["state"] == "idle"
    assert cards[0]["rows"][0]["runway"]["line"] == "No spend in the last 2 days."


def test_runway_reads_one_collector_and_the_balance_on_entering_the_window(tmp_path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    with connect(database) as connection:
        balance(connection, "deepseek", 30.0, NOW - timedelta(days=20), source="deepseek_balance_api")
        balance(connection, "deepseek", 23.0, NOW - timedelta(days=7), source="deepseek_balance_api")
        balance(connection, "deepseek", 16.0, NOW, source="deepseek_balance_api")
        # Another computer's synced copy of the same account is not spend.
        balance(connection, "deepseek", 99.0, NOW - timedelta(days=2), source="sync:laptop:deepseek_balance_api")
        balance(connection, "deepseek", 16.0, NOW + timedelta(seconds=1), source="deepseek_balance_api")
        points = balance_points(connection, "deepseek", now=NOW)
        assert points[0] == (NOW - timedelta(days=14), 30.0)
        assert all(value != 99.0 for _, value in points)
        cards = [card("deepseek", 16.0)]
        apply_balance_runway(cards, connection, now=NOW)
    runway = cards[0]["fundsRunway"]
    # $14 over 14 days is $1/day; $16 left is about 16 days.
    assert (runway["state"], runway["dailyUsd"], round(runway["days"])) == ("estimate", 1.0, 16)


def test_spoke_runway_comes_from_its_own_balance_history(tmp_path) -> None:
    from spend_app.sync import _overlay_fresher_local_quotas

    database = tmp_path / "spend.db"
    initialize(database)
    now = datetime.now(UTC)
    with connect(database) as connection:
        balance(connection, "openrouter", 50.0, now - timedelta(days=5))
        balance(connection, "openrouter", 40.0, now)
    hub = [{"providerKey": "openrouter", "providerName": "OpenRouter", "isPayg": True, "fundsRemainingUsd": 45.0,
            "rows": [{"limitKey": "balance", "label": "OpenRouter funds remaining", "used": 45.0, "unit": "usd",
                      "pct": None, "isPayg": True, "polledAt": stamp(now - timedelta(hours=1))}]}]
    [merged] = _overlay_fresher_local_quotas(database, hub)
    assert merged["fundsRemainingUsd"] == 40.0
    assert (merged["fundsRunway"]["dailyUsd"], round(merged["fundsRunway"]["days"])) == (2.0, 20)
    assert merged["rows"][0]["runway"] == merged["fundsRunway"]


def test_a_current_database_gains_the_balance_table_on_start(tmp_path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    with connect(database) as connection:
        connection.execute("DROP TABLE balance_samples")
    initialize(database)
    with connect(database) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='balance_samples'").fetchone()
