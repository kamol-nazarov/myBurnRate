"""The frozen native app the installer ships (packaging/)."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spend_app import build, service


def test_a_packaged_build_reads_its_recorded_commit(tmp_path: Path) -> None:
    assert build.commit_at(tmp_path) is None
    (tmp_path / "BUILD_COMMIT").write_text("0123456789abcdef0123\n", encoding="utf-8")
    assert build.commit_at(tmp_path) == "0123456789ab"


def test_another_data_root_gets_its_own_service_mutex(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BURNRATE_DATA_ROOT", raising=False)
    assert service.mutex_name() == service.MUTEX_NAME
    monkeypatch.setenv("BURNRATE_DATA_ROOT", str(tmp_path))
    separate = service.mutex_name()
    assert separate.startswith(service.MUTEX_NAME + "-") and separate != service.MUTEX_NAME
    # The data root override moves the database, settings and logs together.
    assert service.data_root() == tmp_path
    from spend_app.config import default_app_dir, default_database_path

    assert default_app_dir() == tmp_path
    monkeypatch.delenv("SPEND_DATABASE_PATH", raising=False)
    monkeypatch.delenv("BURNRATE_DEV", raising=False)
    assert default_database_path() == tmp_path / "spend.db"


def test_the_spec_ships_what_the_service_reads() -> None:
    root = Path(__file__).resolve().parents[1]
    spec = (root / "packaging" / "burnrate-native.spec").read_text(encoding="utf-8")
    for needed in (
        '"pricing"',
        'collect_data_files("tzdata")',
        'copy_metadata("APScheduler")',
        "console=False",
        "assets",
    ):
        assert needed in spec
    assert "spend_web" not in spec
    entry = (root / "packaging" / "burnrate_native.py").read_text(encoding="utf-8")
    assert "multiprocessing.freeze_support()" in entry


def test_the_update_feed_points_each_platform_at_its_signed_bundle(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("make_latest_json", root / "packaging" / "make_latest_json.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    name = "myBurnRate_0.5.0_x64-setup.exe"
    installer = tmp_path / name
    installer.write_bytes(b"bundle")
    sig_text = base64.b64encode(
        (
            "untrusted comment: signature from tauri secret key\n"
            "RUR0\n"
            f"trusted comment: timestamp:1\tfile:{name}\n"
            "AAAA\n"
        ).encode("utf-8")
    ).decode("ascii")
    (tmp_path / (name + ".sig")).write_text(sig_text + "\n", encoding="utf-8")

    feed = module.build_native_feed(tmp_path, tag="v0.5.0", repo="owner/repo", now=datetime(2026, 11, 1, tzinfo=UTC))
    assert feed["version"] == "0.5.0" and feed["pub_date"] == "2026-11-01T00:00:00Z"
    assert set(feed["platforms"]) == {"windows-x86_64"}
    windows = feed["platforms"]["windows-x86_64"]
    assert windows["url"] == f"https://github.com/owner/repo/releases/download/v0.5.0/{name}"
    assert windows["signature"] == sig_text
    assert windows["sha256"] == hashlib.sha256(b"bundle").hexdigest()
    assert windows["size"] == len(b"bundle")

    argv = sys.argv
    sys.argv = ["make_latest_json", "--native", "--tag", "v0.5.0", "--repo", "owner/repo", "--dir", str(tmp_path)]
    try:
        assert module.main() == 0
    finally:
        sys.argv = argv
    written = json.loads((tmp_path / "native-latest.json").read_text(encoding="utf-8"))
    assert written["version"] == "0.5.0"
    assert written["platforms"]["windows-x86_64"] == windows

    (tmp_path / (name + ".sig")).unlink()
    with pytest.raises(SystemExit, match="no update signature"):
        module.build_native_feed(tmp_path, tag="v0.5.0", repo="owner/repo")
