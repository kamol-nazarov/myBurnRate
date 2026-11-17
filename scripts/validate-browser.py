"""Mandatory release browser validation. Missing coverage cannot pass."""

import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests_spend.browser_runtime import CHROME


def main():
    if not CHROME.is_file() or not shutil.which("node"):
        raise SystemExit(
            "Required Chrome/Node runtime missing. Set BURNRATE_CHROME to the Chrome executable."
        )
    report = ROOT / "test-results" / "required-browser.xml"
    report.parent.mkdir(exist_ok=True)
    env = dict(os.environ, BURNRATE_CHROME=str(CHROME))
    env.pop("BURNRATE_RACE_BASELINE", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests_spend",
            "-m",
            "browser",
            "--ignore=tests_spend/test_s01_real_db.py",
            "--ignore=tests_spend/test_s02_real_db.py",
            "--ignore=tests_spend/test_s03_real_db.py",
            "-q",
            "-o",
            "addopts=",
            "--junitxml=" + str(report),
        ],
        cwd=ROOT,
        env=env,
        check=False,
    )
    if result.returncode:
        return result.returncode
    cases = ET.parse(report).findall(".//testcase")
    desktop_ui = [c for c in cases if "test_wealthfront_ui" in c.get("classname", "")]
    desktop_layout = [
        c for c in cases if "test_wealthfront_layout" in c.get("classname", "")
    ]
    if (
        not desktop_ui
        or not desktop_layout
        or len(cases) < 50
        or any(c.find("skipped") is not None for c in cases)
    ):
        raise SystemExit("Required browser coverage missing or skipped")
    print(
        f"Required browser validation: {len(cases)} executed ({len(desktop_ui)} desktop UI, {len(desktop_layout)} layout); zero skipped"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
