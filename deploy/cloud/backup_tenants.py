"""CLI wrapper delegating to spend_app.cloud.backup for host-side backup scripts."""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from spend_app.cloud.backup import backup_cloud_root, main  # noqa: E402

__all__ = ["backup_cloud_root", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
