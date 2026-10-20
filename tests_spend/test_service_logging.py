from __future__ import annotations

from pathlib import Path

from spend_app.service import LOG_BACKUPS, LOG_MAX_BYTES, _log_config


def test_dashboard_log_rotates_and_drops_per_request_chatter(tmp_path: Path) -> None:
    # Regression: a plain FileHandler at INFO grew dashboard.log to ~150 MB,
    # mostly one httpx line per request and an APScheduler line per job.
    config = _log_config(tmp_path / "dashboard.log")
    handler = config["handlers"]["file"]
    assert handler["class"] == "logging.handlers.RotatingFileHandler"
    assert handler["maxBytes"] == LOG_MAX_BYTES and handler["backupCount"] == LOG_BACKUPS
    assert LOG_MAX_BYTES * (LOG_BACKUPS + 1) <= 32 * 1024 * 1024
    loggers = config["loggers"]
    assert loggers["httpx"]["level"] == "WARNING"
    assert loggers["httpcore"]["level"] == "WARNING"
    assert loggers["apscheduler.scheduler"]["level"] == "ERROR"
    assert loggers["apscheduler.executors.default"]["level"] == "WARNING"
    assert config["root"]["level"] == "INFO"
