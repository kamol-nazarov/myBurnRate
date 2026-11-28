"""Update a self-hosted Hub over ssh: ``burnrate hub update``.

The Hub is a git checkout run by systemd. Updating means selecting a
verified signed tag (or, with ``--push-local``, this checkout's commit) and
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
_GIT_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_RELEASE_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?")
_ALLOWED_SIGNERS = "/etc/burnrate/release-allowed-signers"

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


def create_hub_owner(database_path: Path, email: str, *, prompt_fn=None, echo=print) -> int:
    from spend_app.auth_model import Principal, audit
    from spend_app.db import connect, initialize
    from spend_app.setup_wizard import (
        MIN_PASSWORD_LENGTH, _new_owner_record, _read_accounts, _write_accounts,
        central_server_for_accounts, normalize_email,
    )

    clean = normalize_email(email)
    if not central_server_for_accounts() or clean is None:
        echo("Run on the hub with BURNRATE_CENTRAL_SERVER enabled and provide a valid email address.")
        return 2
    prompt = getpass.getpass if prompt_fn is None else prompt_fn
    password = prompt(f"Owner password (at least {MIN_PASSWORD_LENGTH} characters): ")
    if len(password) < MIN_PASSWORD_LENGTH or password != prompt("Confirm owner password: "):
        echo("Use a matching password of the required length.")
        return 2
    initialize(database_path)
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            accounts = _read_accounts(connection, strict=True)
        except ValueError:
            echo("The owner account record is corrupt. Restore it before continuing.")
            return 2
        if accounts["users"]:
            echo("A hub owner already exists. Sign in or reset that owner's password.")
            return 2
        record = _new_owner_record(clean, password)
        accounts["users"][clean] = record
        _write_accounts(connection, accounts)
        audit(connection, Principal("system", "local-cli"), "account.register", record["id"])
    echo("Hub owner created. Sign in on the desktop with the new account.")
    return 0


def delete_hub_owner(
    database_path: Path,
    email: str,
    *,
    confirm: bool = False,
    echo=print,
) -> int:
    """Remove the hub owner account and active owner sessions locally on the server."""
    import sqlite3
    from contextlib import closing
    from spend_app.auth_model import Principal, audit
    from spend_app.db import connect, initialize
    from spend_app.setup_wizard import _read_accounts, _write_accounts, central_server_for_accounts

    clean = (email or "").strip().lower()
    if not central_server_for_accounts() or not clean or not confirm:
        echo("Run on the hub with BURNRATE_CENTRAL_SERVER enabled, pass the exact owner email address, and add --yes.")
        return 2
    initialize(database_path)
    with connect(database_path) as connection:
        connection.execute("PRAGMA secure_delete = ON")
        connection.execute("BEGIN IMMEDIATE")
        accounts = _read_accounts(connection)
        found = _find_account_entry(accounts, clean)
        if found is None:
            echo("No matching account found on this hub.")
            return 2
        key, rec = found
        owner_id = str(rec.get("id") or clean)
        accounts["users"].pop(key, None)
        _write_accounts(connection, accounts)
        connection.execute("DELETE FROM owner_sessions")
        audit(connection, Principal("system", "local-cli"), "account.delete", owner_id)
    with closing(sqlite3.connect(database_path, timeout=30, isolation_level=None)) as compact:
        compact.execute("PRAGMA secure_delete = ON")
        compact.execute("VACUUM")
    echo("Hub owner account and sessions removed.")
    return 0


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


def _update_failed(step: list[str], completed: subprocess.CompletedProcess, echo) -> int:
    echo((completed.stderr or completed.stdout or "").strip()[-2000:])
    echo(f"Hub update stopped: {step[0]} exited with {completed.returncode}.")
    return completed.returncode


def _try_staging_cleanup(ssh: str, checkout: str, cleanup: str, run) -> None:
    try:
        run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", ssh, f"{checkout} && {cleanup}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def update_hub(*, ssh: str, path: str, service: str, branch: str, tag: str = "", push_local: bool = False,
               require_host_preflight: bool = False,
               dry_run: bool = False, port: int = 17331, run=subprocess.run, echo=print) -> int:
    if not ssh:
        echo("Name the hub's ssh host with --ssh or BURNRATE_HUB_SSH (for example: --ssh my-hub).")
        return 2
    for label, value in (("ssh host", ssh), ("path", path), ("service", service), ("branch", branch)):
        if not _SAFE.fullmatch(value or "") or value.startswith("-"):
            echo(f"Refusing an unusual {label}: {value!r}")
            return 2
    if (push_local and tag) or (not push_local and not _RELEASE_TAG.fullmatch(tag)):
        echo("Select a signed release with --tag vX.Y.Z, or use --push-local explicitly for unpublished work.")
        return 2

    def execute(step: list[str]) -> subprocess.CompletedProcess:
        echo("$ " + " ".join(shlex.quote(part) for part in step))
        if dry_run:
            return subprocess.CompletedProcess(step, 0, "", "")
        return run(step, capture_output=True, text=True)

    checkout = f"cd -- {shlex.quote(path)}"
    branch_name = shlex.quote(branch)
    branch_ref = shlex.quote(f"refs/heads/{branch}")
    cleanup = ""
    remote = checkout
    # Serialize source changes, installs and restart against other updater runs.
    # The kernel releases the lock when the shell closes the descriptor.
    lock = (
        'git_dir=$(git rev-parse --git-common-dir)'
        ' && exec 9>"$git_dir/burnrate-update.lock"'
        ' && { flock -n 9 || { echo "Could not lock the hub checkout for update." >&2; exit 1; }; }'
    )
    clean_worktree = 'worktree_status=$(git status --porcelain) && test -z "$worktree_status"'
    if push_local:
        resolve = ["git", "rev-parse", "--verify", "HEAD^{commit}"]
        completed = execute(resolve)
        if completed.returncode:
            return _update_failed(resolve, completed, echo)
        oid = (completed.stdout or "").strip()
        if not dry_run and not _GIT_OID.fullmatch(oid):
            echo("Hub update stopped: could not determine the local commit.")
            return 2
        oid = oid or "<local-head-commit>"
        staging_ref = f"refs/burnrate/update/{secrets.token_hex(16)}"
        expected = shlex.quote(oid)
        # Compare-and-delete prevents cleanup from dropping a replaced ref.
        cleanup = f"git update-ref -d {staging_ref} {expected}"
        push = [
            "git",
            "push",
            "--no-follow-tags",
            "--no-recurse-submodules",
            f"--force-with-lease={staging_ref}:",
            f"{ssh}:{path}",
            f"{oid}:{staging_ref}",
        ]
        push_confirmed = False
        try:
            completed = execute(push)
            push_confirmed = completed.returncode == 0
        finally:
            if not push_confirmed:
                # The ref may have been created before the push lost its reply.
                _try_staging_cleanup(ssh, checkout, cleanup, run)
        if completed.returncode:
            return _update_failed(push, completed, echo)

        cleanup_trap = shlex.quote(f"{cleanup} >/dev/null 2>&1 || :")
        remote += (
            f" && trap {cleanup_trap} 0 && {lock} && {clean_worktree}"
            f" && staged=$(git show-ref --verify --hash {staging_ref})"
            f' && test "$staged" = {expected}'
            f" && git show-ref --verify --quiet {branch_ref}"
            f" && git merge-base --is-ancestor {branch_ref} {expected}"
            f" && git checkout {branch_name}"
            f" && current_branch=$(git symbolic-ref --quiet HEAD) && test \"$current_branch\" = {branch_ref}"
            f" && git merge --ff-only {expected}"
            f" && current_commit=$(git rev-parse --verify HEAD) && test \"$current_commit\" = {expected}"
        )
    else:
        remote += f" && {lock} && {clean_worktree} && {_release_checkout(tag)}"
    preflight = "/usr/local/sbin/tk-security-preflight"
    if require_host_preflight or ssh == "tk-server":
        remote += f' && (test -x {preflight} || {{ echo "Missing required hub security preflight." >&2; exit 1; }})'
    # Updating source alone leaves the old dependency/trust bundle installed.
    # Stop before restart unless both the installed package and host guard pass.
    remote += " && sh scripts/install-hub-runtime.sh"
    remote += " && .venv/bin/python -I -B scripts/verify-runtime.py"
    remote += f" && (test ! -x {preflight} || sudo {preflight} burnrate)"
    # Liveness is public even on a protected hub. Read build metadata over the
    # already-authorized SSH connection instead of passing an API credential.
    health_url = shlex.quote(f"http://127.0.0.1:{port}/healthz")
    remote += f" && sudo systemctl restart {service} && sleep 4"
    remote += f" && health_json=$(curl --fail --silent --show-error --max-time 10 {health_url})"
    remote += f' && .venv/bin/python -I -B -c {shlex.quote(_VERIFY_HEALTH)} "$health_json"'
    step = ["ssh", ssh, remote]
    transport_finished = False
    try:
        completed = execute(step)
        transport_finished = completed.returncode != 255
    finally:
        if cleanup and not transport_finished:
            # SSH may have failed before the remote shell could install its trap.
            # A retry uses a new ref even if the hub is currently unreachable.
            _try_staging_cleanup(ssh, checkout, cleanup, run)
    if completed.returncode:
        return _update_failed(step, completed, echo)
    if dry_run:
        return 0
    try:
        reported = json.loads((completed.stdout or "").strip().splitlines()[-1])
        echo(f"Hub is healthy; installed build {reported.get('build') or reported.get('version')}.")
    except (ValueError, IndexError, AttributeError):
        echo("Hub restarted; it did not report its build yet. Check it again in a minute.")
    return 0


def _release_checkout(tag: str) -> str:
    if not _RELEASE_TAG.fullmatch(tag):
        raise ValueError("A version tag is required.")
    ref = shlex.quote(f"refs/tags/{tag}")
    allowed_signers = shlex.quote(_ALLOWED_SIGNERS)
    # Pin both the exact fetched tag object and its target, then switch to the
    # verified commit. Fetch without force so a moved local tag is refused.
    return (
        f"git fetch --no-tags origin {ref}:{ref}"
        f' && tag_object=$(git rev-parse --verify {ref}) && test "$(git cat-file -t "$tag_object")" = tag'
        f" && test -r {allowed_signers} && test ! -L {allowed_signers}"
        f' && git -c gpg.ssh.allowedSignersFile={allowed_signers} -c gpg.minTrustLevel=fully verify-tag "$tag_object"'
        f' && expected_commit=$(git rev-parse --verify "$tag_object^{{commit}}")'
        f' && test "$(git rev-parse --verify {ref})" = "$tag_object"'
        ' && git checkout --detach "$expected_commit"'
        ' && test "$(git rev-parse --verify HEAD)" = "$expected_commit"'
    )


def add_parser(subparsers) -> None:
    hub = subparsers.add_parser("hub", help="Manage the hub this computer syncs to")
    commands = hub.add_subparsers(dest="hub_command", required=True)
    update = commands.add_parser("update", help="Update the hub over ssh and restart it")
    update.add_argument("--ssh", default=os.getenv("BURNRATE_HUB_SSH", ""), help="ssh host or alias of the hub")
    update.add_argument("--path", default=os.getenv("BURNRATE_HUB_PATH", "/opt/burnrate"), help="Hub checkout path")
    update.add_argument("--service", default=os.getenv("BURNRATE_HUB_SERVICE", "burnrate-hub"), help="systemd unit")
    update.add_argument("--tag", default="", help="The signed release tag to deploy, for example v0.6.10")
    update.add_argument("--branch", default=os.getenv("BURNRATE_HUB_BRANCH", "prod"), help="Branch used only by --push-local")
    update.add_argument("--require-host-preflight", action="store_true", help="Require the host's security preflight")
    update.add_argument("--push-local", action="store_true", help="Deploy this checkout's commit (unpublished work)")
    update.add_argument("--dry-run", action="store_true", help="Print the steps without running them")
    reset_pw = commands.add_parser(
        "reset-password",
        help="Reset the hub owner password locally on the server",
    )
    reset_pw.add_argument("email", help="Account email address")
    create_owner = commands.add_parser("create-owner", help="Create the first account locally on the hub")
    create_owner.add_argument("email", help="Owner email address")
    delete_owner = commands.add_parser("delete-owner", help="Remove the hub owner account and sessions locally")
    delete_owner.add_argument("email", help="Owner email address to remove")
    delete_owner.add_argument("--yes", action="store_true", help="Confirm removing the owner account")
