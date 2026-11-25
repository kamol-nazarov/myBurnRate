import importlib.util
from pathlib import Path
import shutil
import subprocess
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from spend_app import hub_update
from spend_app.exposure import TrustedProxyHeadersMiddleware


def test_release_deploy_requires_named_tag_before_invocation():
    run = Mock()
    assert hub_update.update_hub(
        ssh="fixture", path="/fixture", service="burnrate-hub", branch="prod", run=run,
    ) == 2
    run.assert_not_called()


def test_signed_exact_tag_is_required_before_update(tmp_path, monkeypatch):
    git = shutil.which("git")
    sh = shutil.which("sh")
    ssh_keygen = shutil.which("ssh-keygen")
    if git and not sh:
        sh_candidate = Path(git).parents[1] / "bin" / "sh.exe"
        sh = str(sh_candidate) if sh_candidate.is_file() else None
    if not git or not sh or not ssh_keygen:
        pytest.skip("requires Git, a POSIX shell and OpenSSH")

    def call(command, path):
        return subprocess.run(command, cwd=path, capture_output=True, text=True, check=True)

    origin = tmp_path / "origin"
    origin.mkdir()
    call([git, "init", "--initial-branch=prod"], origin)
    call([git, "config", "user.name", "Fixture maintainer"], origin)
    call([git, "config", "user.email", "maintainer@example.com"], origin)
    call([git, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "release"], origin)
    call([git, "-c", "tag.gpgsign=false", "tag", "-a", "v1.0.0", "-m", "unsigned"], origin)
    key = tmp_path / "fixture_signing"
    call([ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(key)], tmp_path)
    call([
        git, "-c", "gpg.format=ssh", "-c", f"user.signingkey={key.as_posix()}",
        "tag", "-s", "v1.0.1", "-m", "signed",
    ], origin)
    allowed = tmp_path / "allowed_signers"
    allowed.write_text("maintainer@example.com " + key.with_suffix(".pub").read_text().strip() + "\n")
    clone = tmp_path / "consumer"
    call([git, "clone", "--no-tags", str(origin), str(clone)], tmp_path)
    monkeypatch.setattr(hub_update, "_ALLOWED_SIGNERS", allowed.as_posix())
    rejected = subprocess.run(
        [sh, "-c", hub_update._release_checkout("v1.0.0")], cwd=clone, capture_output=True, text=True,
    )
    assert rejected.returncode != 0
    assert call([git, "symbolic-ref", "HEAD"], clone).stdout.strip() == "refs/heads/prod"
    call([sh, "-c", hub_update._release_checkout("v1.0.1")], clone)
    assert call([git, "rev-parse", "HEAD"], clone).stdout == call(
        [git, "rev-parse", "v1.0.1^{commit}"], origin,
    ).stdout


def test_https_proxy_receives_hsts_and_plain_local_http_does_not():
    app = FastAPI()
    app.add_middleware(TrustedProxyHeadersMiddleware, trusted=["127.0.0.1"])

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        assert "strict-transport-security" not in client.get("/healthz").headers
        response = client.get("/healthz", headers={"X-Forwarded-Proto": "https"})
        assert response.headers["strict-transport-security"] == "max-age=31536000"


def test_public_build_omits_private_hub_origin(tmp_path, monkeypatch):
    script = Path(__file__).resolve().parents[1] / "packaging" / "build_config.py"
    spec = importlib.util.spec_from_file_location("release_build_config", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("BURNRATE_BUILTIN_HUB_URL", "https://hub.example.invalid")
    monkeypatch.setenv("BURNRATE_PUBLIC_BUILD", "1")
    path, selected = module.write_build_config(tmp_path / "_build_config.py")
    assert selected == ""
    assert "hub.example.invalid" not in path.read_text()
