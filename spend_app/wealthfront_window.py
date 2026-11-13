"""Frameless Windows 11 WebView2 host for BURNRATE's Wealthfront iOS desktop UI."""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from spend_app.native_desktop import (
    PERIOD_LABELS,
    DemoClient,
    DesktopError,
    NativeClient,
    SourceMutation,
)

LOGGER = logging.getLogger(__name__)

EDGE_HITS = {
    "left": 10,
    "right": 11,
    "top": 12,
    "topleft": 13,
    "topright": 14,
    "bottom": 15,
    "bottomleft": 16,
    "bottomright": 17,
}


def load_ui_html() -> str:
    """Load wealthfront_ui.html with offline Plus Jakarta Sans variable font embedded."""
    html_path = Path(__file__).with_name("wealthfront_ui.html")
    html = html_path.read_text(encoding="utf-8")
    font_path = Path(__file__).parent / "fonts" / "plus-jakarta-sans-latin.woff2"
    if font_path.is_file():
        b64 = base64.b64encode(font_path.read_bytes()).decode("ascii")
        font_css = (
            '@font-face{font-family:"Plus Jakarta Sans";font-style:normal;'
            "font-weight:200 800;font-display:block;"
            f'src:url("data:font/woff2;base64,{b64}") format("woff2");}}'
        )
        html = html.replace("/*__EMBEDDED_FONTS__*/", font_css, 1)
    return html


def _apply_win11_chrome(window, icon_path: Path | None) -> int:
    """Enable native Windows 11 rounded corners, light shadow border, and taskbar icon."""
    if sys.platform != "win32":
        return 0
    hwnd = 0
    try:
        native = getattr(window, "native", None)
        if native is not None and hasattr(native, "Handle"):
            hwnd = int(native.Handle.ToInt64())
            if icon_path is not None and icon_path.is_file():
                try:
                    import System.Drawing  # type: ignore[import-not-found]

                    native.Icon = System.Drawing.Icon(str(icon_path))
                except (OSError, AttributeError, RuntimeError) as exc:
                    LOGGER.debug("Optional UI call skipped: %s", exc)
    except (OSError, AttributeError, RuntimeError):
        hwnd = 0

    if not hwnd:
        return 0

    try:
        import ctypes
        from ctypes import wintypes

        setter = ctypes.windll.dwmapi.DwmSetWindowAttribute
        setter.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        light = ctypes.c_int(0)
        setter(hwnd, 20, ctypes.byref(light), ctypes.sizeof(light))
        rounded = ctypes.c_int(2)  # DWMWCP_ROUND
        setter(hwnd, 33, ctypes.byref(rounded), ctypes.sizeof(rounded))
        border = ctypes.c_int(0x00EAE4E4)  # subtle #E4E4EA hairline in 0x00BBGGRR
        setter(hwnd, 34, ctypes.byref(border), ctypes.sizeof(border))
    except OSError:
        pass
    return hwnd


class DesktopBridge:
    """In-process Python bridge exposed to the local WebView2 UI."""

    def __init__(
        self,
        client: NativeClient | DemoClient,
        *,
        preferences: Path | None = None,
        demo: bool = False,
    ) -> None:
        self._client = client
        self._preferences = preferences
        self._demo = demo
        self._window = None
        self._hwnd = 0
        self._service_ready = demo
        self._lock = threading.Lock()
        self._mutations: dict[str, SourceMutation] = {}

        saved = self._read_preferences()
        period = saved.get("period", "today")
        self._period = period if period in PERIOD_LABELS else "today"
        self._topmost = bool(saved.get("topmost", False))

    def attach(self, window, hwnd_getter) -> None:
        self._window = window
        self._hwnd_getter = hwnd_getter

    def _read_preferences(self) -> dict:
        if self._preferences is None:
            return {}
        try:
            raw = json.loads(self._preferences.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_preferences(self) -> None:
        if self._preferences is None:
            return
        try:
            self._preferences.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._preferences.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"period": self._period, "topmost": self._topmost}),
                encoding="utf-8",
            )
            tmp.replace(self._preferences)
        except OSError:
            pass

    def get_initial_state(self) -> dict:
        return {
            "period": self._period,
            "topmost": self._topmost,
            "demo": self._demo,
        }

    def save_period(self, period: str) -> dict:
        if period in PERIOD_LABELS:
            self._period = period
            self._write_preferences()
        return {"ok": True}

    def set_topmost(self, enabled: bool) -> dict:
        self._topmost = bool(enabled)
        if self._window is not None:
            try:
                self._window.on_top = self._topmost
            except (OSError, AttributeError, RuntimeError) as exc:
                LOGGER.debug("Topmost update skipped: %s", exc)
        self._write_preferences()
        return {"ok": True, "topmost": self._topmost}

    def fetch_usage(
        self, period: str = "today", machine: str = "all", restart: bool = False
    ) -> dict:
        use_period = period if period in PERIOD_LABELS else "today"
        try:
            with self._lock:
                if restart or not self._service_ready:
                    self._client.ensure_service()
                    self._service_ready = True
                report = self._client.snapshot(use_period, machine or "all")
            return {"ok": True, "report": report}
        except DesktopError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Could not refresh usage. Retry shortly."}

    def get_settings_data(self) -> dict:
        try:
            with self._lock:
                if not self._service_ready:
                    self._client.ensure_service()
                    self._service_ready = True
                connections = self._client.request("/api/connections")
                machines = self._client.request("/api/machines")
                setup = self._client.request("/api/setup/state")
            return {
                "ok": True,
                "connections": connections.get("connections") or [],
                "machines": machines.get("machines") or [],
                "setup": setup.get("setup") or {},
                "centralServerUrl": setup.get("centralServerUrl") or "",
            }
        except DesktopError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Could not load settings."}

    def browse_location(self, kind: str = "") -> str:
        if self._window is None:
            return ""
        try:
            import webview

            dialog_type = (
                webview.FileDialog.OPEN
                if kind in {"sqlite", "opencode", "zcode"}
                else webview.FileDialog.FOLDER
            )
            selected = self._window.create_file_dialog(dialog_type)
            if selected and len(selected) > 0:
                return str(selected[0])
        except (OSError, AttributeError, RuntimeError) as exc:
            LOGGER.debug("File dialog skipped: %s", exc)
        return ""

    def connect_source(
        self, source: str, is_api: bool, value: str, revision: int = 0
    ) -> dict:
        if self._demo:
            return {"ok": False, "error": "Settings changes are disabled in demo mode."}
        mutation = self._mutations.setdefault(source, SourceMutation(self._client))
        body = {
            "source": source,
            "operation": "connect",
            "revision": int(revision or 0),
            "mode": "api" if is_api else "manual",
            ("secret" if is_api else "location"): value,
        }
        try:
            mutation.send(body)
            return {"ok": True}
        except DesktopError as exc:
            if exc.status_code == 409:
                try:
                    fresh = self._client.request("/api/connections")
                    item = next(
                        (
                            c
                            for c in (fresh.get("connections") or [])
                            if c.get("source") == source
                        ),
                        None,
                    )
                    if item is not None:
                        body["revision"] = int(item.get("revision") or 0)
                        mutation.send(body)
                        return {"ok": True}
                except (OSError, AttributeError, RuntimeError) as exc:
                    LOGGER.debug("Optional UI call skipped: %s", exc)
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Could not connect source."}

    def disconnect_source(self, source: str, revision: int = 0) -> dict:
        if self._demo:
            return {"ok": False, "error": "Settings changes are disabled in demo mode."}
        mutation = self._mutations.setdefault(source, SourceMutation(self._client))
        body = {
            "source": source,
            "operation": "disable",
            "revision": int(revision or 0),
        }
        try:
            mutation.send(body)
            return {"ok": True}
        except DesktopError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Could not disconnect source."}

    def connect_hub_account(
        self,
        server_url: str,
        username: str,
        password: str,
        computer_name: str = "",
        mode: str = "login",
    ) -> dict:
        if self._demo:
            return {"ok": False, "error": "Hub sign-in is disabled in demo mode."}
        raw_url = (server_url or "").strip().rstrip("/")
        if raw_url and "://" not in raw_url:
            raw_url = "https://" + raw_url
        parsed = urlsplit(raw_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            return {
                "ok": False,
                "error": "Enter your VPS hub URL (for example https://your-hub.ts.net:8449).",
            }
        clean_user = (username or "").strip()
        if not clean_user or not password:
            return {
                "ok": False,
                "error": "Enter your hub account username and password.",
            }
        auth_mode = (
            "register" if str(mode or "").strip().lower() == "register" else "login"
        )
        clean_comp = (computer_name or "").strip()
        try:
            res = self._client.request(
                "/api/setup/account",
                {
                    "mode": auth_mode,
                    "serverUrl": raw_url,
                    "username": clean_user,
                    "password": password,
                    "computerName": clean_comp,
                    "testSync": True,
                },
            )
            try:
                self._client.request("/api/setup/defer", {})
            except DesktopError:
                pass
            return {
                "ok": True,
                "account": res.get("account") or {},
                "setup": res.get("setup") or {},
            }
        except DesktopError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Could not sign in to hub account."}

    def create_pairing_code(self, hub_url: str, name: str) -> dict:
        if self._demo:
            return {"ok": False, "error": "Pairing is disabled in demo mode."}
        parsed = urlsplit(hub_url.strip())
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            return {
                "ok": False,
                "error": "Enter a reachable hub address, not localhost.",
            }
        try:
            res = self._client.request(
                "/api/setup/pair-hub", {"hubUrl": hub_url.strip(), "name": name.strip()}
            )
            return {"ok": True, "pairingCode": res.get("pairingCode", "")}
        except DesktopError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Could not generate pairing code."}

    def join_with_code(self, code: str) -> dict:
        if self._demo:
            return {"ok": False, "error": "Pairing is disabled in demo mode."}
        try:
            self._client.request(
                "/api/setup/connect-spoke",
                {"pairingCode": code.strip(), "testSync": True},
            )
            return {"ok": True}
        except DesktopError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Could not pair computer."}

    def minimize_window(self) -> None:
        if self._window is not None:
            self._window.minimize()

    def close_window(self) -> None:
        if self._window is not None:
            self._window.destroy()

    def begin_resize(self, edge: str) -> None:
        if sys.platform != "win32":
            return
        hit = EDGE_HITS.get(str(edge or "").lower())
        hwnd = self._hwnd_getter() if hasattr(self, "_hwnd_getter") else 0
        if not hit or not hwnd:
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            user32.ReleaseCapture()
            user32.PostMessageW(hwnd, 0xA1, hit, 0)  # WM_NCLBUTTONDOWN
        except OSError:
            pass


def _find_icon() -> Path | None:
    resource_root = Path(__file__).resolve().parents[1]
    for candidate in (
        resource_root / "burnrate.ico",
        resource_root / "burnrate-assets" / "burnrate.ico",
        resource_root / "src-tauri" / "icons" / "icon.ico",
    ):
        if candidate.is_file():
            return candidate
    return None


def _focus_existing_window(title: str) -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return True
    except OSError:
        return False
    return False


def run_webview(
    client: NativeClient | DemoClient,
    *,
    preferences: Path | None = None,
    demo: bool = False,
    smoke_test: bool = False,
) -> int:
    import webview

    if not demo and not smoke_test and _focus_existing_window("BURNRATE"):
        return 0

    html = load_ui_html()

    bridge = DesktopBridge(client, preferences=preferences, demo=demo)
    title = "BURNRATE" + (" · Sample data" if demo else "")
    window = webview.create_window(
        title,
        html=html,
        js_api=bridge,
        width=380,
        height=556,
        min_size=(336, 440),
        frameless=True,
        easy_drag=False,
        on_top=bridge._topmost,
        background_color="#F5F6FA",
    )

    hwnd_box = [0]
    bridge.attach(window, lambda: hwnd_box[0])
    result = [0]

    def on_shown():
        hwnd_box[0] = _apply_win11_chrome(window, _find_icon())

    def on_loaded():
        if not hwnd_box[0]:
            hwnd_box[0] = _apply_win11_chrome(window, _find_icon())
        if not smoke_test:
            return

        def verify():
            deadline = time.monotonic() + 8.0
            ok = False
            while time.monotonic() < deadline:
                try:
                    count = window.evaluate_js(
                        "document.querySelectorAll('#providerList .provider-item').length"
                    )
                    hero = window.evaluate_js(
                        "document.getElementById('heroValue')?.textContent || ''"
                    )
                    font_ok = window.evaluate_js(
                        "document.fonts.check('700 30px \"Plus Jakarta Sans\"')"
                    )
                    if (
                        isinstance(count, int)
                        and count >= 5
                        and hero.startswith("$")
                        and font_ok
                    ):
                        ok = True
                        break
                except (OSError, AttributeError, RuntimeError) as exc:
                    LOGGER.debug("Optional UI call skipped: %s", exc)
                time.sleep(0.15)
            if ok:
                try:
                    window.resize(336, 440)
                    time.sleep(0.15)
                    count_after = window.evaluate_js(
                        "document.querySelectorAll('#providerList .provider-item').length"
                    )
                    ok = isinstance(count_after, int) and count_after >= 5
                except (OSError, AttributeError, RuntimeError, ValueError):
                    ok = False
            result[0] = 0 if ok else 1
            window.destroy()

        threading.Thread(target=verify, daemon=True).start()

    window.events.shown += on_shown
    window.events.loaded += on_loaded

    cache_root = Path(tempfile.gettempdir()) / "burnrate-wv2-sessions"
    cache_root.mkdir(parents=True, exist_ok=True)
    storage_dir = cache_root / f"pid-{os.getpid()}"
    storage_dir.mkdir(parents=True, exist_ok=True)

    gui_backend = "edgechromium" if sys.platform == "win32" else None
    try:
        webview.start(
            gui=gui_backend,
            private_mode=False,
            storage_path=str(storage_dir),
        )
    finally:
        shutil.rmtree(storage_dir, ignore_errors=True)
    return result[0]
