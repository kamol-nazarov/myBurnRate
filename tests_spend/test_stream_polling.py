from __future__ import annotations

import asyncio
import sqlite3
import threading
from types import SimpleNamespace

import httpx
import pytest
from anyio import CancelScope
from fastapi import FastAPI

from spend_app import sync


def test_stream_signature_tracks_commits_without_scanning_history(tmp_path):
    path = tmp_path / "stream.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE samples (value INTEGER)")
    observer = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    statements = []
    observer.set_trace_callback(statements.append)
    try:
        previous = sync._db_live_signature(observer)
        assert sync._db_live_signature(observer) == previous
        for sql in (
            "INSERT INTO samples VALUES (1)",
            "UPDATE samples SET value=2",
            "DELETE FROM samples",
        ):
            writer.execute(sql)
            assert sync._db_live_signature(observer) == previous
            writer.commit()
            current = sync._db_live_signature(observer)
            assert current != previous
            assert sync._db_live_signature(observer) == current
            previous = current
        assert all(sql == "PRAGMA data_version" for sql in statements)
    finally:
        observer.close()
        writer.close()


@pytest.mark.parametrize("boundary", ["_db_live_signature", "_resolve_stream_machines"])
@pytest.mark.parametrize("phase", ["ready", "update"])
def test_slow_stream_reads_leave_health_responsive(tmp_path, monkeypatch, boundary, phase):
    path = tmp_path / "stream.db"
    sqlite3.connect(path).close()
    settings = SimpleNamespace(database_path=path)
    broker = sync.StreamBroker()
    monkeypatch.setattr(sync, "STREAM_BROKER", broker)
    monkeypatch.setattr(sync, "_resolve_stream_machines", lambda _settings: [])
    original = getattr(sync, boundary)
    release = threading.Event()
    observers = []
    original_signature = sync._db_live_signature

    def signature(connection):
        observers.append(connection)
        return original_signature(connection)

    monkeypatch.setattr(sync, "_db_live_signature", signature)
    if boundary == "_db_live_signature":
        original = signature

    class Request:
        async def is_disconnected(self):
            return False

    async def exercise():
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        calls = 0

        def slow_read(*args):
            nonlocal calls
            calls += 1
            if calls == (1 if phase == "ready" else 2):
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(3), "stream read blocked the event loop"
            return original(*args)

        monkeypatch.setattr(sync, boundary, slow_read)
        app = FastAPI()
        router = sync.sync_router(settings, pricing=None)
        app.include_router(router)

        @app.get("/healthz")
        def healthz():
            return {"status": "ok"}

        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/spend/stream")
        response = await endpoint(Request(), once=0)
        stream = response.body_iterator
        if phase == "update":
            assert "event: ready" in await anext(stream)
            broker.publish({"type": "test"})
        pending = asyncio.create_task(anext(stream))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                health = await asyncio.wait_for(client.get("/healthz"), timeout=1)
            assert health.json() == {"status": "ok"}
            assert not pending.done()
        finally:
            release.set()
            try:
                frame = await pending
            finally:
                await stream.aclose()
        assert f"event: {phase}" in frame
        assert not broker._waiters

    asyncio.run(exercise())
    assert observers
    for observer in observers:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            observer.execute("PRAGMA data_version")


def test_disconnected_stream_closes_observer_inside_cancelled_scope(tmp_path, monkeypatch):
    path = tmp_path / "stream.db"
    sqlite3.connect(path).close()
    broker = sync.StreamBroker()
    monkeypatch.setattr(sync, "STREAM_BROKER", broker)
    monkeypatch.setattr(sync, "_resolve_stream_machines", lambda _settings: [])
    observers = []
    original = sync._db_live_signature

    def signature(connection):
        observers.append(connection)
        return original(connection)

    monkeypatch.setattr(sync, "_db_live_signature", signature)

    async def exercise():
        router = sync.sync_router(SimpleNamespace(database_path=path), pricing=None)
        endpoint = next(route.endpoint for route in router.routes if route.path == "/api/spend/stream")
        response = await endpoint(None, once=0)
        stream = response.body_iterator
        assert "event: ready" in await anext(stream)
        with CancelScope() as scope:
            scope.cancel()
            await stream.aclose()
        assert not broker._waiters

    asyncio.run(exercise())
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        observers[0].execute("PRAGMA data_version")
