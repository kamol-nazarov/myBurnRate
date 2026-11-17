"""One browser-discovery contract for optional local and required release tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

CHROME = Path(
    os.environ.get(
        "BURNRATE_CHROME",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    )
)


def _chrome_dump(url: str, width: int, tmp: Path, budget_ms: int = 8000) -> str:
    profile = tmp / f"chrome-{width}-{os.getpid()}"
    profile.mkdir(parents=True, exist_ok=True)
    dump = tmp / f"dump-{width}.html"
    cmd = [
        str(CHROME),
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--mute-audio",
        "--disable-dev-shm-usage",
        f"--user-data-dir={profile}",
        f"--window-size={width},1100",
        f"--virtual-time-budget={budget_ms}",
        "--dump-dom",
        "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost",
        url,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=40,
        check=False,
    )
    html = result.stdout or ""
    dump.write_text(html, encoding="utf-8")
    if "PROBE:" not in html and result.returncode != 0:
        raise AssertionError(
            f"chrome dump failed rc={result.returncode} stderr={result.stderr[-2000:]}"
        )
    return html


def _probe(html: str) -> dict:
    marker = "PROBE:"
    if marker in html:
        raw = html.split(marker, 1)[1]
        raw = raw.split("</title>", 1)[0].split("<", 1)[0]
        return json.loads(raw)
    if 'id="probe-report"' in html:
        block = html.split('id="probe-report"', 1)[1].split("</pre>", 1)[0]
        text = block.split(">", 1)[1]
        return json.loads(text)
    raise AssertionError("probe report missing from dump-dom")
