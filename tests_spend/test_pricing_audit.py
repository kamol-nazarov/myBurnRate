from datetime import UTC, datetime
from pathlib import Path
import sqlite3

import pytest

from spend_app.pricing import PricingEngine
from spend_app.pricing_audit import audit

ROOT = Path(__file__).resolve().parents[1]


def test_audit_checks_both_tables_and_historical_dates_without_writing(tmp_path):
    path = tmp_path / "audit.db"
    with sqlite3.connect(path) as db:
        for table in ("usage_events", "unpriced_usage_events"):
            db.execute(f"CREATE TABLE {table} (model_key TEXT, occurred_at TEXT, input_tokens INTEGER, cache_write_tokens INTEGER, output_tokens INTEGER)")
        db.execute("INSERT INTO usage_events VALUES ('gpt-6-astra', '2026-11-09T12:00:00Z', 100, 20, 10)")
        db.executemany("INSERT INTO unpriced_usage_events VALUES (?, ?, 100, 20, 10)", [
            ("gpt-6-astra", "2026-01-01T00:00:00Z"),
            ("unknown", "2026-11-09T12:00:00Z"),
            ("gpt-6-astra", "2026-11-09T12:00:00Z"),
        ])
    before = path.read_bytes()
    report = audit(path, PricingEngine.load(ROOT / "pricing"), now=datetime(2026, 11, 9, tzinfo=UTC))
    assert path.read_bytes() == before
    assert report["modelsWithoutCurrentRate"] == 1
    assert report["missingRateRecords"] == 2
    astra = report["models"][0]
    assert astra["pricedRecords"] == 2
    assert astra["missingRateTokens"] == 130
    assert astra["currentRate"] is True


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        audit(path, PricingEngine.load(ROOT / "pricing"), now=datetime(2026, 11, 9, tzinfo=UTC))
    assert not path.exists()
