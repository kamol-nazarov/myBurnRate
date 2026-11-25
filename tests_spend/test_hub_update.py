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
    if not kwargs.get("push_local"):
        kwargs.setdefault("tag", "v0.6.10")
    return update_hub(ssh="synthetic-hub", path="/opt/burnrate", service="burnrate-hub",
                      branch="main", **kwargs)


def generated_command():
    calls = []
    done = subprocess.CompletedProcess([], 0, '{"version":"v", "build":"v+abc"}\n', "")
    assert update(run=lambda step, **kw: calls.append(step) or done, echo=lambda _: None) == 0
    return calls[-1][2]


def test_generated_probe_works_on_a_protected_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
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
        # The isolated process imports the installed package. A developer's
        # worktree can be at a different commit than that installation.
        installed_metadata = (
            "from spend_app import __version__\n"
            "from spend_app.build import build_id\n"
            "print(__version__)\n"
            "print(build_id())\n"
        )
        installed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", installed_metadata],
            capture_output=True, text=True, check=True, timeout=10,
        )
        version, build = installed.stdout.splitlines()
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"version": version, "build": build}
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
    oid = "a" * 40
    done = subprocess.CompletedProcess([], 0, '{"build":"v+abc"}\n', "")
    resolved = subprocess.CompletedProcess([], 0, oid + "\n", "")

    def run(step, **kwargs):
        calls.append(step)
        return resolved if step[1] == "rev-parse" else done

    assert update(push_local=True, run=run, echo=lambda _: None) == 0
    assert [step[0] for step in calls] == ["git", "git", "ssh"]
    assert calls[0] == ["git", "rev-parse", "--verify", "HEAD^{commit}"]
    assert calls[1][1] == "push"
    assert calls[1][-1].startswith(f"{oid}:refs/burnrate/update/")
    assert f"git merge --ff-only {oid}" in calls[2][2]
    calls.clear()
    lines = []
    failure = subprocess.CompletedProcess([], 1, "", "push failed")

    def fail_push(step, **kwargs):
        calls.append(step)
        if step[0] == "ssh":
            raise OSError("cleanup transport unavailable")
        return resolved if step[1] == "rev-parse" else failure

    assert update(push_local=True, run=fail_push, echo=lines.append) == 1
    assert [step[0] for step in calls] == ["git", "git", "ssh"]
    stage_ref = calls[1][-1].split(":", 1)[1]
    assert calls[2][-1].endswith(f"git update-ref -d {stage_ref} {oid}")
    assert "push failed" in lines


class _DisposableHub:
    """Run the actual Git commands with disposable repositories and no SSH."""

    def __init__(self, root, shell):
        self.shell = shell
        self.local = root / "local"
        self.hub = root / "hub"
        self.local.mkdir()
        self.git(self.local, "init", "-q", "--initial-branch=main")
        self.commit(self.local, "base")
        self.base = self.git(self.local, "rev-parse", "HEAD").stdout.strip()
        self.git(root, "clone", "-q", str(self.local), str(self.hub))
        self.git(self.hub, "config", "receive.denyCurrentBranch", "refuse")
        self.commit(self.local, "unpublished")
        self.intended = self.git(self.local, "rev-parse", "HEAD").stdout.strip()
        self.before_ssh = None
        self.transport_offline = False
        self.busy = False
        self.staged = []
        self.runtime_invocations = 0
        self.messages = []
        self.real_flock = self.sh("command -v flock").returncode == 0

    @staticmethod
    def git(repo, *args, check=True):
        return subprocess.run(
            ["git", *args], cwd=repo, check=check, capture_output=True, text=True, timeout=15
        )

    def commit(self, repo, value):
        (repo / "file.txt").write_text(value + "\n", encoding="utf-8")
        self.git(repo, "add", "file.txt")
        self.git(
            repo, "-c", "commit.gpgsign=false", "-c", "user.name=Test",
            "-c", "user.email=test@example.org", "commit", "-qm", value,
        )

    def sh(self, command):
        return subprocess.run(
            [self.shell, "-c", command], cwd=self.hub, capture_output=True, text=True, timeout=15
        )

    def run(self, step, **kwargs):
        if step[0] == "git":
            args = step[1:]
            if args[0] == "push":
                args[-2] = str(self.hub)
                self.staged.append(args[-1].split(":", 1)[1])
            return self.git(self.local, *args, check=False)
        assert step[0] == "ssh"
        if self.transport_offline:
            return subprocess.CompletedProcess(step, 255, "", "synthetic SSH disconnect")

        remote = step[-1]
        source, marker, _ = remote.partition(" && sh scripts/install-hub-runtime.sh")
        if marker and self.before_ssh:
            self.before_ssh(self)
        assert source.startswith("cd -- /opt/burnrate")
        source = "cd -- ." + source.removeprefix("cd -- /opt/burnrate")
        if self.busy:
            source = "flock() { return 1; }; " + source
        elif not self.real_flock:
            source = "flock() { return 0; }; " + source
        if marker:
            source += """ && echo runtime-reached && printf '%s\n' '{"build":"test-build"}'"""
        result = self.sh(source)
        self.runtime_invocations += "runtime-reached" in result.stdout
        return result

    def update(self, *, push_local=True):
        return update(push_local=push_local, run=self.run, echo=self.messages.append)

    def head(self):
        return self.git(self.hub, "rev-parse", "HEAD").stdout.strip()

    def stage_refs(self):
        return self.git(
            self.hub, "for-each-ref", "--format=%(refname)", "refs/burnrate/update"
        ).stdout.splitlines()


@pytest.fixture
def disposable_hub(tmp_path):
    shell = shutil.which("sh")
    if not shell or not shutil.which("git"):
        pytest.skip("Git and a POSIX shell are required for the disposable hub")
    return _DisposableHub(tmp_path, shell)


def test_push_local_fast_forwards_checked_out_hub(disposable_hub):
    pair = disposable_hub
    legacy = pair.git(pair.local, "push", str(pair.hub), "HEAD:refs/heads/main", check=False)
    assert legacy.returncode
    assert "checked out branch" in legacy.stderr
    assert pair.head() == pair.base

    # A ref left by an earlier operation is outside this invocation's ownership.
    other_ref = "refs/burnrate/update/earlier"
    pair.git(pair.hub, "update-ref", other_ref, pair.base)
    pair.git(pair.local, "config", "push.followTags", "true")
    pair.git(
        pair.local, "-c", "user.name=Test", "-c", "user.email=test@example.org",
        "tag", "-a", "--no-sign", "disposable-local-tag", "-m", "local-only tag",
    )
    assert pair.update() == 0, pair.messages
    assert pair.head() == pair.intended
    assert (pair.hub / "file.txt").read_text(encoding="utf-8") == "unpublished\n"
    assert pair.git(pair.hub, "status", "--porcelain").stdout == ""
    assert pair.stage_refs() == [other_ref]
    assert pair.git(pair.hub, "tag", "--list").stdout == ""
    assert pair.runtime_invocations == 1


@pytest.mark.parametrize("blocked_by", ["dirty", "diverged", "staged_oid", "lock"])
def test_push_local_stops_before_install_and_restart(disposable_hub, blocked_by):
    pair = disposable_hub
    if blocked_by == "dirty":
        (pair.hub / "file.txt").write_text("local change", encoding="utf-8")
    elif blocked_by == "diverged":
        pair.commit(pair.hub, "independent hub commit")
    elif blocked_by == "staged_oid":
        pair.before_ssh = lambda hub: hub.git(hub.hub, "update-ref", hub.staged[-1], hub.base)
    else:
        pair.busy = True
    original = pair.head()

    assert pair.update() != 0
    assert pair.head() == original
    assert pair.runtime_invocations == 0
    assert pair.stage_refs() == (pair.staged if blocked_by == "staged_oid" else [])


def test_push_local_retries_after_interruption_without_reusing_staging_ref(disposable_hub):
    pair = disposable_hub
    pair.transport_offline = True
    assert pair.update() == 255
    assert pair.head() == pair.base
    assert pair.runtime_invocations == 0
    abandoned, = pair.stage_refs()

    pair.transport_offline = False
    assert pair.update() == 0, pair.messages
    assert pair.head() == pair.intended
    assert pair.staged[0] != pair.staged[1]
    assert pair.stage_refs() == [abandoned]
    assert pair.runtime_invocations == 1


@pytest.mark.parametrize("push_error", ["lost_ack", "exception"])
def test_failed_push_cleans_ref_when_remote_already_created_it(disposable_hub, monkeypatch, push_error):
    pair = disposable_hub
    other_ref = "refs/burnrate/update/earlier"
    pair.git(pair.hub, "update-ref", other_ref, pair.base)
    actual_git = pair.git

    def interrupted_git(repo, *args, **kwargs):
        result = actual_git(repo, *args, **kwargs)
        if args[0] != "push":
            return result
        assert result.returncode == 0, result.stderr
        assert pair.staged[-1] in pair.stage_refs()
        if push_error == "exception":
            raise OSError("push transport interrupted")
        return subprocess.CompletedProcess(args, 128, "", "push acknowledgement lost")

    monkeypatch.setattr(pair, "git", interrupted_git)
    if push_error == "exception":
        with pytest.raises(OSError, match="push transport interrupted"):
            pair.update()
    else:
        assert pair.update() == 128
        assert "push acknowledgement lost" in pair.messages
    assert pair.stage_refs() == [other_ref]
    assert pair.head() == pair.base
    assert pair.runtime_invocations == 0


def test_concurrent_updates_respect_shared_repository_lock(disposable_hub):
    pair = disposable_hub
    if not pair.real_flock:
        pytest.skip("util-linux flock is required to exercise the real repository lock")
    fcntl = pytest.importorskip("fcntl")
    lock_path = pair.hub / ".git" / "burnrate-update.lock"

    # This file descriptor represents an update still in its install stage.
    # The subprocess must contend for the same lock as the existing updater.
    with lock_path.open("wb") as existing_update:
        fcntl.flock(existing_update, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert pair.update() != 0
        assert pair.update(push_local=False) != 0
        assert pair.stage_refs() == []
        assert pair.head() == pair.base
        assert pair.runtime_invocations == 0
        assert any("Could not lock the hub checkout" in msg for msg in pair.messages)

    assert pair.update() == 0, pair.messages
    assert pair.head() == pair.intended
    assert pair.runtime_invocations == 1


def test_upstream_update_without_the_requested_tag_stops(disposable_hub):
    pair = disposable_hub
    assert pair.update(push_local=False) != 0, pair.messages
    assert pair.head() == pair.base
    assert pair.stage_refs() == []
    assert pair.runtime_invocations == 0
