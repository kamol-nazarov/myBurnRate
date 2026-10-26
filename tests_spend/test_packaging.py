"""The frozen service the desktop app ships (packaging/)."""

from __future__ import annotations

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
    spec = (root / "packaging" / "burnrate-service.spec").read_text(encoding="utf-8")
    for needed in ('"pricing"', 'collect_data_files("spend_web")', 'collect_data_files("tzdata")',
                   'copy_metadata("APScheduler")', 'console=False'):
        assert needed in spec
    entry = (root / "packaging" / "burnrate_service.py").read_text(encoding="utf-8")
    assert "multiprocessing.freeze_support()" in entry


def test_the_update_feed_points_each_platform_at_its_signed_bundle(tmp_path: Path) -> None:
    import importlib.util
    from datetime import UTC, datetime

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("make_latest_json", root / "packaging" / "make_latest_json.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("BURNRATE_0.5.0_x64-setup.exe", "BURNRATE_0.5.0_aarch64.app.tar.gz", "BURNRATE_0.5.0_amd64.AppImage"):
        (tmp_path / name).write_bytes(b"bundle")
        (tmp_path / (name + ".sig")).write_text(f"sig-of-{name}\n", encoding="utf-8")
    (tmp_path / "BURNRATE_0.5.0_aarch64.dmg").write_bytes(b"not an update bundle")
    feed = module.build_feed(tmp_path, tag="v0.5.0", repo="owner/repo", now=datetime(2026, 11, 1, tzinfo=UTC))
    assert feed["version"] == "0.5.0" and feed["pub_date"] == "2026-11-01T00:00:00Z"
    assert set(feed["platforms"]) == {"windows-x86_64", "darwin-aarch64", "linux-x86_64"}
    windows = feed["platforms"]["windows-x86_64"]
    assert windows["url"] == "https://github.com/owner/repo/releases/download/v0.5.0/BURNRATE_0.5.0_x64-setup.exe"
    assert windows["signature"] == "sig-of-BURNRATE_0.5.0_x64-setup.exe"
    (tmp_path / "BURNRATE_0.5.0_amd64.AppImage.sig").unlink()
    with pytest.raises(SystemExit, match="no update signature"):
        module.build_feed(tmp_path, tag="v0.5.0", repo="owner/repo")


def test_scoop_and_winget_manifests_name_the_installer_and_its_hash(tmp_path: Path) -> None:
    import hashlib
    import importlib.util
    import json
    import sys
    import zipfile

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("render_manifests", root / "packaging" / "render_manifests.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    setup = tmp_path / "BURNRATE_0.5.0_x64-setup.exe"
    setup.write_bytes(b"installer bytes")
    digest = hashlib.sha256(b"installer bytes").hexdigest()
    argv = sys.argv
    sys.argv = ["render_manifests", "--tag", "v0.5.0", "--repo", "owner/repo", "--dir", str(tmp_path)]
    try:
        assert module.main() == 0
    finally:
        sys.argv = argv
    scoop = json.loads((tmp_path / "myburnrate.json").read_text(encoding="utf-8"))
    url = "https://github.com/owner/repo/releases/download/v0.5.0/BURNRATE_0.5.0_x64-setup.exe"
    assert scoop["version"] == "0.5.0" and scoop["architecture"]["64bit"] == {"url": url + "#/setup.exe", "hash": digest}
    assert scoop["installer"]["args"] == ["/S", "/D=$dir"] and scoop["uninstaller"]["file"] == "uninstall.exe"
    with zipfile.ZipFile(tmp_path / "winget-manifests-0.5.0.zip") as bundle:
        installer = bundle.read("KamolNazarov.myBurnRate.installer.yaml").decode()
        assert set(bundle.namelist()) == {"KamolNazarov.myBurnRate.yaml", "KamolNazarov.myBurnRate.installer.yaml",
                                          "KamolNazarov.myBurnRate.locale.en-US.yaml"}
    assert f"InstallerUrl: {url}" in installer and f"InstallerSha256: {digest.upper()}" in installer
    assert "InstallerType: nullsoft" in installer and "Scope: user" in installer
