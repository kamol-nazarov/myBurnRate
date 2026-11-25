"""Disposable non-editable package smoke; never starts a provider scheduler."""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_api_headers(key_path):
    # Read the disposable install's credential in this process. Neither the
    # key nor any authenticated request URL is passed in a child command.
    with Path(key_path).open("rb") as stream:
        value = stream.read(67)
    if re.fullmatch(rb"[0-9a-f]{64}(?:\r?\n)?", value) is None:
        raise ValueError("Invalid local smoke-test credential")
    return {"Authorization": "Bearer " + value[:64].decode("ascii")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-source", type=Path)
    args = parser.parse_args()
    assert sys.version_info[:2] == (3, 12), "Python 3.12 is required"
    with tempfile.TemporaryDirectory(prefix="burnrate-smoke-") as temp:
        work = Path(temp)
        env = {
            k: v
            for k, v in os.environ.items()
            if not any(
                word in k.upper()
                for word in (
                    "API_KEY",
                    "ADMIN_KEY",
                    "MANAGEMENT_KEY",
                    "SPEND_",
                    "BURNRATE",
                    "PYTHONPATH",
                )
            )
        }
        env.update(
            SPEND_DATABASE_PATH=str(work / "data" / "spend.db"),
            BURNRATE_DATA_ROOT=str(work / "data"),
            LOCALAPPDATA=str(work),
            HOME=str(work),
            USERPROFILE=str(work),
            PYTHONUTF8="1",
        )
        subprocess.run([sys.executable, "-m", "venv", str(work / "venv")], check=True)
        python = (
            work / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        subprocess.run(
            [str(python), "-m", "pip", "install", "--upgrade", "pip"], check=True
        )
        if args.private_source:
            source = args.private_source.resolve()
            subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    str(source / "requirements-spend.txt"),
                ],
                check=True,
            )
            env["PYTHONPATH"] = str(source)
            env["SPEND_PRICING_PATH"] = str(source / "pricing")
        else:
            subprocess.run(
                [sys.executable, "-m", "build", "--wheel", "--outdir", str(work)],
                cwd=ROOT,
                check=True,
            )
            (wheel,) = work.glob("*.whl")
            subprocess.run(
                [str(python), "-m", "pip", "install", str(wheel)], cwd=work, check=True
            )

        def run(*arguments, check=True):
            return subprocess.run(
                [str(python), *arguments],
                cwd=work,
                env=env,
                check=check,
                capture_output=True,
                text=True,
            )

        doctor = run("-m", "spend_app.cli", "doctor", check=False)
        assert doctor.returncode != 0 and not Path(env["SPEND_DATABASE_PATH"]).exists()
        run("-m", "spend_app.cli", "init-db" if args.private_source else "init")
        assert json.loads(run("-m", "spend_app.cli", "doctor").stdout)["ok"]
        version = run(
            "-c", "from spend_app import __version__; print(__version__)"
        ).stdout.strip()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        code = (
            "import uvicorn; from spend_app.api import create_app; app=create_app(enable_scheduler=False); "
            "server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port="
            + str(port)
            + ")); "
            "app.add_api_route('/_smoke/stop',lambda:setattr(server,'should_exit',True),methods=['POST']); server.run()"
        )
        process = subprocess.Popen(
            [str(python), "-c", code],
            cwd=work,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        opener = build_opener(ProxyHandler({}), NoRedirects())
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    with opener.open(base + "/healthz", timeout=1) as response:
                        assert response.status == 200
                    break
                except OSError:
                    if process.poll() is not None:
                        raise AssertionError("Smoke server exited before readiness")
                    time.sleep(0.1)
            else:
                raise AssertionError("Smoke server did not become ready")
            # Web pages were removed: non-API paths return 404 JSON with REQ-404.
            for removed_path in ("/", "/hud", "/setup", "/auth", "/connections"):
                try:
                    opener.open(base + removed_path, timeout=5)
                    raise AssertionError(f"Expected 404 for {removed_path}")
                except HTTPError as exc:
                    assert exc.code == 404, (removed_path, exc.code)
                    err_body = json.loads(exc.read().decode())
                    assert err_body["error"]["code"] == "REQ-404"
                    assert err_body["error"]["message"]
            # Desktop UI HTML loads directly from the installed spend_app package.
            desktop_html = run(
                "-c",
                "from spend_app.wealthfront_window import load_ui_html; print(len(load_ui_html()))",
            ).stdout.strip()
            assert int(desktop_html) > 10000
            try:
                opener.open(base + "/api/spend", timeout=5)
                raise AssertionError(
                    "Expected anonymous local API access to be refused"
                )
            except HTTPError as exc:
                assert exc.code == 401

            # Use the installed package to resolve the current key location.
            key_path = run(
                "-c",
                "from spend_app.config import default_database_path; "
                "from spend_app.local_access import key_path; "
                "print(key_path(default_database_path()))",
            ).stdout.strip()
            headers = local_api_headers(key_path)
            for endpoint in (
                "/api/spend",
                "/api/spend/summary?window=1d",
                "/api/spend/health",
                "/api/spend/hud",
                "/api/spend/hud/usage",
                "/api/spend/freshness?view=hud",
                "/api/subscriptions",
                "/api/onboarding",
            ):
                with opener.open(
                    Request(base + endpoint, headers=headers), timeout=5
                ) as response:
                    payload = json.load(response)
                if endpoint == "/api/spend":
                    assert payload["version"] == version
                if endpoint == "/api/subscriptions" and not args.private_source:
                    assert payload["plans"] == []
                if endpoint == "/api/onboarding":
                    assert payload["hasHistory"] is False
            print(
                f"SMOKE PASS: {version}; init/readonly doctor; desktop HTML; 404 pages; authenticated reads"
            )
        finally:
            try:
                with opener.open(
                    Request(base + "/_smoke/stop", method="POST"), timeout=5
                ):
                    pass
                process.wait(timeout=20)
                assert process.returncode == 0, "Smoke server did not shut down cleanly"
            except Exception:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        check=False,
                    )
                else:
                    process.kill()
                process.wait(timeout=15)
                raise


if __name__ == "__main__":
    main()
