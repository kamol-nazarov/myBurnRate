"""The frozen service the desktop app ships (packaging/)."""

from __future__ import annotations

from pathlib import Path

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
