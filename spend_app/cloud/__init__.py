"""Multi-user hosted cloud backend for myBurnRate (BURNRATE_CLOUD_SERVER=1).

Architecture:
- Central ``auth.db`` (SQLite WAL) for users, pending registrations, 6-digit
  email OTPs, 8-char device pairing codes, cloud sessions (``br_cs_*``),
  cloud machine tokens (``br_cm_*``), and append-only ``cloud_audit``.
- One physically isolated SQLite database per verified user
  (``<cloud_root>/users/<user_id>/spend.db``), never created until email
  verification succeeds.
"""
from __future__ import annotations

import os
from pathlib import Path

from spend_app.config import default_app_dir


def cloud_server_enabled() -> bool:
    """True when running the multi-user hosted cloud server."""
    return os.getenv("BURNRATE_CLOUD_SERVER", "").strip().lower() in {"1", "true", "yes"}


def cloud_data_root() -> Path:
    """Root directory holding ``auth.db`` and ``users/<user_id>/spend.db``."""
    override = os.getenv("BURNRATE_CLOUD_DATA_ROOT", "").strip()
    if override:
        return Path(override)
    return default_app_dir() / "cloud"


def cloud_auth_db_path(root: Path | None = None) -> Path:
    return (Path(root) if root is not None else cloud_data_root()) / "auth.db"


__all__ = [
    "cloud_auth_db_path",
    "cloud_data_root",
    "cloud_server_enabled",
]
