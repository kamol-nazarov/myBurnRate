from pathlib import Path

import pytest

from spend_app.codex_speed import configured_codex_speed


def connections_for(sessions: Path, state: str = "receiving") -> list[dict]:
    return [
        {
            "source": "codex_local",
            "profiles": [
                {
                    "key": "default",
                    "state": state,
                    "location": str(sessions),
                }
            ],
        }
    ]


@pytest.mark.parametrize(
    ("config", "speed"),
    [
        ('service_tier = "ultrafast"', "ultrafast"),
        ('service_tier = "priority"', "fast"),
        ('service_tier = "fast"', "fast"),
        ('service_tier = "default"', "standard"),
        ('service_tier = "auto"', None),
        ('model_reasoning_effort = "high"', None),
        ('service_tier = "ultrafast"\nmodel_provider = "custom"', None),
        ('service_tier = "ultrafast"\n[features]\nultrafast_mode = false', None),
        ('service_tier = "ultrafast"\n[features]\nfast_mode = false', "ultrafast"),
        ("service_tier = [", None),
    ],
)
def test_explicit_codex_speed_settings(tmp_path, monkeypatch, config, speed):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text(config, encoding="utf-8")
    assert configured_codex_speed(connections_for(sessions)) == speed


def test_speed_is_reread_and_stays_with_its_config_root(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    config = home / "config.toml"
    config.write_text('service_tier = "ultrafast"', encoding="utf-8")
    assert configured_codex_speed(connections_for(sessions)) == "ultrafast"
    assert configured_codex_speed(connections_for(sessions, "disabled")) is None
    other = tmp_path / "other" / "sessions"
    other.mkdir(parents=True)
    (other.parent / "config.toml").write_text('service_tier = "fast"', encoding="utf-8")
    assert configured_codex_speed(connections_for(other)) is None
    config.write_text('service_tier = "fast"', encoding="utf-8")
    assert configured_codex_speed(connections_for(sessions)) == "fast"


def test_codex_speed_omits_oversized_configuration(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_bytes(
        b'service_tier = "ultrafast"\n#' + b"x" * (256 * 1024)
    )
    assert configured_codex_speed(connections_for(sessions)) is None
