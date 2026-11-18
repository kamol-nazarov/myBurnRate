from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from spend_app.adapters import opencode_local, traycer_local
from spend_app.adapters.common import stable_id
from spend_app.db import connect, initialize
from spend_app.pricing import PricingEngine


T0 = 1788100000000
PROVIDER = "zai-coding-plan"
MODEL = "glm-5.3-flash"


@pytest.fixture
def pricing():
    return PricingEngine.load(Path(__file__).resolve().parents[1] / "pricing")


@pytest.fixture(autouse=True)
def _default_native_path(tmp_path, monkeypatch):
    monkeypatch.setattr("spend_app.providers.default_opencode_database", lambda: tmp_path / "opencode.db")


def _fact(fact_id, *, timestamp=T0, session="session", output=20, cost=None):
    return (
        fact_id, timestamp, "chat", "opencode", MODEL, "measured", 100, 0, 0,
        output, 0, cost, "providerReported" if cost else "unpriced", session,
    )


def _traycer(path, facts):
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_analytics_facts(
                fact_id TEXT, occurred_at INTEGER, chat_id TEXT, harness_id TEXT, model TEXT,
                usage_completeness TEXT, uncached_input_tokens INTEGER, cache_read_input_tokens INTEGER,
                cache_creation_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER,
                cost_usd REAL, cost_provenance TEXT, tui_session_id TEXT
            )
            """
        )
        connection.execute("DELETE FROM usage_analytics_facts")
        connection.executemany("INSERT INTO usage_analytics_facts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", facts)
    return path


def _native(path, sessions, *, cumulative=False):
    with sqlite3.connect(path) as connection:
        if cumulative:
            connection.execute(
                """
                CREATE TABLE session(
                    id TEXT, project_id TEXT, directory TEXT, path TEXT, model TEXT, cost REAL,
                    tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,
                    tokens_cache_read INTEGER, tokens_cache_write INTEGER, time_updated INTEGER
                )
                """
            )
            for session in sessions:
                model = json.dumps({"id": MODEL, "providerID": PROVIDER})
                connection.execute(
                    "INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (session, None, None, None, model, None, 100, 20, 0, 0, 0, T0),
                )
        else:
            connection.execute("CREATE TABLE message(id TEXT, session_id TEXT, data TEXT)")
            for session in sessions:
                message = {
                    "id": f"message-{session}",
                    "sessionID": session,
                    "role": "assistant",
                    "providerID": PROVIDER,
                    "modelID": MODEL,
                    "tokens": {"input": 100, "output": 20, "cache": {"read": 0, "write": 0}},
                    "time": {"created": T0 - 1000, "completed": T0},
                }
                connection.execute(
                    "INSERT INTO message VALUES(?,?,?)", (message["id"], session, json.dumps(message))
                )
    return path


def _ingest_traycer(database, pricing, source):
    return traycer_local.ingest(database_path=database, pricing=pricing, database_glob=str(source))


def _ingest_opencode(database, pricing, source):
    return opencode_local.ingest(database_path=database, pricing=pricing, source_database=source)


def _events(database):
    with connect(database) as connection:
        return {
            raw_id: (source, session, input_tokens, output_tokens, cost)
            for table in ("usage_events", "unpriced_usage_events")
            for raw_id, source, session, input_tokens, output_tokens, cost in connection.execute(
                f"SELECT raw_id,source,session_id,input_tokens,output_tokens,cost_usd FROM {table}"
            )
        }


def _changes(database):
    with connect(database) as connection:
        return [tuple(row) for row in connection.execute("SELECT seq,raw_id FROM usage_changes ORDER BY seq")]


def test_empty_default_opencode_database_does_not_hide_measured_traycer_fact(tmp_path, pricing):
    database = tmp_path / "spend.db"
    opencode_db = _native(tmp_path / "opencode.db", [], cumulative=True)
    source = _traycer(tmp_path / "app-assets.db", [_fact("fallback")])
    _ingest_opencode(database, pricing, opencode_db)
    _ingest_traycer(database, pricing, source)
    assert _events(database) == {
        stable_id("traycer-fact", "fallback"): ("traycer_local", "session", 100, 20, None)
    }


@pytest.mark.parametrize("native_first", [False, True], ids=["traycer-first", "native-first"])
@pytest.mark.parametrize("cumulative", [False, True], ids=["messages", "cumulative"])
def test_exact_overlap_leaves_independent_usage_in_both_orders(tmp_path, pricing, native_first, cumulative):
    database = tmp_path / "spend.db"
    initialize(database)
    opencode_db = _native(tmp_path / "opencode.db", ["session", "native-unrelated"], cumulative=cumulative)
    traycer_db = _traycer(
        tmp_path / "app-assets.db",
        [
            _fact("mirror"),
            _fact("later-in-same-session", timestamp=T0 + 1000),
            _fact("different-session", session="traycer-unrelated"),
        ],
    )
    inputs = [(_ingest_opencode, opencode_db), (_ingest_traycer, traycer_db)]
    if not native_first:
        inputs.reverse()
    for ingest, source in inputs:
        ingest(database, pricing, source)

    def native_key(session):
        if cumulative:
            from spend_app.adapters.opencode_local import source_identifiers

            location, _ = source_identifiers(opencode_db)
            return stable_id("opencode-source-baseline", location, session, PROVIDER, T0, (100, 0, 0, 20))
        return stable_id("opencode-message", PROVIDER, f"message-{session}")

    expected = {
        native_key("session"): ("opencode_local", "session", 100, 20, None),
        native_key("native-unrelated"): ("opencode_local", "native-unrelated", 100, 20, None),
        stable_id("traycer-fact", "later-in-same-session"): ("traycer_local", "session", 100, 20, None),
        stable_id("traycer-fact", "different-session"): ("traycer_local", "traycer-unrelated", 100, 20, None),
    }
    assert _events(database) == expected
    previous_changes = _changes(database)
    for ingest, source in reversed(inputs):
        ingest(database, pricing, source)
    assert _events(database) == expected
    assert _changes(database) == previous_changes
    changed_ids = [raw_id for _, raw_id in previous_changes]
    # An already accepted mirror must be removed through the normal journal,
    # while native-first has no mirror insertion or deletion to sync.
    assert changed_ids.count(stable_id("traycer-fact", "mirror")) == (0 if native_first else 2)


def test_native_event_cannot_remove_a_second_fact_with_the_same_measurement(tmp_path, pricing):
    database = tmp_path / "spend.db"
    initialize(database)
    opencode_db = _native(tmp_path / "opencode.db", ["session"])
    traycer_db = _traycer(tmp_path / "app-assets.db", [_fact("mirror")])
    _ingest_opencode(database, pricing, opencode_db)
    _ingest_traycer(database, pricing, traycer_db)
    native_id = stable_id("opencode-message", PROVIDER, "message-session")
    assert _events(database) == {native_id: ("opencode_local", "session", 100, 20, None)}

    _traycer(traycer_db, [_fact("independent")])
    _ingest_traycer(database, pricing, traycer_db)
    assert _events(database) == {
        native_id: ("opencode_local", "session", 100, 20, None),
        stable_id("traycer-fact", "independent"): ("traycer_local", "session", 100, 20, None),
    }


def test_equal_candidates_and_unmatched_reported_charge_keep_original_facts(tmp_path, pricing):
    database = tmp_path / "spend.db"
    initialize(database)
    opencode_db = _native(tmp_path / "opencode.db", ["session", "charged"])
    traycer_db = _traycer(
        tmp_path / "app-assets.db",
        [_fact("unknown-match-a"), _fact("unknown-match-b"), _fact("reported", session="charged", cost=1.5)],
    )
    _ingest_opencode(database, pricing, opencode_db)
    _ingest_traycer(database, pricing, traycer_db)
    actual = _events(database)
    assert len(actual) == 5
    for fact in ("unknown-match-a", "unknown-match-b"):
        assert actual[stable_id("traycer-fact", fact)] == ("traycer_local", "session", 100, 20, None)
    assert actual[stable_id("traycer-fact", "reported")] == ("traycer_local", "charged", 100, 20, 1.5)
