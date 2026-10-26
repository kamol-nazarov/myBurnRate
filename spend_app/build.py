"""This checkout's build: the version plus its git commit.

Computers often run the same version number with different code (changes ship
between version bumps), so the Hub check compares builds. Read from the git
files directly; no git executable is needed.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from spend_app import __version__


def _git_dir(root: Path) -> Path | None:
    git = root / ".git"
    if git.is_file():  # a worktree: "gitdir: <path>"
        text = git.read_text(encoding="utf-8").strip()
        return Path(text.split(":", 1)[1].strip()) if text.startswith("gitdir:") else None
    return git if git.is_dir() else None


@lru_cache(maxsize=1)
def commit() -> str | None:
    try:
        git = _git_dir(Path(__file__).resolve().parents[1])
        if git is None:
            return None
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head[:12] or None
        ref = head[5:]
        common = git
        if (git / "commondir").is_file():
            common = (git / (git / "commondir").read_text(encoding="utf-8").strip()).resolve()
        for base in (git, common):
            if (base / ref).is_file():
                return (base / ref).read_text(encoding="utf-8").strip()[:12] or None
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref):
                    return line.split()[0][:12]
    except (OSError, IndexError, ValueError):
        return None
    return None


def build_id() -> str:
    sha = commit()
    return f"{__version__}+{sha}" if sha else __version__
