"""v4 remediation surface: hub shell redirects and supervisor health."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spend_app import auth_throttle, setup_wizard, sync
from spend_app.api import create_app
from spend_app.config import Settings

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "synthetic-long-password"


def settings_at(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "spend.db",
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )


@pytest.fixture(autouse=True)
def _clean_throttles():
    auth_throttle.reset()
    setup_wizard._GLOBAL_THROTTLE.reset()
    sync.clear_hub_proxy_cache()
    yield
    auth_throttle.reset()
    setup_wizard._GLOBAL_THROTTLE.reset()
    sync.clear_hub_proxy_cache()


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "synthetic-break-glass")
    monkeypatch.setenv("BURNRATE_SETUP_TOKEN", "synthetic-setup-token-32-chars-00")
    monkeypatch.setenv("BURNRATE_ALLOW_ADMIN_BEARER", "1")
    app = create_app(settings_at(tmp_path), enable_scheduler=False)
    with TestClient(app, base_url="http://hub.test") as client:
        yield client


def test_anonymous_hub_shells_redirect_and_health_is_minimal(hub):
    for path in ("/", "/hud", "/setup"):
        response = hub.get(path, follow_redirects=False)
        assert response.status_code == 303, (path, response.status_code)
        assert response.headers["location"] == "/auth"
    assert hub.get("/auth").status_code == 200
    assert hub.get("/spend.css").status_code == 200
    assert hub.get("/healthz").json() == {"status": "ok"}
    assert hub.get("/healthz?supervisor=1").status_code == 401


def test_owner_session_unlocks_pages_and_supervisor_health(hub):
    registered = hub.post("/api/account/register", headers={"X-BURNRATE-Request": "1"}, json={
        "username": "synthetic-owner",
        "password": PASSWORD,
        "setupToken": "synthetic-setup-token-32-chars-00",
        "sessionOnly": True,
    })
    assert registered.status_code == 200, registered.text
    assert "sessionToken" not in registered.json()
    session = registered.cookies["burnrate_access"]
    assert hub.get("/hud").status_code == 200
    assert hub.get("/setup").status_code == 200
    health = hub.get("/healthz?supervisor=1", headers={"Authorization": "Bearer " + session})
    assert health.status_code == 200 and health.json()["application"] == "burnrate"


def test_legacy_token_url_exchanges_for_a_session_cookie(hub):
    response = hub.get("/?token=synthetic-break-glass", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers.get("set-cookie", "")
    assert "burnrate_access=br_s_" in cookie
    assert "synthetic-break-glass" not in cookie


def test_local_install_keeps_supervisor_health_public(tmp_path, monkeypatch):
    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    monkeypatch.delenv("BURNRATE_ACCESS_TOKEN", raising=False)
    app = create_app(settings_at(tmp_path), enable_scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1:17331") as client:
        assert client.get("/hud").status_code == 200
        assert client.get("/healthz?supervisor=1").status_code == 200
