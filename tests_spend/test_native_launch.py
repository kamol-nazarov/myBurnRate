"""Trust checks for launching the desktop HUD, and stale setup-token warnings.

No PowerShell here: the signature probe is replaced with synthetic reports so
the policy is tested without a Windows shell or a signed binary.
"""
from __future__ import annotations

from pathlib import Path

from spend_app import native_launch
from spend_app.config import Settings
from spend_app.db import connect, initialize
from spend_app.setup_wizard import setup_token_warning

ROOT = Path(__file__).resolve().parents[1]


def _report(monkeypatch, status: str, subject: str = "") -> None:
    monkeypatch.setattr(native_launch, "_authenticode", lambda _path: (status, subject))


def test_valid_signature_from_a_known_publisher_is_launched(tmp_path, monkeypatch) -> None:
    _report(monkeypatch, "Valid", "CN=SignPath Foundation, O=SignPath Foundation")
    monkeypatch.delenv(native_launch.REQUIRE_SIGNED_ENV, raising=False)
    assert native_launch.evaluate(tmp_path / "burnrate-desktop.exe") == (True, "valid")


def test_valid_signature_from_another_publisher_is_refused(tmp_path, monkeypatch) -> None:
    _report(monkeypatch, "Valid", "CN=Example Malware LLC")
    assert native_launch.evaluate(tmp_path / "burnrate-desktop.exe") == (False, "foreign")


def test_tampered_signature_is_refused_even_without_strict_mode(tmp_path, monkeypatch) -> None:
    _report(monkeypatch, "HashMismatch")
    assert native_launch.evaluate(tmp_path / "burnrate-desktop.exe") == (False, "tampered")


def test_unsigned_binary_is_allowed_but_reported_by_default(tmp_path, monkeypatch) -> None:
    _report(monkeypatch, "NotSigned")
    monkeypatch.delenv(native_launch.REQUIRE_SIGNED_ENV, raising=False)
    assert native_launch.evaluate(tmp_path / "burnrate-desktop.exe") == (True, "unsigned")


def test_unsigned_binary_is_refused_in_strict_mode(tmp_path, monkeypatch) -> None:
    _report(monkeypatch, "NotSigned")
    monkeypatch.setenv(native_launch.REQUIRE_SIGNED_ENV, "1")
    assert native_launch.evaluate(tmp_path / "burnrate-desktop.exe") == (False, "unsigned")


def test_a_self_signed_local_build_is_allowed_by_default(tmp_path, monkeypatch) -> None:
    _report(monkeypatch, "NotTrusted", "CN=myBurnRate local build")
    monkeypatch.delenv(native_launch.REQUIRE_SIGNED_ENV, raising=False)
    assert native_launch.evaluate(tmp_path / "burnrate-desktop.exe") == (True, "untrusted")


def test_a_failed_probe_is_unknown_and_fails_closed_in_strict_mode(tmp_path, monkeypatch) -> None:
    def broken(_path):
        raise RuntimeError("PowerShell is missing")

    monkeypatch.setattr(native_launch, "_authenticode", broken)
    monkeypatch.delenv(native_launch.REQUIRE_SIGNED_ENV, raising=False)
    assert native_launch.evaluate(tmp_path / "burnrate-desktop.exe") == (True, "unknown")
    monkeypatch.setenv(native_launch.REQUIRE_SIGNED_ENV, "1")
    assert native_launch.evaluate(tmp_path / "burnrate-desktop.exe") == (False, "unknown")


def test_publisher_allowlist_can_be_extended(tmp_path, monkeypatch) -> None:
    _report(monkeypatch, "Valid", "CN=Kamol Nazarov, O=myBurnRate")
    monkeypatch.setenv(native_launch.PUBLISHERS_ENV, "Kamol Nazarov")
    assert native_launch.evaluate(tmp_path / "burnrate-desktop.exe") == (True, "valid")


def _settings(tmp_path) -> Settings:
    return Settings(
        database_path=tmp_path / "spend.db",
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )


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
