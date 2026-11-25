import base64
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient


def test_local_api_requires_install_key(tmp_path, monkeypatch):
    from spend_app.api import create_app
    from spend_app.auth_model import extract_cookie
    from spend_app.config import load_settings
    from spend_app.local_access import key_path, read_local_key

    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    monkeypatch.delenv("BURNRATE_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("SPEND_DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("BURNRATE_ENABLE_DASHBOARD", "1")
    settings = load_settings()
    app = create_app(settings, enable_scheduler=False)
    key = read_local_key(settings.database_path)
    assert key and len(key) == 64
    if os.name != "nt":
        assert stat.S_IMODE(key_path(settings.database_path).stat().st_mode) == 0o600
    else:
        verification = (
            "$ErrorActionPreference = 'Stop'; "
            "$acl = [System.IO.File]::GetAccessControl($env:BURNRATE_FIXTURE_KEY); "
            "$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
            "$expected = @($user,'S-1-5-18','S-1-5-32-544'); "
            "$rules = $acl.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier]); "
            "$unexpected = @($rules | Where-Object { $_.IsInherited -or "
            "$_.IdentityReference.Value -notin $expected }); "
            "if (-not $acl.AreAccessRulesProtected -or $rules.Count -ne 3 "
            "-or $unexpected.Count -ne 0) { exit 1 }"
        )
        encoded = base64.b64encode(verification.encode("utf-16-le")).decode("ascii")
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            env={**os.environ, "BURNRATE_FIXTURE_KEY": str(key_path(settings.database_path))},
            capture_output=True, timeout=10,
        )
        assert result.returncode == 0, "Local key ACL must allow exactly the current user, SYSTEM and Administrators."
    with TestClient(app) as client:
        for page in ("/", "/hud", "/setup", "/auth", "/connections"):
            missing = client.get(page)
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "REQ-404"
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/hub/info").status_code == 200
        assert client.get("/healthz?supervisor=1").status_code == 401
        assert client.get("/api/auth/context").status_code == 401
        assert client.post("/api/account/login", json={}).status_code == 401
        headers = {"Authorization": f"Bearer {key}"}
        assert client.get("/api/auth/context", headers=headers).status_code == 200
        assert client.get("/healthz?supervisor=1", headers=headers).status_code == 200
        bootstrap = client.post("/api/auth/bootstrap", headers=headers)
        assert bootstrap.status_code == 200
        assert "httponly" in bootstrap.headers["set-cookie"].lower()
        assert key not in bootstrap.content.decode("utf-8")
        session = extract_cookie(bootstrap)
        assert session and session != key
        client.cookies.clear()
        assert client.get("/api/auth/context", headers={"Authorization": f"Bearer {session}"}).status_code == 200


def test_all_trusted_proxy_hops_use_peer():
    from spend_app.exposure import resolve_client_ip

    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"),
                              headers={"x-forwarded-for": "127.0.0.2, 127.0.0.3"})
    assert resolve_client_ip(request, proxies=["127.0.0.0/8"]) == "127.0.0.1"
    request.headers["x-forwarded-for"] = "192.0.2.10, 127.0.0.3"
    assert resolve_client_ip(request, proxies=["127.0.0.0/8"]) == "192.0.2.10"


@pytest.mark.skipif(os.name != "nt", reason="The operations benchmark is a Windows script.")
def test_benchmark_uses_authorized_json_api(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    token = "b" * 64
    received = []
    expected = {"summary", "health"}

    class FixtureHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_GET(self):
            resource = self.path.split("?", 1)[0].removeprefix("/api/spend/")
            if resource not in expected or self.headers.get("Authorization") != "Bearer " + token:
                self.send_error(401)
                return
            received.append((resource, self.headers.get("Accept-Encoding")))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    key = tmp_path / ".burnrate-auth" / "access.key"
    key.parent.mkdir()
    key.write_text(token, encoding="ascii")
    script = Path(__file__).resolve().parents[1] / "scripts" / "Bench-Burnrate.ps1"
    fixture_env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "TEMP", "TMP")
                   if name in os.environ}
    fixture_env["BURNRATE_DATA_ROOT"] = str(tmp_path)
    with ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler) as server:
        serving = Thread(target=server.serve_forever, daemon=True)
        serving.start()
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(script),
                 "-BaseUrl", f"http://127.0.0.1:{server.server_port}", "-Requests", "2",
                 "-Baseline", str(tmp_path / "unused-baseline.json")],
                # Allow PowerShell startup and the benchmark's own HTTP timeout
                # to finish independently on a loaded Windows runner.
                capture_output=True, text=True, timeout=90,
                env=fixture_env,
            )
        finally:
            server.shutdown()
            serving.join(timeout=5)
    assert result.returncode == 0, result.stderr
    assert len(received) == 12
    assert {resource for resource, _ in received} == expected
    assert ("summary", "identity") in received
    assert ("summary", "gzip") in received
    assert token not in result.stdout + result.stderr


def test_install_folder_cannot_supply_security_settings(tmp_path, monkeypatch):
    from spend_app.config import load_settings

    monkeypatch.delenv("BURNRATE_HUB_URL", raising=False)
    monkeypatch.delenv("BURNRATE_ACCESS_TOKEN", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "BURNRATE_HUB_URL=https://hub.example.test\nBURNRATE_ACCESS_TOKEN=fixture\nSPEND_TIMEZONE=UTC\n",
        encoding="utf-8",
    )
    settings = load_settings(env)
    assert settings.timezone == "UTC"
    assert os.getenv("BURNRATE_HUB_URL") is None
    assert os.getenv("BURNRATE_ACCESS_TOKEN") is None


def test_switching_hub_requires_a_fresh_sign_in(tmp_path, monkeypatch):
    from spend_app import config
    from spend_app.db import connect, initialize, migrate_setup_hub_url
    from spend_app.hub_origin import bound_hub_origin
    from spend_app.setup_wizard import default_setup_state, read_setup_state, write_setup_state

    old, new = "https://previous.example.test", "https://current.example.test"
    monkeypatch.setattr(config, "resolve_hub_url", lambda: old)
    database = tmp_path / "spoke.db"
    initialize(database)
    state = default_setup_state()
    state["role"] = "spoke"
    state["spokeConfig"].update(enabled=True, hubUrl=old, token="fixture")
    with connect(database) as connection:
        write_setup_state(connection, state)
    assert bound_hub_origin(state["spokeConfig"], old) == old
    monkeypatch.setattr(config, "resolve_hub_url", lambda: new)
    assert migrate_setup_hub_url(database)
    with connect(database) as connection:
        spoke = read_setup_state(connection)["spokeConfig"]
    assert spoke["signedOut"]
    assert not spoke.get("token")
    assert spoke["hubUrl"] == new
    with pytest.raises(ValueError, match="Sign in"):
        bound_hub_origin(spoke, new)


def test_keychain_origin_stays_attached_to_issued_credential(tmp_path, monkeypatch):
    from spend_app import config
    from spend_app.connection_secrets import (
        credential_origin_allowed, credential_origin_ref, publish_spoke_credentials, read_vault_secret,
    )

    origin = "https://configured.example.test"
    other = "https://other.example.test"
    monkeypatch.setattr(config, "resolve_hub_url", lambda: origin)
    database = tmp_path / "pairing.db"
    token = "synthetic-scope-key"

    def pair(_connection, state):
        state["role"] = "spoke"
        state["spokeConfig"].update(enabled=True, hubUrl=origin)

    state = publish_spoke_credentials(database, machine_token=token, apply_state=pair)
    assert read_vault_secret(database, credential_origin_ref(token)) == origin
    assert credential_origin_allowed(database, token, origin, state["spokeConfig"])
    assert not credential_origin_allowed(database, token, other, state["spokeConfig"])


def test_hub_response_reader_bounds_buffer(monkeypatch):
    from spend_app import hub_response

    monkeypatch.setattr(hub_response, "MAX_HUB_RESPONSE_BYTES", 8)

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"four"
            yield b"four"
            yield b"x"

    def handler(request):
        assert request.headers["accept-encoding"] == "identity"
        if request.url.path == "/small":
            return httpx.Response(200, content=b"okay")
        return httpx.Response(200, stream=Stream())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert hub_response.bounded_request(client, "GET", "https://hub.example.test/small").content == b"okay"
        with pytest.raises(ValueError, match="size"):
            hub_response.bounded_request(client, "GET", "https://hub.example.test/large")


def test_invalid_hub_plan_is_skipped_without_dropping_valid_plan(caplog):
    from spend_app.subscription_transfer import _read_history

    store = "a" * 32
    term = {"name": "Monthly plan", "tool_key": "codex", "amount_usd": 20,
            "cadence": "monthly", "start_date": "2025-01-01", "end_date": None}
    plan = {"origin": store, "version": 1, "end_date": None}
    history = {"store": store, "plans": [
        dict(plan, sourceId=1, terms=[term]),
        dict(plan, sourceId=2, terms=[{**term, "name": ""}]),
    ]}
    _, plans = _read_history(history, from_hub=True)
    assert [row[0] for row in plans] == [(store, 1)]
    assert "Skipped an invalid hub plan" in caplog.text
