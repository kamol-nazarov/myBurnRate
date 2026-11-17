"""The HUD API endpoints and spoke/hub caching behavior."""

from __future__ import annotations

import json
import secrets
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spend_app import auth_model, sync as sync_module
from spend_app.api import HUD_HUB_STALE_SECONDS, create_app
from spend_app.config import Settings
from spend_app.db import connect, initialize
from spend_app.setup_wizard import write_setup_state

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 22, 16, tzinfo=UTC)


def _spoke(tmp_path: Path, monkeypatch, hub, *, owner_session: str = "synthetic-owner-session",
           db_name: str = "hud.db") -> Settings:
    settings = Settings(database_path=tmp_path / db_name, pricing_path=ROOT / "pricing",
                        anthropic_admin_key=None, openai_admin_key=None, timezone="UTC",
                        cache_hit_threshold=0.75, over_routing_token_ceiling=40000)
    initialize(settings.database_path)
    from spend_app.connection_secrets import publish_spoke_credentials
    def _apply(_conn, st):
        st["role"] = "spoke"
        st["spokeConfig"] = {**st.get("spokeConfig", {}), "enabled": True, "hubUrl": "https://hub.test"}
    publish_spoke_credentials(
        settings.database_path,
        machine_token="spoke-token",
        owner_session=owner_session,
        apply_state=_apply,
    )
    monkeypatch.setitem(sync_module._HUB_TEST_CLIENTS, "https://hub.test", hub)
    sync_module.clear_hub_proxy_cache()
    return settings


class FakeResponse:
    def __init__(self, body: dict, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code

    def json(self) -> dict:
        return json.loads(json.dumps(self._body))


def _wait_for_hub_renewal(deadline_s: float = 5) -> None:
    deadline = time.monotonic() + deadline_s
    while sync_module._HUB_PROXY_RENEWING and time.monotonic() < deadline:
        time.sleep(0.01)


def test_spoke_hud_serves_the_last_hub_copy_while_renewing_it(tmp_path, monkeypatch):
    calls = []
    release = threading.Event()

    class SlowHub:
        def get(self, path, params=None, headers=None):
            calls.append(path)
            if len(calls) > 1:
                release.wait(5)
            return FakeResponse({"capacity": [], "read": len(calls)})

    settings = _spoke(tmp_path, monkeypatch, SlowHub())
    fetch = sync_module.fetch_from_configured_hub
    # A cold HUD read does not wait on the Hub. The copy fills in the background.
    started = time.monotonic()
    assert fetch(settings, "/api/spend/hud", stale_seconds=300) is None
    assert time.monotonic() - started < 1
    _wait_for_hub_renewal()
    assert fetch(settings, "/api/spend/hud", stale_seconds=300)["read"] == 1
    started = time.monotonic()
    # The copy is past its TTL: it is served at once while the Hub is slow.
    assert fetch(settings, "/api/spend/hud", ttl_seconds=0, stale_seconds=300)["read"] == 1
    assert fetch(settings, "/api/spend/hud", ttl_seconds=0, stale_seconds=300)["read"] == 1
    assert time.monotonic() - started < 1
    release.set()
    _wait_for_hub_renewal()
    assert len(calls) == 2, "one background read renews the copy"
    assert fetch(settings, "/api/spend/hud", ttl_seconds=60, stale_seconds=300)["read"] == 2


def test_a_hung_hub_does_not_block_a_hud_fetch(tmp_path, monkeypatch):
    started_read = threading.Event()
    release = threading.Event()

    class HungHub:
        def get(self, path, params=None, headers=None):
            started_read.set()
            release.wait(5)
            return FakeResponse({"capacity": [], "read": 1})

    settings = _spoke(tmp_path, monkeypatch, HungHub())
    fetch = sync_module.fetch_from_configured_hub
    started = time.monotonic()
    assert fetch(settings, "/api/spend/hud/usage", stale_seconds=300) is None
    assert time.monotonic() - started < 1
    assert started_read.wait(5)
    release.set()
    _wait_for_hub_renewal()
    assert fetch(settings, "/api/spend/hud/usage", stale_seconds=300)["read"] == 1


def test_spoke_sync_keeps_the_last_hub_copy(tmp_path, monkeypatch):
    release = threading.Event()
    primed = False

    class Hub:
        def get(self, path, params=None, headers=None):
            nonlocal primed
            if primed:
                release.wait(5)
                return FakeResponse({}, status_code=503)
            primed = True
            return FakeResponse({"capacity": [], "read": 1})

        def post(self, path, json=None, headers=None):
            return FakeResponse({"ok": True, "eventsWritten": 0})

    settings = _spoke(tmp_path, monkeypatch, Hub())
    fetch = sync_module.fetch_from_configured_hub
    assert fetch(settings, "/api/spend/hud/usage")["read"] == 1
    from spend_app.pricing import PricingEngine
    from spend_app.setup_wizard import maybe_run_configured_spoke_sync

    result = maybe_run_configured_spoke_sync(settings, PricingEngine.load(settings.pricing_path))
    assert result is not None and result.get("ok") is True
    started = time.monotonic()
    body = fetch(settings, "/api/spend/hud/usage", stale_seconds=300)
    assert time.monotonic() - started < 1
    assert body["read"] == 1
    release.set()
    _wait_for_hub_renewal()


def test_expired_hub_copy_still_covers_a_failed_hub_read(tmp_path, monkeypatch):
    responses = [FakeResponse({"capacity": [], "read": 1}), FakeResponse({}, status_code=503)]

    class FlakyHub:
        def get(self, path, params=None, headers=None):
            return responses.pop(0)

    settings = _spoke(tmp_path, monkeypatch, FlakyHub())
    fetch = sync_module.fetch_from_configured_hub
    assert fetch(settings, "/api/spend/hud")["read"] == 1
    # New local data expires the copy; the next read goes to the Hub, and a
    # failed read falls back to the last copy instead of a local-only view.
    sync_module.expire_hub_proxy_cache()
    assert fetch(settings, "/api/spend/hud")["read"] == 1
    assert not responses


def test_stale_hub_copy_past_stale_seconds_is_not_served_on_recovery_or_failure(tmp_path, monkeypatch):
    responses = [
        FakeResponse({"capacity": [], "providers": [], "agents": [], "machines": [], "read": 1}),
        FakeResponse({"capacity": [], "providers": [], "agents": [], "machines": [], "read": 2}),
        FakeResponse({}, status_code=503),
        FakeResponse({}, status_code=503),
    ]
    calls = []

    class Hub:
        def get(self, path, params=None, headers=None):
            calls.append(path)
            return responses.pop(0)

    session = f"synthetic-stale-{secrets.token_hex(8)}"
    settings = _spoke(tmp_path, monkeypatch, Hub(), owner_session=session)
    try:
        fetch = sync_module.fetch_from_configured_hub
        # Prime the cache synchronously.
        first = fetch(settings, "/api/spend/hud", {"machine": "all"}, ttl_seconds=60)
        assert first is not None and first["read"] == 1
        assert fetch(settings, "/api/spend/hud", {"machine": "all"},
                     ttl_seconds=HUD_HUB_STALE_SECONDS * 2, stale_seconds=HUD_HUB_STALE_SECONDS)["read"] == 1

        def age_cache_past_stale_limit():
            with sync_module._HUB_PROXY_LOCK:
                for key, (fetched_at, payload, fresh) in list(sync_module._HUB_PROXY_CACHE.items()):
                    sync_module._HUB_PROXY_CACHE[key] = (
                        fetched_at - HUD_HUB_STALE_SECONDS - 1,
                        payload,
                        fresh,
                    )

        # Past stale_seconds, even a large ttl_seconds does not serve the stale copy;
        # the hub is queried synchronously and the fresh copy is returned.
        age_cache_past_stale_limit()
        second = fetch(settings, "/api/spend/hud", {"machine": "all"},
                       ttl_seconds=HUD_HUB_STALE_SECONDS * 2, stale_seconds=HUD_HUB_STALE_SECONDS)
        assert second is not None and second["read"] == 2
        assert len(calls) == 2

        # Past stale_seconds, if the hub fails with 503, the stale copy is never served.
        age_cache_past_stale_limit()
        failed = fetch(settings, "/api/spend/hud", {"machine": "all"},
                       ttl_seconds=60, stale_seconds=HUD_HUB_STALE_SECONDS)
        assert failed is None
        assert len(calls) == 3

        # Through the /api/spend/hud endpoint, an aged-out copy + 503 falls back to local-only.
        with TestClient(create_app(settings, enable_scheduler=False, now=NOW)) as client:
            hud_res = client.get("/api/spend/hud", params={"machine": "all"})
            assert hud_res.status_code == 200
            assert hud_res.json()["scope"] == "local-fallback"
            assert not responses
    finally:
        sync_module.clear_hub_proxy_cache()


@pytest.mark.parametrize("status_code", [401, 403])
def test_hub_auth_rejection_empties_only_that_credentials_proxy_cache(tmp_path, monkeypatch, status_code):
    victim_token = f"synthetic-victim-{status_code}-{secrets.token_hex(8)}"
    other_token = f"synthetic-other-{status_code}-{secrets.token_hex(8)}"
    victim_digest = auth_model.digest(victim_token)
    other_digest = auth_model.digest(other_token)
    reject_next = {"flag": False}
    hub_calls = []

    class Hub:
        def get(self, path, params=None, headers=None):
            auth = (headers or {}).get("Authorization", "")
            hub_calls.append((path, auth))
            if reject_next["flag"] and auth == f"Bearer {victim_token}":
                return FakeResponse({}, status_code=status_code)
            return FakeResponse({"capacity": [], "path": path, "auth": auth})

    hub = Hub()
    victim_settings = _spoke(tmp_path, monkeypatch, hub, owner_session=victim_token, db_name="victim.db")
    # Configure second spoke with a different credential without clearing cache.
    other_settings = Settings(database_path=tmp_path / "other.db", pricing_path=ROOT / "pricing",
                              anthropic_admin_key=None, openai_admin_key=None, timezone="UTC",
                              cache_hit_threshold=0.75, over_routing_token_ceiling=40000)
    initialize(other_settings.database_path)
    from spend_app.connection_secrets import publish_spoke_credentials
    def _apply_other(_conn, st):
        st["role"] = "spoke"
        st["spokeConfig"] = {**st.get("spokeConfig", {}), "enabled": True, "hubUrl": "https://hub.test"}
    publish_spoke_credentials(
        other_settings.database_path,
        machine_token="spoke-token",
        owner_session=other_token,
        apply_state=_apply_other,
    )

    try:
        fetch = sync_module.fetch_from_configured_hub
        assert fetch(victim_settings, "/api/spend/hud", ttl_seconds=60)["path"] == "/api/spend/hud"
        assert fetch(victim_settings, "/api/spend/hud/usage", ttl_seconds=60)["path"] == "/api/spend/hud/usage"
        assert fetch(other_settings, "/api/spend/hud", ttl_seconds=60)["path"] == "/api/spend/hud"

        with sync_module._HUB_PROXY_LOCK:
            victim_keys = [k for k in sync_module._HUB_PROXY_CACHE if len(k) > 3 and k[3] == victim_digest]
            other_keys = [k for k in sync_module._HUB_PROXY_CACHE if len(k) > 3 and k[3] == other_digest]
            assert len(victim_keys) == 2
            assert len(other_keys) == 1

        # Trigger 401/403 on one endpoint for victim_token; ALL cached entries for victim_token
        # must be dropped immediately, while other_token's cache stays intact.
        reject_next["flag"] = True
        assert fetch(victim_settings, "/api/spend/hud/usage", ttl_seconds=0) is None

        with sync_module._HUB_PROXY_LOCK:
            assert victim_digest in sync_module._HUB_REJECTED_CREDENTIALS
            assert other_digest not in sync_module._HUB_REJECTED_CREDENTIALS
            assert not [k for k in sync_module._HUB_PROXY_CACHE if len(k) > 3 and k[3] == victim_digest]
            assert len([k for k in sync_module._HUB_PROXY_CACHE if len(k) > 3 and k[3] == other_digest]) == 1

        # Subsequent reads for the rejected credential on another previously-cached path
        # return None immediately without contacting the hub.
        calls_before = len(hub_calls)
        assert fetch(victim_settings, "/api/spend/hud", stale_seconds=300) is None
        assert len(hub_calls) == calls_before

        # The other credential's cached copy is still served.
        assert fetch(other_settings, "/api/spend/hud", ttl_seconds=60)["path"] == "/api/spend/hud"
    finally:
        sync_module.clear_hub_proxy_cache()


@pytest.mark.parametrize("status_code", [401, 403])
def test_inflight_200_does_not_refill_cache_after_rejection(tmp_path, monkeypatch, status_code):
    token = f"synthetic-inflight-{status_code}-{secrets.token_hex(8)}"
    token_digest = auth_model.digest(token)
    entered_first = threading.Event()
    release_first = threading.Event()

    class RacingHub:
        def get(self, path, params=None, headers=None):
            if path == "/api/spend/hud":
                entered_first.set()
                assert release_first.wait(5), "Timed out waiting for concurrent rejection"
                return FakeResponse({"capacity": [], "providers": [], "read": "stale-inflight"})
            return FakeResponse({}, status_code=status_code)

    settings = _spoke(tmp_path, monkeypatch, RacingHub(), owner_session=token)
    fetch = sync_module.fetch_from_configured_hub
    try:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as pool:
            first_future = pool.submit(fetch, settings, "/api/spend/hud", ttl_seconds=0)
            assert entered_first.wait(5)
            # Second concurrent request gets 401/403 and purges/rejects the credential.
            assert fetch(settings, "/api/spend/hud/usage", ttl_seconds=0) is None
            release_first.set()
            assert first_future.result(timeout=5) is None

        with sync_module._HUB_PROXY_LOCK:
            assert token_digest in sync_module._HUB_REJECTED_CREDENTIALS
            assert not [k for k in sync_module._HUB_PROXY_CACHE if len(k) > 3 and k[3] == token_digest]
        assert fetch(settings, "/api/spend/hud", ttl_seconds=60, stale_seconds=300) is None
    finally:
        sync_module.clear_hub_proxy_cache()


@pytest.mark.parametrize("status_code", [401, 403])
def test_stale_cache_return_is_fenced_when_background_refresh_rejects(tmp_path, monkeypatch, status_code):
    token = f"synthetic-stale-fence-{status_code}-{secrets.token_hex(8)}"
    token_digest = auth_model.digest(token)
    reject_next = {"flag": False}

    class Hub:
        def get(self, path, params=None, headers=None):
            if reject_next["flag"]:
                return FakeResponse({}, status_code=status_code)
            return FakeResponse({"capacity": [], "providers": [], "read": "initial"})

    settings = _spoke(tmp_path, monkeypatch, Hub(), owner_session=token)
    fetch = sync_module.fetch_from_configured_hub
    try:
        primed = fetch(settings, "/api/spend/hud", ttl_seconds=60)
        assert primed is not None and primed["read"] == "initial"

        # Force the stale-serving branch's background renewal to complete (with 401/403)
        # before fetch_from_configured_hub can return the cached payload.
        bg_finished = threading.Event()
        orig_renew = sync_module._renew_hub_copy_in_background

        def sync_background_renew(cache_key, read):
            def wrapped_read():
                try:
                    return read()
                finally:
                    bg_finished.set()

            orig_renew(cache_key, wrapped_read)
            assert bg_finished.wait(5), "Background refresh did not finish in time"

        monkeypatch.setattr(sync_module, "_renew_hub_copy_in_background", sync_background_renew)
        reject_next["flag"] = True

        result = fetch(settings, "/api/spend/hud", ttl_seconds=0, stale_seconds=300)
        assert result is None
        with sync_module._HUB_PROXY_LOCK:
            assert token_digest in sync_module._HUB_REJECTED_CREDENTIALS
            assert not [k for k in sync_module._HUB_PROXY_CACHE if len(k) > 3 and k[3] == token_digest]
    finally:
        sync_module.clear_hub_proxy_cache()
