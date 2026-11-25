"""Regression coverage for the security hardening pass.

These tests use synthetic databases and no live network. They assert the
boundaries that the audit found missing: a fail-closed hub, a Host guard on
reads, a validated bearer, first-account setup tokens, login throttling,
pinned supply-chain inputs and the installer's signature check.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import spend_app
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


def _assert_compose_images_pinned(
    compose: str, *, changelog: str, app_version: str
) -> None:
    assert re.search(r"caddy:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", compose)
    hub_refs = re.findall(r"myburnrate-hub:([^\s\"'}]+)", compose)
    assert hub_refs, "deploy/hub/compose.yaml must reference myburnrate-hub"

    unreleased = bool(
        re.search(
            rf"^## \[{re.escape(app_version)}\] - Unreleased\s*$",
            changelog,
            re.MULTILINE,
        )
    )
    digest_re = re.compile(r"^\d+\.\d+\.\d+(?:-rc\.\d+)?@sha256:[0-9a-f]{64}$")
    tag_only_re = re.compile(r"^(\d+\.\d+\.\d+)$")
    for ref in hub_refs:
        if digest_re.fullmatch(ref):
            continue
        tag_match = tag_only_re.fullmatch(ref)
        assert tag_match is not None, f"Invalid hub image pin: {ref!r}"
        assert tag_match.group(1) == app_version, (
            f"Tag-only pin {ref!r} does not match app_version {app_version!r}"
        )
        assert unreleased, (
            f"Tag-only pin {ref!r} is forbidden once CHANGELOG.md has a release date"
        )


@pytest.fixture(autouse=True)
def _clean_throttle():
    from spend_app import setup_wizard

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
    from spend_app.local_access import read_local_key

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("BURNRATE_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("BURNRATE_TEST_HOSTS", "0")
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    app = create_app(settings=settings, enable_scheduler=False)
    key = read_local_key(settings.database_path)
    assert key
    with TestClient(app, base_url="http://attacker.invalid:17331") as client:
        assert client.get("/api/spend/summary?window=1d").status_code == 403
        assert client.get("/healthz").status_code == 403
        assert client.get("/api/spend/summary?window=1d", headers={"Host": "127.0.0.1:17331"}).status_code == 401
        assert (
            client.get(
                "/api/spend/summary?window=1d",
                headers={"Host": "127.0.0.1:17331", "Authorization": f"Bearer {key}"},
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
    with TestClient(app, base_url="https://127.0.0.1:17331") as client:
        assert client.get("/api/spend/summary?window=1d").status_code == 401
        assert (
            client.get(
                "/api/spend/summary?window=1d",
                headers={"Authorization": "Bearer admin-secret"},
            ).status_code
            == 200
        )
        removed = client.get("/?token=admin-secret")
        assert removed.status_code == 404
        assert removed.json()["error"]["code"] == "REQ-404"
        assert "burnrate_access" not in client.cookies
        boot = client.post(
            "/api/auth/bootstrap",
            headers={
                "Authorization": "Bearer admin-secret",
                "Origin": "https://127.0.0.1:17331",
                "X-BURNRATE-Request": "1",
            },
            json={},
        )
        assert boot.status_code == 200
        primary = next(cookie for cookie in boot.headers.get_list("set-cookie") if cookie.startswith("__Host-"))
        assert "Secure" in primary and "HttpOnly" in primary and "Path=/" in primary
        created = client.post(
            "/api/machines",
            headers={"Origin": "https://127.0.0.1:17331", "X-BURNRATE-Request": "1"},
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


def test_first_account_requires_recovery_authorization_and_closes_afterward(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "admin-secret")
    monkeypatch.delenv("BURNRATE_SETUP_TOKEN", raising=False)
    settings = _settings(tmp_path)
    app = create_app(settings=settings, enable_scheduler=False)
    headers = {"Content-Type": "application/json", "X-BURNRATE-Request": "1", "Origin": "https://127.0.0.1:17331"}
    body = {
        "username": "owner@example.com",
        "password": "long-enough-password",
        "computerName": "Hub",
    }
    with TestClient(app, base_url="https://127.0.0.1:17331") as client:
        missing = client.post("/api/account/register", headers=headers, json=body)
        assert missing.status_code == 403 and missing.json()["error"] == {"code": "AUTH-110"}
        created = client.post("/api/account/register", headers={**headers, "Authorization": "Bearer admin-secret"}, json=body)
        assert created.status_code == 200
        assert "token" not in created.json() and "sessionToken" not in created.json()
        machine = client.post("/api/machines", headers=headers, json={"action": "register", "name": "Hub"})
        assert machine.status_code == 200
        assert machine.json()["token"].startswith("br_m_")
        intruder = dict(body, username="intruder@example.com")
        closed = client.post("/api/account/register", headers=headers, json=intruder)
        assert closed.status_code == 403 and closed.json()["error"] == {"code": "AUTH-103"}
        same = client.post("/api/account/register", headers=headers, json=body)
        assert same.status_code == 403 and same.json()["error"] == {"code": "AUTH-103"}


def test_login_is_throttled_and_answers_generically(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "admin-secret")
    settings = _settings(tmp_path)
    app = create_app(settings=settings, enable_scheduler=False)
    headers = {"Content-Type": "application/json", "X-BURNRATE-Request": "1", "Origin": "http://127.0.0.1:17331"}
    with TestClient(app, base_url="http://127.0.0.1:17331") as client:
        created = client.post(
            "/api/account/register",
            headers={**headers, "Authorization": "Bearer admin-secret"},
            json={
                "username": "owner@example.com",
                "password": "long-enough-password",
                "computerName": "Hub",
            },
        )
        assert created.status_code == 200
        unknown = client.post(
            "/api/account/login",
            headers=headers,
            json={"username": "nobody@example.com", "password": "wrong-password"},
        )
        assert unknown.status_code == 401
        assert unknown.json()["error"] == {"code": "AUTH-101"}
        for _ in range(5):
            response = client.post(
                "/api/account/login",
                headers=headers,
                json={"username": "owner@example.com", "password": "wrong-password"},
            )
        assert response.status_code == 429
        assert response.headers["retry-after"]
        assert response.json()["error"]["code"] == "AUTH-102"


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
    local_session = sanitize_reason("cookie br_l_abc-123_abcdefghijklmnop")
    assert "br_l_" not in local_session and "[redacted]" in local_session
    pairing = redact("code brpair_eyJodWJVcmwiOiJ4In0=")
    assert "brpair_" not in pairing and "[redacted]" in pairing


def test_cli_reads_the_token_from_a_file_or_environment(tmp_path, monkeypatch) -> None:
    from spend_app.cli import _resolve_sync_token

    monkeypatch.delenv("BURNRATE_SYNC_TOKEN", raising=False)
    token_file = tmp_path / "token.txt"
    token_file.write_text(" br_m_from_file \n", encoding="utf-8")
    assert _resolve_sync_token(str(token_file)) == "br_m_from_file"
    monkeypatch.setenv("BURNRATE_SYNC_TOKEN", "br_m_from_env")
    assert _resolve_sync_token(None) == "br_m_from_env"
    assert _resolve_sync_token(str(token_file)) == "br_m_from_env"


def test_desktop_shell_and_supply_chain_inputs_are_pinned() -> None:
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
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "BURNRATE_ACCESS_TOKEN" in compose
    assert "BURNRATE_ALLOWED_HOSTS" in compose
    _assert_compose_images_pinned(
        compose, changelog=changelog, app_version=spend_app.__version__
    )
    prices = (ROOT / ".github" / "workflows" / "prices.yml").read_text(encoding="utf-8")
    assert "--require-hashes" in prices and "requirements-build.txt" in prices


def test_compose_image_pin_rule_rejects_tag_only_on_dated_changelog_or_version_mismatch() -> None:
    caddy_line = (
        "image: caddy:2.11.9@sha256:4e2425f6009577e7c651010f16679c28f7d12df3e7b1914f941a7a3370746b3c\n"
    )
    tag_only_compose = (
        caddy_line
        + "image: ${BURNRATE_HUB_IMAGE:-ghcr.io/kamol-nazarov/myburnrate-hub:0.5.9}\n"
    )
    digest_compose = (
        caddy_line
        + "image: ${BURNRATE_HUB_IMAGE:-ghcr.io/kamol-nazarov/myburnrate-hub:0.5.9@sha256:"
        + ("b" * 64)
        + "}\n"
    )
    rc_digest_compose = (
        caddy_line
        + "image: ${BURNRATE_HUB_IMAGE:-ghcr.io/kamol-nazarov/myburnrate-hub:0.5.9-rc.1@sha256:"
        + ("b" * 64)
        + "}\n"
    )
    unreleased_changelog = "# Changelog\n\n## [0.5.9] - Unreleased\n"
    dated_changelog = "# Changelog\n\n## [0.5.9] - 2026-11-17\n"

    # Negative 1: dated changelog + tag-only pin must fail
    with pytest.raises(AssertionError):
        _assert_compose_images_pinned(
            tag_only_compose, changelog=dated_changelog, app_version="0.5.9"
        )

    # Negative 2: mismatched version + tag-only pin must fail even with Unreleased
    with pytest.raises(AssertionError):
        _assert_compose_images_pinned(
            caddy_line + "image: ghcr.io/kamol-nazarov/myburnrate-hub:0.5.8\n",
            changelog=unreleased_changelog,
            app_version="0.5.9",
        )
    with pytest.raises(AssertionError):
        _assert_compose_images_pinned(
            caddy_line + "image: ghcr.io/kamol-nazarov/myburnrate-hub:0.6.0\n",
            changelog=unreleased_changelog,
            app_version="0.5.9",
        )

    # Negative 3: tag-only rc pin (without @sha256:) must fail even when Unreleased
    with pytest.raises(AssertionError):
        _assert_compose_images_pinned(
            caddy_line + "image: ${BURNRATE_HUB_IMAGE:-ghcr.io/kamol-nazarov/myburnrate-hub:0.5.9-rc.1}\n",
            changelog=unreleased_changelog,
            app_version="0.5.9",
        )

    # Negative 4: non-rc suffix with digest must fail
    with pytest.raises(AssertionError):
        _assert_compose_images_pinned(
            caddy_line
            + "image: ${BURNRATE_HUB_IMAGE:-ghcr.io/kamol-nazarov/myburnrate-hub:0.5.9-dirty@sha256:"
            + ("b" * 64)
            + "}\n",
            changelog=unreleased_changelog,
            app_version="0.5.9",
        )

    # Positive: digest-pinned (final or rc candidate) passes regardless of changelog dating
    for good_compose in (digest_compose, rc_digest_compose):
        _assert_compose_images_pinned(
            good_compose, changelog=unreleased_changelog, app_version="0.5.9"
        )
        _assert_compose_images_pinned(
            good_compose, changelog=dated_changelog, app_version="0.5.9"
        )


def test_installer_verifies_the_minisign_signature() -> None:
    installer = (ROOT / "scripts" / "install-native.ps1").read_text(encoding="utf-8")
    assert "Test-MinisignFile" in installer
    assert "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IDVDQUNGNEMxNUMxRkNENkQK" in installer
    assert "Ed25519" in installer and "Blake2b" in installer
    verifier = (ROOT / "scripts" / "minisign-verify.ps1").read_text(encoding="utf-8")
    assert "Test-MinisignFile" in verifier
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


def test_setup_token_in_body_is_ignored_on_first_registration(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "admin-secret")
    monkeypatch.setenv("BURNRATE_SETUP_TOKEN", "setup-secret-at-least-32-chars-1")
    settings = _settings(tmp_path)
    app = create_app(settings=settings, enable_scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1:17331") as client:
        created = client.post(
            "/api/account/register",
            headers={"Content-Type": "application/json", "X-BURNRATE-Request": "1"},
            json={
                "username": "owner@example.com",
                "password": "long-enough-password",
                "computerName": "Hub",
                "setupToken": "setup-secret-at-least-32-chars-1",
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


def test_machine_token_can_only_manage_itself(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "admin-secret")
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    own = register_machine(settings.database_path, name="OwnLaptop")
    other = register_machine(settings.database_path, name="OtherLaptop")
    app = create_app(settings=settings, enable_scheduler=False)
    headers = {
        "Authorization": f"Bearer {own['token']}",
        "X-BURNRATE-Request": "1",
        "Content-Type": "application/json",
    }
    with TestClient(app, base_url="http://127.0.0.1:17331") as client:
        renamed = client.post(
            "/api/machines",
            headers=headers,
            json={"action": "rename", "id": own["machine"]["id"], "name": "Renamed"},
        )
        assert renamed.status_code == 200
        foreign = client.post(
            "/api/machines",
            headers=headers,
            json={"action": "rename", "id": other["machine"]["id"], "name": "Nope"},
        )
        assert foreign.status_code == 403
        created = client.post(
            "/api/machines",
            headers=headers,
            json={"action": "register", "name": "Intruder"},
        )
        assert created.status_code == 403


def test_anonymous_account_bodies_are_bounded(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "admin-secret")
    settings = _settings(tmp_path)
    app = create_app(settings=settings, enable_scheduler=False)
    with TestClient(app, base_url="http://127.0.0.1:17331") as client:
        oversized = b'{"username":"a","password":"b","padding":"' + b"x" * 70000 + b'"}'
        response = client.post(
            "/api/account/login",
            headers={"Content-Type": "application/json", "X-BURNRATE-Request": "1"},
            content=oversized,
        )
        assert response.status_code == 422
        assert response.json()["error"] == {"code": "REQ-401"}
