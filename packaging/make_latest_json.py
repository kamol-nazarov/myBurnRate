"""Write latest.json, the Tauri updater feed, from a release's signed bundles.

    python packaging/make_latest_json.py --tag v0.4.0-beta.2 --repo owner/name --dir release/

Each platform entry points at the asset on the GitHub release and carries the
contents of its .sig file. The feed is served from
https://github.com/<repo>/releases/latest/download/latest.json, so only the
release marked Latest offers updates.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

# (platform key, how the updater bundle's file name ends)
PLATFORMS = (
    ("windows-x86_64", "_x64-setup.exe"),
    ("darwin-aarch64", "_aarch64.app.tar.gz"),
    ("darwin-x86_64", "_x64.app.tar.gz"),
    ("linux-x86_64", "_amd64.AppImage"),
)


def build_feed(directory: Path, *, tag: str, repo: str, notes: str = "", now: datetime | None = None) -> dict:
    version = tag.removeprefix("v")
    platforms = {}
    for key, suffix in PLATFORMS:
        for bundle in sorted(directory.glob(f"*{suffix}")):
            signature = bundle.with_name(bundle.name + ".sig")
            if not signature.is_file():
                raise SystemExit(f"{bundle.name} has no update signature ({signature.name})")
            platforms[key] = {
                "signature": signature.read_text(encoding="utf-8").strip(),
                "url": f"https://github.com/{repo}/releases/download/{tag}/{bundle.name}",
            }
    if not platforms:
        raise SystemExit(f"No update bundles in {directory}")
    return {
        "version": version,
        "notes": notes or f"See https://github.com/{repo}/releases/tag/{tag}",
        "pub_date": (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
        "platforms": platforms,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--dir", required=True, type=Path)
    args = parser.parse_args()
    feed = build_feed(args.dir, tag=args.tag, repo=args.repo)
    (args.dir / "latest.json").write_text(json.dumps(feed, indent=2) + "\n", encoding="utf-8")
    print("latest.json:", ", ".join(feed["platforms"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
