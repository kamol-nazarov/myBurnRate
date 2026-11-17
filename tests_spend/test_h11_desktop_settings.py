"""Comprehensive fixture-backed test suite for H11: setup scan & settings in the desktop app.

Covers:
1. Python setup scan payload assembly & graceful degradation (``native_onboarding.py``)
   and 45s timeout tuple enforcement for H11 POST endpoints (``native_desktop.py``).
2. All 11 ``DesktopBridge`` H11 methods (``wealthfront_window.py``): happy paths,
   ``DesktopError`` / fallback error shaping, demo guards, consent validation,
   onboarding completion/deferral, notification preferences persistence, 502/HUB-501
   test notification guidance, and sync failure code mapping.
3. Headless Chrome UI tests (``wealthfront_ui.html``): Onboarding ``.ob`` scan & polling,
   Settings -> Sources / General / Computers cards, clipboard success & fallback
   ``<textarea id="diagReportText">``, XSS escaping, and >= 10.5px typography in
   both light and dark themes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import httpx
import pytest

from spend_app.error_codes import format_error
from spend_app.native_desktop import DesktopError, NativeClient
from spend_app.native_onboarding import build_setup_scan_data, demo_setup_scan_data
from spend_app.notify import KINDS
from spend_app.wealthfront_window import DesktopBridge
from tests_spend.test_api import make_settings
from tests_spend.test_wealthfront_ui import (
    FAST,
    browser,
    build_page,
    demo_report,
    empty_report,
    needs_chrome,
    probe,
)


# ---------------------------------------------------------------------------
# Helpers & fixtures
# ---------------------------------------------------------------------------


def _sample_check_payload() -> dict:
    return {
        "lanes": [
            {
                "key": "claude-code",
                "name": "Claude",
                "plan": "Claude Max 20x",
                "tool": "claude-code",
                "usage": {"state": "ok", "text": "Reading ~/.claude"},
                "meter": {
                    "state": "ok",
                    "text": "72% remaining (5h)",
                    "detail": None,
                    "fix": None,
                },
                "consent": None,
            },
            {
                "key": "codex",
                "name": "Codex",
                "plan": "ChatGPT Plus",
                "tool": "codex",
                "usage": {"state": "waiting", "text": "Found — no usage yet"},
                "meter": {
                    "state": "unavailable",
                    "text": "No current reading",
                    "detail": "No reading yet.",
                    "fix": "Finish a Codex turn. The meter comes from Codex's own session log.",
                },
                "consent": None,
            },
            {
                "key": "opencode",
                "name": "Z.AI",
                "plan": None,
                "tool": "opencode",
                "usage": {"state": "ok", "text": "Reading OpenCode DB"},
                "meter": {
                    "state": "held",
                    "text": "Held on previous reading",
                    "detail": "Updated 20m ago",
                    "fix": "Check Z.AI connection.",
                },
                "consent": "zai",
            },
            {
                "key": "opencode-go",
                "name": "OpenCode Go",
                "plan": None,
                "tool": "opencode-go",
                "usage": {"state": "ok", "text": "Reading OpenCode DB"},
                "meter": {
                    "state": "unavailable",
                    "text": "No current reading",
                    "detail": "Off until enabled below.",
                    "fix": "Sign in to OpenCode with your Console account.",
                },
                "consent": "opencode_go",
            },
        ],
        "usageOnly": [
            {
                "key": "cursor_local",
                "name": "Cursor",
                "tool": "cursor",
                "usage": {
                    "state": "ok",
                    "text": "Reading usage",
                    "path": "~/.cursor",
                },
            }
        ],
        "notFound": ["Antigravity", "Muse"],
        "consent": [
            {
                "key": "zai",
                "label": "Read Z.AI credits with your Coding Plan key",
                "detail": "Uses the Z.AI key stored by OpenCode.",
                "enabled": True,
                "lanes": ["opencode"],
            },
            {
                "key": "opencode_go",
                "label": "Read OpenCode Go usage with OpenCode's sign-in",
                "detail": "Uses the OpenCode Console key stored by OpenCode.",
                "enabled": False,
                "lanes": ["opencode-go"],
            },
        ],
        "checkedAt": "2026-11-16T12:00:00Z",
    }


def _sample_progress_payload() -> dict:
    return {
        "heartbeat": {"lastPassAt": "2026-11-16T12:00:00Z"},
        "sources": [
            {
                "source": "claude_local",
                "name": "Claude Code",
                "status": "success",
                "startedAt": "2026-11-16T11:59:58Z",
                "finishedAt": "2026-11-16T12:00:00Z",
                "events": 42,
            },
            {
                "source": "codex_local",
                "name": "Codex",
                "status": "success",
                "startedAt": "2026-11-16T11:59:58Z",
                "finishedAt": "2026-11-16T12:00:00Z",
                "events": 0,
            },
        ],
    }


def _sample_onboarding_sources_payload() -> dict:
    return {
        "sources": [
            {
                "source": "claude_local",
                "state": "healthy",
                "path": "~/.claude/projects",
                "nextAction": "No action needed for usage.",
                "lastAttempt": "2026-11-16T12:00:00Z",
                "lastSuccess": "2026-11-16T12:00:00Z",
                "reason": None,
            },
            {
                "source": "codex_local",
                "state": "detected_without_history",
                "path": "~/.codex/sessions",
                "nextAction": "Complete a supported turn and recheck.",
                "lastAttempt": "2026-11-16T11:58:00Z",
                "lastSuccess": None,
                "reason": "no_session_files_yet",
            },
        ]
    }


class _FakeBridgeClient:
    """Stub local HTTP client for ``build_setup_scan_data`` & ``DesktopBridge`` tests."""

    def __init__(self, routes: dict | None = None, *, ensure_error: BaseException | None = None):
        self.routes = dict(routes or {})
        self.ensure_error = ensure_error
        self.ensure_calls = 0
        self.calls: list[tuple[str, dict | None]] = []

    def ensure_service(self) -> None:
        self.ensure_calls += 1
        if self.ensure_error is not None:
            raise self.ensure_error

    def request(self, path: str, body: dict | None = None):
        self.calls.append((path, body))
        handler = self.routes.get(path)
        if isinstance(handler, BaseException):
            raise handler
        if callable(handler):
            return handler(body)
        return handler


class _Response:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.payload = payload if payload is not None else {}
        self.headers = {}
        self.cookies = {}
        self.is_success = 200 <= status < 300

    def json(self):
        return self.payload


class _TimeoutRecordingHttpClient:
    instances: ClassVar[list] = []

    def __init__(self, *args, **kwargs):
        self.options = kwargs
        self.calls = []
        self.__class__.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return _Response(payload={"ok": True})

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return _Response(payload={"ok": True})


# ---------------------------------------------------------------------------
# 1. Onboarding & Setup Scan Python Data (native_onboarding.py & native_desktop.py)
# ---------------------------------------------------------------------------


def test_demo_setup_scan_data_structure():
    data = demo_setup_scan_data()
    assert data["ok"] is True
    assert len(data["lanes"]) == 4
    assert data["lanes"][0]["key"] == "claude-code"
    assert data["lanes"][1]["meter"]["fix"]
    assert len(data["usageOnly"]) == 1
    assert data["notFound"] == ["Antigravity", "Muse"]
    assert [c["key"] for c in data["consent"]] == ["zai", "opencode_go"]
    assert data["consent"][0]["enabled"] is True
    assert data["consent"][1]["enabled"] is False
    assert data["progress"]["heartbeat"]["lastPassAt"] == "2026-11-16T12:00:00Z"
    assert set(data["sourceDetails"].keys()) == {"claude_local", "codex_local"}
    assert data["sourceDetails"]["codex_local"]["state"] == "detected_without_history"
    assert data["sourceDetails"]["codex_local"]["nextAction"]


def test_build_setup_scan_data_demo_skips_client():
    client = _FakeBridgeClient(
        {"/api/setup/check": AssertionError("demo mode must not call client")}
    )
    data = build_setup_scan_data(client, demo=True)
    assert data == demo_setup_scan_data()
    assert client.calls == []


def test_build_setup_scan_data_combines_check_progress_and_onboarding():
    check = _sample_check_payload()
    progress = _sample_progress_payload()
    onboarding = _sample_onboarding_sources_payload()
    onboarding["sources"].extend(["bad-entry", {"state": "healthy"}])  # ignored safely
    client = _FakeBridgeClient(
        {
            "/api/setup/check": check,
            "/api/setup/progress": progress,
            "/api/onboarding": onboarding,
        }
    )

    result = build_setup_scan_data(client, demo=False)

    assert result["ok"] is True
    assert result["lanes"] == check["lanes"]
    assert result["usageOnly"] == check["usageOnly"]
    assert result["notFound"] == check["notFound"]
    assert result["consent"] == check["consent"]
    assert result["checkedAt"] == check["checkedAt"]
    assert result["progress"] == progress

    claude_detail = result["sourceDetails"]["claude_local"]
    assert claude_detail["detected_without_history"] is False
    assert claude_detail["path"] == "~/.claude/projects"
    assert claude_detail["lastAttempt"] == "2026-11-16T12:00:00Z"
    assert claude_detail["lastSuccess"] == "2026-11-16T12:00:00Z"
    assert claude_detail["reason"] is None

    codex_detail = result["sourceDetails"]["codex_local"]
    assert codex_detail == {
        "source": "codex_local",
        "state": "detected_without_history",
        "path": "~/.codex/sessions",
        "nextAction": "Complete a supported turn and recheck.",
        "lastAttempt": "2026-11-16T11:58:00Z",
        "lastSuccess": None,
        "reason": "no_session_files_yet",
        "detected_without_history": True,
    }
    assert result["sources"] == [claude_detail, codex_detail]
    assert [path for path, _ in client.calls] == [
        "/api/setup/check",
        "/api/setup/progress",
        "/api/onboarding",
    ]


def test_build_setup_scan_data_degrades_when_setup_check_fails():
    coded_client = _FakeBridgeClient(
        {"/api/setup/check": DesktopError("Error HUB-501", status_code=502, code="HUB-501")}
    )
    res1 = build_setup_scan_data(coded_client, demo=False)
    assert res1 == {"ok": False, "error": "Error HUB-501", "code": "HUB-501"}
    assert len(coded_client.calls) == 1

    blank_err_client = _FakeBridgeClient({"/api/setup/check": RuntimeError("")})
    res2 = build_setup_scan_data(blank_err_client, demo=False)
    assert res2 == {"ok": False, "error": "Could not check this computer."}

    non_dict_client = _FakeBridgeClient({"/api/setup/check": ["not-a-dict"]})
    res3 = build_setup_scan_data(non_dict_client, demo=False)
    assert res3 == {"ok": False, "error": "Could not check this computer."}


def test_build_setup_scan_data_degrades_gracefully_when_progress_or_onboarding_fail():
    check = _sample_check_payload()
    client = _FakeBridgeClient(
        {
            "/api/setup/check": check,
            "/api/setup/progress": DesktopError("progress timeout", 503, code="APP-901"),
            "/api/onboarding": RuntimeError("onboarding unavailable"),
        }
    )
    result = build_setup_scan_data(client, demo=False)
    assert result["ok"] is True
    assert result["lanes"] == check["lanes"]
    assert result["progress"] == {"heartbeat": {}, "sources": []}
    assert result["sources"] == []
    assert result["sourceDetails"] == {}

    # Also handles non-dict payloads on progress/onboarding
    non_dict_client = _FakeBridgeClient(
        {
            "/api/setup/check": {"checkedAt": "2026-11-16T12:00:00Z"},
            "/api/setup/progress": ["invalid"],
            "/api/onboarding": "invalid",
        }
    )
    result2 = build_setup_scan_data(non_dict_client, demo=False)
    assert result2["ok"] is True
    assert result2["lanes"] == []
    assert result2["usageOnly"] == []
    assert result2["notFound"] == []
    assert result2["consent"] == []
    assert result2["progress"] == {"heartbeat": {}, "sources": []}
    assert result2["sources"] == []
    assert result2["sourceDetails"] == {}


@pytest.mark.parametrize(
    ("path", "body", "expected_read_timeout"),
    [
        ("/api/setup/scan", {}, 45),
        ("/api/setup/notifications/test", {}, 45),
        ("/api/setup/diagnostics", {}, 45),
        ("/api/sync/trigger", {}, 45),
        ("/api/setup/check", None, 20),
        ("/api/setup/progress", None, 20),
    ],
)
def test_native_client_request_applies_45s_timeout_for_h11_endpoints(
    tmp_path, monkeypatch, path, body, expected_read_timeout
):
    import spend_app.native_desktop as desktop

    _TimeoutRecordingHttpClient.instances.clear()
    monkeypatch.delenv("BURNRATE_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(desktop.httpx, "Client", _TimeoutRecordingHttpClient)

    client = NativeClient(make_settings(tmp_path / "timeout.db"), port=17441)
    res = client.request(path, body)
    assert res == {"ok": True}

    transport = _TimeoutRecordingHttpClient.instances[-1]
    timeout = transport.options["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 2
    assert timeout.read == expected_read_timeout
    assert timeout.write == expected_read_timeout
    assert timeout.pool == expected_read_timeout


# ---------------------------------------------------------------------------
# 2. DesktopBridge H11 Methods (wealthfront_window.py)
# ---------------------------------------------------------------------------


def _bridge(tmp_path: Path, client=None, *, demo: bool = False) -> DesktopBridge:
    prefs = tmp_path / "native-window.json"
    return DesktopBridge(
        client if client is not None else _FakeBridgeClient(),
        preferences=prefs,
        demo=demo,
    )


def test_bridge_h11_error_shaping_helper(tmp_path):
    assert DesktopBridge._h11_error(DesktopError("Custom msg", 502, code="NET-001")) == {
        "ok": False,
        "code": "NET-001",
        "error": "Custom msg",
    }
    assert DesktopBridge._h11_error(DesktopError("", 502, code="NET-001")) == {
        "ok": False,
        "code": "NET-001",
        "error": format_error("NET-001"),
    }
    assert DesktopBridge._h11_error(DesktopError("Plain error"), fallback_code="SYNC-201") == {
        "ok": False,
        "code": "SYNC-201",
        "error": "Plain error",
    }
    assert DesktopBridge._h11_error(RuntimeError("boom")) == {
        "ok": False,
        "code": "APP-901",
        "error": format_error("APP-901"),
    }


def test_bridge_get_setup_scan_happy_error_and_demo(tmp_path):
    client = _FakeBridgeClient(
        {
            "/api/setup/check": _sample_check_payload(),
            "/api/setup/progress": _sample_progress_payload(),
            "/api/onboarding": _sample_onboarding_sources_payload(),
        }
    )
    bridge = _bridge(tmp_path, client)
    res = bridge.get_setup_scan()
    assert res["ok"] is True
    assert client.ensure_calls == 1
    assert "codex_local" in res["sourceDetails"]
    # Second call reuses _service_ready without calling ensure_service again
    bridge.get_setup_scan()
    assert client.ensure_calls == 1

    # DesktopError from ensure_service
    err_bridge = _bridge(
        tmp_path,
        _FakeBridgeClient(ensure_error=DesktopError("Service down", 503, code="APP-901")),
    )
    assert err_bridge.get_setup_scan() == {
        "ok": False,
        "code": "APP-901",
        "error": "Service down",
    }

    # Non-DesktopError fallback
    crash_bridge = _bridge(
        tmp_path, _FakeBridgeClient(ensure_error=OSError("socket fail"))
    )
    assert crash_bridge.get_setup_scan() == {
        "ok": False,
        "code": "APP-901",
        "error": format_error("APP-901"),
    }

    # Demo mode skips ensure_service and returns demo payload
    demo_client = _FakeBridgeClient(ensure_error=AssertionError("must not start service"))
    demo_bridge = _bridge(tmp_path, demo_client, demo=True)
    demo_res = demo_bridge.get_setup_scan()
    assert demo_res == demo_setup_scan_data()
    assert demo_client.ensure_calls == 0


def test_bridge_run_setup_scan_happy_error_and_demo(tmp_path):
    client = _FakeBridgeClient(
        {
            "/api/setup/scan": lambda body: {
                "started": "2026-11-16T12:05:00Z",
                "harnesses": ["claude-code", "codex"],
            },
            "/api/setup/check": _sample_check_payload(),
            "/api/setup/progress": _sample_progress_payload(),
            "/api/onboarding": _sample_onboarding_sources_payload(),
        }
    )
    bridge = _bridge(tmp_path, client)
    res = bridge.run_setup_scan()
    assert res["ok"] is True
    assert res["started"] == "2026-11-16T12:05:00Z"
    assert res["harnesses"] == ["claude-code", "codex"]
    assert client.calls[0] == ("/api/setup/scan", {})

    err_bridge = _bridge(
        tmp_path,
        _FakeBridgeClient(
            {"/api/setup/scan": DesktopError("Scan failed", 502, code="HUB-501")}
        ),
    )
    assert err_bridge.run_setup_scan() == {
        "ok": False,
        "code": "HUB-501",
        "error": "Scan failed",
    }

    crash_bridge = _bridge(
        tmp_path, _FakeBridgeClient({"/api/setup/scan": RuntimeError("unexpected")})
    )
    assert crash_bridge.run_setup_scan() == {
        "ok": False,
        "code": "APP-901",
        "error": format_error("APP-901"),
    }

    demo_res = _bridge(tmp_path, demo=True).run_setup_scan()
    assert demo_res["ok"] is True
    assert demo_res["started"] == demo_res["checkedAt"]


def test_bridge_get_setup_progress_happy_error_and_demo(tmp_path):
    prog = _sample_progress_payload()
    client = _FakeBridgeClient({"/api/setup/progress": prog})
    bridge = _bridge(tmp_path, client)
    assert bridge.get_setup_progress() == {
        "ok": True,
        "heartbeat": prog["heartbeat"],
        "sources": prog["sources"],
    }
    assert client.calls == [("/api/setup/progress", None)]

    err_bridge = _bridge(
        tmp_path,
        _FakeBridgeClient(
            {"/api/setup/progress": DesktopError("Offline", 503, code="NET-001")}
        ),
    )
    assert err_bridge.get_setup_progress() == {
        "ok": False,
        "code": "NET-001",
        "error": "Offline",
    }

    crash_bridge = _FakeBridgeClient({"/api/setup/progress": ValueError("bad json")})
    assert _bridge(tmp_path, crash_bridge).get_setup_progress() == {
        "ok": False,
        "code": "APP-901",
        "error": format_error("APP-901"),
    }

    demo_res = _bridge(tmp_path, demo=True).get_setup_progress()
    assert demo_res["ok"] is True
    assert demo_res["heartbeat"]["lastPassAt"] == "2026-11-16T12:00:00Z"
    assert len(demo_res["sources"]) == 2


def test_bridge_set_setup_consent_validation_payload_errors_and_demo(tmp_path):
    client = _FakeBridgeClient(
        {
            "/api/setup/consent": {
                "consent": [{"key": "opencode_go", "enabled": True}],
                "warning": "Restart not required.",
            }
        }
    )
    bridge = _bridge(tmp_path, client)

    # Validation rejects blank keys without hitting client
    assert bridge.set_setup_consent("", True) == {
        "ok": False,
        "error": "Choose a setting to change.",
    }
    assert bridge.set_setup_consent("   ", False) == {
        "ok": False,
        "error": "Choose a setting to change.",
    }
    assert client.calls == []

    # Happy path strips key and sends {"switches": {key: bool}}
    res = bridge.set_setup_consent(" opencode_go ", 1)
    assert res == {
        "ok": True,
        "consent": [{"key": "opencode_go", "enabled": True}],
        "warning": "Restart not required.",
    }
    assert client.calls == [("/api/setup/consent", {"switches": {"opencode_go": True}})]

    # DesktopError & fallback
    assert _bridge(
        tmp_path,
        _FakeBridgeClient(
            {"/api/setup/consent": DesktopError("Denied", 403, code="AUTH-107")}
        ),
    ).set_setup_consent("zai", False) == {
        "ok": False,
        "code": "AUTH-107",
        "error": "Denied",
    }
    assert _bridge(
        tmp_path,
        _FakeBridgeClient({"/api/setup/consent": RuntimeError("crash")}),
    ).set_setup_consent("zai", False) == {
        "ok": False,
        "code": "APP-901",
        "error": format_error("APP-901"),
    }

    # Demo guard
    assert _bridge(tmp_path, demo=True).set_setup_consent("zai", True) == {
        "ok": False,
        "error": "Settings changes are disabled in demo mode.",
    }


def test_bridge_complete_and_defer_onboarding(tmp_path):
    client = _FakeBridgeClient(
        {
            "/api/setup/complete": {"setup": {"completed": True, "computerName": "Studio"}},
            "/api/setup/defer": {"setup": {"deferred": True}},
        }
    )
    bridge = _bridge(tmp_path, client)

    res_named = bridge.complete_onboarding("  Studio  ")
    assert res_named == {"ok": True, "setup": {"completed": True, "computerName": "Studio"}}
    res_anon = bridge.complete_onboarding("   ")
    assert res_anon == {"ok": True, "setup": {"completed": True, "computerName": "Studio"}}
    res_defer = bridge.defer_onboarding()
    assert res_defer == {"ok": True, "setup": {"deferred": True}}

    assert client.calls == [
        ("/api/setup/complete", {"computerName": "Studio"}),
        ("/api/setup/complete", {}),
        ("/api/setup/defer", {}),
    ]

    # Errors
    err_client = _FakeBridgeClient(
        {
            "/api/setup/complete": DesktopError("Complete fail", 502, code="HUB-501"),
            "/api/setup/defer": TypeError("bad"),
        }
    )
    err_bridge = _bridge(tmp_path, err_client)
    assert err_bridge.complete_onboarding() == {
        "ok": False,
        "code": "HUB-501",
        "error": "Complete fail",
    }
    assert err_bridge.defer_onboarding() == {
        "ok": False,
        "code": "APP-901",
        "error": format_error("APP-901"),
    }

    # Non-DesktopError on complete & DesktopError on defer
    err_bridge2 = _bridge(
        tmp_path,
        _FakeBridgeClient(
            {
                "/api/setup/complete": OSError("disk"),
                "/api/setup/defer": DesktopError("Defer fail", 503, code="APP-901"),
            }
        ),
    )
    assert err_bridge2.complete_onboarding()["code"] == "APP-901"
    assert err_bridge2.defer_onboarding() == {
        "ok": False,
        "code": "APP-901",
        "error": "Defer fail",
    }

    # Demo mode
    demo_bridge = _bridge(tmp_path, demo=True)
    assert demo_bridge.complete_onboarding("X") == {
        "ok": True,
        "setup": {"completed": True},
    }
    assert demo_bridge.defer_onboarding() == {"ok": True, "setup": {}}


def test_bridge_get_and_set_notification_settings(tmp_path):
    prefs_path = tmp_path / "prefs-notify.json"
    client = _FakeBridgeClient(
        {
            "/api/setup/notifications": lambda body: (
                {
                    "available": True,
                    "kinds": [
                        {"key": "threshold", "label": "Threshold", "on": True},
                        {"key": "dry", "label": "Dry", "on": False},
                        {"key": "held", "label": "Held", "on": True},
                        {"key": "sync", "label": "Sync", "on": False},
                    ],
                }
                if body is None
                else {
                    "available": True,
                    "kinds": [
                        {
                            "key": "threshold",
                            "label": "Threshold",
                            "on": body["kinds"].get("threshold", False),
                        },
                        {
                            "key": "dry",
                            "label": "Dry",
                            "on": body["kinds"].get("dry", False),
                        },
                        {
                            "key": "held",
                            "label": "Held",
                            "on": body["kinds"].get("held", True),
                        },
                    ],
                }
            )
        }
    )
    bridge = DesktopBridge(client, preferences=prefs_path, demo=False)

    # get_notification_settings
    got = bridge.get_notification_settings()
    assert got["ok"] is True
    assert got["available"] is True
    assert len(got["kinds"]) == 4

    # Blank key rejected
    assert bridge.set_notification_kind(" ", True) == {
        "ok": False,
        "error": "Choose a notification to change.",
    }

    # Updating "threshold" / "dry" updates _low_quota_alerts and persists preferences
    bridge._service_alerts = {"cached": True}
    res_on = bridge.set_notification_kind("threshold", True)
    assert res_on["ok"] is True
    assert res_on["lowQuotaAlerts"] is True
    assert bridge._service_alerts is None
    assert json.loads(prefs_path.read_text())["lowQuotaAlerts"] is True

    res_off = bridge.set_notification_kind("dry", False)
    assert res_off["ok"] is True
    assert res_off["lowQuotaAlerts"] is False
    assert json.loads(prefs_path.read_text())["lowQuotaAlerts"] is False

    # Updating non-plan kind ("held") clears _service_alerts without overwriting _low_quota_alerts
    bridge._low_quota_alerts = True
    bridge._service_alerts = {"cached": True}
    res_held = bridge.set_notification_kind("held", False)
    assert res_held["ok"] is True
    assert bridge._service_alerts is None
    assert res_held["lowQuotaAlerts"] is True
    assert json.loads(prefs_path.read_text())["lowQuotaAlerts"] is False

    # Error handling
    err_bridge = _bridge(
        tmp_path,
        _FakeBridgeClient(
            {"/api/setup/notifications": DesktopError("Notify err", 502, code="HUB-501")}
        ),
    )
    assert err_bridge.get_notification_settings() == {
        "ok": False,
        "code": "HUB-501",
        "error": "Notify err",
    }
    assert err_bridge.set_notification_kind("dry", True) == {
        "ok": False,
        "code": "HUB-501",
        "error": "Notify err",
    }
    crash_bridge = _bridge(
        tmp_path, _FakeBridgeClient({"/api/setup/notifications": RuntimeError("bad")})
    )
    assert crash_bridge.get_notification_settings()["code"] == "APP-901"
    assert crash_bridge.set_notification_kind("dry", True)["code"] == "APP-901"

    # Demo mode
    demo_bridge = _bridge(tmp_path, demo=True)
    demo_got = demo_bridge.get_notification_settings()
    assert demo_got == {
        "ok": True,
        "available": False,
        "kinds": [{"key": k, "label": lbl, "on": True} for k, lbl in KINDS],
    }
    assert demo_bridge.set_notification_kind("dry", False) == {
        "ok": False,
        "error": "Notification changes are disabled in demo mode.",
    }


def test_bridge_send_test_notification_and_502_guidance(tmp_path):
    ok_bridge = _bridge(tmp_path, _FakeBridgeClient({"/api/setup/notifications/test": {}}))
    assert ok_bridge.send_test_notification() == {"ok": True}

    # Special 502 + HUB-501 guidance
    guidance_bridge = _bridge(
        tmp_path,
        _FakeBridgeClient(
            {
                "/api/setup/notifications/test": DesktopError(
                    "toast failed", status_code=502, code="HUB-501"
                )
            }
        ),
    )
    assert guidance_bridge.send_test_notification() == {
        "ok": False,
        "error": "Windows did not show the test notification. Check Windows notification settings for BURNRATE.",
    }

    # Non-502 HUB-501 or other DesktopError falls through to _h11_error
    other_err_bridge = _bridge(
        tmp_path,
        _FakeBridgeClient(
            {
                "/api/setup/notifications/test": DesktopError(
                    "Service unavailable", status_code=503, code="APP-901"
                )
            }
        ),
    )
    assert other_err_bridge.send_test_notification() == {
        "ok": False,
        "code": "APP-901",
        "error": "Service unavailable",
    }

    crash_bridge = _bridge(
        tmp_path, _FakeBridgeClient({"/api/setup/notifications/test": OSError("pipe")})
    )
    assert crash_bridge.send_test_notification() == {
        "ok": False,
        "code": "APP-901",
        "error": format_error("APP-901"),
    }

    assert _bridge(tmp_path, demo=True).send_test_notification() == {
        "ok": False,
        "error": "Test notifications are disabled in demo mode.",
    }


def test_bridge_get_diagnostics_report(tmp_path):
    ok_bridge = _bridge(
        tmp_path,
        _FakeBridgeClient({"/api/setup/diagnostics": {"text": "BURNRATE diag report"}}),
    )
    assert ok_bridge.get_diagnostics_report() == {
        "ok": True,
        "text": "BURNRATE diag report",
    }

    err_bridge = _bridge(
        tmp_path,
        _FakeBridgeClient(
            {"/api/setup/diagnostics": DesktopError("No diag", 500, code="HUB-501")}
        ),
    )
    assert err_bridge.get_diagnostics_report() == {
        "ok": False,
        "code": "HUB-501",
        "error": "No diag",
    }

    crash_bridge = _bridge(
        tmp_path, _FakeBridgeClient({"/api/setup/diagnostics": RuntimeError("fail")})
    )
    assert crash_bridge.get_diagnostics_report() == {
        "ok": False,
        "code": "APP-901",
        "error": format_error("APP-901"),
    }

    assert _bridge(tmp_path, demo=True).get_diagnostics_report() == {
        "ok": False,
        "error": "Diagnostics are disabled in demo mode.",
    }


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (
            {"ok": True, "syncResult": {"ok": False, "error": "hub_unreachable"}},
            "NET-001",
        ),
        (
            {"ok": True, "syncResult": {"ok": False, "error": "hub_rejected"}},
            "AUTH-101",
        ),
        (
            {"ok": True, "syncResult": {"ok": False, "error": "other_reason"}},
            "SYNC-201",
        ),
        (
            {
                "ok": False,
                "error": "hub_unreachable",
                "syncResult": {"ok": False, "code": "HUB-502", "error": "hub_unreachable"},
            },
            "HUB-502",
        ),
    ],
)
def test_bridge_trigger_sync_failure_mapping(tmp_path, payload, expected_code):
    bridge = _bridge(tmp_path, _FakeBridgeClient({"/api/sync/trigger": payload}))
    res = bridge.trigger_sync()
    assert res["ok"] is False
    assert res["code"] == expected_code
    assert res["error"] == format_error(expected_code)


def test_bridge_trigger_sync_happy_exceptions_and_demo(tmp_path):
    machines = [{"id": "pc1", "name": "Desk PC", "online": True}]
    ok_bridge = _bridge(
        tmp_path,
        _FakeBridgeClient(
            {
                "/api/sync/trigger": {
                    "ok": True,
                    "triggeredAt": "2026-11-16T12:15:00Z",
                    "setup": {"spokeConfig": {"enabled": True}},
                    "machines": machines,
                    "syncResult": {"ok": True},
                }
            }
        ),
    )
    res = ok_bridge.trigger_sync()
    assert res == {
        "ok": True,
        "triggeredAt": "2026-11-16T12:15:00Z",
        "setup": {"spokeConfig": {"enabled": True}},
        "machines": machines,
        "syncResult": {"ok": True},
    }

    # DesktopError without explicit code uses SYNC-201 fallback
    err_bridge = _bridge(
        tmp_path,
        _FakeBridgeClient({"/api/sync/trigger": DesktopError("Sync timed out", 502)}),
    )
    assert err_bridge.trigger_sync() == {
        "ok": False,
        "code": "SYNC-201",
        "error": "Sync timed out",
    }

    crash_bridge = _bridge(
        tmp_path, _FakeBridgeClient({"/api/sync/trigger": RuntimeError("oops")})
    )
    assert crash_bridge.trigger_sync() == {
        "ok": False,
        "code": "SYNC-201",
        "error": format_error("SYNC-201"),
    }

    assert _bridge(tmp_path, demo=True).trigger_sync() == {
        "ok": False,
        "error": "Sync is disabled in demo mode.",
    }


# ---------------------------------------------------------------------------
# 3. Headless Browser UI Tests (spend_app/wealthfront_ui.html)
# ---------------------------------------------------------------------------


def _json_for_script(obj: object) -> str:
    return json.dumps(obj).replace("</", "<\\/")


def _h11_scan_fixture() -> dict:
    data = demo_setup_scan_data()
    data["sourceDetails"]["codex_local"] = {
        "source": "codex_local",
        "state": "detected_without_history",
        "path": "~/.codex/sessions",
        "nextAction": "Complete a supported turn and recheck.",
        "lastAttempt": "2026-11-16T11:58:00Z",
        "lastSuccess": None,
        "reason": "no_session_files_yet",
        "detected_without_history": True,
    }
    data["sources"] = [
        data["sourceDetails"]["claude_local"],
        data["sourceDetails"]["codex_local"],
    ]
    return data


def _h11_api_js(
    *,
    scan: dict | None = None,
    notifications: dict | None = None,
    diag_text: str = "BURNRATE Diagnostics\nSources: 2 healthy",
    clipboard_mode: str = "ok",
    extra_js: str = "",
) -> str:
    scan_json = _json_for_script(scan if scan is not None else _h11_scan_fixture())
    notif_json = _json_for_script(
        notifications
        if notifications is not None
        else {
            "ok": True,
            "available": True,
            "kinds": [
                {"key": "threshold", "label": "A plan passes its alert level", "on": True},
                {"key": "dry", "label": "A plan will run out before it resets", "on": True},
                {"key": "held", "label": "A meter stops updating for over an hour", "on": True},
                {"key": "sync", "label": "Hub sync keeps failing", "on": False},
            ],
        }
    )
    diag_json = _json_for_script(diag_text)
    clip_stub = (
        """
        window.__copiedText = null;
        try {
          Object.defineProperty(navigator, 'clipboard', {
            configurable: true,
            value: { writeText: async function (t) { window.__copiedText = t; } }
          });
        } catch (e) {}
        """
        if clipboard_mode == "ok"
        else """
        try {
          Object.defineProperty(navigator, 'clipboard', {
            configurable: true,
            value: { writeText: async function () { throw new Error('Clipboard blocked'); } }
          });
        } catch (e) {}
        """
    )
    return f"""
    window.__h11Scan = {scan_json};
    window.__h11Notif = {notif_json};
    api.get_setup_scan = async function () {{
      window.__calls.push(["get_setup_scan"]);
      return JSON.parse(JSON.stringify(window.__h11Scan));
    }};
    api.run_setup_scan = async function () {{
      window.__calls.push(["run_setup_scan"]);
      return {{ ok: true, started: "2026-11-16T12:00:05Z", harnesses: ["claude-code", "codex"] }};
    }};
    api.get_setup_progress = async function () {{
      window.__calls.push(["get_setup_progress"]);
      return {{
        ok: true,
        heartbeat: {{ lastPassAt: "2026-11-16T12:00:06Z" }},
        sources: [
          {{ source: "claude_local", name: "Claude Code", status: "success", finishedAt: "2026-11-16T12:00:06Z" }},
          {{ source: "codex_local", name: "Codex", status: "success", finishedAt: "2026-11-16T12:00:06Z" }}
        ]
      }};
    }};
    api.set_setup_consent = async function (key, enabled) {{
      window.__calls.push(["set_setup_consent", key, enabled]);
      if (window.__consentFail && window.__consentFail[key]) {{
        return {{ ok: false, error: window.__consentFail[key] }};
      }}
      window.__h11Scan.consent = window.__h11Scan.consent.map(function (c) {{
        return c.key === key ? Object.assign({{}}, c, {{ enabled: !!enabled }}) : c;
      }});
      return {{ ok: true, consent: window.__h11Scan.consent, warning: null }};
    }};
    api.complete_onboarding = async function (name) {{
      window.__calls.push(["complete_onboarding", name]);
      return {{ ok: true, setup: {{ completed: true }} }};
    }};
    api.defer_onboarding = async function () {{
      window.__calls.push(["defer_onboarding"]);
      return {{ ok: true, setup: {{ deferred: true }} }};
    }};
    api.get_notification_settings = async function () {{
      window.__calls.push(["get_notification_settings"]);
      return JSON.parse(JSON.stringify(window.__h11Notif));
    }};
    api.set_notification_kind = async function (key, enabled) {{
      window.__calls.push(["set_notification_kind", key, enabled]);
      if (window.__notifFail && window.__notifFail[key]) {{
        return {{ ok: false, error: window.__notifFail[key] }};
      }}
      window.__h11Notif.kinds = window.__h11Notif.kinds.map(function (k) {{
        return k.key === key ? Object.assign({{}}, k, {{ on: !!enabled }}) : k;
      }});
      var planOn = window.__h11Notif.kinds.some(function (k) {{
        return (k.key === "threshold" || k.key === "dry") && !!k.on;
      }});
      return {{ ok: true, available: true, kinds: window.__h11Notif.kinds, lowQuotaAlerts: planOn }};
    }};
    api.send_test_notification = async function () {{
      window.__calls.push(["send_test_notification"]);
      if (window.__testNotifResp) return window.__testNotifResp;
      return {{ ok: true }};
    }};
    api.get_diagnostics_report = async function () {{
      window.__calls.push(["get_diagnostics_report"]);
      if (window.__diagResp) return window.__diagResp;
      return {{ ok: true, text: {diag_json} }};
    }};
    api.trigger_sync = async function () {{
      window.__calls.push(["trigger_sync"]);
      if (window.__syncResp) return window.__syncResp;
      return {{
        ok: true,
        triggeredAt: "2026-11-16T12:30:00Z",
        machines: [
          {{ id: "pc-1", name: "Desk PC", online: true }},
          {{ id: "pc-2", name: "Travel Laptop", online: true }}
        ]
      }};
    }};
    {clip_stub}
    {extra_js}
    """


ONBOARDING_H11_PROBE = """
await until(() => document.querySelector('#providerList .ob .h11-scan-body'));
await wait(40);
const list = document.getElementById('providerList');
const text = list.textContent.replace(/\\s+/g, ' ');
const out = {
  hasWhatWeFound: text.includes('What we found'),
  hasLanes: ['Claude', 'Codex', 'Z.AI', 'OpenCode Go'].every(n => text.includes(n)),
  fixHints: [...list.querySelectorAll('.h11-fix-hint')].map(el => el.textContent.trim()),
  hasUsageOnly: text.includes('Also reading usage from') && text.includes('Cursor') && text.includes('~/.cursor'),
  hasNotFound: text.includes('Not installed on this computer: Antigravity, Muse.'),
  hasSourceDetails: {
    claudeHealthy: text.includes('claude_local') && text.includes('Healthy'),
    codexDetectedNoHistory: text.includes('codex_local')
      && text.includes('No usage yet')
      && text.includes('Installed on this computer, waiting for recorded usage.')
      && text.includes('Complete a supported turn and recheck.'),
    hasReason: text.includes('no_session_files_yet'),
    hasTimestamps: text.includes('Last attempt:') && text.includes('Last success:'),
  },
};

// Toggle opencode_go consent ON -> set_setup_consent("opencode_go", true)
const goSwitch = document.getElementById('ob_consent_opencode_go');
out.goCheckedBefore = goSwitch.getAttribute('aria-checked');
goSwitch.click();
await until(() => window.__calls.some(c => c[0] === 'set_setup_consent' && c[1] === 'opencode_go'));
await wait(30);
out.goCheckedAfter = document.getElementById('ob_consent_opencode_go').getAttribute('aria-checked');

// Toggle zai consent OFF with failure -> reverts back to true and shows error
window.__consentFail = { zai: 'Could not save consent preference.' };
const zaiSwitch = document.getElementById('ob_consent_zai');
out.zaiBefore = zaiSwitch.getAttribute('aria-checked');
zaiSwitch.click();
await until(() => window.__calls.some(c => c[0] === 'set_setup_consent' && c[1] === 'zai'));
await wait(30);
out.zaiAfterRevert = document.getElementById('ob_consent_zai').getAttribute('aria-checked');
out.consentErrStatus = (document.getElementById('obScanStatus') || {}).textContent || '';

// Shared H5 Hub account form inside .h11-ob-account
const obAcct = list.querySelector('.h11-ob-account');
out.hasSharedHubForm = !!(
  obAcct
  && obAcct.querySelector('#obHubAccountUser')
  && obAcct.querySelector('#obHubAccountPass')
  && obAcct.querySelector('#obHubAccountComp')
  && obAcct.querySelector('#ob_authMode_login')
  && obAcct.querySelector('#ob_authMode_register')
  && obAcct.querySelector('#obHubSignInBtn')
);
obAcct.querySelector('#ob_authMode_register').click();
await wait(30);
out.obCreateBtnText = (document.getElementById('obHubSignInBtn') || {}).textContent?.trim() || '';
document.getElementById('ob_authMode_login').click();
await wait(30);

// Defer onboarding ("Set up later")
list.querySelector('[data-ob-defer]').click();
await until(() => window.__calls.some(c => c[0] === 'defer_onboarding'));
await wait(30);
out.deferStatus = (document.getElementById('obScanStatus') || {}).textContent || '';
out.deferToast = document.getElementById('toast').textContent;

// Complete onboarding ("Complete setup")
const usageCallsBefore = window.__calls.filter(c => c[0] === 'fetch_usage').length;
list.querySelector('[data-ob-complete]').click();
await until(() => window.__calls.some(c => c[0] === 'complete_onboarding'));
await wait(40);
out.completeCalls = window.__calls.filter(c => c[0] === 'complete_onboarding');
out.completeToast = document.getElementById('toast').textContent;
out.usageReloaded = window.__usageCallsAfterComplete >= 1;
report(out);
"""


@needs_chrome
@browser
def test_h11_onboarding_scan_consent_hub_complete_and_defer(tmp_path: Path):
    extra = """
    window.__usageCallsAfterComplete = 0;
    var origFetch = api.fetch_usage;
    api.fetch_usage = async function () {
      if (window.__calls.some(function (c) { return c[0] === 'complete_onboarding'; })) {
        window.__usageCallsAfterComplete++;
      }
      return origFetch.apply(this, arguments);
    };
    """
    html = build_page(
        pref="light",
        report=empty_report(),
        init={"demo": False},
        api_js=_h11_api_js(extra_js=extra),
        probe=ONBOARDING_H11_PROBE,
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=960)
    assert r["hasWhatWeFound"] is True
    assert r["hasLanes"] is True
    assert any("Codex" in h for h in r["fixHints"])
    assert r["hasUsageOnly"] is True
    assert r["hasNotFound"] is True
    assert r["hasSourceDetails"] == {
        "claudeHealthy": True,
        "codexDetectedNoHistory": True,
        "hasReason": True,
        "hasTimestamps": True,
    }
    assert r["goCheckedBefore"] == "false"
    assert r["goCheckedAfter"] == "true"
    assert r["zaiBefore"] == "true"
    assert r["zaiAfterRevert"] == "true"
    assert r["consentErrStatus"] == "Could not save consent preference."
    assert r["hasSharedHubForm"] is True
    assert r["obCreateBtnText"] == "Create account"
    assert "Settings" in r["deferStatus"]
    assert r["deferToast"] == "Setup paused"
    assert r["completeCalls"] == [["complete_onboarding", ""]]
    assert r["completeToast"] == "Setup complete"
    assert r["usageReloaded"] is True


POLLING_PROBE = """
await until(() => document.querySelector('#providerList .ob [data-ob-scan]'));
const scanCallsBefore = window.__calls.filter(c => c[0] === 'get_setup_scan').length;
document.querySelector('[data-ob-scan]').click();
await until(() => window.__calls.some(c => c[0] === 'get_setup_progress'));
await wait(20);
const mid = {
  timerActive: !!state.setupScan.pollTimer,
  statusText: (document.getElementById('obScanStatus') || {}).textContent || '',
};
// Wait for the 2500ms interval poll where heartbeat.lastPassAt >= started
const stopped = await until(() => state.setupScan.pollTimer === 0, 500);
await wait(40);
report({
  mid,
  stopped,
  progressCalls: window.__calls.filter(c => c[0] === 'get_setup_progress').length,
  refreshedScan: window.__calls.filter(c => c[0] === 'get_setup_scan').length > scanCallsBefore,
  finalStatus: (document.getElementById('obScanStatus') || {}).textContent || '',
});
"""


@needs_chrome
@browser
def test_h11_onboarding_scan_button_and_progress_polling_stops_when_complete(tmp_path: Path):
    poll_api_js = _h11_api_js(
        extra_js="""
        var pollCount = 0;
        api.get_setup_progress = async function () {
          pollCount++;
          window.__calls.push(["get_setup_progress", pollCount]);
          if (pollCount === 1) {
            return {
              ok: true,
              heartbeat: { lastPassAt: "2026-11-16T11:59:59Z" },
              sources: [
                { source: "claude_local", name: "Claude Code", status: "running", finishedAt: null },
                { source: "codex_local", name: "Codex", status: "success", finishedAt: "2026-11-16T12:00:06Z" }
              ]
            };
          }
          return {
            ok: true,
            heartbeat: { lastPassAt: "2026-11-16T12:00:07Z" },
            sources: [
              { source: "claude_local", name: "Claude Code", status: "success", finishedAt: "2026-11-16T12:00:07Z" },
              { source: "codex_local", name: "Codex", status: "success", finishedAt: "2026-11-16T12:00:06Z" }
            ]
          };
        };
        """
    )
    html = build_page(
        pref="light",
        report=empty_report(),
        init={"demo": False},
        api_js=poll_api_js,
        probe=POLLING_PROBE,
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=900)
    assert r["mid"]["timerActive"] is True
    assert "1 of 2 done" in r["mid"]["statusText"]
    assert r["stopped"] is True
    assert r["progressCalls"] >= 2
    assert r["refreshedScan"] is True
    assert r["finalStatus"] == "Checked 2 tools."


SOURCES_CARD_PROBE = """
await until(() => document.querySelector('.provider-row'));
await openSettings('sources');
await until(() => document.querySelector('#h11SourcesScanCard .h11-scan-body'));
await wait(40);
const card = document.getElementById('h11SourcesScanCard');
const out = {
  hasCard: !!card,
  hasTitle: card.querySelector('.card-title').textContent.trim() === 'Tool scan & status',
  hasRecheckBtn: !!document.getElementById('srcScanBtn'),
  hasCodexFix: card.textContent.includes('Finish a Codex turn'),
};

document.getElementById('srcScanBtn').click();
await until(() => window.__calls.some(c => c[0] === 'run_setup_scan'));
await until(() => state.setupScan.pollTimer === 0);
await wait(40);
out.statusAfterScan = (document.getElementById('srcScanStatus') || {}).textContent || '';

// Toggle consent inside Settings -> Sources
const sw = document.getElementById('src_consent_opencode_go');
out.consentBefore = sw.getAttribute('aria-checked');
sw.click();
await until(() => window.__calls.some(c => c[0] === 'set_setup_consent'));
await wait(30);
out.consentAfter = document.getElementById('src_consent_opencode_go').getAttribute('aria-checked');
report(out);
"""


@needs_chrome
@browser
def test_h11_settings_sources_scan_card_recheck_and_consent(tmp_path: Path):
    html = build_page(
        pref="light",
        report=demo_report(),
        init={"demo": False},
        api_js=_h11_api_js(),
        probe=SOURCES_CARD_PROBE,
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=900)
    assert r["hasCard"] is True
    assert r["hasTitle"] is True
    assert r["hasRecheckBtn"] is True
    assert r["hasCodexFix"] is True
    assert r["statusAfterScan"] == "Checked 2 tools."
    assert r["consentBefore"] == "false"
    assert r["consentAfter"] == "true"


GENERAL_TAB_H11_PROBE = """
await until(() => document.querySelector('.provider-row'));
await openSettings('general');
await until(() => document.getElementById('sw_notify_threshold'));
await wait(40);
const card = document.getElementById('h11NotificationsCard');
const diagCard = document.getElementById('h11DiagnosticsCard');
const out = {
  hasNotifyCard: !!card,
  hasDiagCard: !!diagCard,
  switches: [...card.querySelectorAll('.switch')].map(s => [s.id, s.getAttribute('aria-checked')]),
};

// Toggle dry OFF -> succeeds and updates lowQuotaAlerts / aria-checked
document.getElementById('sw_notify_dry').click();
await until(() => window.__calls.some(c => c[0] === 'set_notification_kind' && c[1] === 'dry'));
await wait(30);
out.dryAfterSuccess = document.getElementById('sw_notify_dry').getAttribute('aria-checked');

// Toggle held OFF -> fails -> reverts back to "true" and sets error status
window.__notifFail = { held: 'Could not save held preference.' };
document.getElementById('sw_notify_held').click();
await until(() => window.__calls.some(c => c[0] === 'set_notification_kind' && c[1] === 'held'));
await wait(30);
out.heldAfterError = document.getElementById('sw_notify_held').getAttribute('aria-checked');
out.notifErrStatus = {
  text: document.getElementById('notifyTestStatus').textContent,
  isErr: document.getElementById('notifyTestStatus').classList.contains('err'),
};

// Send a test: success
document.getElementById('notifyTestBtn').click();
await until(() => window.__calls.some(c => c[0] === 'send_test_notification'));
await wait(30);
out.testOk = {
  text: document.getElementById('notifyTestStatus').textContent,
  isOk: document.getElementById('notifyTestStatus').classList.contains('ok'),
  isErr: document.getElementById('notifyTestStatus').classList.contains('err'),
};

// Send a test: failure
window.__testNotifResp = { ok: false, error: 'Windows did not show the test notification.' };
document.getElementById('notifyTestBtn').click();
await until(() => window.__calls.filter(c => c[0] === 'send_test_notification').length === 2);
await wait(30);
out.testFail = {
  text: document.getElementById('notifyTestStatus').textContent,
  isErr: document.getElementById('notifyTestStatus').classList.contains('err'),
};

// Copy diagnostics -> clipboard success path
document.getElementById('diagCopyBtn').click();
await until(() => window.__calls.some(c => c[0] === 'get_diagnostics_report'));
await wait(30);
const boxOk = document.getElementById('diagReportText');
out.diagClipboardOk = {
  copiedText: window.__copiedText,
  status: document.getElementById('diagStatus').textContent,
  isErr: document.getElementById('diagStatus').classList.contains('err'),
  boxHidden: boxOk ? boxOk.hidden : null,
};
report(out);
"""


@needs_chrome
@browser
def test_h11_general_notifications_and_diagnostics_clipboard_success(tmp_path: Path):
    html = build_page(
        pref="light",
        report=demo_report(),
        init={"demo": False},
        api_js=_h11_api_js(
            diag_text="Redacted Diag\nSync: ok\nErrors: 0",
            clipboard_mode="ok",
        ),
        probe=GENERAL_TAB_H11_PROBE,
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=950)
    assert r["hasNotifyCard"] is True
    assert r["hasDiagCard"] is True
    assert r["switches"] == [
        ["sw_notify_threshold", "true"],
        ["sw_notify_dry", "true"],
        ["sw_notify_held", "true"],
        ["sw_notify_sync", "false"],
    ]
    assert r["dryAfterSuccess"] == "false"
    assert r["heldAfterError"] == "true"
    assert r["notifErrStatus"] == {
        "text": "Could not save held preference.",
        "isErr": True,
    }
    assert r["testOk"] == {
        "text": "Sent. If nothing appeared, check Windows notification settings for BURNRATE.",
        "isOk": True,
        "isErr": False,
    }
    assert r["testFail"] == {
        "text": "Windows did not show the test notification.",
        "isErr": True,
    }
    assert r["diagClipboardOk"] == {
        "copiedText": "Redacted Diag\nSync: ok\nErrors: 0",
        "status": "Copied to the clipboard.",
        "isErr": False,
        "boxHidden": True,
    }


DIAG_FALLBACK_PROBE = """
await until(() => document.querySelector('.provider-row'));
await openSettings('general');
await until(() => document.getElementById('diagCopyBtn'));
await wait(40);
document.getElementById('diagCopyBtn').click();
await until(() => window.__calls.some(c => c[0] === 'get_diagnostics_report'));
await wait(40);
const box = document.getElementById('diagReportText');
report({
  status: (document.getElementById('diagStatus') || {}).textContent || '',
  isErr: document.getElementById('diagStatus').classList.contains('err'),
  boxPresent: !!box,
  boxHidden: box ? box.hidden : null,
  readOnly: box ? box.readOnly : null,
  value: box ? box.value : '',
});
"""


@needs_chrome
@browser
def test_h11_diagnostics_clipboard_failure_shows_readonly_textarea(tmp_path: Path):
    diag_text = "BURNRATE Redacted Report\nSources: claude_local=healthy"
    html = build_page(
        pref="light",
        report=demo_report(),
        init={"demo": False},
        api_js=_h11_api_js(diag_text=diag_text, clipboard_mode="fail"),
        probe=DIAG_FALLBACK_PROBE,
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=950)
    assert r == {
        "status": "Select the text below and copy it.",
        "isErr": False,
        "boxPresent": True,
        "boxHidden": False,
        "readOnly": True,
        "value": diag_text,
    }


SYNC_CARD_PROBE = """
await until(() => document.querySelector('.provider-row'));
await openSettings('computers');
await until(() => document.getElementById('h11SyncCard'));
await wait(40);
const out = { hasCard: !!document.getElementById('h11SyncCard') };

// 1. Success sync updates status and machines list
document.getElementById('syncNowBtn').click();
await until(() => window.__calls.some(c => c[0] === 'trigger_sync'));
await wait(40);
out.afterOk = {
  status: document.getElementById('syncNowStatus').textContent,
  isOk: document.getElementById('syncNowStatus').classList.contains('ok'),
  isErr: document.getElementById('syncNowStatus').classList.contains('err'),
  machinesUpdated: document.getElementById('sheetBody').textContent.includes('Travel Laptop'),
};

// 2. Coded error states: NET-001, AUTH-101, SYNC-201
out.codedErrors = {};
for (const [code, msg] of [
  ['NET-001', 'Hub unreachable'],
  ['AUTH-101', 'Invalid credential'],
  ['SYNC-201', 'Sync timed out'],
]) {
  window.__syncResp = { ok: false, code, error: msg };
  document.getElementById('syncNowBtn').click();
  await wait(40);
  out.codedErrors[code] = {
    text: document.getElementById('syncNowStatus').textContent,
    isErr: document.getElementById('syncNowStatus').classList.contains('err'),
  };
}
report(out);
"""


@needs_chrome
@browser
def test_h11_settings_computers_sync_now_success_and_coded_errors(tmp_path: Path):
    html = build_page(
        pref="light",
        report=demo_report(),
        init={"demo": False},
        settings={
            "ok": True,
            "connections": [],
            "machines": [{"id": "pc-1", "name": "Desk PC", "online": True}],
            "hasOwnerSession": True,
            "accountEmail": "owner@example.com",
            "setup": {},
            "hubAvailable": True,
        },
        api_js=_h11_api_js(),
        probe=SYNC_CARD_PROBE,
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=950)
    assert r["hasCard"] is True
    assert r["afterOk"]["status"].startswith("Synced at ")
    assert r["afterOk"]["isOk"] is True
    assert r["afterOk"]["isErr"] is False
    assert r["afterOk"]["machinesUpdated"] is True
    assert r["codedErrors"] == {
        "NET-001": {"text": "[NET-001] Hub unreachable", "isErr": True},
        "AUTH-101": {"text": "[AUTH-101] Invalid credential", "isErr": True},
        "SYNC-201": {"text": "[SYNC-201] Sync timed out", "isErr": True},
    }


XSS_H11_PROBE = """
const pImg = '<img src=x onerror=window.__xss=1>';
const pScript = '<script>window.__xss=2<\\/script>';
const pQuotes = `'\\" onclick="window.__xss=3" data-x='`;
const checkClean = (root) => ({
  xssFlag: window.__xss === undefined ? null : window.__xss,
  imgs: root.querySelectorAll('img').length,
  scripts: root.querySelectorAll('script').length,
});

// 1. Onboarding .ob XSS surface
await until(() => document.querySelector('#providerList .ob .h11-scan-body'));
await wait(40);
const list = document.getElementById('providerList');
const obText = list.textContent;
const out = {
  onboardingClean: checkClean(list),
  onboardingLiterals: {
    laneName: obText.includes('Lane ' + pImg),
    lanePlan: obText.includes('Plan ' + pScript),
    usageText: obText.includes('Usage ' + pQuotes),
    meterFix: obText.includes('Fix ' + pImg),
    consentLabel: obText.includes('Consent ' + pImg),
    consentDetail: obText.includes('Detail ' + pScript),
    usageOnlyPath: obText.includes('C:/' + pImg),
    notFound: obText.includes('Missing ' + pScript),
    sourcePath: obText.includes('~/.src/' + pImg),
    sourceReason: obText.includes('Reason ' + pQuotes),
    sourceNextAction: obText.includes('Next ' + pScript),
  },
};

// Trigger consent error & toggle with quoted key
const consentBtn = list.querySelector('[data-consent-key]');
consentBtn.click();
await wait(30);
out.afterConsentErrClean = checkClean(list);
out.consentErrLiteral = ((document.getElementById('obScanStatus') || {}).textContent || '').includes(pImg);

// 2. Settings -> Sources
await openSettings('sources');
await until(() => document.querySelector('#h11SourcesScanCard .h11-scan-body'));
await wait(40);
const sheet = document.getElementById('sheetBody');
out.sourcesClean = checkClean(sheet);

// 3. Settings -> General (notifications + diagnostics)
await setSettingsTab('general');
await until(() => document.getElementById('h11NotificationsCard'));
await wait(40);
document.getElementById('notifyTestBtn').click();
await wait(30);
document.getElementById('diagCopyBtn').click();
await wait(40);
const genText = sheet.textContent;
const diagBox = document.getElementById('diagReportText');
out.generalClean = checkClean(sheet);
out.generalLiterals = {
  notifLabel: genText.includes('Alert ' + pImg),
  notifErr: (document.getElementById('notifyTestStatus').textContent || '').includes(pScript),
  diagBoxValue: diagBox && diagBox.value.includes('Diag ' + pImg + ' ' + pScript),
};

// 4. Settings -> Computers (sync error)
await setSettingsTab('computers');
await until(() => document.getElementById('syncNowBtn'));
await wait(30);
document.getElementById('syncNowBtn').click();
await wait(40);
out.computersClean = checkClean(sheet);
out.syncErrLiteral = (document.getElementById('syncNowStatus').textContent || '').includes('SyncFail ' + pImg);
report(out);
"""


@needs_chrome
@browser
def test_h11_xss_escaping_across_all_surfaces(tmp_path: Path):
    p_img = "<img src=x onerror=window.__xss=1>"
    p_script = "<script>window.__xss=2</script>"
    p_quotes = """'\" onclick="window.__xss=3" data-x='"""
    xss_scan = {
        "ok": True,
        "lanes": [
            {
                "key": f"lane-{p_quotes}",
                "name": f"Lane {p_img}",
                "plan": f"Plan {p_script}",
                "tool": f"tool-{p_img}",
                "usage": {"state": "warn", "text": f"Usage {p_quotes}"},
                "meter": {
                    "state": "unavailable",
                    "text": f"Meter {p_script}",
                    "detail": f"Detail {p_img}",
                    "fix": f"Fix {p_img} {p_quotes}",
                },
                "consent": f"key-{p_quotes}",
            }
        ],
        "usageOnly": [
            {
                "key": "u1",
                "name": f"Only {p_img}",
                "tool": "cursor",
                "usage": {"state": "ok", "text": f"U {p_quotes}", "path": f"C:/{p_img}"},
            }
        ],
        "notFound": [f"Missing {p_script}"],
        "consent": [
            {
                "key": f"key-{p_quotes}",
                "label": f"Consent {p_img}",
                "detail": f"Detail {p_script}",
                "enabled": False,
            }
        ],
        "sources": [
            {
                "source": f"src-{p_img}",
                "state": "detected_without_history",
                "path": f"~/.src/{p_img}",
                "reason": f"Reason {p_quotes}",
                "nextAction": f"Next {p_script}",
                "lastAttempt": f"bad-{p_img}",
                "lastSuccess": None,
            }
        ],
        "sourceDetails": {},
        "progress": {"heartbeat": {}, "sources": []},
    }
    xss_notif = {
        "ok": True,
        "available": True,
        "kinds": [
            {"key": f"k-{p_quotes}", "label": f"Alert {p_img} {p_quotes}", "on": True}
        ],
    }
    extra = f"""
    api.set_setup_consent = async function (k, e) {{
      return {{ ok: false, error: {_json_for_script("Err " + p_img)} }};
    }};
    window.__testNotifResp = {{ ok: false, error: {_json_for_script("TestErr " + p_script)} }};
    window.__syncResp = {{ ok: false, code: "SYNC-201", error: {_json_for_script("SyncFail " + p_img)} }};
    """
    html = build_page(
        pref="light",
        report=empty_report(),
        init={"demo": False},
        api_js=_h11_api_js(
            scan=xss_scan,
            notifications=xss_notif,
            diag_text=f"Diag {p_img} {p_script} {p_quotes}",
            clipboard_mode="fail",
            extra_js=extra,
        ),
        probe=XSS_H11_PROBE,
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=960)
    clean_expected = {"xssFlag": None, "imgs": 0, "scripts": 0}
    assert r["onboardingClean"] == clean_expected
    assert all(r["onboardingLiterals"].values()), r["onboardingLiterals"]
    assert r["afterConsentErrClean"] == clean_expected
    assert r["consentErrLiteral"] is True
    assert r["sourcesClean"] == clean_expected
    assert r["generalClean"] == clean_expected
    assert all(r["generalLiterals"].values()), r["generalLiterals"]
    assert r["computersClean"] == clean_expected
    assert r["syncErrLiteral"] is True


TYPOGRAPHY_PROBE = """
const out = {};

// 1. Onboarding .ob surface
await until(() => document.querySelector('#providerList .ob .h11-scan-body'));
await wait(40);
out.onboardingSmall = smallText();

// 2. Settings -> Sources
await openSettings('sources');
await until(() => document.querySelector('#h11SourcesScanCard .h11-scan-body'));
await wait(40);
out.sourcesSmall = smallText();

// 3. Settings -> General (with status lines and visible diag textarea)
await setSettingsTab('general');
await until(() => document.getElementById('h11NotificationsCard'));
await wait(40);
document.getElementById('notifyTestBtn').click();
await wait(30);
document.getElementById('diagCopyBtn').click();
await wait(30);
out.generalSmall = smallText();

// 4. Settings -> Computers (with sync status)
await setSettingsTab('computers');
await until(() => document.getElementById('syncNowBtn'));
await wait(30);
document.getElementById('syncNowBtn').click();
await wait(30);
out.computersSmall = smallText();
report(out);
"""


@needs_chrome
@browser
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_h11_typography_min_font_size_light_and_dark(tmp_path: Path, theme: str):
    html = build_page(
        pref=theme,
        color_scheme=theme,
        report=empty_report(),
        init={"demo": False, "theme": theme},
        api_js=_h11_api_js(clipboard_mode="fail"),
        probe=TYPOGRAPHY_PROBE,
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=960)
    assert r == {
        "onboardingSmall": [],
        "sourcesSmall": [],
        "generalSmall": [],
        "computersSmall": [],
    }


OB_OVERFLOW_PROBE = """
await until(() => document.querySelector('#providerList .ob .h11-scan-body'));
await wait(40);
const ob = document.querySelector('#providerList .ob');
const obRect = ob.getBoundingClientRect();
const overflowScroll = [];
const overflowRight = [];
for (const el of [ob, ...ob.querySelectorAll('*')]) {
  const cs = getComputedStyle(el);
  if (cs.display === 'none' || cs.visibility === 'hidden') continue;
  if (typeof el.scrollWidth === 'number' && typeof el.clientWidth === 'number' && el.clientWidth > 0) {
    if (el.scrollWidth > el.clientWidth + 1) {
      overflowScroll.push({
        tag: el.tagName,
        cls: typeof el.className === 'string' ? el.className : '',
        scrollWidth: el.scrollWidth,
        clientWidth: el.clientWidth,
      });
    }
  }
  const rect = el.getBoundingClientRect();
  if (rect.width > 0 && rect.height > 0 && rect.right > obRect.right + 1) {
    overflowRight.push({
      tag: el.tagName,
      cls: typeof el.className === 'string' ? el.className : '',
      right: rect.right,
      obRight: obRect.right,
    });
  }
}
const rowDiffs = [...ob.querySelectorAll('.h11-scan-row')].map(row => {
  const rowRect = row.getBoundingClientRect();
  const parentRect = row.parentElement.getBoundingClientRect();
  return Math.abs(rowRect.width - parentRect.width);
});
report({
  docOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
  overflowScroll,
  overflowRight,
  rowCount: rowDiffs.length,
  maxRowDiff: rowDiffs.length ? Math.max(...rowDiffs) : -1,
});
"""


@needs_chrome
@browser
@pytest.mark.parametrize("width", [304, 336, 380])
def test_h11_onboarding_no_horizontal_overflow_at_narrow_widths(
    tmp_path: Path, width: int
):
    html = build_page(
        pref="light",
        report=empty_report(),
        init={"demo": False},
        api_js=_h11_api_js(scan=_h11_scan_fixture()),
        probe=OB_OVERFLOW_PROBE,
        extra_css=FAST,
    )
    r = probe(html, width, tmp_path, height=960)
    assert r["docOverflow"] is False
    assert r["overflowScroll"] == []
    assert r["overflowRight"] == []
    assert r["rowCount"] > 0
    assert r["maxRowDiff"] <= 2


def test_h11_utf8_emdash_and_arrow_preserved_in_python_and_ui_html(tmp_path: Path):
    from spend_app.wealthfront_window import load_ui_html

    check = _sample_check_payload()
    check["lanes"][1]["usage"]["text"] = "Found — no usage yet"
    check["lanes"][1]["meter"]["fix"] = "→ Finish a Codex turn."
    client = _FakeBridgeClient(
        {
            "/api/setup/check": check,
            "/api/setup/progress": _sample_progress_payload(),
            "/api/onboarding": _sample_onboarding_sources_payload(),
        }
    )
    scan = build_setup_scan_data(client, demo=False)
    assert scan["lanes"][1]["usage"]["text"] == "Found — no usage yet"
    assert "Found ?" not in scan["lanes"][1]["usage"]["text"]
    assert scan["lanes"][1]["meter"]["fix"] == "→ Finish a Codex turn."

    bridge = _bridge(tmp_path, client)
    bridge_scan = bridge.get_setup_scan()
    assert bridge_scan["lanes"][1]["usage"]["text"] == "Found — no usage yet"
    assert bridge_scan["lanes"][1]["meter"]["fix"] == "→ Finish a Codex turn."

    demo_scan = demo_setup_scan_data()
    assert demo_scan["lanes"][1]["usage"]["text"] == "Found — no usage yet"
    assert "Found ?" not in demo_scan["lanes"][1]["usage"]["text"]

    ui_html = load_ui_html()
    assert "—" in ui_html
    assert "Settings → Sources." in ui_html
    assert "Found ?" not in ui_html


UTF8_DOM_PROBE = """
await until(() => document.querySelector('#providerList .ob .h11-scan-body'));
await wait(40);
const list = document.getElementById('providerList');

// Defer onboarding to trigger literal "Setup paused. You can recheck anytime in Settings \u2192 Sources."
list.querySelector('[data-ob-defer]').click();
await until(() => window.__calls.some(c => c[0] === 'defer_onboarding'));
await wait(40);
const obText = list.textContent;

// Open Settings -> Sources to verify scan card + deferred status carry literal \u2014 and \u2192
await openSettings('sources');
await until(() => document.querySelector('#h11SourcesScanCard .h11-scan-body'));
await wait(40);
const srcText = document.getElementById('h11SourcesScanCard').textContent;

// Open Settings -> General to verify UTF-8 notification & diagnostics rendering
await setSettingsTab('general');
await until(() => document.getElementById('h11NotificationsCard'));
await wait(40);
const genText = document.getElementById('sheetBody').textContent;

report({
  obHasEmdash: obText.includes('Found \u2014 no usage yet'),
  obHasFixArrow: obText.includes('\u2192 Finish a Codex turn.'),
  obHasDeferredArrow: obText.includes('Settings \u2192 Sources.'),
  obNoQuestionReplace: !obText.includes('Found ?'),
  srcHasEmdash: srcText.includes('Found \u2014 no usage yet'),
  srcHasFixArrow: srcText.includes('\u2192 Finish a Codex turn.'),
  srcHasDeferredArrow: srcText.includes('Settings \u2192 Sources.'),
  srcNoQuestionReplace: !srcText.includes('Found ?'),
  genHasEmdash: genText.includes('\u2014'),
  genHasArrow: genText.includes('\u2192'),
  genNoQuestionReplace: !genText.includes('Found ?'),
});
"""


@needs_chrome
@browser
def test_h11_utf8_emdash_and_arrow_survive_into_rendered_dom(tmp_path: Path):
    demo_scan = demo_setup_scan_data()
    html = build_page(
        pref="light",
        report=empty_report(),
        init={"demo": False},
        api_js=_h11_api_js(
            scan=demo_scan,
            notifications={
                "ok": True,
                "available": True,
                "kinds": [
                    {
                        "key": "threshold",
                        "label": "Plan — alert → threshold",
                        "on": True,
                    }
                ],
            },
        ),
        probe=UTF8_DOM_PROBE,
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=960)
    assert r == {
        "obHasEmdash": True,
        "obHasFixArrow": True,
        "obHasDeferredArrow": True,
        "obNoQuestionReplace": True,
        "srcHasEmdash": True,
        "srcHasFixArrow": True,
        "srcHasDeferredArrow": True,
        "srcNoQuestionReplace": True,
        "genHasEmdash": True,
        "genHasArrow": True,
        "genNoQuestionReplace": True,
    }
