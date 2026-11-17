"""v4 remediation surface: removed web pages return REQ-404 JSON and supervisor health."""
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


def test_anonymous_hub_removed_pages_return_404_and_health_is_minimal(hub):
    for path in ("/", "/hud", "/setup", "/auth", "/connections", "/spend.css"):
        response = hub.get(path, follow_redirects=False)
        assert response.status_code == 404, (path, response.status_code)
        assert response.json()["error"]["code"] == "REQ-404"
    assert hub.get("/healthz").json() == {"status": "ok"}
    assert hub.get("/healthz?supervisor=1").status_code == 401


def test_owner_session_unlocks_supervisor_health_while_pages_stay_404(hub):
    registered = hub.post("/api/account/register", headers={"X-BURNRATE-Request": "1"}, json={
        "username": "synthetic-owner@example.com",
        "password": PASSWORD,
        "sessionOnly": True,
    })
    assert registered.status_code == 200, registered.text
    assert "sessionToken" not in registered.json()
    session = registered.cookies["burnrate_access"]
    for path in ("/hud", "/setup", "/auth", "/connections"):
        res = hub.get(path, follow_redirects=False)
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "REQ-404"
    health = hub.get("/healthz?supervisor=1", headers={"Authorization": "Bearer " + session})
    assert health.status_code == 200 and health.json()["application"] == "burnrate"


def test_legacy_token_url_returns_404_and_mints_no_cookie(hub):
    response = hub.get("/?token=synthetic-break-glass", follow_redirects=False)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REQ-404"
    assert "set-cookie" not in response.headers
    assert "synthetic-break-glass" not in response.text


def test_local_install_keeps_supervisor_health_public_and_pages_404(tmp_path, monkeypatch):
    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    monkeypatch.delenv("BURNRATE_ACCESS_TOKEN", raising=False)
    app = create_app(settings_at(tmp_path), enable_scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1:17331") as client:
        for path in ("/", "/hud", "/setup", "/auth", "/connections"):
            res = client.get(path, follow_redirects=False)
            assert res.status_code == 404
            assert res.json()["error"]["code"] == "REQ-404"
        assert client.get("/healthz?supervisor=1").status_code == 200
