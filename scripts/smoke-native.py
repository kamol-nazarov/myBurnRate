"""Smoke-test the frozen BURNRATE collector and its native-client IPC."""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APP = ROOT / "packaging" / "build" / "native" / "BURNRATE" / "BURNRATE.exe"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def smoke(app: Path) -> None:
    from spend_app.config import Settings
    from spend_app.native_desktop import NativeClient
    from spend_app.platform_paths import NO_WINDOW

    app = app.expanduser().resolve()
    if not app.is_file():
        raise FileNotFoundError(f"Frozen BURNRATE executable not found: {app}")

    with TemporaryDirectory(prefix="burnrate-native-smoke-") as temporary:
        root = Path(temporary).resolve()
        home = root / "home"
        home.mkdir()
        data = root / "data"
        database = data / "usage.db"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]

        # Keep only the Windows variables needed to load native libraries.
        inherited = {
            key: value
            for key, value in os.environ.items()
            if key.upper()
            in {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "SYSTEMDRIVE"}
        }
        env = {
            **inherited,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(home / "Roaming"),
            "LOCALAPPDATA": str(home / "Local"),
            "XDG_DATA_HOME": str(home / "data"),
            "XDG_CONFIG_HOME": str(home / "config"),
            "CODEX_HOME": str(home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "GROK_HOME": str(home / ".grok"),
            "OPENCODE_HOME": str(home / ".opencode"),
            "BURNRATE_DATA_ROOT": str(data),
            "SPEND_DATABASE_PATH": str(database),
            "SPEND_TIMEZONE": "UTC",
            "BURNRATE_HOST": "127.0.0.1",
            "BURNRATE_PORT": str(port),
            "BURNRATE_USAGE_ONLY": "1",
            "BURNRATE_COLLECTOR_PROCESS": "0",
            "BURNRATE_ENABLE_ZAI_QUOTA": "0",
            "BURNRATE_ENABLE_OPENCODE_GO_QUOTA": "0",
            "BURNRATE_SUMMARY_PREWARM": "0",
            "PYTHONNOUSERSITE": "1",
        }

        process = subprocess.Popen(
            [str(app), "--service", "--port", str(port)],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
        try:
            deadline = time.monotonic() + 25
            health = None
            with httpx.Client(trust_env=False, timeout=2) as http:
                while time.monotonic() < deadline:
                    try:
                        response = http.get(
                            f"http://127.0.0.1:{port}/healthz?supervisor=1"
                        )
                        if response.status_code == 200:
                            health = response.json()
                            break
                    except (httpx.HTTPError, ValueError):
                        pass
                    if process.poll() is not None:
                        raise RuntimeError("Frozen collector exited before startup")
                    time.sleep(0.1)

            if health is None:
                raise TimeoutError("Frozen collector did not start within 25 seconds")
            assert health.get("application") == "burnrate", health
            assert health.get("status") == "ok", health
            assert health.get("pid") == process.pid, (
                f"Health endpoint PID {health.get('pid')} does not match owned process {process.pid}"
            )

            settings = Settings(
                database_path=database,
                pricing_path=app.parent / "_internal" / "pricing",
                anthropic_admin_key=None,
                openai_admin_key=None,
                timezone="UTC",
                cache_hit_threshold=0.75,
                over_routing_token_ceiling=40000,
            )
            with patch.dict(os.environ, env, clear=True):
                client = NativeClient(settings, port)
                client.ensure_service()
                report = client.snapshot("all", "all")

            assert report["scope"] == "all-computers", report
            assert report["total"]["events"] == 0, report["total"]
            assert report["total"]["valueUsd"] is None, report["total"]
            assert report["tools"] == [], report["tools"]
            print(
                "Frozen collector startup, ownership check, and native usage IPC: passed"
            )
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app",
        type=Path,
        default=DEFAULT_APP,
        help="Frozen BURNRATE executable (defaults to the repository native bundle)",
    )
    args = parser.parse_args(argv)
    smoke(args.app)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
