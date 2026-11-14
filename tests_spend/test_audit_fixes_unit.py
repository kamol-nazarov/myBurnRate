from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from spend_app import hud, platform_paths
from spend_app.api import create_app
from spend_app.config import Settings, load_settings
from spend_app.connection_api import access_allowed
from spend_app.db import connect, initialize, prune_quotas, prune_usage_changes
from spend_app.sync import register_machine


def _make_settings(tmp_path: Path) -> Settings:
    return replace(
        load_settings(),
        database_path=tmp_path / "burnrate.sqlite",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        logs_path=tmp_path / "logs",
    )


def test_windows_process_running_matches_stem_prefixes(monkeypatch) -> None:
    monkeypatch.setattr(platform_paths, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        platform_paths,
        "_windows_processes",
        lambda: [(4120, "language_server_windows_x64.exe"), (1100, "explorer.exe")],
    )
    assert platform_paths.process_running("language_server_windows") is True
    assert platform_paths.process_running("language_server_windows_x64") is True
    assert platform_paths.process_running("unrelated_daemon") is False


def test_csrf_and_dns_rebinding_guard_blocks_untrusted_mutations(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BURNRATE_DEMO", "1")
    settings = _make_settings(tmp_path)
    initialize(settings.database_path)
    app = create_app(settings=settings, enable_scheduler=False)

    with TestClient(app, base_url="http://127.0.0.1:17331") as client:
        evil_origin = client.post(
            "/api/spend/quotas/refresh",
            headers={"Origin": "https://evil.example", "X-BURNRATE-Request": "1"},
        )
        assert evil_origin.status_code == 403

        cross_site = client.post(
            "/api/spend/quotas/refresh",
            headers={"Sec-Fetch-Site": "cross-site", "X-BURNRATE-Request": "1"},
        )
        assert cross_site.status_code == 403

        rebinding = client.post(
            "/api/spend/quotas/refresh",
            headers={"Host": "attacker.invalid:17331", "X-BURNRATE-Request": "1"},
        )
        assert rebinding.status_code == 403

        trusted_req = client.post(
            "/api/spend/quotas/refresh",
            headers={
                "Origin": "http://127.0.0.1:17331",
                "X-BURNRATE-Request": "1",
            },
        )
        assert trusted_req.status_code == 200
        assert trusted_req.json()["ok"] is True


def test_spoke_hud_reads_require_owner_session_and_cookie_is_scoped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "hub-secret-token-123")
    settings = _make_settings(tmp_path)
    initialize(settings.database_path)
    reg = register_machine(settings.database_path, name="SpokeLaptop", os_name="win32")
    spoke_headers = {"Authorization": f"Bearer {reg['token']}"}

    app = create_app(settings=settings, enable_scheduler=False)

    with TestClient(app, base_url="http://127.0.0.1:17331") as client:
        for path in (
            "/api/spend/hud",
            "/api/spend/hud/usage",
            "/api/spend/share/week",
            "/api/spend/freshness",
        ):
            res = client.get(path, headers=spoke_headers)
            assert res.status_code == 403, f"{path} leaked with {res.status_code}"

        denied = client.get("/api/connections", headers=spoke_headers)
        assert denied.status_code == 403

        hud_res = client.get("/hud?token=hub-secret-token-123")
        assert hud_res.status_code == 200
        set_cookie = hud_res.headers.get("set-cookie", "")
        assert "burnrate_access=" in set_cookie
        assert "HttpOnly" in set_cookie

    fake_stream_req = SimpleNamespace(
        method="GET",
        url=SimpleNamespace(path="/api/spend/stream", scheme="http"),
        headers={"host": "127.0.0.1:17331"},
        cookies={"burnrate_access": "hub-secret-token-123"},
    )
    assert access_allowed(fake_stream_req, "hub-secret-token-123") is False  # Static cookies were retired.

    fake_untrusted_host = SimpleNamespace(
        method="GET",
        url=SimpleNamespace(path="/api/spend/stream", scheme="http"),
        headers={"host": "evil.example"},
        cookies={"burnrate_access": "hub-secret-token-123"},
    )
    assert access_allowed(fake_untrusted_host, "hub-secret-token-123") is False

    fake_post_with_cookie_only = SimpleNamespace(
        method="POST",
        url=SimpleNamespace(path="/api/connections", scheme="http"),
        headers={"host": "127.0.0.1:17331"},
        cookies={"burnrate_access": "hub-secret-token-123"},
    )
    assert access_allowed(fake_post_with_cookie_only, "hub-secret-token-123") is False


def test_sqlite_prune_quotas_and_usage_changes(tmp_path: Path) -> None:
    db_path = tmp_path / "prune.sqlite"
    initialize(db_path)
    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    now_iso = now.isoformat().replace("+00:00", "Z")
    old_ts = (now - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    mid_ts = (now - timedelta(days=20)).isoformat().replace("+00:00", "Z")
    fresh_ts = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")

    with connect(db_path) as conn:
        for ts, used in ((old_ts, 10.0), (mid_ts, 20.0), (fresh_ts, 30.0)):
            conn.execute(
                """
                INSERT INTO quotas (provider_key, limit_key, label, used, allowance, unit, pct, resets_at, source, polled_at)
                VALUES (?, ?, '5-hour', ?, 100, 'pct', ?, NULL, 'test', ?)
                """,
                ("codex", "5h", used, used, ts),
            )
        # Single historical row for an inactive provider must be kept as latest snapshot
        conn.execute(
            """
            INSERT INTO quotas (provider_key, limit_key, label, used, allowance, unit, pct, resets_at, source, polled_at)
            VALUES ('grok', 'monthly', 'Monthly', 5, 100, 'pct', 5, NULL, 'test', ?)
            """,
            (old_ts,),
        )
        deleted_quotas = prune_quotas(conn, now=now_iso, retention_days=14, keep_per_lane=1)
        assert deleted_quotas == 2
        remaining = conn.execute(
            "SELECT provider_key, polled_at FROM quotas ORDER BY provider_key, id"
        ).fetchall()
        assert [(r["provider_key"], r["polled_at"]) for r in remaining] == [
            ("codex", fresh_ts),
            ("grok", old_ts),
        ]

        # Seed usage_changes with distinct and duplicate raw_ids
        for idx in range(8):
            conn.execute("INSERT INTO usage_changes (raw_id) VALUES (?)", (f"ev-{idx}",))
        # Add duplicate updates for ev-6 and ev-7
        conn.execute("INSERT INTO usage_changes (raw_id) VALUES ('ev-6')")
        conn.execute("INSERT INTO usage_changes (raw_id) VALUES ('ev-7')")

        all_seqs = [r[0] for r in conn.execute("SELECT seq FROM usage_changes ORDER BY seq").fetchall()]
        acked_cutoff = all_seqs[4]  # first 5 entries acknowledged by Hub
        conn.execute(
            "INSERT OR REPLACE INTO app_meta (key, value) VALUES ('sync.cursor.v1:hub-a', ?)",
            (json.dumps({"change_seq": acked_cutoff}),),
        )
        deleted_changes = prune_usage_changes(conn)
        assert deleted_changes == 7  # 5 acked + 2 superseded duplicates for ev-6 and ev-7
        kept = conn.execute("SELECT seq, raw_id FROM usage_changes ORDER BY seq").fetchall()
        assert [r["raw_id"] for r in kept] == ["ev-5", "ev-6", "ev-7"]
        assert all(r["seq"] > acked_cutoff for r in kept)


def test_importing_api_module_is_lazy() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import spend_app.api as api_mod; assert api_mod._DEFAULT_APP is None",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_hud_markup_and_mtime_cache(tmp_path: Path, monkeypatch) -> None:
    fake_js = tmp_path / "hud.js"
    fake_js.write_text("console.log('v1');\n", encoding="utf-8")
    monkeypatch.setattr(hud, "_HUD_JS_PATH", fake_js)
    hud._HUD_CACHE = (None, "")

    html_v1 = hud.render_hud(dashboard=True)
    assert 'id="hud-more-btn"' in html_v1
    assert 'id="hud-more-menu"' in html_v1
    assert 'id="hud-quick-plan"' in html_v1
    assert "console.log('v1');" in html_v1

    fake_js.write_text("console.log('v2-updated');\n", encoding="utf-8")
    stat = fake_js.stat()
    os.utime(fake_js, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    html_v2 = hud.render_hud(dashboard=True)
    assert "console.log('v2-updated');" in html_v2
