"""Trust checks for launching the desktop HUD, and stale setup-token warnings.

No PowerShell here: the signature probe is replaced with synthetic reports so
the policy is tested without a Windows shell or a signed binary.
"""
from __future__ import annotations

import contextlib
import ctypes
import json
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from spend_app import api, native_launch
from spend_app.config import Settings
from spend_app.db import connect, initialize
from spend_app.setup_wizard import setup_token_warning

ROOT = Path(__file__).resolve().parents[1]


def _report(monkeypatch, status: str, thumbprint: str = "", subject: str = "") -> None:
    monkeypatch.setattr(native_launch, "_authenticode", lambda _path: (status, thumbprint, subject))


def test_thumbprint_constants_replaced_publisher_allowlist() -> None:
    assert not hasattr(native_launch, "PUBLISHERS_ENV")
    assert not hasattr(native_launch, "DEFAULT_PUBLISHERS")
    assert native_launch.THUMBPRINTS_ENV == "BURNRATE_NATIVE_THUMBPRINTS"
    assert native_launch.DEFAULT_THUMBPRINTS == ()
    assert native_launch.FOREIGN in native_launch.BLOCKED_ALWAYS
    assert native_launch.TAMPERED in native_launch.BLOCKED_ALWAYS
    assert native_launch.UNKNOWN in native_launch.BLOCKED_ALWAYS


def test_authenticode_returns_three_tuple(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(native_launch, "_powershell", lambda: r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
    payload = json.dumps({"status": "Valid", "thumbprint": "AABB11", "subject": "CN=Test"})
    monkeypatch.setattr(
        native_launch.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(args=[], returncode=0, stdout=payload, stderr=""),
    )
    assert native_launch._authenticode(tmp_path / "BURNRATE.exe") == ("Valid", "AABB11", "CN=Test")


def test_valid_signature_without_pinned_thumbprint_is_untrusted(tmp_path, monkeypatch) -> None:
    _report(monkeypatch, "Valid", "112233445566", "CN=Example Signer")
    monkeypatch.delenv(native_launch.THUMBPRINTS_ENV, raising=False)
    monkeypatch.delenv(native_launch.REQUIRE_SIGNED_ENV, raising=False)
    assert native_launch.signature_state(tmp_path / "BURNRATE.exe") == native_launch.UNTRUSTED
    assert native_launch.evaluate(tmp_path / "BURNRATE.exe") == (True, "untrusted")
    monkeypatch.setenv(native_launch.REQUIRE_SIGNED_ENV, "1")
    assert native_launch.evaluate(tmp_path / "BURNRATE.exe") == (False, "untrusted")


@pytest.mark.parametrize(
    "subject",
    [
        "CN=SignPath Foundation, O=SignPath Foundation",
        "CN=Kamol Nazarov, O=myBurnRate",
    ],
)
def test_valid_signature_with_known_subject_but_unpinned_thumbprint_is_untrusted(
    tmp_path, monkeypatch, subject: str
) -> None:
    _report(monkeypatch, "Valid", "DEADBEEF01234567", subject)
    monkeypatch.delenv(native_launch.THUMBPRINTS_ENV, raising=False)
    monkeypatch.delenv(native_launch.REQUIRE_SIGNED_ENV, raising=False)
    assert native_launch.signature_state(tmp_path / "BURNRATE.exe") == native_launch.UNTRUSTED
    assert native_launch.evaluate(tmp_path / "BURNRATE.exe") == (True, "untrusted")
    monkeypatch.setenv(native_launch.REQUIRE_SIGNED_ENV, "1")
    assert native_launch.evaluate(tmp_path / "BURNRATE.exe") == (False, "untrusted")


@pytest.mark.parametrize(
    ("env_value", "thumbprint", "subject"),
    [
        ("AA BB 11 , ccdd22", "aabb11", "CN=Arbitrary Signer"),
        ("AA BB 11 , ccdd22", "CC DD 22", "CN=Example Malware LLC"),
        ("aabb11", "AABB11", ""),
    ],
)
def test_valid_signature_with_pinned_thumbprint_is_always_valid(
    tmp_path, monkeypatch, env_value: str, thumbprint: str, subject: str
) -> None:
    _report(monkeypatch, "Valid", thumbprint, subject)
    monkeypatch.setenv(native_launch.THUMBPRINTS_ENV, env_value)
    monkeypatch.delenv(native_launch.REQUIRE_SIGNED_ENV, raising=False)
    assert native_launch.signature_state(tmp_path / "BURNRATE.exe") == native_launch.VALID
    assert native_launch.evaluate(tmp_path / "BURNRATE.exe") == (True, "valid")
    monkeypatch.setenv(native_launch.REQUIRE_SIGNED_ENV, "1")
    assert native_launch.evaluate(tmp_path / "BURNRATE.exe") == (True, "valid")


def test_empty_thumbprint_never_matches_pinned_list(tmp_path, monkeypatch) -> None:
    _report(monkeypatch, "Valid", "", "CN=SignPath Foundation")
    monkeypatch.setenv(native_launch.THUMBPRINTS_ENV, " , , ")
    monkeypatch.setenv(native_launch.REQUIRE_SIGNED_ENV, "1")
    assert native_launch.signature_state(tmp_path / "BURNRATE.exe") == native_launch.UNTRUSTED
    assert native_launch.evaluate(tmp_path / "BURNRATE.exe") == (False, "untrusted")


def test_not_trusted_is_untrusted(tmp_path, monkeypatch) -> None:
    _report(monkeypatch, "NotTrusted", "AA11", "CN=myBurnRate local build")
    monkeypatch.delenv(native_launch.REQUIRE_SIGNED_ENV, raising=False)
    assert native_launch.signature_state(tmp_path / "BURNRATE.exe") == native_launch.UNTRUSTED
    assert native_launch.evaluate(tmp_path / "BURNRATE.exe") == (True, "untrusted")
    monkeypatch.setenv(native_launch.REQUIRE_SIGNED_ENV, "1")
    assert native_launch.evaluate(tmp_path / "BURNRATE.exe") == (False, "untrusted")


def test_unknown_error_is_unknown_and_always_refused(tmp_path, monkeypatch) -> None:
    _report(monkeypatch, "UnknownError", "AA11", "CN=myBurnRate local build")
    monkeypatch.delenv(native_launch.REQUIRE_SIGNED_ENV, raising=False)
    assert native_launch.signature_state(tmp_path / "BURNRATE.exe") == native_launch.UNKNOWN
    assert native_launch.evaluate(tmp_path / "BURNRATE.exe") == (False, "unknown")
    monkeypatch.setenv(native_launch.REQUIRE_SIGNED_ENV, "1")
    assert native_launch.evaluate(tmp_path / "BURNRATE.exe") == (False, "unknown")


@pytest.mark.parametrize("status", ["NotSigned", "NotSupported"])
def test_not_signed_and_not_supported_are_unsigned(tmp_path, monkeypatch, status: str) -> None:
    _report(monkeypatch, status)
    monkeypatch.delenv(native_launch.REQUIRE_SIGNED_ENV, raising=False)
    assert native_launch.evaluate(tmp_path / "BURNRATE.exe") == (True, "unsigned")
    monkeypatch.setenv(native_launch.REQUIRE_SIGNED_ENV, "1")
    assert native_launch.evaluate(tmp_path / "BURNRATE.exe") == (False, "unsigned")


def test_tampered_signature_is_refused_even_without_strict_mode(tmp_path, monkeypatch) -> None:
    _report(monkeypatch, "HashMismatch", "AABB11", "CN=SignPath Foundation")
    monkeypatch.setenv(native_launch.THUMBPRINTS_ENV, "AABB11")
    monkeypatch.delenv(native_launch.REQUIRE_SIGNED_ENV, raising=False)
    assert native_launch.evaluate(tmp_path / "BURNRATE.exe") == (False, "tampered")


@pytest.mark.parametrize("status", ["", "Revoked", "BogusStatus"])
def test_unrecognized_status_is_unknown_and_always_refused(tmp_path, monkeypatch, status: str) -> None:
    _report(monkeypatch, status, "AABB11", "CN=Test")
    monkeypatch.delenv(native_launch.REQUIRE_SIGNED_ENV, raising=False)
    assert native_launch.evaluate(tmp_path / "BURNRATE.exe") == (False, "unknown")
    monkeypatch.setenv(native_launch.REQUIRE_SIGNED_ENV, "1")
    assert native_launch.evaluate(tmp_path / "BURNRATE.exe") == (False, "unknown")


def test_a_failed_probe_is_unknown_and_always_refused(tmp_path, monkeypatch) -> None:
    def broken(_path):
        raise RuntimeError("PowerShell is missing")

    monkeypatch.setattr(native_launch, "_authenticode", broken)
    monkeypatch.delenv(native_launch.REQUIRE_SIGNED_ENV, raising=False)
    assert native_launch.evaluate(tmp_path / "BURNRATE.exe") == (False, "unknown")
    monkeypatch.setenv(native_launch.REQUIRE_SIGNED_ENV, "1")
    assert native_launch.evaluate(tmp_path / "BURNRATE.exe") == (False, "unknown")


def test_powershell_resolution_uses_an_absolute_system_path(tmp_path, monkeypatch) -> None:
    system_root = tmp_path / "Windows"
    shell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    shell.parent.mkdir(parents=True)
    shell.write_bytes(b"MZ")
    monkeypatch.setattr(native_launch.sys, "platform", "win32")
    monkeypatch.setenv("SystemRoot", str(system_root))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    resolved = native_launch._powershell()
    assert resolved == str(shell)
    assert Path(resolved).is_absolute()
    source = Path(native_launch.__file__).read_text(encoding="utf-8")
    assert "shutil.which" not in source


def test_locked_binary_denies_write_and_delete_until_released(tmp_path) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows share-mode lock")
    binary = tmp_path / "BURNRATE.exe"
    binary.write_bytes(b"MZ")
    with native_launch.locked_binary(binary):
        with pytest.raises(PermissionError):
            binary.write_bytes(b"tampered")
        with pytest.raises(PermissionError):
            binary.unlink()
    binary.write_bytes(b"released")
    assert binary.read_bytes() == b"released"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "spend.db",
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )


def _hud_client(tmp_path: Path, monkeypatch) -> tuple[TestClient, Path, Path]:
    local_app_data = tmp_path / "localappdata"
    local_app_data.mkdir(parents=True, exist_ok=True)
    fake_repo = tmp_path / "repo"
    fake_api_file = fake_repo / "spend_app" / "api.py"
    fake_api_file.parent.mkdir(parents=True, exist_ok=True)
    fake_api_file.write_text("# isolated\n", encoding="utf-8")

    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.delenv("BURNRATE_DEV", raising=False)
    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    monkeypatch.delenv("BURNRATE_DEMO", raising=False)
    monkeypatch.setattr(api, "__file__", str(fake_api_file))
    if sys.platform == "win32":
        monkeypatch.setattr(ctypes.windll.user32, "FindWindowW", lambda *_args: 0)

    settings = _settings(tmp_path)
    initialize(settings.database_path)
    app = api.create_app(settings, enable_scheduler=False)
    return TestClient(app, base_url="http://127.0.0.1:17331"), local_app_data, fake_repo


@pytest.mark.parametrize("dev_mode", ["0", "1"])
def test_hud_native_ignores_legacy_binary(tmp_path, monkeypatch, dev_mode: str) -> None:
    client, local_app_data, _fake_repo = _hud_client(tmp_path, monkeypatch)
    if dev_mode == "1":
        monkeypatch.setenv("BURNRATE_DEV", "1")
    else:
        monkeypatch.delenv("BURNRATE_DEV", raising=False)
    legacy = local_app_data / "BURNRATE" / "BURNRATE.exe"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"MZ")

    with client:
        res = client.post("/api/hud/native", json={"action": "open"})
    assert res.status_code == 200
    assert res.json() == {"ok": False, "reason": "not_found"}


@pytest.mark.parametrize("dev_mode", ["0", "1"])
def test_hud_native_launches_programs_binary_with_or_without_dev_mode(
    tmp_path, monkeypatch, dev_mode: str
) -> None:
    client, local_app_data, _fake_repo = _hud_client(tmp_path, monkeypatch)
    if dev_mode == "1":
        monkeypatch.setenv("BURNRATE_DEV", "1")
    else:
        monkeypatch.delenv("BURNRATE_DEV", raising=False)
    legacy = local_app_data / "BURNRATE" / "BURNRATE.exe"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"MZ")
    expected_exe = local_app_data / "Programs" / "BURNRATE" / "BURNRATE.exe"
    expected_exe.parent.mkdir(parents=True, exist_ok=True)
    expected_exe.write_bytes(b"MZ")

    seen: list[Path] = []
    launched: list[list[str]] = []
    monkeypatch.setattr(api, "locked_binary", lambda _p: contextlib.nullcontext())
    monkeypatch.setattr(api, "evaluate_native_binary", lambda p: (seen.append(p) or True, "valid"))
    monkeypatch.setattr(subprocess, "Popen", lambda args, **_kwargs: launched.append(args))

    with client:
        res = client.post("/api/hud/native", json={"action": "open"})
    assert res.status_code == 200
    data = res.json()
    assert data == {"ok": True, "state": "valid"}
    assert seen == [expected_exe]
    assert launched == [[str(expected_exe)]]


def test_hud_native_signature_refused_returns_state_without_path(tmp_path, monkeypatch) -> None:
    client, local_app_data, _fake_repo = _hud_client(tmp_path, monkeypatch)
    exe = local_app_data / "Programs" / "BURNRATE" / "BURNRATE.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ")

    monkeypatch.setattr(api, "locked_binary", lambda _p: contextlib.nullcontext())
    monkeypatch.setattr(api, "evaluate_native_binary", lambda _p: (False, "tampered"))

    with client:
        res = client.post("/api/hud/native", json={"action": "open"})
    assert res.status_code == 200
    data = res.json()
    assert data == {"ok": False, "reason": "binary_refused", "state": "tampered"}
    dumped = json.dumps(data)
    assert "exe" not in dumped.lower()
    assert str(exe) not in dumped


def test_hud_native_oserror_returns_binary_busy_without_path_or_exc(tmp_path, monkeypatch) -> None:
    client, local_app_data, _fake_repo = _hud_client(tmp_path, monkeypatch)
    exe = local_app_data / "Programs" / "BURNRATE" / "BURNRATE.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ")
    secret = "secret-lock-details-xyz"

    @contextlib.contextmanager
    def busy_lock(_p):
        raise OSError(secret)
        yield  # pragma: no cover

    monkeypatch.setattr(api, "locked_binary", busy_lock)
    with client:
        res = client.post("/api/hud/native", json={"action": "open"})
    assert res.status_code == 200
    data = res.json()
    assert data == {"ok": False, "reason": "binary_busy"}
    dumped = json.dumps(data)
    assert "exe" not in dumped.lower()
    assert str(exe) not in dumped
    assert secret not in dumped


def test_hud_native_other_exception_returns_launch_failed_without_path_or_exc(
    tmp_path, monkeypatch
) -> None:
    client, local_app_data, _fake_repo = _hud_client(tmp_path, monkeypatch)
    exe = local_app_data / "Programs" / "BURNRATE" / "BURNRATE.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ")
    secret = "secret-unexpected-failure-abc"

    monkeypatch.setattr(api, "locked_binary", lambda _p: contextlib.nullcontext())
    monkeypatch.setattr(api, "evaluate_native_binary", lambda _p: (True, "valid"))

    def boom(*_args, **_kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(subprocess, "Popen", boom)
    with client:
        res = client.post("/api/hud/native", json={"action": "open"})
    assert res.status_code == 200
    data = res.json()
    assert data == {"ok": False, "reason": "launch_failed"}
    dumped = json.dumps(data)
    assert "exe" not in dumped.lower()
    assert str(exe) not in dumped
    assert secret not in dumped


def test_hud_native_launch_success_omits_path_and_exe(tmp_path, monkeypatch) -> None:
    client, local_app_data, _fake_repo = _hud_client(tmp_path, monkeypatch)
    exe = local_app_data / "Programs" / "BURNRATE" / "BURNRATE.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ")

    monkeypatch.setattr(api, "locked_binary", lambda _p: contextlib.nullcontext())
    monkeypatch.setattr(api, "evaluate_native_binary", lambda _p: (True, "valid"))
    popen_calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "Popen", lambda args, **_kwargs: popen_calls.append(args))

    with client:
        res = client.post("/api/hud/native", json={"action": "open"})
    assert res.status_code == 200
    data = res.json()
    assert data == {"ok": True, "state": "valid"}
    assert "exe" not in data
    dumped = json.dumps(data)
    assert "exe" not in dumped.lower()
    assert str(exe) not in dumped
    assert popen_calls == [[str(exe)]]


def test_setup_token_warning_only_after_an_owner_exists(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_SETUP_TOKEN", "one-time-value")
    with connect(settings.database_path) as connection:
        assert setup_token_warning(connection) is None
        connection.execute(
            "INSERT INTO app_meta(key, value) VALUES('accounts.v1', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ('{"version":1,"users":{"owner":{"iterations":600000}}}',),
        )
        warning = setup_token_warning(connection)
    assert warning is not None and "BURNRATE_SETUP_TOKEN" in warning


def test_setup_token_warning_is_silent_off_the_hub(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    monkeypatch.setenv("BURNRATE_SETUP_TOKEN", "one-time-value")
    with connect(settings.database_path) as connection:
        assert setup_token_warning(connection) is None
