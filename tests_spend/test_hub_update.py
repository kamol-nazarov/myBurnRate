"""Synthetic checks for the SSH updater's protected-hub health probe."""

import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.db import initialize
from spend_app.hub_update import update_hub


def update(**kwargs):
    return update_hub(ssh="synthetic-hub", path="/opt/burnrate", service="burnrate-hub",
                      branch="main", **kwargs)


def generated_command():
    calls = []
    done = subprocess.CompletedProcess([], 0, '{"version":"v", "build":"v+abc"}\n', "")
    assert update(run=lambda step, **kw: calls.append(step) or done, echo=lambda _: None) == 0
    return calls[-1][2]


def test_generated_probe_works_on_a_protected_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "synthetic-secret")
    settings = Settings(database_path=tmp_path / "test.db", pricing_path=Path(__file__).resolve().parents[1] / "pricing",
                        anthropic_admin_key=None, openai_admin_key=None, timezone="UTC",
                        cache_hit_threshold=0.75, over_routing_token_ceiling=40000)
    initialize(settings.database_path)
    client = TestClient(create_app(settings, enable_scheduler=False))
    command = generated_command()
    url = re.search(r"http://127\.0\.0\.1:[0-9]+/[^'\s)]+", command).group()
    target = urlsplit(url)
    assert client.get("/api/spend").status_code == 401
    response = client.get(target.path)
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "synthetic-secret" not in command


@pytest.mark.parametrize("payload,valid", [
    ('{"status":"ok"}', True),
    ("not JSON", False),
    ('{"status":"error"}', False),
    ("[]", False),
])
def test_generated_verifier_process(payload, valid):
    command = generated_command()
    verifier = shlex.split(command.rsplit(" && .venv/bin/python -I -B -c ", 1)[1])[0]
    result = subprocess.run([sys.executable, "-I", "-B", "-c", verifier, payload],
                            capture_output=True, text=True, check=False, timeout=10)
    if valid:
        from spend_app import __version__
        from spend_app.build import build_id
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"version": __version__, "build": build_id()}
    else:
        assert result.returncode != 0
        assert not result.stdout.strip()


@pytest.mark.parametrize("code", [22, 255])
def test_failed_remote_update_never_claims_success(code):
    lines = []
    failure = subprocess.CompletedProcess([], code, "", "synthetic failure")
    assert update(run=lambda *a, **kw: failure, echo=lines.append) == code
    assert any("stopped" in line for line in lines)
    assert not any("Hub is healthy" in line for line in lines)


def test_curl_failure_prevents_verifier_execution():
    shell = shutil.which("sh")
    if not shell:
        pytest.skip("A POSIX shell is required to exercise the SSH command")
    tail = generated_command().split(" && sleep 4 && ", 1)[1]
    tail = tail.replace(".venv/bin/python", "verifier_was_called", 1)
    script = "curl() { return 22; }; verifier_was_called() { echo verifier-ran; }; " + tail
    result = subprocess.run([shell, "-c", script], capture_output=True, text=True, check=False, timeout=10)
    assert result.returncode == 22
    assert "verifier-ran" not in result.stdout


def test_dry_run_has_no_subprocess_or_secret(monkeypatch):
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "synthetic-secret")
    calls, lines = [], []
    assert update(dry_run=True, push_local=True, run=lambda *a, **kw: calls.append(a), echo=lines.append) == 0
    assert not calls
    assert "synthetic-secret" not in "\n".join(lines)


def test_push_local_precedes_remote_update_and_stops_on_failure():
    calls = []
    done = subprocess.CompletedProcess([], 0, '{"build":"v+abc"}\n', "")
    assert update(push_local=True, run=lambda step, **kw: calls.append(step) or done, echo=lambda _: None) == 0
    assert [step[0] for step in calls] == ["git", "ssh"]
    assert calls[0][1] == "push"
    assert "git checkout main" in calls[1][2]
    calls.clear()
    failure = subprocess.CompletedProcess([], 1, "", "push failed")
    assert update(push_local=True, run=lambda step, **kw: calls.append(step) or failure, echo=lambda _: None) == 1
    assert [step[0] for step in calls] == ["git"]
