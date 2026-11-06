"""Synthetic regression checks: no live database, accounts, or provider calls."""

import asyncio
import contextvars
import threading
from importlib.metadata import version
from pathlib import Path

import httpx
import pytest
from spend_app.bounded_reads import BoundedReadExecutor, ReadCapacityExceeded


def test_cancelled_waiter_retains_capacity_until_worker_finishes():
    async def exercise():
        executor = BoundedReadExecutor()
        started, release = threading.Event(), threading.Event()

        def blocking():
            started.set()
            assert release.wait(5)

        waiter = asyncio.create_task(executor.run(blocking))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            with pytest.raises(ReadCapacityExceeded):
                await executor.run(lambda: None)
        finally:
            release.set()
            await asyncio.to_thread(executor.close)
        with pytest.raises(ReadCapacityExceeded):
            await executor.run(lambda: None)

    asyncio.run(exercise())


def test_context_and_exception_do_not_leak_between_reads(monkeypatch):
    async def exercise():
        executor = BoundedReadExecutor()
        value = contextvars.ContextVar("request", default="unset")
        try:
            value.set("first")
            assert await executor.run(value.get) == "first"
            value.set("second")
            assert await executor.run(value.get) == "second"
            with pytest.raises(ZeroDivisionError):
                await executor.run(lambda: 1 / 0)
            submit = executor._executor.submit
            with monkeypatch.context() as patch:

                def reject(*args, **kwargs):
                    raise RuntimeError("submission rejected")

                patch.setattr(executor._executor, "submit", reject)
                with pytest.raises(RuntimeError):
                    await executor.run(value.get)
            assert executor._executor.submit == submit
            assert await executor.run(lambda: 42) == 42
        finally:
            await asyncio.to_thread(executor.close)

    asyncio.run(exercise())


def test_saturated_reads_leave_health_responsive(tmp_path, monkeypatch):
    import spend_app.hud_data as hud
    from spend_app import sync
    from spend_app.api import create_app
    from spend_app.config import Settings

    settings = Settings(
        database_path=tmp_path / "synthetic.db",
        pricing_path=Path(__file__).resolve().parents[1] / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )
    monkeypatch.setattr(sync, "fetch_from_configured_hub", lambda *a, **kw: None)
    started, release = threading.Event(), threading.Event()

    def slow_snapshot(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return {"synthetic": True}

    monkeypatch.setattr(hud, "live_snapshot", slow_snapshot)
    monkeypatch.setattr(hud, "recorded_usage", lambda *a, **kw: {"usage": True})
    app = create_app(settings, enable_scheduler=False)

    async def exercise():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            pending = asyncio.create_task(client.get("/api/spend/hud"))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                health = await asyncio.wait_for(client.get("/healthz"), 1)
                assert health.json() == {"status": "ok"}
                for path in [
                    "/api/spend/hud",
                    "/api/spend/hud/usage",
                    "/api/spend/summary",
                ]:
                    response = await client.get(path)
                    assert response.status_code == 503
                    assert response.headers["retry-after"] == "1"
            finally:
                release.set()
                assert (await pending).status_code == 200
            legacy = await client.get(
                "/api/spend/summary", headers={"referer": "http://test/hud"}
            )
            assert legacy.status_code == 200
            assert legacy.json() == {"usage": True, "synthetic": True}

    asyncio.run(exercise())


def test_certificate_floor_and_effective_httpx_trust():
    assert tuple(map(int, version("certifi").split("."))) >= (2026, 10, 23)
    from httpx import create_ssl_context

    subjects = repr([cert["subject"] for cert in create_ssl_context().get_ca_certs()])
    assert "emSign Root CA - G3" not in subjects
