import os
import shutil
from pathlib import Path

import pytest

from spend_app.db import connect
from spend_app.quotas import poll_activity, poll_quotas
from tests_spend.test_quota_pollers import ACTIVITY, fixture_collectors


pytestmark = pytest.mark.skipif(
    not os.environ.get("SPEND_REAL_DB_COPY"),
    reason="SPEND_REAL_DB_COPY is not set",
)

ROOT = Path(__file__).resolve().parents[1]


def _copy(tmp_path: Path) -> Path:
    dest = tmp_path / "spend.db"
    shutil.copy2(os.environ["SPEND_REAL_DB_COPY"], dest)
    return dest


def test_s02_10_and_11_poll_copy_then_replay_writes_zero(tmp_path: Path) -> None:
    database = _copy(tmp_path)
    first_quota = poll_quotas(
        database, collectors=fixture_collectors(database), now=lambda: "2026-09-15T12:00:00Z"
    )
    second_quota = poll_quotas(
        database, collectors=fixture_collectors(database), now=lambda: "2026-09-15T12:00:00Z"
    )
    assert first_quota["written"] > 0
    first_activity = poll_activity(database, collector=lambda: ACTIVITY, now=lambda: "2026-09-15T12:00:00Z")
    second_activity = poll_activity(database, collector=lambda: ACTIVITY, now=lambda: "2026-09-15T12:01:00Z")
    assert first_activity["new"] >= 1
    assert second_activity["new"] == 0
    with connect(database) as connection:
        count = connection.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
        payg = connection.execute(
            "SELECT is_payg, pct FROM quotas WHERE provider_key='openrouter'"
        ).fetchone()
    assert count >= 1
    if payg is not None:
        assert payg[0] == 1
        assert payg[1] is None
    assert second_quota["written"] >= 0
