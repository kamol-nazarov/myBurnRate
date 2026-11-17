"""Update a self-hosted Hub over ssh: ``burnrate hub update``.

The Hub is a git checkout run by systemd. Updating means fast-forwarding it to
its tracked branch (or, with ``--push-local``, to this checkout's commit) and
restarting the service, checking its health, and reading the installed build.
Nothing here runs from the web UI: deploying code to another machine stays a
deliberate command its owner types.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import secrets
import shlex
import subprocess
from pathlib import Path

_SAFE = re.compile(r"[A-Za-z0-9_.@:/+-]+")

_VERIFY_HEALTH = """import json
import sys
from spend_app import __version__
from spend_app.build import build_id

health = json.loads(sys.argv[1])
if not isinstance(health, dict) or health.get("status") != "ok":
    raise SystemExit("Hub health check did not report a healthy BURNRATE service.")
print(json.dumps({"version": __version__, "build": build_id()}))
"""


def _find_account_entry(accounts: dict, target: str) -> tuple[str, dict] | None:
    users = accounts.get("users")
    if not isinstance(users, dict):
        return None
    clean = (target or "").strip().lower()
    if not clean:
        return None
    direct = users.get(clean)
    if isinstance(direct, dict):
        return clean, direct
    for key, rec in users.items():
        if not isinstance(rec, dict):
            continue
        if (
            str(key).strip().lower() == clean
            or str(rec.get("email") or "").strip().lower() == clean
            or str(rec.get("username") or "").strip().lower() == clean
        ):
            return key, rec
    return None


def reset_hub_password(
    database_path: Path,
    email: str,
    *,
    prompt_fn=None,
    echo=print,
) -> int:
    from spend_app.auth_model import Principal, audit
    from spend_app.db import connect, initialize, utc_now
    from spend_app.setup_wizard import (
        MIN_PASSWORD_LENGTH,
        PBKDF2_ITERATIONS,
        _hash_password,
        _read_accounts,
        _write_accounts,
    )

    clean = (email or "").strip().lower()
    if not clean:
        echo("Provide the account email address.")
        return 2
    initialize(database_path)
    with connect(database_path) as connection:
        accounts = _read_accounts(connection)
        if _find_account_entry(accounts, clean) is None:
            echo("No matching account found on this hub.")
            return 2

    prompt = getpass.getpass if prompt_fn is None else prompt_fn
    pw1 = prompt(f"New password (at least {MIN_PASSWORD_LENGTH} characters): ")
    pw2 = prompt("Confirm new password: ")
    if pw1 != pw2:
        echo("Passwords do not match.")
        return 2
    if len(pw1) < MIN_PASSWORD_LENGTH:
        echo(f"Use a password of at least {MIN_PASSWORD_LENGTH} characters.")
        return 2

    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        accounts = _read_accounts(connection)
        found = _find_account_entry(accounts, clean)
        if found is None:
            echo("No matching account found on this hub.")
            return 2
        _, rec = found
        salt_hex, hash_hex = _hash_password(pw1, iterations=PBKDF2_ITERATIONS)
        rec.setdefault("id", secrets.token_hex(16))
        rec.update(
            salt=salt_hex,
            passwordHash=hash_hex,
            iterations=PBKDF2_ITERATIONS,
            passwordChangedAt=utc_now(),
        )
        _write_accounts(connection, accounts)
        now_stamp = utc_now()
        connection.execute(
            "UPDATE owner_sessions SET revoked_at=? WHERE revoked_at IS NULL",
            (now_stamp,),
        )
        audit(
            connection,
            Principal("system", "local-cli"),
            "account.password_reset",
            rec["id"],
        )
    echo("Password updated. All active owner sessions have been revoked.")
    return 0


def update_hub(*, ssh: str, path: str, service: str, branch: str, push_local: bool = False,
               dry_run: bool = False, port: int = 17331, run=subprocess.run, echo=print) -> int:
    if not ssh:
        echo("Name the hub's ssh host with --ssh or BURNRATE_HUB_SSH (for example: --ssh my-hub).")
        return 2
    for label, value in (("ssh host", ssh), ("path", path), ("service", service), ("branch", branch)):
        if not _SAFE.fullmatch(value or ""):
            echo(f"Refusing an unusual {label}: {value!r}")
            return 2
    steps: list[list[str]] = []
    if push_local:
        # Unpublished work: push this commit to its own branch, then check it out there.
        steps.append(["git", "push", f"{ssh}:{path}", f"HEAD:refs/heads/{branch}"])
        remote = f'cd {path} && test -z "$(git status --porcelain)" && git checkout {branch}'
    else:
        remote = f'cd {path} && test -z "$(git status --porcelain)" && git fetch origin && git checkout {branch} && git merge --ff-only origin/{branch}'
    # Updating source alone leaves the old dependency/trust bundle installed.
    # Stop before restart unless both the installed package and host guard pass.
    remote += " && .venv/bin/python -m pip install --require-hashes -r requirements-build.txt && .venv/bin/python -m pip install --no-deps --no-build-isolation . && .venv/bin/python -m pip check"
    remote += " && .venv/bin/python -I -B scripts/verify-runtime.py"
    remote += " && (test ! -x /usr/local/sbin/tk-security-preflight || sudo /usr/local/sbin/tk-security-preflight burnrate)"
    # Liveness is public even on a protected hub. Read build metadata over the
    # already-authorized SSH connection instead of passing an API credential.
    health_url = shlex.quote(f"http://127.0.0.1:{port}/healthz")
    remote += f" && sudo systemctl restart {service} && sleep 4"
    remote += f" && health_json=$(curl --fail --silent --show-error --max-time 10 {health_url})"
    remote += f' && .venv/bin/python -I -B -c {shlex.quote(_VERIFY_HEALTH)} "$health_json"'
    steps.append(["ssh", ssh, remote])
    output = ""
    for step in steps:
        echo("$ " + " ".join(shlex.quote(part) for part in step))
        if dry_run:
            continue
        completed = run(step, capture_output=True, text=True)
        if completed.returncode != 0:
            echo((completed.stderr or completed.stdout or "").strip()[-2000:])
            echo(f"Hub update stopped: {step[0]} exited with {completed.returncode}.")
            return completed.returncode
        output = completed.stdout or ""
    if dry_run:
        return 0
    try:
        reported = json.loads(output.strip().splitlines()[-1])
        echo(f"Hub is healthy; installed build {reported.get('build') or reported.get('version')}.")
    except (ValueError, IndexError, AttributeError):
        echo("Hub restarted; it did not report its build yet. Check it again in a minute.")
    return 0


def add_parser(subparsers) -> None:
    hub = subparsers.add_parser("hub", help="Manage the hub this computer syncs to")
    commands = hub.add_subparsers(dest="hub_command", required=True)
    update = commands.add_parser("update", help="Update the hub over ssh and restart it")
    update.add_argument("--ssh", default=os.getenv("BURNRATE_HUB_SSH", ""), help="ssh host or alias of the hub")
    update.add_argument("--path", default=os.getenv("BURNRATE_HUB_PATH", "/opt/burnrate"), help="Hub checkout path")
    update.add_argument("--service", default=os.getenv("BURNRATE_HUB_SERVICE", "burnrate-hub"), help="systemd unit")
    update.add_argument("--branch", default=os.getenv("BURNRATE_HUB_BRANCH", "prod"), help="Branch the hub tracks")
    update.add_argument("--push-local", action="store_true", help="Deploy this checkout's commit (unpublished work)")
    update.add_argument("--dry-run", action="store_true", help="Print the steps without running them")
    reset_pw = commands.add_parser(
        "reset-password",
        help="Reset the hub owner password locally on the server",
    )
    reset_pw.add_argument("email", help="Account email address")
