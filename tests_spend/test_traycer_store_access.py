from __future__ import annotations

import pytest

from spend_app.adapters.local_common import sqlite_read_only
from spend_app.adapters.traycer_local import covered_sessions
from spend_app.connection_paths import LocationError, approved
from spend_app.db import connect, initialize


def test_traycer_reads_the_app_store_outside_its_approved_location(tmp_path) -> None:
    # Regression: the native-session check opened BURNRATE's own database
    # through the provider-file confinement, so every Traycer pass failed with
    # "A source link points outside the approved location".
    database = tmp_path / "burnrate" / "spend.db"
    database.parent.mkdir()
    initialize(database)
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO usage_events(source,tool_key,model_key,occurred_at,session_id,input_tokens,output_tokens,"
            "computed_cost_usd,raw_id,ingested_at) VALUES('claude_local','claude-code','claude-opus-5',"
            "'2026-10-01T00:00:00Z','session-1',1,1,0,'raw-1','2026-10-01T00:00:00Z')"
        )
    root = tmp_path / "traycer"
    root.mkdir()
    with approved(str(root)):
        assert covered_sessions(database) == {("claude-code", "session-1")}
        # Provider files stay confined to the approved location.
        with pytest.raises(LocationError):
            sqlite_read_only(database)
