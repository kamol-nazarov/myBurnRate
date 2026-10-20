"""Restart the dashboard when it stops answering or its collection stalls.

The "BURNRATE Watchdog" scheduled task runs ``pythonw -m spend_app.watchdog``
every few minutes, windowless. Each run makes two loopback reads: ``/healthz``
(is the web server up?) and the freshness heartbeat (has a local collection
pass finished recently?). A failed check is confirmed once more before acting.

A restart goes through ``scripts/restart-burnrate.ps1`` so the lifecycle
scripts' process-ownership checks still decide what may be stopped. After an
explicit ``stop-burnrate.ps1`` the watchdog does nothing until the service is
started again, and it restarts at most ``MAX_RESTARTS_PER_HOUR`` times.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from spend_app import service

STOP_MARKER = "stopped"
STATE_FILE = "watchdog.json"
LOG_FILE = "watchdog.log"
MAX_RESTARTS_PER_HOUR = 3
CONFIRM_DELAY_SECONDS = 20
HEALTH_TIMEOUT_SECONDS = 10
FRESHNESS_TIMEOUT_SECONDS = 45
RESTART_TIMEOUT_SECONDS = 240
CREATE_NO_WINDOW = 0x08000000

LOG = logging.getLogger("burnrate.watchdog")


def stop_marker() -> Path:
    return service.run_dir() / STOP_MARKER


def explicitly_stopped() -> bool:
    return stop_marker().exists()


def _get(url: str, timeout: float, token: str | None = None) -> dict:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise urllib.error.HTTPError(url, response.status, "unexpected status", response.headers, None)
        return json.loads(response.read().decode("utf-8"))


def probe(port: int = service.DEFAULT_PORT, *, get=_get, token: str | None = None) -> tuple[bool, str]:
    """(healthy, reason). Reads only the loopback health and freshness endpoints."""
    base = f"http://{service.DEFAULT_HOST}:{port}"
    try:
        get(base + "/healthz", HEALTH_TIMEOUT_SECONDS)
    except Exception as exc:
        return False, f"down ({type(exc).__name__})"
    try:
        report = get(base + "/api/spend/freshness", FRESHNESS_TIMEOUT_SECONDS, token)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403, 404}:
            return True, "freshness unavailable; health only"
        return False, f"unresponsive (HTTP {exc.code})"
    except Exception as exc:
        return False, f"unresponsive ({type(exc).__name__})"
    ingest = report.get("ingest") if isinstance(report, dict) else None
    if isinstance(ingest, dict) and ingest.get("stalled"):
        age = ingest.get("lastPassAgeSeconds")
        return False, "collection stalled" + (f" (last pass {age}s ago)" if age is not None else " (no pass finished)")
    return True, "ok"


def _read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def recent_restarts(state: dict, now: datetime) -> list[str]:
    cutoff = now - timedelta(hours=1)
    kept = []
    for stamp in state.get("restarts", []):
        try:
            moment = datetime.fromisoformat(str(stamp))
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        if moment > cutoff:
            kept.append(moment.isoformat())
    return kept


def _powershell() -> str:
    root = os.environ.get("SystemRoot") or os.environ.get("windir")
    if root:
        candidate = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if candidate.is_file():
            return str(candidate)
    return "powershell"


def restart_script() -> Path:
    return Path(__file__).resolve().parents[1] / "scripts" / "restart-burnrate.ps1"


def restart(*, run=subprocess.run) -> tuple[bool, str]:
    script = restart_script()
    if not script.is_file():
        return False, "restart script not found"
    completed = run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=RESTART_TIMEOUT_SECONDS,
        creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    output = " ".join((completed.stdout + " " + completed.stderr).split())[-400:]
    return completed.returncode == 0, output


def _configure_logging() -> None:
    service.ensure_directories()
    handler = logging.handlers.RotatingFileHandler(
        service.log_dir() / LOG_FILE, maxBytes=512 * 1024, backupCount=1, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(service.LOG_FORMAT))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def check_once(*, port: int, dry_run: bool = False, token: str | None = None, sleep=time.sleep,
               probe_fn=probe, restart_fn=restart, now=None) -> str:
    """One watchdog run. Returns what it did: ok, stopped, recovered, rate-limited, restarted, failed, dry-run."""
    if explicitly_stopped():
        return "stopped"
    healthy, reason = probe_fn(port, token=token)
    if healthy:
        return "ok"
    sleep(CONFIRM_DELAY_SECONDS)
    healthy, reason = probe_fn(port, token=token)
    if healthy:
        LOG.info("transient problem cleared on recheck")
        return "recovered"
    if explicitly_stopped():
        return "stopped"
    moment = now or datetime.now(UTC)
    state_path = service.run_dir() / STATE_FILE
    state = _read_state(state_path)
    restarts = recent_restarts(state, moment)
    if len(restarts) >= MAX_RESTARTS_PER_HOUR:
        LOG.warning("dashboard %s; %d restarts in the last hour, waiting before another", reason, len(restarts))
        return "rate-limited"
    if dry_run:
        LOG.info("dashboard %s; dry run, not restarting", reason)
        return "dry-run"
    LOG.warning("dashboard %s; restarting", reason)
    ok, output = restart_fn()
    state["restarts"] = [*restarts, moment.isoformat()]
    state["lastReason"] = reason
    state["lastResult"] = "restarted" if ok else "failed"
    try:
        service.ensure_directories()
        state_path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass
    if ok:
        LOG.info("restart completed: %s", output or "no output")
        return "restarted"
    LOG.error("restart failed: %s", output or "no output")
    return "failed"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spend_app.watchdog")
    parser.add_argument("--port", type=int, default=int(os.environ.get("BURNRATE_PORT", str(service.DEFAULT_PORT))))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    from spend_app.config import load_user_env

    load_user_env()
    _configure_logging()
    token = os.environ.get("BURNRATE_ACCESS_TOKEN", "").strip() or None
    try:
        outcome = check_once(port=args.port, dry_run=args.dry_run, token=token)
    except Exception:
        LOG.exception("watchdog run failed")
        return 1
    return 0 if outcome in {"ok", "stopped", "recovered", "restarted", "dry-run"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
