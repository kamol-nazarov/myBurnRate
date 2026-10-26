"""Update a self-hosted Hub over ssh: ``burnrate hub update``.

The Hub is a git checkout run by systemd. Updating means fast-forwarding it to
its tracked branch (or, with ``--push-local``, to this checkout's commit) and
restarting the service, then reading back the build it reports. Nothing here
runs from the web UI: deploying code to another machine stays a deliberate
command its owner types.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess

_SAFE = re.compile(r"[A-Za-z0-9_.@:/+-]+")


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
        remote = f"cd {path} && git checkout -f {branch}"
    else:
        remote = f"cd {path} && git fetch origin && git checkout {branch} && git merge --ff-only origin/{branch}"
    remote += f" && sudo systemctl restart {service} && sleep 4 && curl -s http://127.0.0.1:{port}/api/spend"
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
        echo(f"Hub now runs {reported.get('build') or reported.get('version')}.")
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
    update.add_argument("--branch", default=os.getenv("BURNRATE_HUB_BRANCH", "main"), help="Branch the hub tracks")
    update.add_argument("--push-local", action="store_true", help="Deploy this checkout's commit (unpublished work)")
    update.add_argument("--dry-run", action="store_true", help="Print the steps without running them")
