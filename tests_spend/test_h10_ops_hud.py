"""Unit and headless-browser tests for H10 Desktop Ops & HUD features."""

from __future__ import annotations

import base64
import json
import math
import struct
import zlib
from datetime import UTC, datetime, timedelta

import pytest

from spend_app import wealthfront_window as host_module
from spend_app.native_desktop import DemoClient, DesktopError
from spend_app.wealthfront_window import DesktopBridge, _paeth_predictor, _png_to_cf_dib
from tests_spend.browser_runtime import CHROME
from tests_spend.test_wealthfront_ui import (
    FAST,
    build_page,
    demo_report,
    probe,
)

needs_chrome = pytest.mark.skipif(
    not CHROME.exists(), reason="Chromium/Edge binary is needed for H10 UI tests"
)
browser = pytest.mark.browser


# ---------------------------------------------------------------------------
# Helpers for building synthetic PNGs & fake clients
# ---------------------------------------------------------------------------


def _png_chunk(ctype: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(ctype + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", crc)


def _encode_png(
    width: int,
    height: int,
    rows_pixels: list[bytes],
    *,
    color_type: int,
    row_filters: list[int],
    bit_depth: int = 8,
    interlace: int = 0,
    compression: int = 0,
    png_filter_method: int = 0,
) -> bytes:
    bpp = 4 if color_type == 6 else 3
    stride = width * bpp
    assert len(rows_pixels) == height
    assert len(row_filters) == height

    raw_scanlines = bytearray()
    prev = bytes(stride)
    for y in range(height):
        cur = rows_pixels[y]
        assert len(cur) == stride
        ftype = row_filters[y]
        raw_scanlines.append(ftype)
        if ftype == 0:
            raw_scanlines.extend(cur)
        elif ftype == 1:
            for i in range(stride):
                left = cur[i - bpp] if i >= bpp else 0
                raw_scanlines.append((cur[i] - left) & 0xFF)
        elif ftype == 2:
            for i in range(stride):
                raw_scanlines.append((cur[i] - prev[i]) & 0xFF)
        elif ftype == 3:
            for i in range(stride):
                left = cur[i - bpp] if i >= bpp else 0
                raw_scanlines.append((cur[i] - ((left + prev[i]) >> 1)) & 0xFF)
        elif ftype == 4:
            for i in range(stride):
                left = cur[i - bpp] if i >= bpp else 0
                up = prev[i]
                up_left = prev[i - bpp] if i >= bpp else 0
                raw_scanlines.append((cur[i] - _paeth_predictor(left, up, up_left)) & 0xFF)
        else:
            raw_scanlines.extend(cur)
        prev = cur

    ihdr = struct.pack(
        ">IIBBBBB",
        width,
        height,
        bit_depth,
        color_type,
        compression,
        png_filter_method,
        interlace,
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw_scanlines)))
        + _png_chunk(b"IEND", b"")
    )


def _sample_rows(width: int, height: int, bpp: int) -> list[bytes]:
    rows: list[bytes] = []
    for y in range(height):
        row = bytearray()
        for x in range(width):
            r = (x * 53 + y * 97 + 17) & 0xFF
            g = (x * 29 + y * 61 + 83) & 0xFF
            b = (x * 113 + y * 19 + 201) & 0xFF
            if bpp == 4:
                a = (x * 41 + y * 73 + 120) & 0xFF
                row.extend((r, g, b, a))
            else:
                row.extend((r, g, b))
        rows.append(bytes(row))
    return rows


def _expected_bottom_up_bgra(
    width: int, height: int, rows_pixels: list[bytes], bpp: int
) -> bytes:
    out = bytearray()
    for y in range(height - 1, -1, -1):
        row = rows_pixels[y]
        for x in range(width):
            off = x * bpp
            r, g, b = row[off], row[off + 1], row[off + 2]
            a = row[off + 3] if bpp == 4 else 255
            out.extend((b, g, r, a))
    return bytes(out)


class FakeClient:
    def __init__(self, *, ensure_error: Exception | None = None):
        self.ensure_error = ensure_error
        self.ensure_calls = 0
        self.requests: list[tuple[str, dict | None]] = []
        self.snapshots: dict[tuple[str, str], dict] = {}
        self.routes: dict[str, object] = {}

    def ensure_service(self) -> None:
        self.ensure_calls += 1
        if self.ensure_error is not None:
            raise self.ensure_error

    def snapshot(self, period: str, machine: str) -> dict:
        key = (period, machine)
        if key in self.snapshots:
            val = self.snapshots[key]
            if isinstance(val, Exception):
                raise val
            return json.loads(json.dumps(val))
        return {
            "period": period,
            "scope": "all",
            "total": {"valueUsd": 10.0, "tokens": 1000, "events": 2, "complete": True},
            "tools": [],
            "machines": [],
            "series": [],
        }

    def request(self, path: str, body: dict | None = None) -> dict:
        self.requests.append((path, body))
        handler = self.routes.get(path)
        if handler is None:
            for prefix, candidate in self.routes.items():
                if path.startswith(prefix):
                    handler = candidate
                    break
        if callable(handler):
            return handler(path, body)
        if isinstance(handler, Exception):
            raise handler
        if handler is not None:
            return json.loads(json.dumps(handler))
        raise DesktopError(f"Unexpected path: {path}")


# ---------------------------------------------------------------------------
# 1. Python bridge & helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("color_type", [6, 2], ids=["rgba", "rgb"])
@pytest.mark.parametrize("filter_type", [0, 1, 2, 3, 4])
def test_png_to_cf_dib_decodes_rgba_and_rgb_across_all_filters(color_type, filter_type):
    width, height = 4, 5
    bpp = 4 if color_type == 6 else 3
    rows = _sample_rows(width, height, bpp)
    png = _encode_png(
        width,
        height,
        rows,
        color_type=color_type,
        row_filters=[filter_type] * height,
    )

    dib = _png_to_cf_dib(png)
    assert len(dib) == 40 + width * height * 4

    (
        bi_size,
        bi_width,
        bi_height,
        bi_planes,
        bi_bit_count,
        bi_compression,
        bi_size_image,
        bi_xppm,
        bi_yppm,
        bi_clr_used,
        bi_clr_imp,
    ) = struct.unpack("<IiiHHIIiiII", dib[:40])

    assert bi_size == 40
    assert bi_width == width
    assert bi_height == height and bi_height > 0
    assert bi_planes == 1
    assert bi_bit_count == 32
    assert bi_compression == 0
    assert bi_size_image == width * height * 4
    assert (bi_xppm, bi_yppm, bi_clr_used, bi_clr_imp) == (0, 0, 0, 0)
    assert dib[40:] == _expected_bottom_up_bgra(width, height, rows, bpp)


def test_png_to_cf_dib_decodes_mixed_row_filters():
    width, height = 4, 5
    rows = _sample_rows(width, height, 4)
    png = _encode_png(
        width,
        height,
        rows,
        color_type=6,
        row_filters=[0, 1, 2, 3, 4],
    )
    dib = _png_to_cf_dib(png)
    assert dib[40:] == _expected_bottom_up_bgra(width, height, rows, 4)


def test_png_to_cf_dib_rejects_invalid_and_unsupported_inputs():
    with pytest.raises(ValueError, match="Invalid PNG signature"):
        _png_to_cf_dib(b"")
    with pytest.raises(ValueError, match="Invalid PNG signature"):
        _png_to_cf_dib(b"GIF89a" + b"\x00" * 30)

    # Truncated chunk (advertises 100 bytes, ends early)
    truncated = b"\x89PNG\r\n\x1a\n" + struct.pack(">I4s", 100, b"IHDR") + b"\x00" * 10
    with pytest.raises(ValueError, match="Truncated PNG chunk"):
        _png_to_cf_dib(truncated)

    # Invalid IHDR length
    bad_ihdr = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", b"\x00" * 12)
        + _png_chunk(b"IEND", b"")
    )
    with pytest.raises(ValueError, match="Invalid IHDR chunk"):
        _png_to_cf_dib(bad_ihdr)

    # Unsupported color type (indexed = 3), 16-bit depth, zero width, interlaced
    for bad_fields in (
        (2, 2, 8, 3, 0, 0, 0),
        (2, 2, 16, 6, 0, 0, 0),
        (0, 2, 8, 6, 0, 0, 0),
    ):
        png = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", *bad_fields))
            + _png_chunk(b"IEND", b"")
        )
        with pytest.raises(ValueError, match="Unsupported PNG format"):
            _png_to_cf_dib(png)

    interlaced = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 1))
        + _png_chunk(b"IEND", b"")
    )
    with pytest.raises(ValueError, match="Unsupported PNG compression or interlacing"):
        _png_to_cf_dib(interlaced)

    # Missing IDAT / missing IHDR
    no_idat = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0))
        + _png_chunk(b"IEND", b"")
    )
    with pytest.raises(ValueError, match="Missing PNG image data"):
        _png_to_cf_dib(no_idat)

    no_ihdr = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IDAT", b"abc") + _png_chunk(b"IEND", b"")
    with pytest.raises(ValueError, match="Missing PNG image data"):
        _png_to_cf_dib(no_ihdr)

    # Corrupt scanline length
    bad_len = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(b"\x00\x01\x02"))
        + _png_chunk(b"IEND", b"")
    )
    with pytest.raises(ValueError, match="Corrupt PNG scanline length"):
        _png_to_cf_dib(bad_len)

    # Unsupported filter type 5
    bad_filter = _encode_png(
        2,
        1,
        [b"\x01\x02\x03\x04\x05\x06\x07\x08"],
        color_type=6,
        row_filters=[5],
    )
    with pytest.raises(ValueError, match="Unsupported PNG filter type 5"):
        _png_to_cf_dib(bad_filter)


def test_copy_image_to_clipboard_validation_and_dispatch(tmp_path, monkeypatch):
    bridge = DesktopBridge(DemoClient(), preferences=tmp_path / "p.json", demo=True)
    clip_calls: list[tuple[bytes, bytes]] = []
    dib_calls: list[bytes] = []

    real_png_to_dib = host_module._png_to_cf_dib

    def spy_dib(png_bytes: bytes) -> bytes:
        dib_calls.append(png_bytes)
        return real_png_to_dib(png_bytes)

    monkeypatch.setattr(host_module, "_png_to_cf_dib", spy_dib)
    monkeypatch.setattr(
        host_module,
        "_write_win32_clipboard",
        lambda p, d: clip_calls.append((p, d)),
    )

    for bad in (
        None,
        123,
        "",
        "data:image/jpeg;base64,AAAA",
        "https://example.com/card.png",
    ):
        res = bridge.copy_image_to_clipboard(bad)  # type: ignore[arg-type]
        assert res["ok"] is False and "PNG" in res["error"]

    assert bridge.copy_image_to_clipboard("data:image/png;base64,")["ok"] is False
    assert (
        bridge.copy_image_to_clipboard("data:image/png;base64," + "A" * 16_000_001)[
            "ok"
        ]
        is False
    )

    invalid_b64 = bridge.copy_image_to_clipboard("data:image/png;base64,!!!bad-b64!!!")
    assert invalid_b64["ok"] is False and invalid_b64["error"]

    not_png = bridge.copy_image_to_clipboard(
        "data:image/png;base64," + base64.b64encode(b"not-a-png-at-all").decode("ascii")
    )
    assert not_png == {"ok": False, "error": "Invalid PNG signature."}
    assert clip_calls == []
    dib_calls.clear()

    rows = _sample_rows(2, 2, 4)
    png_bytes = _encode_png(2, 2, rows, color_type=6, row_filters=[0, 1])
    data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

    ok = bridge.copy_image_to_clipboard(data_url)
    assert ok == {"ok": True}
    assert dib_calls == [png_bytes]
    assert len(clip_calls) == 1
    assert clip_calls[0][0] == png_bytes
    assert len(clip_calls[0][1]) == 40 + 2 * 2 * 4

    def fail_clip(_p: bytes, _d: bytes) -> None:
        raise OSError("clipboard locked")

    monkeypatch.setattr(host_module, "_write_win32_clipboard", fail_clip)
    err = bridge.copy_image_to_clipboard(data_url)
    assert err == {"ok": False, "error": "clipboard locked"}


def test_fetch_usage_attaches_activity_and_today_total(tmp_path):
    # Demo mode fallback
    demo_bridge = DesktopBridge(DemoClient(), preferences=tmp_path / "d.json", demo=True)
    demo_week = demo_bridge.fetch_usage(period="week")
    assert demo_week["ok"] is True
    assert len(demo_week["report"]["activity"]) == 3
    assert all(row["state"] == "LIVE" for row in demo_week["report"]["activity"])
    assert demo_week["report"]["todayTotal"] == DemoClient().snapshot("today", "all")["total"]

    # Mocked live client
    client = FakeClient()
    week_total = {"valueUsd": 145.20, "tokens": 900_000, "events": 40, "complete": True}
    today_total = {"valueUsd": 18.75, "tokens": 52_000, "events": 6, "complete": False}
    client.snapshots[("week", "all")] = {
        "period": "week",
        "scope": "all",
        "total": week_total,
        "tools": [],
        "machines": [],
        "series": [],
    }
    client.snapshots[("today", "all")] = {
        "period": "today",
        "scope": "all",
        "total": today_total,
        "tools": [],
        "machines": [],
        "series": [],
    }
    live_rows = [
        {
            "id": "sess-1",
            "name": "Fix HUD tests",
            "state": "running",
            "modelKey": "claude-sonnet-4",
            "machineName": "Workstation",
            "startedAt": "2026-11-16T10:00:00Z",
        }
    ]
    client.routes["/api/spend/hud?"] = {"capacity": [], "activity": live_rows}
    client.routes["/api/spend/hud/usage?"] = {"windowTokens": {}}
    client.routes["/api/connections"] = {"connections": []}

    bridge = DesktopBridge(client, preferences=tmp_path / "l.json", demo=False)
    res = bridge.fetch_usage(period="week", machine="all")
    assert res["ok"] is True
    assert res["report"]["activity"] == live_rows
    assert res["report"]["total"] == week_total
    assert res["report"]["todayTotal"] == today_total

    # Missing activity in hud response falls back to [] in non-demo mode
    client.routes["/api/spend/hud?"] = {"capacity": []}
    res_empty = bridge.fetch_usage(period="today", machine="all")
    assert res_empty["ok"] is True
    assert res_empty["report"]["activity"] == []
    assert res_empty["report"]["todayTotal"] == today_total


def test_hud_mini_and_density_persistence_and_initial_state(tmp_path):
    prefs = tmp_path / "native-window.json"
    prefs.write_text(json.dumps({"period": "week", "customFlag": 42}), encoding="utf-8")

    bridge = DesktopBridge(DemoClient(), preferences=prefs, demo=True)
    init = bridge.get_initial_state()
    assert init["hudMini"] is False
    assert init["hudDensity"] == "compact"

    assert bridge.set_hud_mini(True) == {"ok": True, "hudMini": True}
    assert bridge.set_hud_density("comfortable") == {
        "ok": True,
        "hudDensity": "comfortable",
    }
    assert bridge.set_hud_density("  COMFORTABLE ") == {
        "ok": True,
        "hudDensity": "comfortable",
    }
    assert bridge.set_hud_density("spacious") == {"ok": True, "hudDensity": "compact"}
    assert bridge.set_hud_density("comfortable") == {
        "ok": True,
        "hudDensity": "comfortable",
    }

    stored = json.loads(prefs.read_text(encoding="utf-8"))
    assert stored["hudMini"] is True
    assert stored["hudDensity"] == "comfortable"
    assert stored["customFlag"] == 42

    reloaded = DesktopBridge(DemoClient(), preferences=prefs, demo=True)
    reloaded_init = reloaded.get_initial_state()
    assert reloaded_init["hudMini"] is True
    assert reloaded_init["hudDensity"] == "comfortable"

    # Unknown density on disk normalizes to "compact"
    prefs.write_text(json.dumps({"hudMini": 1, "hudDensity": "ultra"}), encoding="utf-8")
    normalized = DesktopBridge(DemoClient(), preferences=prefs, demo=True).get_initial_state()
    assert normalized["hudMini"] is True
    assert normalized["hudDensity"] == "compact"


def test_get_diagnostics_data_demo_and_mocked_client(tmp_path):
    demo_res = DesktopBridge(DemoClient(), demo=True).get_diagnostics_data()
    assert demo_res["ok"] is True
    assert demo_res["freshness"]["status"] == "ok"
    assert len(demo_res["health"]["ingest"]) >= 2

    client = FakeClient()
    health_payload = {"ingest": [{"source": "claude_local", "status": "success"}], "quotas": []}
    fresh_payload = {"status": "warn", "machines": [], "issues": []}
    client.routes["/api/spend/health"] = health_payload
    client.routes["/api/spend/freshness"] = fresh_payload

    bridge = DesktopBridge(client, preferences=tmp_path / "p.json")
    ok = bridge.get_diagnostics_data()
    assert ok == {"ok": True, "health": health_payload, "freshness": fresh_payload}

    # Partial failure still returns ok: True
    client.routes["/api/spend/health"] = DesktopError("health down")
    partial = bridge.get_diagnostics_data()
    assert partial == {"ok": True, "health": None, "freshness": fresh_payload}

    # Both unavailable returns ok: False
    client.routes["/api/spend/freshness"] = DesktopError("fresh down")
    both_down = bridge.get_diagnostics_data()
    assert both_down == {"ok": False, "error": "Diagnostics unavailable."}

    # ensure_service DesktopError and OSError handling
    failing_svc = DesktopBridge(FakeClient(ensure_error=DesktopError("tracker stopped")))
    assert failing_svc.get_diagnostics_data() == {"ok": False, "error": "tracker stopped"}
    os_err_svc = DesktopBridge(FakeClient(ensure_error=OSError("boom")))
    assert os_err_svc.get_diagnostics_data() == {
        "ok": False,
        "error": "Could not load diagnostics.",
    }


def test_get_week_share_data_demo_and_mocked_client():
    demo_res = DesktopBridge(DemoClient(), demo=True).get_week_share_data()
    assert demo_res["ok"] is True
    assert demo_res["week"]["days"] == 7
    assert demo_res["week"]["usd"] > 0

    client = FakeClient()
    week_payload = {"usd": 88.5, "tokens": 420000, "sessions": 12, "tools": []}
    client.routes["/api/spend/share/week?"] = week_payload
    bridge = DesktopBridge(client)
    res = bridge.get_week_share_data("desk 1")
    assert res == {"ok": True, "week": week_payload}
    assert client.requests[-1][0] == "/api/spend/share/week?machine=desk+1"

    client.routes["/api/spend/share/week?"] = DesktopError("hub error")
    assert bridge.get_week_share_data() == {"ok": False, "error": "hub error"}

    client.routes["/api/spend/share/week?"] = ValueError("bad json")
    assert bridge.get_week_share_data() == {
        "ok": False,
        "error": "Could not build weekly share card.",
    }


def test_get_setup_check_demo_and_mocked_client():
    demo_res = DesktopBridge(DemoClient(), demo=True).get_setup_check()
    assert demo_res == {"ok": True, "check": {"plans": {"active": [], "suggestions": []}}}

    client = FakeClient()
    check_payload = {
        "plans": {
            "active": [],
            "suggestions": [
                {
                    "lane": "claude-code",
                    "tool_key": "claude-code",
                    "name": "Claude Pro",
                    "amount_usd": 20,
                    "cadence": "monthly",
                }
            ],
        }
    }
    client.routes["/api/setup/check"] = check_payload
    bridge = DesktopBridge(client)
    assert bridge.get_setup_check() == {"ok": True, "check": check_payload}

    client.routes["/api/setup/check"] = DesktopError("check failed")
    assert bridge.get_setup_check() == {"ok": False, "error": "Setup check unavailable."}

    svc_err = DesktopBridge(FakeClient(ensure_error=DesktopError("offline")))
    assert svc_err.get_setup_check() == {"ok": False, "error": "offline"}


def test_confirm_suggested_plan_demo_validation_and_submission():
    demo_bridge = DesktopBridge(DemoClient(), demo=True)
    assert demo_bridge.confirm_suggested_plan(
        {"name": "Claude Pro", "tool_key": "claude-code", "amount_usd": 20}
    ) == {"ok": False, "error": "Plan changes are disabled in demo mode."}

    client = FakeClient()
    client.routes["/api/subscriptions"] = lambda _p, body: {"saved": True, "id": "sub-1"}
    bridge = DesktopBridge(client)

    for invalid in (
        None,
        "not-a-dict",
        {"name": "", "tool_key": "claude-code", "amount_usd": 20},
        {"name": "Claude Pro", "tool_key": "", "amount_usd": 20},
        {"name": "Claude Pro", "tool_key": "claude-code", "amount_usd": 0},
        {"name": "Claude Pro", "tool_key": "claude-code", "amount_usd": -10},
        {"name": "Claude Pro", "tool_key": "claude-code", "amount_usd": "abc"},
        {"name": "Claude Pro", "tool_key": "claude-code", "amount_usd": float("nan")},
        {"name": "Claude Pro", "tool_key": "claude-code", "amount_usd": math.inf},
    ):
        err = bridge.confirm_suggested_plan(invalid)  # type: ignore[arg-type]
        assert err["ok"] is False and err["error"]
    assert client.requests == []

    res = bridge.confirm_suggested_plan(
        {
            "name": "Claude Max",
            "tool_key": "claude-code",
            "amount_usd": 100,
            "cadence": "monthly",
        }
    )
    assert res == {"ok": True, "result": {"saved": True, "id": "sub-1"}}
    assert len(client.requests) == 1
    path, body = client.requests[0]
    assert path == "/api/subscriptions"
    assert body is not None
    assert body["operation"] == "add"
    assert body["request_id"].startswith("desktop-hud-plan-")
    now_utc = datetime.now(UTC)
    assert body["values"] == {
        "name": "Claude Max",
        "tool_key": "claude-code",
        "amount_usd": "100.00",
        "cadence": "monthly",
        "start_date": f"{now_utc.year:04d}-{now_utc.month:02d}-01",
        "end_date": None,
    }

    client.routes["/api/subscriptions"] = DesktopError("Plan already exists", status_code=409)
    assert bridge.confirm_suggested_plan(
        {"name": "Claude Max", "tool_key": "claude-code", "amount_usd": 100}
    ) == {"ok": False, "error": "Plan already exists"}

    client.routes["/api/subscriptions"] = OSError("network")
    assert bridge.confirm_suggested_plan(
        {"name": "Claude Max", "tool_key": "claude-code", "amount_usd": 100}
    ) == {"ok": False, "error": "Could not save plan."}


# ---------------------------------------------------------------------------
# 2. Browser UI tests
# ---------------------------------------------------------------------------


def _iso_ago(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def _iso_ahead(minutes: int) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


@needs_chrome
@browser
def test_browser_running_now_overview_mini_and_xss(tmp_path):
    report = demo_report()
    report["activity"] = [
        {
            "id": "live-1",
            "name": '<img src=x onerror="window.__xss=1">Refactor bridge <b>bold</b>',
            "state": "LIVE",
            "modelKey": "<b>claude-opus-4-1</b>",
            "machineName": "<svg onload='window.__xss=3'>DeskPC",
            "startedAt": _iso_ago(60),
        },
        {
            "id": "live-2",
            "name": "Verify quota polling",
            "state": "running",
            "modelKey": "gpt-5-codex",
            "machineName": "Laptop",
            "startedAt": _iso_ago(30),
        },
        {
            "id": "live-3",
            "name": "Audit pricing table",
            "state": "LIVE",
            "modelKey": "gemini-2.5-pro",
            "machineName": "MacBook",
            "startedAt": _iso_ago(15),
        },
        {
            "id": "live-4",
            "name": "Fourth live session",
            "state": "running",
            "modelKey": "deepseek-chat",
            "machineName": "Box4",
            "startedAt": _iso_ago(6),
        },
        {
            "id": "live-5",
            "name": "Fifth live session",
            "state": "LIVE",
            "modelKey": "grok-3",
            "machineName": "Box5",
            "startedAt": _iso_ago(1),
        },
        {
            "id": "stopped-1",
            "name": "Stopped task should not render",
            "state": "stopped",
            "modelKey": "claude-sonnet-4",
            "machineName": "DeskPC",
            "startedAt": _iso_ago(90),
        },
        {
            "id": "idle-1",
            "name": "Idle task should not render",
            "state": "idle",
            "modelKey": "gpt-5",
            "machineName": "DeskPC",
            "startedAt": _iso_ago(10),
        },
    ]

    probe_js = """
    await until(() => document.querySelectorAll('#runningNowList .running-row').length === 5);
    const card = document.getElementById('runningNowCard');
    const list = document.getElementById('runningNowList');
    const rows = [...list.querySelectorAll('.running-row')];
    const fills = rows.map(r => r.querySelector('.running-fill').style.width);
    const firstTitle = rows[0].querySelector('.running-title').textContent;
    const firstMeta = rows[0].querySelector('.running-meta').textContent;
    const ages = rows.map(r => r.querySelector('.running-age').textContent);
    const out = {
      cardHidden: card.hidden,
      countText: document.getElementById('runningNowCount').textContent,
      hintOverview: document.getElementById('runningNowHint').textContent,
      overviewRows: rows.length,
      miniLiveText: document.getElementById('hudMiniLive').textContent,
      miniLiveHidden: document.getElementById('hudMiniLive').hidden,
      fills,
      ages,
      excludedStopped: !list.textContent.includes('Stopped task') && !list.textContent.includes('Idle task'),
      xssFlag: window.__xss || null,
      injectedTags: list.querySelectorAll('img, script, b, svg[onload]').length,
      escapedTitlePresent: firstTitle.includes('<img') && firstTitle.includes('Refactor bridge'),
      escapedMetaPresent: firstMeta.includes('<b>claude-opus-4-1</b>') && firstMeta.includes('DeskPC'),
    };
    document.getElementById('miniModeBtn').click();
    await wait(30);
    out.miniRows = document.querySelectorAll('#runningNowList .running-row').length;
    out.hintMini = document.getElementById('runningNowHint').textContent;
    out.countMini = document.getElementById('runningNowCount').textContent;

    // Also verify <script> payload in name is safely escaped
    renderRunningNow({
      ...state.report,
      activity: [{ id: 's', name: '<scr' + 'ipt>window.__xss=9<\\/scr' + 'ipt>Safe', state: 'LIVE', modelKey: 'gpt-5', startedAt: new Date().toISOString() }]
    });
    out.scriptTagInjected = !!list.querySelector('script') || !!window.__xss;
    out.scriptTextEscaped = list.querySelector('.running-title').textContent.includes('Safe');

    // Empty/stopped-only activity hides the card
    renderRunningNow({ ...state.report, activity: [{ state: 'stopped', name: 'done' }] });
    out.hiddenWhenNone = document.getElementById('runningNowCard').hidden;
    out.miniLiveHiddenWhenNone = document.getElementById('hudMiniLive').hidden;
    report(out);
    """
    api_js = "api.set_hud_mini = async function(v) { return { ok: true, hudMini: v }; };"
    html = build_page(pref="light", report=report, api_js=api_js, probe=probe_js, extra_css=FAST)
    r = probe(html, 380, tmp_path, height=900)

    assert r["cardHidden"] is False
    assert r["countText"] == "5 live"
    assert r["hintOverview"] == ""
    assert r["overviewRows"] == 5
    assert r["miniLiveText"] == "5 live" and r["miniLiveHidden"] is False
    assert r["fills"][0] == "100%"
    assert r["fills"][1] in ("50%", "48%", "52%")
    assert r["fills"][2] in ("25%", "23%", "27%")
    assert all(int(f.rstrip("%")) >= 8 for f in r["fills"])
    assert r["ages"][0] in ("1h", "60m")
    assert r["excludedStopped"] is True
    assert r["xssFlag"] is None
    assert r["injectedTags"] == 0
    assert r["escapedTitlePresent"] is True
    assert r["escapedMetaPresent"] is True
    assert r["miniRows"] == 3
    assert r["hintMini"] == "top 3 of 5"
    assert r["countMini"] == "5 live"
    assert r["scriptTagInjected"] is False and r["scriptTextEscaped"] is True
    assert r["hiddenWhenNone"] is True
    assert r["miniLiveHiddenWhenNone"] is True


@needs_chrome
@browser
def test_browser_diagnostics_view_sections_navigation_and_xss(tmp_path):
    api_js = r"""
    window.__diagCalls = 0;
    api.get_diagnostics_data = async function () {
      window.__diagCalls++;
      if (window.__diagCalls > 1) {
        return {
          ok: true,
          freshness: { status: "warn", ingest: { lastPassSeconds: 0.9, lastPassAgeSeconds: 5 }, machines: [], issues: [] },
          health: { ingest: [], quotas: [], sources: [], pricingGaps: [], providerVsComputedVariancePct: null }
        };
      }
      return {
        ok: true,
        freshness: {
          status: "alarm",
          version: "0.5.9",
          hub: { version: "0.6.0", matches: false },
          ingest: { lastPassSeconds: 2.4, lastPassAgeSeconds: 18 },
          sync: { failing: false, ageSeconds: 120 },
          issues: [
            {
              short: "<img src=x onerror='window.__dxss=1'>Sync issue",
              state: "lagging",
              severity: "alarm",
              ageSeconds: 400,
              detail: "<script>window.__dxss=2<\/script>Delayed 7m"
            }
          ],
          machines: [
            { key: "<svg onload='window.__dxss=3'>Desktop", state: "online", ageSeconds: 15, lastSeenAt: "2026-11-16T12:00:00Z" },
            { key: "Laptop", state: "offline", ageSeconds: 7200, lastSeenAt: null }
          ]
        },
        health: {
          ingest: [
            { source: "claude_local", status: "success", eventsLast24h: 142, lastSuccess: "2026-11-16T12:00:00Z" },
            { source: "codex_local", status: "partial", eventsLast24h: 12, lastSuccess: "2026-11-16T11:55:00Z" },
            { source: "opencode_local", status: "skipped", eventsLast24h: 0, error: "Not configured" },
            { source: "openrouter<img src=x>", status: "error", eventsLast24h: 0, error: "<script>window.__dxss=4<\/script>401 Unauthorized" }
          ],
          quotas: [
            { providerKey: "claude-code", status: "ok", lastPoll: "12:00Z" },
            { providerKey: "codex", status: "error", reason: "Token expired" },
            { providerKey: "openrouter<b>", status: "stale", reason: "Cached <img src=x>" }
          ],
          sources: [
            {
              source: "claude_local",
              pricingCoverage: {
                models: [
                  { model: "covered-ok-model", records: 50, reasons: [] },
                  {
                    model: "mystery-model<img src=x>",
                    records: 19,
                    reasons: [
                      { code: "unknown_model_identity", message: "raw" },
                      { code: "missing_current_rate", message: "raw" },
                      { code: "missing_current_rate", message: "dup" }
                    ]
                  },
                  {
                    model: "legacy-rate-model",
                    records: 4,
                    reasons: [
                      { code: "missing_historical_rate" },
                      { code: "incomplete_telemetry" },
                      { code: "invalid_timestamp" },
                      { code: "custom_code", message: "Custom <script>window.__dxss=5<\/script>reason" }
                    ]
                  }
                ]
              }
            }
          ],
          pricingGaps: ["covered-ok-model", "gap-only-model"],
          providerVsComputedVariancePct: 3.74
        }
      };
    };
    """

    probe_js = """
    await until(() => document.querySelector('.provider-row'));
    const diagBtn = document.getElementById('diagBtn');
    const view = document.getElementById('diagnosticsView');
    const scrollBody = document.getElementById('scrollBody');
    const opsBar = document.getElementById('opsBar');
    scrollBody.style.scrollBehavior = 'auto';
    scrollBody.scrollTop = 140;
    await wait(20);
    const savedScrollBefore = scrollBody.scrollTop;
    diagBtn.click();
    await until(() => document.querySelectorAll('#diagIngestGrid .diag-ingest-card').length === 4);
    await wait(30);

    const pricingItems = [...document.querySelectorAll('#diagPricingList .diag-item')].map(el => ({
      title: el.querySelector('strong').textContent,
      detail: el.querySelector('small').textContent,
      tag: el.querySelector('.diag-tag').textContent
    }));
    const out = {
      savedScrollBefore,
      openState: {
        viewHidden: view.hidden,
        viewDisplay: getComputedStyle(view).display,
        scrollHidden: scrollBody.hidden,
        scrollDisplay: getComputedStyle(scrollBody).display,
        heroHeight: document.getElementById('heroSection').getBoundingClientRect().height,
        opsHidden: opsBar.hidden,
        opsDisplay: getComputedStyle(opsBar).display,
        focusId: document.activeElement.id,
        hasAlarm: diagBtn.classList.contains('has-alarm'),
      },
      freshnessNote: document.getElementById('diagFreshnessNote').textContent,
      freshnessCount: document.querySelectorAll('#diagFreshnessList .diag-item').length,
      freshnessTones: [...document.querySelectorAll('#diagFreshnessList .diag-tag')].map(e => e.className),
      ingestCount: document.querySelectorAll('#diagIngestGrid .diag-ingest-card').length,
      ingestTones: [...document.querySelectorAll('#diagIngestGrid .diag-ingest-status')].map(e => e.className),
      quotasCount: document.querySelectorAll('#diagQuotasList .diag-item').length,
      quotaTones: [...document.querySelectorAll('#diagQuotasList .diag-tag')].map(e => e.className),
      pricingCount: pricingItems.length,
      pricingDetailsOk:
        pricingItems[0].detail === 'Model identity unavailable · Current rate unavailable' &&
        pricingItems[0].tag === '19 records' &&
        pricingItems[1].detail.includes('Historical rate unavailable') &&
        pricingItems[1].detail.includes('Token details incomplete') &&
        pricingItems[1].detail.includes('Usage date unavailable') &&
        pricingItems[1].detail.includes('Custom') &&
        pricingItems[2].title === 'gap-only-model' &&
        pricingItems[2].detail === 'Pricing unavailable' &&
        pricingItems[2].tag === '—',
      coveredExcluded: !pricingItems.some(p => p.title === 'covered-ok-model'),
      varianceText: document.getElementById('diagVarianceNote').textContent,
      xssFlag: window.__dxss || null,
      injectedTags: view.querySelectorAll('img, script, b, svg[onload]').length,
    };

    // Refresh updates to second payload (variance null, status warn)
    document.getElementById('diagRefreshBtn').click();
    await until(() => window.__diagCalls === 2 && document.getElementById('diagVarianceNote').textContent === 'Variance unavailable.');
    out.afterRefresh = {
      variance: document.getElementById('diagVarianceNote').textContent,
      hasWarn: diagBtn.classList.contains('has-warn'),
      hasAlarm: diagBtn.classList.contains('has-alarm'),
    };

    // Return via Back button
    document.getElementById('diagnosticsBackBtn').click();
    await wait(50);
    out.afterBack = {
      viewHidden: view.hidden,
      scrollHidden: scrollBody.hidden,
      scrollDisplay: getComputedStyle(scrollBody).display,
      scrollTop: scrollBody.scrollTop,
      opsHidden: opsBar.hidden,
      opsDisplay: getComputedStyle(opsBar).display,
      focusId: document.activeElement.id,
    };

    // Scroll again, reopen and return via Escape
    scrollBody.scrollTop = 95;
    await wait(20);
    const savedScrollSecond = scrollBody.scrollTop;
    diagBtn.click();
    await until(() => !view.hidden);
    await wait(20);
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await wait(50);
    out.savedScrollSecond = savedScrollSecond;
    out.afterEsc = {
      viewHidden: view.hidden,
      scrollHidden: scrollBody.hidden,
      scrollDisplay: getComputedStyle(scrollBody).display,
      scrollTop: scrollBody.scrollTop,
      opsHidden: opsBar.hidden,
      opsDisplay: getComputedStyle(opsBar).display,
      focusId: document.activeElement.id,
    };
    report(out);
    """
    html = build_page(pref="light", api_js=api_js, probe=probe_js, extra_css=FAST)
    r = probe(html, 380, tmp_path, height=340)

    assert r["savedScrollBefore"] > 0
    assert r["openState"] == {
        "viewHidden": False,
        "viewDisplay": "grid",
        "scrollHidden": True,
        "scrollDisplay": "none",
        "heroHeight": 0,
        "opsHidden": True,
        "opsDisplay": "none",
        "focusId": "diagnosticsBackBtn",
        "hasAlarm": True,
    }
    assert "Last collection pass 2.4s" in r["freshnessNote"]
    assert "Hub sync 2m ago" in r["freshnessNote"]
    assert "Hub 0.6.0, this computer 0.5.9" in r["freshnessNote"]
    assert r["freshnessCount"] == 3
    assert "tone-danger" in r["freshnessTones"][0]
    assert "tone-mint" in r["freshnessTones"][1]
    assert "tone-warn" in r["freshnessTones"][2]

    assert r["ingestCount"] == 4
    assert "tone-mint" in r["ingestTones"][0]
    assert "tone-mint" in r["ingestTones"][1]
    assert "tone-warn" in r["ingestTones"][2]
    assert "tone-danger" in r["ingestTones"][3]

    assert r["quotasCount"] == 3
    assert "tone-mint" in r["quotaTones"][0]
    assert "tone-danger" in r["quotaTones"][1]
    assert "tone-warn" in r["quotaTones"][2]

    assert r["pricingCount"] == 3
    assert r["pricingDetailsOk"] is True
    assert r["coveredExcluded"] is True
    assert r["varianceText"] == "Provider-reported versus computed cost differs by 3.7%."
    assert r["xssFlag"] is None
    assert r["injectedTags"] == 0

    assert r["afterRefresh"] == {
        "variance": "Variance unavailable.",
        "hasWarn": True,
        "hasAlarm": False,
    }
    assert r["afterBack"] == {
        "viewHidden": True,
        "scrollHidden": False,
        "scrollDisplay": "flex",
        "scrollTop": r["savedScrollBefore"],
        "opsHidden": False,
        "opsDisplay": "flex",
        "focusId": "diagBtn",
    }
    assert r["savedScrollSecond"] > 0
    assert r["afterEsc"] == {
        "viewHidden": True,
        "scrollHidden": False,
        "scrollDisplay": "flex",
        "scrollTop": r["savedScrollSecond"],
        "opsHidden": False,
        "opsDisplay": "flex",
        "focusId": "diagBtn",
    }


@needs_chrome
@browser
def test_browser_hud_mini_bar_density_and_initial_state(tmp_path):
    report = demo_report("week")
    report["todayTotal"] = {"valueUsd": 42.5, "tokens": 50000, "events": 5, "complete": False}
    report["activity"] = [
        {"id": "a1", "name": "Run A", "state": "LIVE", "modelKey": "claude-opus-4-1", "startedAt": _iso_ago(4)},
        {"id": "a2", "name": "Run B", "state": "running", "modelKey": "gpt-5-codex", "startedAt": _iso_ago(2)},
    ]
    report["providers"] = [
        {
            "key": "claude-code",
            "name": "Claude Code",
            "badge": "Work",
            "plan": "Max",
            "inUse": True,
            "isPayg": False,
            "fundsRemainingUsd": None,
            "organization": False,
            "remaining": {"kind": "quota", "headline": "15% left", "sub": "5h", "barPct": 15.0, "tone": "warn", "resetsAt": _iso_ahead(125), "summary": "Max"},
            "burn": {"dayTokens": 1000, "sessionTokens": 500, "hasSession": True, "weekTokens": 4000, "weekBasis": "window"},
            "windows": [
                {"limitKey": "5h", "shortLabel": "5h", "label": "5h", "usedPct": 85.0, "remainingPct": 15.0, "unavailable": False, "resetsAt": _iso_ahead(125)}
            ],
            "tokens": 4000,
            "valueUsd": 25.0,
            "complete": True,
            "models": [],
        },
        {
            "key": "codex",
            "name": "Codex",
            "badge": "",
            "plan": "Pro",
            "inUse": False,
            "isPayg": False,
            "fundsRemainingUsd": None,
            "organization": False,
            "remaining": {"kind": "quota", "headline": "82% left", "sub": "7d", "barPct": 82.0, "tone": "accent", "resetsAt": _iso_ahead(500), "summary": "Pro"},
            "burn": {"dayTokens": 200, "sessionTokens": None, "hasSession": False, "weekTokens": 1000, "weekBasis": "window"},
            "windows": [
                {"limitKey": "weekly", "shortLabel": "7d", "label": "7d", "usedPct": 18.0, "remainingPct": 82.0, "unavailable": False, "resetsAt": _iso_ahead(500)}
            ],
            "tokens": 1000,
            "valueUsd": 5.0,
            "complete": True,
            "models": [],
        },
    ]

    api_js = """
    api.set_hud_mini = async function(v) { window.__calls.push(['set_hud_mini', v]); return { ok: true, hudMini: v }; };
    api.set_hud_density = async function(v) { window.__calls.push(['set_hud_density', v]); return { ok: true, hudDensity: v }; };
    """
    probe_js = """
    await until(() => document.querySelector('.provider-row'));
    const root = document.documentElement;
    const fullBtn = document.getElementById('fullModeBtn');
    const miniBtn = document.getElementById('miniModeBtn');
    const densBtn = document.getElementById('densityBtn');
    const miniBar = document.getElementById('hudMiniBar');
    const out = {
      bootMini: root.classList.contains('is-mini'),
      bootComfy: root.classList.contains('is-comfortable'),
      bootFullPressed: fullBtn.getAttribute('aria-pressed'),
      bootMiniPressed: miniBtn.getAttribute('aria-pressed'),
      bootFullLabel: fullBtn.textContent,
      bootMiniLabel: miniBtn.textContent,
      bootDensityLabel: densBtn.textContent,
      bootCalls: window.__calls.filter(c => c[0] === 'set_hud_mini' || c[0] === 'set_hud_density'),
      miniDisplayBefore: getComputedStyle(miniBar).display,
    };

    miniBtn.click();
    await wait(20);
    out.afterMiniClick = {
      isMini: root.classList.contains('is-mini'),
      fullPressed: fullBtn.getAttribute('aria-pressed'),
      miniPressed: miniBtn.getAttribute('aria-pressed'),
      fullLabel: fullBtn.textContent,
      miniLabel: miniBtn.textContent,
      miniDisplay: getComputedStyle(miniBar).display,
      heroDisplay: getComputedStyle(document.getElementById('heroSection')).display,
      todayText: document.getElementById('hudMiniToday').textContent,
      liveText: document.getElementById('hudMiniLive').textContent,
      leadText: document.getElementById('hudMiniLead').textContent,
      pctText: document.getElementById('hudMiniPct').textContent,
      pctClass: document.getElementById('hudMiniPct').className,
    };

    // Clicking mini bar expands back to overview
    miniBar.click();
    await wait(20);
    out.afterMiniBarExpand = {
      isMini: root.classList.contains('is-mini'),
      fullPressed: fullBtn.getAttribute('aria-pressed'),
      miniPressed: miniBtn.getAttribute('aria-pressed'),
    };

    // Clicking Full button also works explicitly
    miniBtn.click();
    await wait(20);
    fullBtn.click();
    await wait(20);
    out.afterFullBtn = {
      isMini: root.classList.contains('is-mini'),
      fullPressed: fullBtn.getAttribute('aria-pressed'),
      miniPressed: miniBtn.getAttribute('aria-pressed'),
    };

    // Density toggle
    densBtn.click();
    await wait(20);
    out.afterComfy = {
      isComfy: root.classList.contains('is-comfortable'),
      pressed: densBtn.getAttribute('aria-pressed'),
      label: densBtn.textContent,
    };
    densBtn.click();
    await wait(20);
    out.afterCompact = {
      isComfy: root.classList.contains('is-comfortable'),
      pressed: densBtn.getAttribute('aria-pressed'),
      label: densBtn.textContent,
    };
    out.calls = window.__calls.filter(c => c[0] === 'set_hud_mini' || c[0] === 'set_hud_density');
    report(out);
    """

    html = build_page(
        pref="light",
        report=report,
        init={"period": "week", "hudMini": False, "hudDensity": "compact"},
        api_js=api_js,
        probe=probe_js,
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=850)

    assert r["bootMini"] is False and r["bootComfy"] is False
    assert r["bootFullPressed"] == "true" and r["bootMiniPressed"] == "false"
    assert r["bootFullLabel"] == "Full" and r["bootMiniLabel"] == "Mini"
    assert r["bootDensityLabel"] == "Density: Compact"
    assert r["bootCalls"] == []
    assert r["miniDisplayBefore"] == "none"

    m = r["afterMiniClick"]
    assert m["isMini"] is True and m["fullPressed"] == "false" and m["miniPressed"] == "true"
    assert m["fullLabel"] == "Full" and m["miniLabel"] == "Mini"
    assert m["miniDisplay"] == "grid" and m["heroDisplay"] == "none"
    assert m["todayText"] == "≥$42.50"
    assert m["liveText"] == "2 live"
    assert m["leadText"].startswith("Claude Code · Work · 5h (")
    assert m["pctText"] == "15% left"
    assert "tone-warn" in m["pctClass"]

    assert r["afterMiniBarExpand"] == {"isMini": False, "fullPressed": "true", "miniPressed": "false"}
    assert r["afterFullBtn"] == {"isMini": False, "fullPressed": "true", "miniPressed": "false"}
    assert r["afterComfy"] == {"isComfy": True, "pressed": "true", "label": "Density: Comfortable"}
    assert r["afterCompact"] == {"isComfy": False, "pressed": "false", "label": "Density: Compact"}
    assert r["calls"] == [
        ["set_hud_mini", True],
        ["set_hud_density", "compact"],
        ["set_hud_mini", False],
        ["set_hud_density", "compact"],
        ["set_hud_mini", True],
        ["set_hud_density", "compact"],
        ["set_hud_mini", False],
        ["set_hud_density", "compact"],
        ["set_hud_mini", False],
        ["set_hud_density", "comfortable"],
        ["set_hud_mini", False],
        ["set_hud_density", "compact"],
    ]


@needs_chrome
@browser
def test_browser_hud_boots_saved_mini_and_comfortable_and_healthy_fallback(tmp_path):
    report = demo_report()
    for p in report["providers"]:
        p["windows"] = []
    probe_js = """
    await until(() => document.querySelector('.provider-row'));
    report({
      isMini: document.documentElement.classList.contains('is-mini'),
      isComfy: document.documentElement.classList.contains('is-comfortable'),
      lead: document.getElementById('hudMiniLead').textContent,
      pct: document.getElementById('hudMiniPct').textContent,
      pctTone: document.getElementById('hudMiniPct').classList.contains('tone-accent'),
    });
    """
    html = build_page(
        pref="dark",
        report=report,
        init={"hudMini": True, "hudDensity": "comfortable"},
        probe=probe_js,
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=700)
    assert r == {
        "isMini": True,
        "isComfy": True,
        "lead": "All subscription limits healthy",
        "pct": "—",
        "pctTone": True,
    }


@needs_chrome
@browser
def test_browser_share_week_draws_1200x630_png_and_copies_to_clipboard(tmp_path):
    api_js = """
    window.__shareArgs = [];
    window.__copiedUrls = [];
    window.__clipFail = false;
    api.get_week_share_data = async function(machine) {
      window.__shareArgs.push(machine);
      return {
        ok: true,
        week: {
          from: "2026-11-09T00:00:00Z",
          to: "2026-11-16T00:00:00Z",
          days: 7,
          usd: 127.34,
          complete: false,
          planUsd: 60.0,
          tokens: 8200000,
          sessions: 50,
          cacheReusePct: 68.0,
          tools: [
            { name: "Claude Code", usd: 52.10, sharePct: 40.9 },
            { name: "Codex", usd: 41.82, sharePct: 32.8 }
          ]
        }
      };
    };
    api.copy_image_to_clipboard = async function(dataUrl) {
      window.__copiedUrls.push(dataUrl);
      if (window.__clipFail) return { ok: false, error: "Clipboard busy" };
      return { ok: true };
    };
    """
    probe_js = """
    await until(() => document.querySelector('.provider-row'));
    const btn = document.getElementById('shareWeekBtn');
    btn.click();
    await until(() => window.__copiedUrls.length === 1);
    await wait(30);
    const okToast = document.getElementById('toast').textContent;
    const url = window.__copiedUrls[0];
    const b64 = url.split(',')[1];
    const bin = atob(b64.slice(0, 48));
    const width = (bin.charCodeAt(16) << 24) | (bin.charCodeAt(17) << 16) | (bin.charCodeAt(18) << 8) | bin.charCodeAt(19);
    const height = (bin.charCodeAt(20) << 24) | (bin.charCodeAt(21) << 16) | (bin.charCodeAt(22) << 8) | bin.charCodeAt(23);

    window.__clipFail = true;
    btn.click();
    await until(() => window.__copiedUrls.length === 2);
    await wait(30);
    const failToast = document.getElementById('toast').textContent;

    report({
      shareArgs: window.__shareArgs,
      prefixOk: url.startsWith('data:image/png;base64,'),
      width,
      height,
      payloadLen: b64.length,
      okToast,
      failToast,
      btnDisabled: btn.disabled,
    });
    """
    html = build_page(pref="light", api_js=api_js, probe=probe_js, extra_css=FAST)
    r = probe(html, 380, tmp_path, height=700)

    assert r["shareArgs"] == ["all", "all"]
    assert r["prefixOk"] is True
    assert (r["width"], r["height"]) == (1200, 630)
    assert r["payloadLen"] > 1000
    assert r["okToast"] == "This week's card copied to clipboard"
    assert r["failToast"] == "Clipboard busy"
    assert r["btnDisabled"] is False


@needs_chrome
@browser
def test_browser_quick_plan_banner_confirm_and_dismiss(tmp_path):
    api_js = """
    window.__confirmed = [];
    window.__usageCalls = 0;
    const baseFetch = api.fetch_usage;
    api.fetch_usage = async function() { window.__usageCalls++; return baseFetch(); };
    api.get_setup_check = async function() {
      return {
        ok: true,
        check: {
          plans: {
            active: [],
            suggestions: [
              {
                lane: "claude-code",
                tool_key: "claude-code",
                name: "Claude Max",
                amount_usd: 100,
                cadence: "monthly",
                basis: "Based on 200K tokens/day"
              },
              {
                lane: "codex",
                tool_key: "codex",
                name: "Codex Pro",
                amount_usd: 20,
                cadence: "monthly",
                basis: "Detected steady usage"
              }
            ]
          }
        }
      };
    };
    api.confirm_suggested_plan = async function(sug) {
      window.__confirmed.push(sug);
      return { ok: true, result: { saved: true } };
    };
    """
    probe_js = """
    await until(() => !document.getElementById('hudQuickPlan').hidden && document.getElementById('quickPlanDismissBtn'));
    const banner = document.getElementById('hudQuickPlan');
    const firstCopy = banner.textContent.replace(/\\s+/g, ' ').trim();

    // 1. Dismiss first suggestion -> stored in localStorage, shows second suggestion
    document.getElementById('quickPlanDismissBtn').click();
    await until(() => banner.textContent.includes('Codex Pro'));
    const dismissedAfterOne = JSON.parse(localStorage.getItem('burnrate:hud-dismissed-plans') || '[]');
    const secondCopy = banner.textContent.replace(/\\s+/g, ' ').trim();

    // 2. Confirm second suggestion -> calls confirm_suggested_plan, hides banner, refreshes usage
    const beforeUsage = window.__usageCalls;
    document.getElementById('quickPlanConfirmBtn').click();
    await until(() => banner.hidden && window.__confirmed.length === 1);
    await until(() => window.__usageCalls > beforeUsage);
    await wait(30);

    report({
      firstCopy,
      dismissedAfterOne,
      secondCopy,
      confirmed: window.__confirmed,
      bannerHidden: banner.hidden,
      toast: document.getElementById('toast').textContent,
      refreshed: window.__usageCalls > beforeUsage,
    });
    """
    html = build_page(pref="light", api_js=api_js, probe=probe_js, extra_css=FAST)
    r = probe(html, 380, tmp_path, height=800)

    assert "Claude Max" in r["firstCopy"] and "$100/monthly" in r["firstCopy"]
    assert "Based on 200K tokens/day" in r["firstCopy"]
    assert r["dismissedAfterOne"] == ["claude-code"]
    assert "Codex Pro" in r["secondCopy"] and "$20/monthly" in r["secondCopy"]
    assert len(r["confirmed"]) == 1 and r["confirmed"][0]["tool_key"] == "codex"
    assert r["bannerHidden"] is True
    assert r["toast"] == "Added Codex Pro"
    assert r["refreshed"] is True


@needs_chrome
@browser
def test_browser_quick_plan_skips_pre_dismissed_plans(tmp_path):
    api_js = """
    localStorage.setItem('burnrate:hud-dismissed-plans', JSON.stringify(['claude-code']));
    api.get_setup_check = async function() {
      return {
        ok: true,
        check: {
          plans: {
            active: [],
            suggestions: [
              { lane: "claude-code", tool_key: "claude-code", name: "Claude Max", amount_usd: 100, cadence: "monthly" }
            ]
          }
        }
      };
    };
    """
    probe_js = """
    await until(() => document.querySelector('.provider-row'));
    await wait(60);
    report({
      hidden: document.getElementById('hudQuickPlan').hidden,
      html: document.getElementById('hudQuickPlan').innerHTML,
    });
    """
    html = build_page(pref="light", api_js=api_js, probe=probe_js, extra_css=FAST)
    r = probe(html, 380, tmp_path, height=700)
    assert r == {"hidden": True, "html": ""}


@needs_chrome
@browser
def test_browser_responsive_no_horizontal_overflow_at_304_and_480(tmp_path):
    report = demo_report()
    report["activity"] = [
        {
            "id": "r1",
            "name": "VeryLongUnbreakableSessionIdentifierWithoutSpaces_1234567890_ABCDEFGHIJ",
            "state": "LIVE",
            "modelKey": "claude-opus-4-1-20260101-long-identifier",
            "machineName": "Workstation-Desktop-Primary",
            "startedAt": _iso_ago(20),
        },
        {
            "id": "r2",
            "name": "Second active session",
            "state": "running",
            "modelKey": "gpt-5-codex",
            "machineName": "Laptop",
            "startedAt": _iso_ago(8),
        },
    ]
    api_js = """
    api.set_hud_mini = async function(v) { return { ok: true, hudMini: v }; };
    api.get_setup_check = async function() {
      return {
        ok: true,
        check: {
          plans: {
            active: [],
            suggestions: [
              {
                lane: "claude-code",
                tool_key: "claude-code",
                name: "Claude Max Plan",
                amount_usd: 100,
                cadence: "monthly",
                basis: "Based on sustained daily coding usage across machines"
              }
            ]
          }
        }
      };
    };
    api.get_diagnostics_data = async function() {
      return {
        ok: true,
        freshness: {
          status: "warn",
          version: "0.5.9",
          hub: { version: "0.6.0", matches: false },
          ingest: { lastPassSeconds: 1.5, lastPassAgeSeconds: 10 },
          issues: [{ short: "LongIssueIdentifierWithoutAnySpacesToWrapProperly", state: "warn", severity: "warn", ageSeconds: 120, detail: "https://example.invalid/very/long/path/without/spaces" }],
          machines: [{ key: "Desktop", state: "online", ageSeconds: 10, lastSeenAt: "2026-11-16T12:00:00Z" }]
        },
        health: {
          ingest: [
            { source: "claude_local", status: "success", eventsLast24h: 100, lastSuccess: "2026-11-16T12:00:00Z" },
            { source: "codex_local", status: "success", eventsLast24h: 50, lastSuccess: "2026-11-16T12:00:00Z" }
          ],
          quotas: [{ providerKey: "claude-code", status: "ok", lastPoll: "12:00Z" }],
          sources: [{ source: "claude_local", pricingCoverage: { models: [{ model: "long-unpriced-model-identifier-0001", records: 5, reasons: [{ code: "missing_current_rate" }] }] } }],
          pricingGaps: [],
          providerVsComputedVariancePct: 2.1
        }
      };
    };
    """
    probe_js = """
    await until(() => document.querySelectorAll('#runningNowList .running-row').length === 2);
    await until(() => !document.getElementById('hudQuickPlan').hidden);

    function measure(tag) {
      const de = document.documentElement;
      const sb = document.getElementById('scrollBody');
      const dv = document.getElementById('diagnosticsView');
      const ops = document.getElementById('opsBar');
      return {
        tag,
        docOverflow: de.scrollWidth - de.clientWidth,
        opsOverflow: ops.hidden ? 0 : ops.scrollWidth - ops.clientWidth,
        scrollOverflow: sb.hidden ? 0 : sb.scrollWidth - sb.clientWidth,
        diagOverflow: dv.hidden ? 0 : dv.scrollWidth - dv.clientWidth,
      };
    }

    const checks = [];
    // 304px overview
    checks.push(measure('overview-304'));
    // 304px mini
    document.getElementById('miniModeBtn').click();
    await wait(30);
    checks.push(measure('mini-304'));
    // 304px diagnostics
    document.getElementById('diagBtn').click();
    await until(() => document.querySelectorAll('#diagIngestGrid .diag-ingest-card').length === 2);
    await wait(30);
    checks.push(measure('diag-304'));

    // Switch to 480px
    await setWidth(480);
    checks.push(measure('diag-480'));
    document.getElementById('diagnosticsBackBtn').click();
    await wait(30);
    checks.push(measure('mini-480'));
    document.getElementById('fullModeBtn').click();
    await wait(30);
    checks.push(measure('overview-480'));

    report({ checks });
    """
    html = build_page(pref="light", api_js=api_js, report=report, probe=probe_js, extra_css=FAST)
    r = probe(html, 304, tmp_path, height=850)

    assert len(r["checks"]) == 6
    for entry in r["checks"]:
        assert entry["docOverflow"] <= 1, entry
        assert entry["opsOverflow"] <= 1, entry
        assert entry["scrollOverflow"] <= 1, entry
        assert entry["diagOverflow"] <= 1, entry
