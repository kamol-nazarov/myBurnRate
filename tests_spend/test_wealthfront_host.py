"""Host-side behaviour of the native window: theme, geometry, tray, alerts.

Nothing here opens a real window; Windows-only calls are replaced by fakes.
"""

import json
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spend_app import native_alerts, native_geometry, native_onboarding
from spend_app import wealthfront_window as host_module
from spend_app.native_desktop import DemoClient, DesktopError
from spend_app.native_geometry import Monitor, Rect, SavedGeometry
from spend_app.native_links import is_allowed_external_url
from spend_app.native_prefs import PreferenceStore, store_for
from spend_app.wealthfront_window import (
    DesktopBridge,
    WindowHost,
    apply_theme_pref,
    background_for,
    load_ui_html,
    normalize_theme,
    resolve_dark,
)

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def iso(delta: timedelta) -> str:
    return (NOW + delta).isoformat().replace("+00:00", "Z")


# --- theme -------------------------------------------------------------------


def test_theme_preference_is_normalised_persisted_and_reloaded(tmp_path):
    prefs = tmp_path / "native-window.json"
    bridge = DesktopBridge(DemoClient(), preferences=prefs, demo=True)
    assert bridge.get_initial_state()["theme"] == "system"
    assert bridge.set_theme("dark") == {"ok": True, "theme": "dark"}
    assert bridge.set_theme("neon") == {"ok": True, "theme": "system"}
    bridge.set_theme("light")
    assert json.loads(prefs.read_text())["theme"] == "light"
    assert DesktopBridge(DemoClient(), preferences=prefs, demo=True).theme == "light"
    assert normalize_theme(None) == "system"
    assert normalize_theme(" DARK ") == "dark"


def test_theme_attribute_is_substituted_only_for_an_explicit_choice():
    page = '<html lang="en" data-theme-pref="system"><body data-theme-pref="system">'
    assert apply_theme_pref(page, "system") == page
    dark = apply_theme_pref(page, "dark")
    assert dark.count('data-theme-pref="dark"') == 1  # only the <html> tag
    assert 'data-theme-pref="system"' in dark.split("<body", 1)[1]
    assert apply_theme_pref("<html>", "dark") == "<html>"  # tolerates absence
    assert 'data-theme-pref="light"' in load_ui_html("light")
    assert 'data-theme-pref="system"' in load_ui_html("system")


def test_background_follows_the_resolved_theme():
    assert resolve_dark("dark", system_dark=False) is True
    assert resolve_dark("light", system_dark=True) is False
    assert resolve_dark("system", system_dark=True) is True
    assert resolve_dark("system", system_dark=False) is False
    assert background_for(True) == "#101117"
    assert background_for(False) == "#F5F6FA"


def test_system_theme_is_read_from_the_registry_value(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows registry only")
    import winreg

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    for value, expected in ((0, True), (1, False)):
        monkeypatch.setattr(winreg, "OpenKey", lambda *a, **k: Key())
        monkeypatch.setattr(winreg, "QueryValueEx", lambda *a, v=value: (v, 4))
        assert host_module.system_prefers_dark() is expected

    def missing(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(winreg, "OpenKey", missing)
    assert host_module.system_prefers_dark() is False


def test_effective_theme_report_recolours_the_chrome(monkeypatch):
    seen = []
    host = type("Host", (), {"apply_theme": lambda self, dark: seen.append(dark)})()
    bridge = DesktopBridge(DemoClient(), demo=True)
    bridge.attach_host(host)
    assert bridge.report_effective_theme(True) == {"ok": True}
    assert bridge.report_effective_theme(False) == {"ok": True}
    bridge.set_theme("dark")
    assert seen == [True, False, True]


def test_chrome_uses_dark_mode_flag_and_matching_border(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("DWM is Windows only")
    import ctypes

    calls = []

    class Setter:
        argtypes = None

        def __call__(self, hwnd, attr, ref, size):
            calls.append(
                (hwnd, attr, ctypes.cast(ref, ctypes.POINTER(ctypes.c_int))[0])
            )
            return 0

    class Dwm:
        DwmSetWindowAttribute = Setter()

    monkeypatch.setattr(ctypes, "windll", type("W", (), {"dwmapi": Dwm()})())
    host_module._set_theme_chrome(77, True)
    host_module._set_theme_chrome(77, False)
    assert (77, 20, 1) in calls and (77, 20, 0) in calls
    assert (77, 34, host_module.DARK_BORDER) in calls
    assert (77, 34, host_module.LIGHT_BORDER) in calls
    assert host_module.LIGHT_BORDER == 0x00EAE4E4  # #E4E4EA as 0x00BBGGRR
    assert host_module.DARK_BORDER == 0x00372C2A  # #2A2C37 as 0x00BBGGRR


# --- preferences -------------------------------------------------------------


def test_preference_writers_never_clobber_each_others_keys(tmp_path):
    prefs = tmp_path / "native-window.json"
    prefs.write_text(
        json.dumps({"period": "week", "futureKey": {"a": 1}, "autoFade": False})
    )
    bridge = DesktopBridge(DemoClient(), preferences=prefs, demo=True)
    store = store_for(prefs)
    store.update({"window": {"x": 1}, "alertState": {"lanes": {}}})
    bridge.save_period("month")
    bridge.set_close_to_tray(False)
    store.update({"window": {"x": 2}})
    data = json.loads(prefs.read_text())
    assert data["futureKey"] == {"a": 1}
    assert data["window"] == {"x": 2}
    assert data["alertState"] == {"lanes": {}}
    assert data["period"] == "month"
    assert data["closeToTray"] is False
    assert data["autoFade"] is False


def test_preference_store_is_shared_and_thread_safe(tmp_path):
    prefs = tmp_path / "p.json"
    assert store_for(prefs) is store_for(tmp_path / "." / "p.json")
    store = store_for(prefs)

    def writer(index):
        for n in range(20):
            store.update({f"k{index}": n})

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert json.loads(prefs.read_text()) == {f"k{i}": 19 for i in range(6)}
    store.update({"k0": None})
    assert "k0" not in json.loads(prefs.read_text())
    assert PreferenceStore(None).read() == {}
    PreferenceStore(None).update({"x": 1})  # no path: a quiet no-op


def test_existing_preferences_file_still_loads_without_new_keys(tmp_path):
    prefs = tmp_path / "native-window.json"
    prefs.write_text(
        json.dumps(
            {
                "period": "week",
                "topmost": True,
                "autoFade": False,
                "idleOpacity": 0.5,
                "providerOrder": ["codex"],
            }
        )
    )
    state = DesktopBridge(
        DemoClient(), preferences=prefs, demo=True
    ).get_initial_state()
    assert state["period"] == "week" and state["topmost"] is True
    assert state["theme"] == "system"
    assert state["closeToTray"] is True
    assert state["lowQuotaAlerts"] is True
    assert state["trayAvailable"] is False
    assert state["providerOrder"] == ["codex"]
    from spend_app import __version__

    assert state["version"] == __version__


# --- geometry ----------------------------------------------------------------

PRIMARY = Monitor(Rect(0, 0, 3440, 1440), Rect(0, 0, 3440, 1392), primary=True)
LEFT = Monitor(Rect(-1920, -200, 0, 880), Rect(-1920, -200, 0, 840))


@pytest.mark.parametrize("dpi", [96, 144, 192])
@pytest.mark.parametrize(
    "rect",
    [(210, 100, 444, 612), (-1500, -190, 400, 500), (2400, 100, 380, 556)],
)
def test_geometry_round_trips_exactly_at_each_dpi_and_across_monitors(dpi, rect):
    monitors = [PRIMARY, LEFT]
    x, y, w, h = rect
    w, h = round(w * dpi / 96), round(h * dpi / 96)
    saved = SavedGeometry(x, y, w, h, dpi)
    stored = json.loads(json.dumps(saved.as_preference()))
    plan = native_geometry.plan_restore(
        native_geometry.parse_saved(stored), monitors, dpi
    )
    assert (plan.position, plan.width, plan.height) == ((x, y), w, h)


def test_pywebview_logical_units_would_not_round_trip_but_physical_do():
    scale = 1.5
    for physical in (-1499, 2999, 211):
        # pywebview: int(x * scale) on the way in, int(x / scale) on the way out
        assert int(int(physical / scale) * scale) != physical
    plan = native_geometry.plan_restore(
        SavedGeometry(-1499, 7, 400, 500, 144), [LEFT, PRIMARY], 144
    )
    assert plan.position == (-1499, 7)


def test_dpi_change_between_launches_rescales_the_size():
    saved = SavedGeometry(200, 100, 380, 556, 96)
    plan = native_geometry.plan_restore(saved, [PRIMARY], 192)
    assert (plan.width, plan.height) == (760, 1112)
    assert plan.position == (400, 200)
    back = native_geometry.plan_restore(
        SavedGeometry(400, 200, 760, 1112, 192), [PRIMARY], 96
    )
    assert (back.width, back.height, back.position) == (380, 556, (200, 100))


def test_offscreen_saved_rect_keeps_size_and_drops_position():
    saved = SavedGeometry(-30000, 5000, 420, 600, 96)
    plan = native_geometry.plan_restore(saved, [PRIMARY], 96)
    assert plan.position is None
    assert (plan.width, plan.height) == (420, 600)
    # A monitor that was unplugged: the rect sat on LEFT, which is gone now.
    gone = native_geometry.plan_restore(
        SavedGeometry(-1500, 0, 400, 500, 96), [PRIMARY], 96
    )
    assert gone.position is None and (gone.width, gone.height) == (400, 500)
    # Only a sliver visible is also treated as off-screen.
    sliver = native_geometry.plan_restore(
        SavedGeometry(-390, 0, 400, 500, 96), [PRIMARY], 96
    )
    assert sliver.position is None


def test_size_is_clamped_to_the_minimum_and_the_work_area():
    tiny = native_geometry.plan_restore(
        SavedGeometry(100, 100, 100, 100, 96), [PRIMARY], 96
    )
    assert (tiny.width, tiny.height) == native_geometry.MIN_SIZE
    assert native_geometry.min_size_for(150) == (525, 687)
    huge = native_geometry.plan_restore(
        SavedGeometry(0, 0, 9000, 9000, 96), [PRIMARY], 96
    )
    assert (huge.width, huge.height) == (3440, 1392)
    over_edge = native_geometry.plan_restore(
        SavedGeometry(3300, 1300, 380, 556, 96), [PRIMARY], 96
    )
    assert over_edge.position == (3440 - 380, 1392 - 556)
    no_monitors = native_geometry.plan_restore(
        SavedGeometry(10, 10, 400, 500, 96), [], 96
    )
    assert no_monitors.position is None
    assert native_geometry.plan_restore(None, [PRIMARY], 96) is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"x": 1},
        {"x": "a", "y": 1, "width": 3, "height": 4, "dpi": 96},
        {"x": 1, "y": 1, "width": 0, "height": 4, "dpi": 96},
        {"x": 1.5, "y": 1, "width": 3, "height": 4, "dpi": 96},
        {"x": 10**9, "y": 1, "width": 3, "height": 4, "dpi": 96},
        {"x": True, "y": 1, "width": 3, "height": 4, "dpi": 96},
    ],
)
def test_malformed_saved_geometry_means_nothing_saved(raw):
    assert native_geometry.parse_saved(raw) is None


def test_default_size_is_kept_when_nothing_is_saved():
    assert native_geometry.DEFAULT_SIZE == (380, 556)
    assert native_geometry.MIN_SIZE == (336, 440)


def test_geometry_tracker_saves_only_a_visible_normal_window(tmp_path):
    store = PreferenceStore(tmp_path / "p.json")
    state = {"rect": SavedGeometry(5, 6, 400, 500, 96)}
    tracker = native_geometry.GeometryTracker(
        store, lambda: 1, snapshotter=lambda _h: state["rect"], delay=0.05
    )
    assert tracker.flush() is True
    assert store.read()["window"] == state["rect"].as_preference()

    state["rect"] = None  # minimized / hidden / maximized
    store.update({"window": {"x": 99}})
    assert tracker.flush() is False
    assert store.read()["window"] == {"x": 99}

    state["rect"] = SavedGeometry(7, 8, 410, 510, 96)
    tracker.notify_changed()
    tracker.notify_changed()  # debounced into one write
    time.sleep(0.4)
    assert store.read()["window"]["x"] == 7

    tracker.disable()
    state["rect"] = SavedGeometry(70, 80, 410, 510, 96)
    tracker.notify_changed()
    assert tracker.flush() is False
    time.sleep(0.2)
    assert store.read()["window"]["x"] == 7


def test_snapshot_refuses_iconic_hidden_and_maximized(monkeypatch):
    monkeypatch.setattr(native_geometry, "is_restorable_state", lambda _h: False)
    assert native_geometry.snapshot(5) is None
    monkeypatch.setattr(native_geometry, "is_restorable_state", lambda _h: True)
    monkeypatch.setattr(
        native_geometry, "window_rect", lambda _h: Rect(-10, 20, 390, 576)
    )
    monkeypatch.setattr(native_geometry, "window_dpi", lambda _h: 144)
    assert native_geometry.snapshot(5) == SavedGeometry(-10, 20, 400, 556, 144)
    monkeypatch.setattr(native_geometry, "window_rect", lambda _h: Rect(0, 0, 0, 0))
    assert native_geometry.snapshot(5) is None


# --- alerts ------------------------------------------------------------------


def quota(key, remaining, *, limit_key="5h", resets=None, name=None, badge=""):
    return {
        "key": key,
        "name": name or key,
        "badge": badge,
        "fundsRemainingUsd": None,
        "windows": [
            {
                "limitKey": limit_key,
                "shortLabel": limit_key,
                "remainingPct": remaining,
                "unavailable": False,
                "resetsAt": resets or iso(timedelta(hours=3)),
            }
        ],
    }


def balance(key, usd):
    return {"key": key, "name": key.title(), "fundsRemainingUsd": usd, "windows": []}


def run(providers, state=None, **kwargs):
    return native_alerts.evaluate_low_quota(providers, state, NOW, **kwargs)


def test_quota_thresholds_warn_at_twenty_and_turn_critical_at_eight():
    assert run([quota("claude-code", 21)])[0] == []
    (warn,), _ = run([quota("claude-code", 20, name="Claude Code")])
    assert warn["level"] == "warning" and warn["id"] == "claude-code|5h|warning"
    assert warn["title"] == "Claude Code is running low"
    assert "20% left on the 5h window" in warn["message"]
    assert "resets in 3h 0m" in warn["message"]
    (crit,), _ = run([quota("claude-code", 8)])
    assert crit["level"] == "critical" and "almost out" in crit["title"]


def test_each_level_notifies_once_per_cycle_and_survives_a_restart():
    first, state = run([quota("codex", 15, limit_key="weekly")])
    assert [n["level"] for n in first] == ["warning"]
    again, state = run([quota("codex", 14, limit_key="weekly")], state)
    assert again == []
    persisted = json.loads(json.dumps(state))  # a restart reloads it from disk
    assert run([quota("codex", 13, limit_key="weekly")], persisted)[0] == []
    crit, state = run([quota("codex", 6, limit_key="weekly")], persisted)
    assert [n["level"] for n in crit] == ["critical"]
    assert run([quota("codex", 3, limit_key="weekly")], state)[0] == []


def test_jumping_straight_to_critical_sends_one_notification_and_no_warning_later():
    sent, state = run([quota("codex", 4)])
    assert [n["level"] for n in sent] == ["critical"]
    assert (
        run([quota("codex", 15)], state)[0] == []
    )  # partial recovery: warning delivered


def test_rearms_when_the_window_resets_or_the_value_recovers():
    state = run([quota("codex", 5, resets=iso(timedelta(hours=1)))])[1]
    # Reset: resetsAt moves a whole window later while still low.
    sent, state = run([quota("codex", 5, resets=iso(timedelta(hours=6)))], state)
    assert [n["level"] for n in sent] == ["critical"]
    # Seconds of drift in a rolling window's reset time is not a reset.
    drift = iso(timedelta(hours=6, seconds=40))
    assert run([quota("codex", 5, resets=drift)], state)[0] == []
    # Recovery above the warning threshold re-arms every level.
    _, state = run([quota("codex", 90, resets=drift)], state)
    assert state["lanes"] == {}
    assert [n["level"] for n in run([quota("codex", 19, resets=drift)], state)[0]] == [
        "warning"
    ]
    # Recovery from critical into warning re-arms only critical.
    state = run([quota("codex", 5)])[1]
    _, state = run([quota("codex", 15)], state)
    assert [n["level"] for n in run([quota("codex", 7)], state)[0]] == ["critical"]


def test_only_the_tightest_window_of_a_provider_is_considered():
    provider = quota("claude-code", 50, limit_key="5h")
    provider["windows"].append(
        {
            "limitKey": "weekly",
            "shortLabel": "7d",
            "remainingPct": 9.0,
            "unavailable": False,
            "resetsAt": iso(timedelta(days=3)),
        }
    )
    (sent,), state = run([provider])
    assert sent["window"] == "weekly" and sent["level"] == "warning"
    assert "claude-code|weekly" in state["lanes"]


def test_unmeasured_or_unavailable_providers_neither_alert_nor_rearm():
    state = run([quota("codex", 5)])[1]
    gone = quota("codex", 5)
    gone["windows"][0]["unavailable"] = True
    sent, after = run([gone, {"key": "x", "windows": []}], state)
    assert sent == [] and after == state


def test_balance_thresholds_and_rearm_on_top_up():
    assert run([balance("openrouter", 5.0)])[0] == []
    (warn,), state = run([balance("openrouter", 4.99)])
    assert warn["level"] == "warning" and warn["message"] == "$4.99 left"
    assert run([balance("openrouter", 3.0)], state)[0] == []
    (crit,), state = run([balance("openrouter", 0.5)], state)
    assert crit["level"] == "critical" and "almost empty" in crit["title"]
    _, state = run([balance("openrouter", 20.0)], state)
    assert state["lanes"] == {}
    assert len(run([balance("openrouter", 0.9)], state)[0]) == 1


def test_service_owned_mode_alerts_on_balances_only_and_fallback_does_both():
    providers = [quota("codex", 5), balance("deepseek", 0.4)]
    owned, owned_state = run(providers, plan_windows_service_owned=True)
    assert [n["provider"] for n in owned] == ["deepseek"]
    assert "codex|5h" not in owned_state["lanes"]
    fallback, fallback_state = run(providers)
    assert sorted(n["provider"] for n in fallback) == ["codex", "deepseek"]
    # Flipping modes later never re-notifies a lane the other mode delivered.
    again, _ = run(providers, fallback_state, plan_windows_service_owned=True)
    assert again == []
    again, _ = run(providers, owned_state)  # plan lane first seen now: delivered once
    assert [n["provider"] for n in again] == ["codex"]
    for mode in (True, False):
        sent, _ = run(providers, plan_windows_service_owned=mode)
        assert len({n["id"] for n in sent}) == len(sent)


def test_input_state_is_not_mutated():
    state = {"lanes": {"codex|5h": {"resetsAt": None, "levels": ["warning"]}}}
    before = json.dumps(state)
    run([quota("codex", 90)], state)
    assert json.dumps(state) == before


def test_tooltip_names_the_tightest_lane_and_fits_the_limit():
    providers = [quota("a", 40, name="Codex"), quota("b", 12, name="Claude Code")]
    assert (
        native_alerts.tooltip_text(providers) == "BURNRATE - Claude Code 12% left (5h)"
    )
    assert native_alerts.tooltip_text([]) == "BURNRATE"
    assert native_alerts.tooltip_text([balance("openrouter", 15.84)]) == (
        "BURNRATE - Openrouter $15.84 left"
    )
    low = native_alerts.tooltip_text([quota("a", 60), balance("deepseek", 2.0)])
    assert "Deepseek $2.00 left" in low
    long_name = quota(
        "a", 3, name="An Extremely Long Provider Name For The Tray Tooltip"
    )
    assert len(native_alerts.tooltip_text([long_name])) <= native_alerts.TOOLTIP_LIMIT


def test_watcher_respects_the_preference_persists_state_and_never_repeats(tmp_path):
    store = PreferenceStore(tmp_path / "p.json")
    sent, tooltips = [], []
    enabled = {"on": False}
    providers = [quota("codex", 5, name="Codex")]

    def make():
        return native_alerts.QuotaWatcher(
            lambda: providers,
            store,
            sent.append,
            tooltips.append,
            alerts_enabled=lambda: enabled["on"],
        )

    watcher = make()
    assert watcher.run_once() == [] and store.read() == {}
    assert tooltips[-1] == "BURNRATE - Codex 5% left (5h)"  # tooltip still updates
    enabled["on"] = True
    assert [n["level"] for n in watcher.run_once()] == ["critical"]
    assert watcher.run_once() == []
    assert make().run_once() == []  # a restarted watcher reads the saved state
    assert len(sent) == 1
    assert store.read()["alertState"]["lanes"]["codex|5h"]["levels"] == [
        "critical",
        "warning",
    ]


def test_watcher_survives_failures_and_reports_service_ownership(tmp_path):
    store = PreferenceStore(tmp_path / "p.json")

    def boom():
        raise OSError("down")

    quiet = native_alerts.QuotaWatcher(
        boom, store, lambda n: None, lambda t: None, alerts_enabled=lambda: True
    )
    assert quiet.run_once() == []
    none = native_alerts.QuotaWatcher(
        lambda: None, store, lambda n: None, lambda t: None, alerts_enabled=lambda: True
    )
    assert none.run_once() == []
    sent = []
    providers = [quota("codex", 5, name="Codex"), balance("deepseek", 0.5)]
    owned = native_alerts.QuotaWatcher(
        lambda: providers,
        store,
        sent.append,
        lambda t: None,
        alerts_enabled=lambda: True,
        plan_windows_service_owned=lambda: True,
    )
    assert [n["provider"] for n in owned.run_once()] == ["deepseek"]
    broken = native_alerts.QuotaWatcher(
        lambda: providers,
        PreferenceStore(tmp_path / "q.json"),
        sent.append,
        lambda t: None,
        alerts_enabled=lambda: True,
        plan_windows_service_owned=boom,
    )
    assert sorted(n["provider"] for n in broken.run_once()) == ["codex", "deepseek"]


class AlertClient:
    def __init__(self, notifications=None, hud=None):
        self.notifications = notifications
        self.hud = hud
        self.calls = []

    def request(self, path, body=None):
        self.calls.append((path, body))
        if path.startswith("/api/setup/notifications"):
            if isinstance(self.notifications, Exception):
                raise self.notifications
            return {} if body is not None else self.notifications
        if path.startswith("/api/spend/hud"):
            return self.hud
        raise DesktopError("unexpected " + path)


def test_service_ownership_is_checked_cached_and_falls_back_on_failure():
    client = AlertClient({"available": True, "kinds": []})
    bridge = DesktopBridge(client)
    assert bridge.service_owns_plan_alerts() is True
    assert bridge.service_owns_plan_alerts() is True
    assert len([c for c in client.calls if c[0] == "/api/setup/notifications"]) == 1
    bridge._service_alerts = (0.0, True)  # cache expired: asked again
    client.notifications = {"available": False, "kinds": []}
    assert bridge.service_owns_plan_alerts() is False
    bridge._service_alerts = (0.0, True)
    client.notifications = DesktopError("offline")
    assert bridge.service_owns_plan_alerts() is False
    assert DesktopBridge(client, demo=True).service_owns_plan_alerts() is False


def test_low_quota_switch_mirrors_to_the_service_but_never_fails(tmp_path):
    client = AlertClient({"available": True})
    bridge = DesktopBridge(client, preferences=tmp_path / "p.json")
    assert bridge.set_low_quota_alerts(False) == {"ok": True, "lowQuotaAlerts": False}
    assert (
        "/api/setup/notifications",
        {"kinds": {"threshold": False, "dry": False}},
    ) in client.calls
    assert json.loads((tmp_path / "p.json").read_text())["lowQuotaAlerts"] is False
    client.notifications = DesktopError("service down")

    def failing(path, body=None):
        raise DesktopError("service down")

    client.request = failing
    assert bridge.set_low_quota_alerts(True) == {"ok": True, "lowQuotaAlerts": True}
    demo_client = AlertClient()
    demo = DesktopBridge(demo_client, demo=True)
    assert demo.set_low_quota_alerts(False)["ok"] is True
    assert demo_client.calls == []
    # held / sync kinds are never touched
    assert all(set(c[1]["kinds"]) == {"threshold", "dry"} for c in client.calls if c[1])


def test_alert_provider_feed_reads_the_hud_and_is_empty_in_demo():
    hud = {
        "capacity": [
            {
                "providerKey": "codex",
                "title": "Codex",
                "rows": [
                    {
                        "limitKey": "weekly",
                        "label": "Weekly",
                        "pct": 90.0,
                        "unit": "pct",
                    }
                ],
            },
            {
                "providerKey": "openrouter",
                "title": "OpenRouter",
                "isPayg": True,
                "fundsRemainingUsd": 3.2,
                "rows": [],
            },
        ]
    }
    bridge = DesktopBridge(AlertClient(hud=hud))
    providers = bridge.fetch_alert_providers()
    assert {p["key"] for p in providers} == {"codex", "openrouter"}
    notes, _ = run(providers, plan_windows_service_owned=False)
    assert {n["provider"] for n in notes} == {"codex", "openrouter"}
    assert DesktopBridge(DemoClient(), demo=True).fetch_alert_providers() is None

    class Down:
        def request(self, path, body=None):
            raise DesktopError("offline")

    assert DesktopBridge(Down()).fetch_alert_providers() is None


# --- onboarding --------------------------------------------------------------


class ConnectionsClient:
    def __init__(self, rows, setup=None, fail=None):
        self.rows, self.setup, self.fail = rows, setup or {}, fail

    def request(self, path, body=None):
        if self.fail:
            raise self.fail
        if path == "/api/connections":
            return {"connections": self.rows}
        if path == "/api/setup/state":
            return {"setup": self.setup}
        raise DesktopError(path)


def row(source, name, kind, state, **extra):
    return {
        "source": source,
        "name": name,
        "kind": kind,
        "state": state,
        "revision": 3,
        "credentialHelp": "Paste a key",
        **extra,
    }


def test_onboarding_separates_detected_from_active_and_lists_api_sources(tmp_path):
    (tmp_path / ".codex").mkdir()  # Codex folder exists but is not connected
    which = {"claude": "C:/bin/claude.exe"}.get
    rows = [
        row("claude_local", "Claude Code", "jsonl", "receiving_usage"),
        row("codex_local", "Codex", "jsonl", "not_connected"),
        row("opencode_local", "OpenCode", "opencode", "not_connected"),
        row("openrouter", "OpenRouter balance", "api", "balance_available"),
        row("deepseek", "DeepSeek balance", "api", "not_connected"),
        row("openai_admin", "OpenAI Admin", "api", "needs_attention"),
    ]
    client = ConnectionsClient(
        rows, {"spokeConfig": {"enabled": True, "hubUrl": "https://hub.example"}}
    )
    data = native_onboarding.build_onboarding_data(
        client, which=which, env={}, home=tmp_path
    )
    tools = {t["source"]: t for t in data["tools"]}
    assert data["ok"] is True and data["hubConnected"] is True
    assert (tools["claude_local"]["detected"], tools["claude_local"]["active"]) == (
        True,
        True,
    )
    assert tools["claude_local"]["brand"] == "claude"
    assert (tools["codex_local"]["detected"], tools["codex_local"]["active"]) == (
        True,
        False,
    )
    assert tools["codex_local"]["detail"] == "Found on this PC, not connected yet"
    assert (tools["opencode_local"]["detected"], tools["opencode_local"]["active"]) == (
        False,
        False,
    )
    assert tools["opencode_local"]["detail"] == "Not found on this PC"
    assert tools["opencode_local"]["brand"] == "opencode-go"
    apis = {a["source"]: a for a in data["apiSources"]}
    assert apis["openrouter"]["connected"] is True
    assert apis["deepseek"]["connected"] is False
    assert apis["openai_admin"]["connected"] is False
    assert (
        apis["openrouter"]["revision"] == 3
        and apis["openrouter"]["brand"] == "openrouter"
    )
    assert apis["openrouter"]["credentialHelp"] == "Paste a key"
    assert set(apis) == {"openrouter", "deepseek", "openai_admin"}


def test_onboarding_probe_uses_existence_only_and_honours_env_overrides(
    tmp_path, monkeypatch
):
    def forbidden(*_a, **_k):
        raise AssertionError("onboarding must not open files")

    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "iterdir", forbidden)
    custom = tmp_path / "claude-profile"
    custom.mkdir()
    (tmp_path / ".local" / "share" / "opencode").mkdir(parents=True)
    found = native_onboarding.probe_tools(
        which=lambda _name: None, env={"CLAUDE_CONFIG_DIR": str(custom)}, home=tmp_path
    )
    assert found == {"claude_local": True, "codex_local": False, "opencode_local": True}


def test_onboarding_reports_hub_only_when_enabled_with_a_url():
    base = [row("claude_local", "Claude Code", "jsonl", "already_monitored")]
    for setup, expected in (
        ({"spokeConfig": {"enabled": True, "hubUrl": ""}}, False),
        ({"spokeConfig": {"enabled": False, "hubUrl": "https://h"}}, False),
        ({}, False),
    ):
        data = native_onboarding.build_onboarding_data(
            ConnectionsClient(base, setup),
            which=lambda _n: None,
            env={},
            home=Path("nope"),
        )
        assert data["hubConnected"] is expected
        assert data["tools"][0]["active"] is True  # already_monitored counts as active


def test_onboarding_error_path_still_returns_the_local_probe_and_never_raises(tmp_path):
    (tmp_path / ".claude").mkdir()
    client = ConnectionsClient(
        [], fail=DesktopError("The local tracker is unavailable.")
    )
    data = native_onboarding.build_onboarding_data(
        client, which=lambda _n: None, env={}, home=tmp_path
    )
    assert data["ok"] is False and "unavailable" in data["error"]
    assert data["apiSources"] == [] and data["hubConnected"] is False
    assert [t["detected"] for t in data["tools"]] == [True, False, False]

    class Exploding:
        def request(self, path, body=None):
            raise ValueError("bad payload")

    bridge = DesktopBridge(Exploding())
    result = bridge.get_onboarding_data()
    assert result["ok"] is False and isinstance(result["tools"], list)


def test_onboarding_demo_returns_sample_data_without_probing(monkeypatch):
    monkeypatch.setattr(
        native_onboarding,
        "probe_tools",
        lambda **_k: pytest.fail("demo must not probe"),
    )
    data = DesktopBridge(DemoClient(), demo=True).get_onboarding_data()
    assert data["ok"] is True and data["hubConnected"] is True
    assert {t["brand"] for t in data["tools"]} == {"claude", "codex", "opencode-go"}
    assert any(a["connected"] for a in data["apiSources"])
    assert any(not a["connected"] for a in data["apiSources"])


def test_brand_keys_match_the_pages_logo_vocabulary():
    for source, brand in {
        "claude_local": "claude",
        "codex_local": "codex",
        "opencode_local": "opencode-go",
        "openrouter": "openrouter",
        "deepseek": "deepseek",
        "grok_local": "grok",
        "antigravity_local": "antigravity",
        "mystery": "custom",
    }.items():
        assert native_onboarding.brand_for(source) == brand


# --- updates and links -------------------------------------------------------


def install_fake_update(monkeypatch, **functions):
    import types

    module = types.ModuleType("spend_app.native_update")
    for name, function in functions.items():
        setattr(module, name, function)
    monkeypatch.setitem(sys.modules, "spend_app.native_update", module)
    return module


def test_updates_degrade_cleanly_when_the_module_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "spend_app.native_update", None)
    bridge = DesktopBridge(DemoClient(), demo=True)
    from spend_app import __version__

    assert bridge.get_initial_state()["update"] == {
        "kind": "source",
        "current": __version__,
        "canAutoUpdate": False,
    }
    result = bridge.check_for_update()
    assert (
        result["ok"] is False
        and result["error"] == "Updates are not available in this build."
    )
    assert result["canInstall"] is False and result["available"] is False
    assert bridge.install_update() == {
        "ok": False,
        "error": "Updates are not available in this build.",
    }


def test_updates_delegate_and_normalise_the_module_results(monkeypatch):
    bridge = DesktopBridge(DemoClient(), demo=True)
    install_fake_update(
        monkeypatch,
        update_status=lambda: {
            "kind": "installer",
            "current": "0.5.3",
            "canAutoUpdate": 1,
            "x": 1,
        },
        check_for_update=lambda timeout=0: {
            "available": True,
            "version": "0.6.0",
            "notes": "Fixes",
            "canInstall": True,
            "pageUrl": "https://github.com/kamol-nazarov/myBurnRate/releases/tag/v0.6.0",
        },
    )
    assert bridge.get_initial_state()["update"] == {
        "kind": "installer",
        "current": "0.5.3",
        "canAutoUpdate": True,
    }
    checked = bridge.check_for_update()
    assert checked == {
        "ok": True,
        "current": checked["current"],
        "available": True,
        "version": "0.6.0",
        "notes": "Fixes",
        "pageUrl": checked["pageUrl"],
        "canInstall": True,
    }
    install_fake_update(
        monkeypatch, check_for_update=lambda timeout=0: {"error": "offline"}
    )
    failed = bridge.check_for_update()
    assert failed["ok"] is False and failed["error"] == "offline"

    def raises(timeout=0):
        raise RuntimeError("network")

    install_fake_update(monkeypatch, check_for_update=raises, update_status=raises)
    assert bridge.check_for_update()["error"] == "network"
    assert bridge.get_initial_state()["update"]["kind"] == "source"
    install_fake_update(monkeypatch, update_status=lambda: {"kind": "weird"})
    assert bridge.get_initial_state()["update"]["kind"] == "source"


def test_install_update_reports_restart_quits_and_forwards_progress(monkeypatch):
    bridge = DesktopBridge(DemoClient(), demo=True)
    pages, quits, timers = [], [], []
    bridge.call_page = lambda method, *args: pages.append((method, args))
    bridge.quit_app = lambda: quits.append(True)

    class FakeTimer:
        def __init__(self, delay, function):
            timers.append(function)

        def start(self):
            timers[-1]()

    monkeypatch.setattr(host_module.threading, "Timer", FakeTimer)
    install_fake_update(
        monkeypatch,
        check_for_update=lambda timeout=0: {
            "available": True,
            "version": "9.9.9",
            "canInstall": True,
        },
    )
    bridge.check_for_update()

    def installer(progress=None):
        progress({"phase": "downloading", "percent": 40})
        progress("verifying")
        progress(percent=float("nan"))

    install_fake_update(monkeypatch, install_update=installer)
    assert bridge.install_update() == {"ok": True, "restarting": True}
    assert quits == [True]
    (method, (first,)), (_, (second,)), (_, (third,)) = pages
    assert method == "setUpdate"
    assert (
        first["version"] == "9.9.9" and first["available"] is True
    )  # a check-shaped dict
    assert first["progress"] == {"phase": "downloading", "percent": 40}
    assert second["progress"] == {"phase": "verifying", "percent": None}
    assert third["progress"]["percent"] is None

    for outcome in ({"ok": False, "error": "disk full"},):
        install_fake_update(
            monkeypatch, install_update=lambda progress=None, o=outcome: o
        )
        assert bridge.install_update() == {"ok": False, "error": "disk full"}

    def explode(progress=None):
        raise RuntimeError("signature mismatch")

    install_fake_update(monkeypatch, install_update=explode)
    assert bridge.install_update() == {"ok": False, "error": "signature mismatch"}
    assert quits == [True]  # failures never quit


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/kamol-nazarov/myBurnRate/releases/latest",
        "https://github.com/kamol-nazarov/myBurnRate/releases/tag/v0.6.0?x=1#top",
    ],
)
def test_open_external_accepts_only_the_pinned_repository(url, monkeypatch):
    opened = []
    monkeypatch.setattr(
        host_module, "open_in_browser", lambda u: opened.append(u) or True
    )
    assert DesktopBridge(DemoClient(), demo=True).open_external(url) == {"ok": True}
    assert opened == [url]


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/kamol-nazarov/myBurnRate/releases",
        "https://evil.example/kamol-nazarov/myBurnRate/releases",
        "https://github.com.evil.example/kamol-nazarov/myBurnRate/x",
        "https://github.com/someone-else/myBurnRate/releases",
        "https://github.com/kamol-nazarov/otherRepo/releases",
        "https://github.com/kamol-nazarov/myBurnRate",
        "https://github.com/kamol-nazarov/myBurnRateX/releases",
        "https://github.com/kamol-nazarov/myburnrate/releases",
        "javascript:alert(1)",
        "file:///C:/Windows/System32/calc.exe",
        "https://user:pw@github.com/kamol-nazarov/myBurnRate/x",
        "https://github.com@evil.example/kamol-nazarov/myBurnRate/x",
        "https://github.com:444/kamol-nazarov/myBurnRate/x",
        " https://github.com/kamol-nazarov/myBurnRate/x",
        "https://github.com/kamol-nazarov/myBurnRate/x\n",
        "https://github.com/kamol-nazarov/myBurnRate/x\nhttp://evil.example",
        "https://github.com/kamol-nazarov/myBurnRate/a b",
        "https://github.com/kamol-nazarov/myBurnRate/x\r\nHost: evil",
        "https://github.com/kamol-nazarov/myBurnRate/x\t",
        "https://github.com/kamol-nazarov/myBurnRate/../../evil/repo",
        "https://github.com\\@evil.example/kamol-nazarov/myBurnRate/x",
        "//github.com/kamol-nazarov/myBurnRate/x",
        "",
        None,
        42,
    ],
)
def test_open_external_rejects_everything_else(url, monkeypatch):
    monkeypatch.setattr(
        host_module, "open_in_browser", lambda u: pytest.fail("opened " + str(u))
    )
    assert is_allowed_external_url(url) is False
    result = DesktopBridge(DemoClient(), demo=True).open_external(url)
    assert result["ok"] is False and result["error"]


def test_open_external_is_inert_in_the_smoke_test_and_reports_launch_failure(
    monkeypatch,
):
    good = "https://github.com/kamol-nazarov/myBurnRate/releases"
    monkeypatch.setattr(host_module, "open_in_browser", lambda u: pytest.fail("opened"))
    smoke = DesktopBridge(DemoClient(), demo=True, smoke_test=True)
    assert smoke.open_external(good)["ok"] is False
    monkeypatch.setattr(host_module, "open_in_browser", lambda u: False)
    assert DesktopBridge(DemoClient(), demo=True).open_external(good)["ok"] is False

    def boom(u):
        raise OSError("no browser")

    monkeypatch.setattr(host_module, "open_in_browser", boom)
    assert DesktopBridge(DemoClient(), demo=True).open_external(good)["ok"] is False


# --- close: hide vs quit -----------------------------------------------------


class FakeWindow:
    def __init__(self):
        self.calls = []

    def hide(self):
        self.calls.append("hide")

    def show(self):
        self.calls.append("show")

    def destroy(self):
        self.calls.append("destroy")


class FakeTracker:
    def __init__(self):
        self.events = []

    def flush(self):
        self.events.append("flush")

    def disable(self):
        self.events.append("disable")


class FakeOpacity:
    def __init__(self):
        self.stopped = False
        self.woken = 0

    def stop(self):
        self.stopped = True

    def wake(self):
        self.woken += 1


class FakeTray:
    def __init__(self, available=True):
        self.available = available
        self.disposed = False

    def dispose(self):
        self.disposed = True


def make_host(*, demo=False, tray=True, close_to_tray=True, tmp_path=None):
    bridge = DesktopBridge(DemoClient(), demo=demo)
    bridge.set_close_to_tray(close_to_tray)
    window, tracker, opacity = FakeWindow(), FakeTracker(), FakeOpacity()
    host = WindowHost(
        window,
        bridge,
        [0],
        opacity=opacity,
        tracker=tracker,
        saved_geometry=None,
        tray_enabled=False,
        alerts_enabled=False,
        store=PreferenceStore(None),
        icon_path=None,
    )
    if tray is not None:
        host.tray = FakeTray(available=bool(tray))
    bridge.attach_host(host)
    return bridge, host, window, tracker, opacity


def wait_for(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_close_hides_to_the_tray_when_enabled_and_available():
    bridge, host, window, tracker, _ = make_host()
    assert host.can_hide_to_tray() is True
    assert bridge.close_window() == {"ok": True, "hidden": True}
    assert window.calls == ["hide"] and tracker.events == ["flush"]
    assert host.quitting is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"close_to_tray": False},
        {"tray": False},  # the icon could not be created
        {"tray": None},  # no tray at all (smoke test)
        {"demo": True},  # demo close quits so sample windows are not stranded
    ],
)
def test_close_quits_when_hiding_is_not_possible(kwargs):
    bridge, host, window, tracker, opacity = make_host(**kwargs)
    assert host.can_hide_to_tray() is False
    assert bridge.close_window() == {"ok": True, "hidden": False}
    assert wait_for(lambda: "destroy" in window.calls)
    assert host.quitting is True and opacity.stopped is True
    assert "flush" in tracker.events and "disable" in tracker.events
    assert "hide" not in window.calls


def test_quit_is_idempotent_and_overrides_hide_to_tray():
    bridge, host, window, _, opacity = make_host()
    assert bridge.quit_app() == {"ok": True}
    bridge.quit_app()
    assert wait_for(lambda: "destroy" in window.calls)
    assert window.calls.count("destroy") == 1
    assert host.can_hide_to_tray() is False  # once quitting, never hides again
    host.shutdown()
    assert host.tray.disposed is True and opacity.stopped is True


def test_tray_toggle_shows_a_hidden_window_and_wakes_the_fade(monkeypatch):
    _, host, window, _, opacity = make_host()
    monkeypatch.setattr(WindowHost, "is_visible", lambda self: False)
    host.toggle()
    assert window.calls == ["show"] and opacity.woken == 1
    monkeypatch.setattr(WindowHost, "is_visible", lambda self: True)
    host.toggle()
    assert window.calls == ["show", "hide"]


def test_host_to_page_calls_are_guarded_and_whitelisted():
    bridge = DesktopBridge(DemoClient(), demo=True)
    scripts = []
    bridge._window = type(
        "W", (), {"evaluate_js": lambda self, js: scripts.append(js)}
    )()
    bridge.call_page("refresh")
    bridge.call_page("notify", 'hi "there"')
    bridge.call_page("openSettings", None)
    bridge.call_page("evil", 1)
    assert wait_for(lambda: len(scripts) == 3)
    assert all(
        s.startswith("window.burnrateHost && window.burnrateHost.") for s in scripts
    )
    assert any('notify("hi \\"there\\"")' in s for s in scripts)
    assert any("openSettings(null)" in s for s in scripts)


def test_second_launch_unhides_a_tray_hidden_window(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows only")
    import ctypes

    shown = []

    class User32:
        FindWindowW = staticmethod(lambda _c, _t: 42)
        IsWindowVisible = staticmethod(lambda _h: False)
        IsIconic = staticmethod(lambda _h: False)
        SetForegroundWindow = staticmethod(lambda _h: 1)

        @staticmethod
        def ShowWindow(_h, command):
            shown.append(command)

    monkeypatch.setattr(ctypes, "windll", type("W", (), {"user32": User32})())
    assert host_module._focus_existing_window("BURNRATE") is True
    assert shown == [5]  # SW_SHOW before anything else


# --- automatic update checks -------------------------------------------------


def make_checker(tmp_path, *, kind="installer", result=None, checker_error=None):
    from spend_app.native_autoupdate import UpdateChecker

    log = {"pushed": [], "notified": [], "checks": 0}

    def check():
        log["checks"] += 1
        if checker_error:
            raise checker_error
        return result

    def status():
        if isinstance(kind, Exception):
            raise kind
        return {"kind": kind}

    checker = UpdateChecker(
        status=status,
        check=check,
        store=PreferenceStore(tmp_path / "p.json"),
        push=log["pushed"].append,
        notify=log["notified"].append,
        first_delay=0.01,
        interval=0.05,
    )
    return checker, log


AVAILABLE = {"ok": True, "available": True, "version": "0.6.0", "canInstall": True}


@pytest.mark.parametrize(
    ("kind", "expected_checks"),
    [("installer", 1), ("portable", 0), ("source", 0), ("weird", 0), (None, 0)],
)
def test_only_installer_copies_check_on_their_own(tmp_path, kind, expected_checks):
    checker, log = make_checker(tmp_path, kind=kind, result=AVAILABLE)
    assert checker.start() is (expected_checks == 1)
    checker.stop()
    checker.run_once()
    assert log["checks"] == expected_checks
    assert len(log["notified"]) == expected_checks
    assert len(log["pushed"]) == expected_checks


def test_an_unreadable_status_means_no_automatic_check(tmp_path):
    checker, log = make_checker(tmp_path, kind=RuntimeError("boom"), result=AVAILABLE)
    assert checker.start() is False
    assert checker.run_once() is None
    assert log["checks"] == 0


def test_each_version_is_announced_once_even_across_restarts(tmp_path):
    checker, log = make_checker(tmp_path, result=AVAILABLE)
    assert checker.run_once()["version"] == "0.6.0"
    checker.run_once()
    assert log["notified"] == ["0.6.0"]
    assert len(log["pushed"]) == 2  # the page still gets the fresh result
    restarted, log2 = make_checker(tmp_path, result=AVAILABLE)
    restarted.run_once()
    assert log2["notified"] == []
    newer, log3 = make_checker(tmp_path, result={**AVAILABLE, "version": "0.6.1"})
    newer.run_once()
    assert log3["notified"] == ["0.6.1"]
    stored = json.loads((tmp_path / "p.json").read_text())
    assert stored["lastNotifiedUpdate"] == "0.6.1"


@pytest.mark.parametrize(
    "result",
    [
        None,
        "nope",
        {"ok": False, "error": "offline"},
        {"ok": True, "available": False, "version": "0.5.3"},
        {"ok": True, "available": True, "version": ""},
    ],
)
def test_no_notification_without_a_newer_version(tmp_path, result):
    checker, log = make_checker(tmp_path, result=result)
    assert checker.run_once() is None
    assert log["notified"] == [] and log["pushed"] == []


def test_checker_exceptions_never_escape(tmp_path):
    checker, _log = make_checker(tmp_path, checker_error=OSError("network down"))
    assert checker.run_once() is None
    from spend_app.native_autoupdate import UpdateChecker

    def explode(_value):
        raise RuntimeError("tray gone")

    angry = UpdateChecker(
        status=lambda: {"kind": "installer"},
        check=lambda: AVAILABLE,
        store=PreferenceStore(tmp_path / "q.json"),
        push=explode,
        notify=explode,
    )
    assert angry.run_once() is None
    broken_store = UpdateChecker(
        status=lambda: {"kind": "installer"},
        check=lambda: AVAILABLE,
        store=PreferenceStore(tmp_path / "r.json"),
        push=lambda r: None,
        notify=explode,
    )
    assert broken_store.run_once() is None


def test_checker_thread_runs_repeatedly_then_stops(tmp_path):
    checker, log = make_checker(tmp_path, result={"ok": True, "available": False})
    assert checker.start() is True
    assert wait_for(lambda: log["checks"] >= 3)
    checker.stop()
    time.sleep(0.15)
    settled = log["checks"]
    time.sleep(0.2)
    assert log["checks"] == settled


def test_host_starts_the_checker_only_for_real_installer_runs(monkeypatch, tmp_path):
    install_fake_update(
        monkeypatch,
        update_status=lambda: {
            "kind": "installer",
            "current": "0.5.3",
            "canAutoUpdate": True,
        },
        check_for_update=lambda timeout=0: dict(AVAILABLE),
    )
    started = []
    monkeypatch.setattr(
        host_module.UpdateChecker, "start", lambda self: started.append(self) or True
    )
    monkeypatch.setattr(host_module, "_apply_win11_chrome", lambda *a, **k: 0)

    def build(updates_enabled):
        bridge = DesktopBridge(DemoClient(), demo=updates_enabled is None)
        host = WindowHost(
            FakeWindow(),
            bridge,
            [0],
            opacity=FakeOpacity(),
            tracker=FakeTracker(),
            saved_geometry=None,
            tray_enabled=False,
            alerts_enabled=False,
            updates_enabled=bool(updates_enabled),
            store=PreferenceStore(tmp_path / "p.json"),
            icon_path=None,
        )
        host.tray = FakeTray()
        notes = []
        host.tray.notify = lambda title, message, **kw: notes.append((title, kw))
        host.bridge.call_page = lambda *a: notes.append(a)
        host.on_shown()
        return host, notes

    build(False)  # demo / smoke test: never
    assert started == []
    host, notes = build(True)
    assert len(started) == 1
    host._notify_update("0.6.0")
    title, kwargs = notes[-1]
    assert title == "BURNRATE 0.6.0 is available"
    kwargs["on_click"]()  # clicking it shows the window and opens Settings -> General
    assert host.window.calls[-1] == "show"
    assert ("openSettings", "general") in notes
    host.shutdown()
    assert host.updater is not None


def test_allow_list_shares_the_update_modules_repository(monkeypatch):
    from spend_app import native_links, native_update

    assert native_links.PINNED_REPO == native_update.REPO
    assert is_allowed_external_url(native_update.RELEASES_URL + "/latest")


# --- first-hide hint and the quitting invariant --------------------------------


def make_hint_host(tmp_path, *, demo=False, tray_enabled=True):
    bridge = DesktopBridge(DemoClient(), demo=demo)
    window = FakeWindow()
    host = WindowHost(
        window,
        bridge,
        [0],
        opacity=FakeOpacity(),
        tracker=FakeTracker(),
        saved_geometry=None,
        tray_enabled=tray_enabled,
        alerts_enabled=False,
        store=PreferenceStore(tmp_path / "p.json"),
        icon_path=None,
    )
    host.tray = FakeTray()
    host.tray.notes = []
    host.tray.notify = lambda title, message, **kw: host.tray.notes.append(
        (title, message, kw)
    )
    bridge.attach_host(host)
    return bridge, host


def test_first_hide_shows_one_hint_ever_and_clicking_it_shows_the_window(tmp_path):
    bridge, host = make_hint_host(tmp_path)
    assert bridge.close_window()["hidden"] is True
    ((title, message, kwargs),) = host.tray.notes
    assert title == "BURNRATE is still running"
    assert message == (
        "It keeps tracking in the notification area. Right-click the icon to quit."
    )
    assert kwargs == {}  # default balloon click action is "show the window"
    assert json.loads((tmp_path / "p.json").read_text())["trayHintShown"] is True
    bridge.close_window()
    host.hide_for_close()
    assert len(host.tray.notes) == 1
    # A new process (restart) reads the flag from the preferences file.
    _, again = make_hint_host(tmp_path)
    again.hide_for_close()
    assert again.tray.notes == []


def test_tray_toggle_hide_is_not_a_close_and_gives_no_hint(tmp_path, monkeypatch):
    _, host = make_hint_host(tmp_path)
    monkeypatch.setattr(WindowHost, "is_visible", lambda self: True)
    host.toggle()
    assert host.tray.notes == []


@pytest.mark.parametrize("demo,tray_enabled", [(True, True), (False, False)])
def test_no_hint_in_demo_or_smoke(tmp_path, demo, tray_enabled):
    _, host = make_hint_host(tmp_path, demo=demo, tray_enabled=tray_enabled)
    host.hide_for_close()
    assert host.tray.notes == []
    assert not (tmp_path / "p.json").exists()


def test_quitting_is_set_before_every_destroy(tmp_path, monkeypatch):
    """A destroy that arrives as FormClosing/UserClosing must never be cancelled
    into a hide: every route sets ``quitting`` first."""
    seen = []

    def watch(host):
        host.window.destroy = lambda: seen.append(host.quitting)
        return host

    # quit_app (also what install_update's timer calls), the tray's Quit item
    # (handler is host.quit) and the final shutdown path.
    bridge, host = make_hint_host(tmp_path)
    watch(host)
    bridge.quit_app()
    assert wait_for(lambda: seen == [True])
    _, host = make_hint_host(tmp_path)
    watch(host)
    host.quit()
    assert wait_for(lambda: seen == [True, True])
    _, host = make_hint_host(tmp_path)
    host.shutdown()
    assert host.quitting is True and host.can_hide_to_tray() is False
