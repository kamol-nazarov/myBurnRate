"""Hatchling build hook to validate and embed ``spend_app/_build_config.py``."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

ROOT = Path(__file__).resolve().parent
PACKAGING_DIR = ROOT / "packaging"
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

from build_config import BUILTIN_HUB_ENV, validate_builtin_hub_url, write_build_config


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        self._temp_dir: str | None = None
        validate_builtin_hub_url(os.environ.get(BUILTIN_HUB_ENV, ""))
        if self.target_name == "wheel" and version != "editable":
            temp_dir = tempfile.mkdtemp(prefix="burnrate-build-config-")
            self._temp_dir = temp_dir
            generated, _ = write_build_config(Path(temp_dir) / "_build_config.py")
            force_include = build_data.setdefault("force_include", {})
            force_include[str(generated)] = "spend_app/_build_config.py"
            web_dir = Path(self.root) / "spend_web"
            if web_dir.is_dir():
                for path in sorted(web_dir.rglob("*")):
                    if (
                        path.is_file()
                        and "__pycache__" not in path.parts
                        and path.suffix != ".pyc"
                    ):
                        force_include[str(path)] = (
                            f"spend_web/{path.relative_to(web_dir).as_posix()}"
                        )

    def finalize(
        self, version: str, build_data: dict[str, Any], artifact_path: str
    ) -> None:
        temp_dir = getattr(self, "_temp_dir", None)
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
            self._temp_dir = None
