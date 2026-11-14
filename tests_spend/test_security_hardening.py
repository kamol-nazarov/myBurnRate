"""Regression coverage for the security hardening pass.

These tests use synthetic databases and no live network. They assert the
boundaries that the audit found missing: a fail-closed hub, a Host guard on
reads, a validated bearer, first-account setup tokens, login throttling,
pinned supply-chain inputs and the installer's signature check.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spend_app import auth_throttle
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.db import initialize
from spend_app.setup_wizard import (
    LEGACY_PBKDF2_ITERATIONS,
    PBKDF2_ITERATIONS,
    _hash_password,
    _verify_password,
)
from spend_app.sync import register_machine

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_throttle():
    auth_throttle.reset()
    yield
    auth_throttle.reset()


def _settings(tmp_path: Path, name: str = "hub.db") -> Settings:
    return Settings(
        database_path=tmp_path / name,
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )


def test_reads_reject_an_unrecognized_host(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("BURNRATE_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("BURNRATE_TEST_HOSTS", "0")
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    app = create_app(settings=settings, enable_scheduler=False)
    with TestClient(app, base_url="http://attacker.invalid:17331") as client:
        assert client.get("/api/spend/summary?window=1d").status_code == 403
        assert client.get("/healthz").status_code == 403
        assert (
            client.get(
                "/api/spend/summary?window=1d",
                headers={"Host": "127.0.0.1:17331"},
            ).status_code
            == 200
        )


def test_central_server_without_token_fails_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "")
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    machine = register_machine(settings.database_path, name="Spoke")
    app = create_app(settings=settings, enable_scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1:17331") as client:
        assert client.get("/api/spend/summary?window=1d").status_code == 401
        assert client.get("/api/machines").status_code == 401
        assert (
            client.post(
                "/api/machines",
                headers={
                    "Authorization": "Bearer not-a-real-token",
                    "X-BURNRATE-Request": "1",
                    "Content-Type": "application/json",
                },
                json={"action": "register", "name": "attacker"},
            ).status_code
            == 401
        )
        push = client.post(
            "/api/sync/push",
            headers={"Authorization": f"Bearer {machine['token']}"},
            json={
                "machine": {"name": "Spoke"},
                "usageEvents": [],
                "sessions": [],
                "agentRuns": [],
            },
        )
        assert push.status_code == 200


def test_central_server_with_token_authenticates_reads_and_cookie_mutations(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "admin-secret")
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    app = create_app(settings=settings, enable_scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1:17331") as client:
        assert client.get("/api/spend/summary?window=1d").status_code == 401
        assert (
            client.get(
                "/api/spend/summary?window=1d",
                headers={"Authorization": "Bearer admin-secret"},
            ).status_code
            == 200
        )
        assert client.get("/?token=admin-secret").status_code == 200
        created = client.post(
            "/api/machines",
            headers={"Origin": "http://127.0.0.1:17331", "X-BURNRATE-Request": "1"},
            json={"action": "register", "name": "Cookie Computer"},
        )
        assert created.status_code == 200
        assert created.json()["token"].startswith("br_m_")
        assert (
            client.post(
                "/api/machines",
                headers={
                    "Origin": "https://evil.example",
                    "X-BURNRATE-Request": "1",
                },
                json={"action": "register", "name": "Evil"},
            ).status_code
            == 403
        )


def test_test_hostname_is_not_trusted_in_production(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("BURNRATE_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("BURNRATE_TEST_HOSTS", "0")
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    app = create_app(settings=settings, enable_scheduler=False)
    with TestClient(app, base_url="http://testserver") as client:
        assert client.get("/api/spend/summary?window=1d").status_code == 403


def test_first_account_requires_the_setup_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "admin-secret")
    monkeypatch.setenv("BURNRATE_SETUP_TOKEN", "setup-secret")
    settings = _settings(tmp_path)
    app = create_app(settings=settings, enable_scheduler=False)
    headers = {"Content-Type": "application/json", "X-BURNRATE-Request": "1"}
    body = {
        "username": "owner",
        "password": "long-enough-password",
        "computerName": "Hub",
    }
    with TestClient(app, base_url="http://127.0.0.1:17331") as client:
        assert client.post("/api/account/register", headers=headers, json=body).status_code == 403
        wrong = dict(headers, **{"X-BURNRATE-Setup-Token": "wrong"})
        assert client.post("/api/account/register", headers=wrong, json=body).status_code == 403
        right = dict(headers, **{"X-BURNRATE-Setup-Token": "setup-secret"})
        created = client.post("/api/account/register", headers=right, json=body)
        assert created.status_code == 200
        assert created.json()["token"].startswith("br_m_")
        intruder = dict(body, username="intruder")
        assert client.post("/api/account/register", headers=right, json=intruder).status_code == 403


def test_login_is_throttled_and_answers_generically(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "admin-secret")
    monkeypatch.setenv("BURNRATE_SETUP_TOKEN", "setup-secret")
    settings = _settings(tmp_path)
    app = create_app(settings=settings, enable_scheduler=False)
    headers = {"Content-Type": "application/json", "X-BURNRATE-Request": "1"}
    with TestClient(app, base_url="http://127.0.0.1:17331") as client:
        created = client.post(
            "/api/account/register",
            headers=dict(headers, **{"X-BURNRATE-Setup-Token": "setup-secret"}),
            json={
                "username": "owner",
                "password": "long-enough-password",
                "computerName": "Hub",
            },
        )
        assert created.status_code == 200
        unknown = client.post(
            "/api/account/login",
            headers=headers,
            json={"username": "nobody", "password": "wrong-password"},
        )
        assert unknown.status_code == 401
        assert unknown.json()["error"] == "Invalid username or password."
        for _ in range(5):
            response = client.post(
                "/api/account/login",
                headers=headers,
                json={"username": "owner", "password": "wrong-password"},
            )
        assert response.status_code == 429
        assert response.headers["retry-after"]
        assert response.json()["error"] == "Too many attempts. Try again later."


def test_password_hashing_uses_the_new_work_factor_and_reads_legacy_hashes() -> None:
    salt, digest = _hash_password("long-enough-password")
    assert _verify_password("long-enough-password", salt, digest, PBKDF2_ITERATIONS)
    legacy_salt, legacy_digest = _hash_password(
        "long-enough-password", iterations=LEGACY_PBKDF2_ITERATIONS
    )
    assert _verify_password(
        "long-enough-password", legacy_salt, legacy_digest, LEGACY_PBKDF2_ITERATIONS
    )
    assert not _verify_password(
        "wrong-password", legacy_salt, legacy_digest, LEGACY_PBKDF2_ITERATIONS
    )


def test_machine_and_pairing_tokens_are_redacted() -> None:
    from spend_app.source_evidence import sanitize_reason
    from spend_app.support_report import redact

    machine = sanitize_reason("failed with br_m_abc-123_abcdefghijklmnop")
    assert "br_m_" not in machine and "[redacted]" in machine
    pairing = redact("code brpair_eyJodWJVcmwiOiJ4In0=")
    assert "brpair_" not in pairing and "[redacted]" in pairing


def test_cli_reads_the_token_from_a_file_or_environment(tmp_path, monkeypatch) -> None:
    from spend_app.cli import _resolve_sync_token

    monkeypatch.delenv("BURNRATE_SYNC_TOKEN", raising=False)
    token_file = tmp_path / "token.txt"
    token_file.write_text(" br_m_from_file \n", encoding="utf-8")
    assert _resolve_sync_token(None, str(token_file)) == "br_m_from_file"
    monkeypatch.setenv("BURNRATE_SYNC_TOKEN", "br_m_from_env")
    assert _resolve_sync_token(None, None) == "br_m_from_env"
    assert _resolve_sync_token("br_m_explicit", None) == "br_m_explicit"


def test_desktop_shell_and_supply_chain_inputs_are_pinned() -> None:
    tauri = json.loads((ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    assert isinstance(tauri["app"]["security"]["csp"], str)
    assert "default-src" in tauri["app"]["security"]["csp"]
    capabilities = (ROOT / "src-tauri" / "capabilities" / "default.json").read_text(
        encoding="utf-8"
    )
    assert "127.0.0.1:*" not in capabilities

    workflows = list((ROOT / ".github" / "workflows").glob("*.yml"))
    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "uses:" not in line or "#" in line.split("uses:", 1)[1].split("@", 1)[0]:
                continue
            ref = line.split("uses:", 1)[1].split("#", 1)[0].strip()
            if "@" in ref:
                sha = ref.rsplit("@", 1)[1]
                assert re.fullmatch(r"[0-9a-f]{40}", sha), f"{workflow.name}: {ref}"
        assert "@tauri-apps/cli@2 " not in text
        assert "@tauri-apps/cli@2\n" not in text
    assert (ROOT / "uv.lock").is_file()
    assert (ROOT / "requirements-build.txt").is_file()
    compose = (ROOT / "deploy" / "hub" / "compose.yaml").read_text(encoding="utf-8")
    assert "BURNRATE_ACCESS_TOKEN" in compose
    assert "BURNRATE_ALLOWED_HOSTS" in compose


def test_installer_verifies_the_minisign_signature() -> None:
    installer = (ROOT / "scripts" / "install-native.ps1").read_text(encoding="utf-8")
    assert "Test-MinisignFile" in installer
    assert "minisign-verify.ps1" in installer
    verifier = (ROOT / "scripts" / "minisign-verify.ps1").read_text(encoding="utf-8")
    assert "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IDVDQUNGNEMxNUMxRkNENkQK" in verifier
    assert "Ed25519" in verifier and "Blake2b" in verifier


def test_host_variants_that_could_smuggle_a_loopback_name_are_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("BURNRATE_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("BURNRATE_TEST_HOSTS", "0")
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    app = create_app(settings=settings, enable_scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1:17331") as client:
        for host in (
            "127.0.0.1@evil.example",
            "127.0.0.1.evil.example",
            "127.0.0.1:17331@evil.example",
            "evil.example#127.0.0.1",
            "evil.example/127.0.0.1",
        ):
            assert client.get(
                "/api/spend/summary?window=1d", headers={"Host": host}
            ).status_code == 403, host


def test_anonymous_cannot_reach_any_hub_data_or_setup_route(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "admin-secret")
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    app = create_app(settings=settings, enable_scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1:17331") as client:
        for path in (
            "/api/spend/summary?window=1d",
            "/api/spend/hud",
            "/api/spend/freshness",
            "/api/spend/limits",
            "/api/machines",
            "/api/subscriptions",
            "/api/attention",
            "/api/onboarding",
            "/api/setup/state",
            "/api/setup/notifications",
        ):
            assert client.get(path).status_code == 401, path
        for path, body in (
            ("/api/machines", {"action": "register", "name": "x"}),
            ("/api/setup/consent", {"switches": {"zai": False}}),
            ("/api/setup/notifications", {"kinds": {"plan": False}}),
            ("/api/sync/trigger", {}),
            ("/api/spend/quotas/refresh", {}),
        ):
            assert (
                client.post(
                    path,
                    headers={"X-BURNRATE-Request": "1", "Content-Type": "application/json"},
                    json=body,
                ).status_code
                == 401
            ), path


def test_setup_token_can_arrive_in_the_body(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "admin-secret")
    monkeypatch.setenv("BURNRATE_SETUP_TOKEN", "setup-secret")
    settings = _settings(tmp_path)
    app = create_app(settings=settings, enable_scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1:17331") as client:
        created = client.post(
            "/api/account/register",
            headers={"Content-Type": "application/json", "X-BURNRATE-Request": "1"},
            json={
                "username": "owner",
                "password": "long-enough-password",
                "computerName": "Hub",
                "setupToken": "setup-secret",
            },
        )
        assert created.status_code == 200


def test_throttle_expires_and_success_clears_failures() -> None:
    from spend_app.auth_throttle import Throttle

    clock = 1000.0
    throttle = Throttle(window=10, max_failures=2, lockout=5)
    assert throttle.fail("k", now=clock) == 0
    assert throttle.fail("k", now=clock + 1) == 6
    assert throttle.retry_after("k", now=clock + 2) > 0
    assert throttle.retry_after("k", now=clock + 7) == 0
    assert throttle.fail("k", now=clock + 7) == 0
    throttle.succeed("k")
    assert throttle.retry_after("k", now=clock + 7) == 0


def test_rust_audit_policy_and_zip_removal_are_committed() -> None:
    cargo = (ROOT / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
    assert 'default-features = false, features = ["rustls-tls"]' in cargo
    assert (ROOT / "audit.toml").is_file()
    lock = (ROOT / "src-tauri" / "Cargo.lock").read_text(encoding="utf-8")
    assert 'name = "zip"' not in lock
    assert 'version = "1.1.6"' in lock
