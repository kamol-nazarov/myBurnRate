"""Default poll follow-ups and cancellation of an in-flight collector."""

from datetime import UTC, datetime
import os
from pathlib import Path
import threading

from fastapi.testclient import TestClient
import pytest

from spend_app import api, quotas
from spend_app.collector import CollectorStopped, CollectorWorker
from spend_app.config import Settings
from spend_app.pricing import PricingEngine
from spend_app.scheduler import CollectionScheduler

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-11-09T12:00:00Z"


def settings_for(tmp_path):
    return Settings(database_path=tmp_path / "spend.db", pricing_path=ROOT / "pricing",
                    anthropic_admin_key=None, openai_admin_key=None, timezone="UTC",
                    cache_hit_threshold=0.75, over_routing_token_ceiling=40000,
                    logs_path=tmp_path / "logs")


@pytest.mark.parametrize("inject_engine", [False, True])
def test_default_polls_sync_written_records_with_configured_pricing(tmp_path, monkeypatch, inject_engine):
    settings = settings_for(tmp_path)
    engine = PricingEngine.load(settings.pricing_path) if inject_engine else None
    sample = quotas.QuotaSample("grok", "weekly", "Weekly", "pct", "fixture", pct=10)
    monkeypatch.setattr(quotas, "default_quota_collectors", lambda _path: {"grok": lambda: [sample]})
    activity = {"grokSessions": [{"sessionId": "fixture-session", "startedAt": NOW}]}
    monkeypatch.setattr(quotas, "default_activity_collector", lambda _path: activity)
    calls = []
    monkeypatch.setattr("spend_app.setup_wizard.maybe_run_configured_spoke_sync",
                        lambda s, p: calls.append((s, p)))
    q = quotas.poll_quotas(settings.database_path, settings=settings, pricing=engine,
                          activity=lambda _provider: False, now=lambda: NOW)
    a = quotas.poll_activity(settings.database_path, settings=settings, pricing=engine, now=lambda: NOW)
    assert q["written"] == a["new"] == 1
    assert len(calls) == 2
    activity["grokSessions"][0]["title"] = "Updated fixture"
    updated = quotas.poll_activity(settings.database_path, settings=settings, pricing=engine, now=lambda: NOW)
    assert updated["new"] == 0 and len(calls) == 3
    quotas.poll_activity(settings.database_path, settings=settings, pricing=engine, now=lambda: NOW)
    assert len(calls) == 3, "unchanged live state does not trigger another push"
    for configured, pricing in calls:
        assert configured is settings
        assert pricing.resolve("gpt-6-astra", datetime(2026, 11, 9, tzinfo=UTC))
        if inject_engine:
            assert pricing is engine


def test_sync_failure_is_logged_without_exception_contents(tmp_path, monkeypatch, caplog):
    settings = settings_for(tmp_path)
    def broken(*_args):
        raise RuntimeError("Bearer synthetic-secret https://private.invalid/account")
    monkeypatch.setattr("spend_app.setup_wizard.maybe_run_configured_spoke_sync", broken)
    quotas._sync_after_update(settings.database_path, kind="quota", settings=settings)
    assert "Immediate quota sync failed (RuntimeError)" in caplog.text
    assert "synthetic-secret" not in caplog.text and "private.invalid" not in caplog.text


def test_stop_interrupts_active_pass_and_prevents_worker_restart():
    entered = threading.Event()
    killed = threading.Event()
    errors = []

    class Pipe:
        closed = False
        def send(self, _message):
            pass
        def poll(self, timeout):
            entered.set()
            killed.wait(timeout)
            return killed.is_set()
        def recv(self):
            raise AssertionError("A cancelled pass must not read another result")
        def close(self):
            self.closed = True

    class Process:
        def is_alive(self):
            return not killed.is_set()
        def kill(self):
            killed.set()
        def join(self, timeout):
            assert killed.is_set()

    worker = CollectorWorker(None)
    pipe = Pipe()
    worker._connection, worker._process = pipe, Process()
    def collect():
        try:
            worker.run_pass(timeout=3600)
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=collect, daemon=True)
    thread.start()
    assert entered.wait(2)
    stopper = threading.Thread(target=worker.stop, daemon=True)
    stopper.start()
    stopper.join(2)
    assert not stopper.is_alive(), "stop must not wait for the pass lock"
    thread.join(2)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], CollectorStopped)
    assert killed.is_set() and pipe.closed
    assert worker._process is worker._connection is None
    worker.stop()  # idempotent
    with pytest.raises(CollectorStopped):
        worker.run_pass()


def test_api_owns_worker_and_stops_it_before_reads_and_resources(tmp_path, monkeypatch):
    calls = []
    class Worker:
        def stop(self):
            calls.append("worker")
    worker = Worker()
    scheduler = CollectionScheduler(worker=worker)
    monkeypatch.setattr(api, "create_scheduler", lambda *_args, **_kwargs: scheduler)
    original_close = api.BoundedReadExecutor.close
    def close_reads(self):
        calls.append("reads")
        original_close(self)
    monkeypatch.setattr(api.BoundedReadExecutor, "close", close_reads)
    app = api.create_app(settings_for(tmp_path))
    app.state._resource_stack.callback(lambda: calls.append("resources"))
    assert app.state.collector_worker is worker
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        health = client.get("/healthz?supervisor=1").json()
        assert health["application"] == "burnrate" and health["pid"] == os.getpid()
        assert health["collectorEnabled"] is True
        assert isinstance(health["ingest"]["stalled"], bool)
    assert scheduler.collection_stopping.is_set()
    assert calls == ["worker", "reads", "resources"]


def test_supervisor_health_does_not_claim_collection_when_disabled(tmp_path):
    app = api.create_app(settings_for(tmp_path), enable_scheduler=False)
    with TestClient(app) as client:
        result = client.get("/healthz?supervisor=1").json()
        assert result["collectorEnabled"] is False and result["ingest"] is None
