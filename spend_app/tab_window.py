"""The always-on-top usage tab: its data, its bridge and its window.

The tab is a second frameless pywebview window next to the main one. It never
refreshes anything itself. The main bridge pushes every successful or failed
usage fetch into a :class:`TabModel`; the tab page either receives the new
payload through ``evaluate_js`` or reads it back with ``TabBridge.get_state``.
Which provider shows where, and how urgent it looks, comes from
``provider_urgency`` so the tab and the full window always agree.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from spend_app import native_geometry
from spend_app.error_codes import ERROR_CATALOG
from spend_app.provider_urgency import (
    LEVEL_RANK,
    compute_urgency,
    focus_window,
    format_hours,
    profile_family,
    format_reset,
    grade_window,
    sort_key,
)

LOGGER = logging.getLogger(__name__)

SURFACES = ("full", "tab")
TAB_PREFERENCE_KEY = "tabWindow"
SURFACE_KEY = "surface"

# Layout, in logical pixels. The page uses the same numbers (they ride along
# in every payload), so the window is exactly as wide as its content.
TAB_HEIGHT = 60
MAX_SEGMENTS = 6
HOME_WIDTH = 50
SEGMENT_WIDTH = 156
FINE_WIDTH = 68
OPEN_WIDTH = 38
CHIP_WIDTH = 244
LOADING_WIDTH = 150
EMPTY_WIDTH = 340
CARD_WIDTH = 316
FOLDABLE_LEVELS = ("ok", "balance", "idle")
URGENT_LEVELS = ("out", "soon")

LIMIT_LABELS = {
    "5h": "5-hour",
    "weekly": "Weekly",
    "monthly": "Monthly",
    "gemini-5h": "Gemini 5-hour",
    "gemini-weekly": "Gemini weekly",
    "3p-5h": "3P 5-hour",
    "3p-weekly": "3P weekly",
    "balance": "Balance",
}

_FIRST_SENTENCE = re.compile(r"^(.*?[.!?])(?:\s|$)")


def short_error(message: str, code: str | None) -> str:
    """The plain one-line chip text: first sentence, no full stop, plus the code."""
    text = str(message or "").strip()
    match = _FIRST_SENTENCE.match(text)
    if match:
        text = match.group(1)
    text = text.rstrip(".!? ").strip() or "Something went wrong"
    return f"{text} · {code}" if code else text


def window_label(window: dict) -> str:
    """``5-hour`` / ``Weekly`` for a window, falling back to its own label."""
    key = str(window.get("limitKey") or "")
    if key in LIMIT_LABELS:
        return LIMIT_LABELS[key]
    label = str(window.get("label") or window.get("shortLabel") or "Window").strip()
    label = re.sub(r"\s+window$", "", label, flags=re.IGNORECASE)
    return label[:1].upper() + label[1:] if label else "Window"


def _pct(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _span(hours) -> str:
    """``1h`` / ``45m`` without the leading tilde."""
    return format_hours(hours).lstrip("~")


def _pace_line(window: dict, level: str, now: datetime) -> dict | None:
    pace = window.get("pace")
    if level == "out":
        reset = format_reset(window.get("resetsAt"), now)
        return {"level": "soon", "text": f"Out of quota{' · ' + reset if reset else ''}"}
    if not isinstance(pace, dict):
        return None
    state = pace.get("state")
    if state in ("tight", "dry"):
        text = f"At this pace: dry in {format_hours(pace.get('hoursToDry'))}"
        before = pace.get("hoursDryBeforeReset")
        if isinstance(before, (int, float)) and before > 0:
            text += f", {_span(before)} before it resets"
        return {"level": level if level in ("soon", "warn") else "warn", "text": text}
    if state == "safe":
        return {"level": "ok", "text": "On pace to last until it resets"}
    return None


def _card_rows(provider: dict, now: datetime) -> list[dict]:
    rows = []
    for window in provider.get("windows") or []:
        left = _pct(window.get("remainingPct"))
        label = window_label(window)
        if window.get("unavailable") or left is None:
            if window.get("unit") == "usd":
                continue
            rows.append(
                {
                    "label": label,
                    "muted": True,
                    "note": str(window.get("reason") or "Awaiting reading"),
                }
            )
            continue
        level = grade_window(window)
        reset = format_reset(window.get("resetsAt"), now)
        rows.append(
            {
                "label": label,
                "muted": False,
                "level": level,
                "leftPct": round(left),
                "usedPct": round(100 - left),
                "resets": reset[:1].upper() + reset[1:] if reset else "",
                "pace": _pace_line(window, level, now),
            }
        )
    return rows


def _balance_card(provider: dict) -> dict:
    runway = provider.get("fundsRunwayInfo") or {}
    days = runway.get("days") if isinstance(runway, dict) else None
    summary = str((provider.get("remaining") or {}).get("summary") or "")
    if isinstance(days, (int, float)) and runway.get("state") == "estimate":
        summary = f"About {round(days)} days left at your current spend"
    return {"amount": provider.get("fundsRemainingUsd"), "summary": summary}


def _headline(provider: dict, window: dict | None) -> tuple[str, str, float]:
    """``(big text, small unit label, fuel percent)`` for a segment."""
    if (provider.get("remaining") or {}).get("kind") == "balance":
        balance = provider.get("fundsRemainingUsd")
        amount = max(0.0, float(balance)) if isinstance(balance, (int, float)) else 0.0
        fuel = 100.0 if amount >= 25 else max(6.0, amount / 25 * 100)
        return f"${amount:,.2f}", "balance", fuel
    if window is None:
        return "—", "", 0.0
    left = _pct(window.get("remainingPct")) or 0.0
    return f"{round(left)}%", window_label(window).lower(), left


def build_segment(provider: dict, now: datetime) -> dict:
    urgency = provider.get("urgency") or compute_urgency(provider, now)
    level = urgency.get("level") or "idle"
    window = focus_window(provider)
    big, unit, fuel = _headline(provider, window)
    profile = provider.get("profile")
    unit_label = f"{profile} · {unit}" if profile and unit and unit != "balance" else unit
    if profile and unit == "balance":
        unit_label = f"{profile} · balance"
    status = str(urgency.get("text") or "")
    if level == "idle":
        big, unit_label, fuel = str(provider.get("name") or provider.get("key") or ""), profile or "", 0.0
        status = status or "Awaiting reading"
    if level == "balance":
        runway = provider.get("fundsRunwayInfo") or {}
        days = runway.get("days") if isinstance(runway, dict) else None
        status = f"~{round(days)}d runway" if isinstance(days, (int, float)) else "pay as you go"
    name = str(provider.get("name") or provider.get("key") or "")
    title = f"{name} · {profile}" if profile else name
    aria = f"{title}: {big} {unit}".strip() + (f", {status}" if status else "")
    return {
        "key": provider.get("key"),
        "name": name,
        "profile": profile,
        "title": title,
        "plan": provider.get("plan") or "",
        "level": level,
        "limitKey": urgency.get("limitKey"),
        "big": big,
        "unit": unit_label,
        "fuel": round(fuel, 1),
        "status": status,
        "muted": level == "idle",
        "aria": aria,
        "card": {
            "rows": _card_rows(provider, now),
            "balance": _balance_card(provider) if provider.get("fundsRemainingUsd") is not None else None,
            "summary": str((provider.get("remaining") or {}).get("summary") or ""),
        },
    }


def tab_providers(providers) -> list[dict]:
    """Providers the tab can say something about, most urgent first."""
    usable = [
        p
        for p in providers or []
        if isinstance(p, dict) and (p.get("remaining") or {}).get("kind") != "usage_only"
    ]
    ordered = sorted(usable, key=sort_key)
    # The profiles of one provider sit together, at the spot of the most urgent.
    families: dict[str, list[dict]] = {}
    for provider in ordered:
        families.setdefault(profile_family(provider.get("key")), []).append(provider)
    grouped: list[dict] = []
    for provider in ordered:
        family = profile_family(provider.get("key"))
        if family in families:
            grouped.extend(families.pop(family))
    return grouped


def split_overflow(segments: list[dict], cap: int = MAX_SEGMENTS) -> tuple[list[dict], list[dict]]:
    """Keep every urgent segment; fill to ``cap`` from the rest; fold the remainder."""
    if len(segments) <= cap:
        return segments, []
    urgent = [s for s in segments if s["level"] not in FOLDABLE_LEVELS]
    room = max(0, cap - len(urgent))
    foldable = [s for s in segments if s["level"] in FOLDABLE_LEVELS]
    kept_fine = {id(s) for s in foldable[:room]}
    shown = [s for s in segments if s["level"] not in FOLDABLE_LEVELS or id(s) in kept_fine]
    return shown, foldable[room:]


def layout_width(shown: int, fine: bool, *, chip: bool = False, status: int = 0) -> int:
    """Window width for ``shown`` segments, an optional fine pill, error chip
    and status text (``status`` is that text's width in pixels)."""
    width = HOME_WIDTH + shown * SEGMENT_WIDTH + OPEN_WIDTH + status
    if fine:
        width += FINE_WIDTH
    if chip:
        width += CHIP_WIDTH
    return width


class TabModel:
    """Last known usage plus the last error; the single source for the tab."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._providers: list[dict] = []
        self._loaded = False
        self._error: dict | None = None
        self._listeners: list = []
        self._updated = 0.0

    def subscribe(self, listener) -> None:
        self._listeners.append(listener)

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def age(self) -> float:
        """Seconds since the last successful fetch (``inf`` before the first)."""
        return time.monotonic() - self._updated if self._loaded else float("inf")

    def publish_report(self, report) -> None:
        providers = report.get("providers") if isinstance(report, dict) else None
        if not isinstance(providers, list):
            return
        with self._lock:
            self._providers = providers
            self._loaded = True
            self._error = None
            self._updated = time.monotonic()
        self._notify()

    def publish_error(self, message: str, code: str | None) -> None:
        with self._lock:
            self._error = {"message": str(message or ""), "code": code, "text": short_error(message, code)}
        self._notify()

    def refresh(self) -> None:
        """Re-render with the data already held (the theme changed, say)."""
        self._notify()

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 -- a dead tab must not break a refresh
                LOGGER.debug("Tab listener failed", exc_info=True)

    def payload(self, *, dark: bool, demo: bool = False, now: datetime | None = None) -> dict:
        now = now or datetime.now(UTC)
        with self._lock:
            providers = list(self._providers)
            loaded = self._loaded
            error = dict(self._error) if self._error else None
        segments = [build_segment(p, now) for p in tab_providers(providers)]
        shown, fine = split_overflow(segments)
        if not loaded and error is None:
            state = "loading"
        elif not segments:
            state = "error" if error else "empty"
        else:
            state = "ready"
        urgent = sum(1 for s in segments if s["level"] in URGENT_LEVELS)
        fine_info = (
            {
                "count": len(fine),
                "items": [
                    {"key": s["key"], "title": s["title"], "level": s["level"], "text": f"{s['big']} {s['unit']}".strip()}
                    for s in fine
                ],
            }
            if fine
            else None
        )
        status = {"loading": LOADING_WIDTH, "empty": EMPTY_WIDTH}.get(state, 0)
        width = layout_width(len(shown), bool(fine), chip=bool(error), status=status)
        return {
            "state": state,
            "dark": bool(dark),
            "demo": bool(demo),
            "stale": bool(error) and bool(segments),
            "error": error,
            "segments": shown,
            "fine": fine_info,
            "urgentCount": urgent,
            "layout": {
                "height": TAB_HEIGHT,
                "width": width,
                "segmentWidth": SEGMENT_WIDTH,
                "cardWidth": CARD_WIDTH,
            },
        }


# --- the page ---------------------------------------------------------------

SPRITE_START = "<!-- Official Vector Provider Brand Symbols -->"
BRAND_JS_START = "const MONO_DARK"
BRAND_JS_END_FUNCTION = "function logoHtml("


def extract_brand_assets(full_html: str) -> tuple[str, str]:
    """The provider logo sprite and the brand-colour JS, read out of the full
    window's page so the two pages share one source and never drift."""
    start = full_html.find(SPRITE_START)
    end = full_html.find("</svg>", start)
    if start < 0 or end < 0:
        raise ValueError("The logo sprite is missing from wealthfront_ui.html")
    sprite = full_html[start : end + len("</svg>")]
    js_start = full_html.find(BRAND_JS_START)
    fn_start = full_html.find(BRAND_JS_END_FUNCTION, js_start)
    fn_end = full_html.find("\n}\n", fn_start)
    if js_start < 0 or fn_start < 0 or fn_end < 0:
        raise ValueError("The brand table is missing from wealthfront_ui.html")
    return sprite, full_html[js_start : fn_end + 2]


def load_tab_html(theme_pref: str = "system") -> str:
    """tab_ui.html with the shared logos, the embedded font and the saved theme."""
    from spend_app.wealthfront_window import apply_theme_pref, embedded_font_css

    root = Path(__file__).parent
    html = (root / "tab_ui.html").read_text(encoding="utf-8")
    sprite, brand_js = extract_brand_assets((root / "wealthfront_ui.html").read_text(encoding="utf-8"))
    html = html.replace("<!--__BRAND_SPRITE__-->", sprite, 1).replace("/*__BRAND_JS__*/", brand_js, 1)
    html = html.replace("/*__EMBEDDED_FONTS__*/", embedded_font_css(), 1)
    return apply_theme_pref(html, theme_pref)


# --- the page-facing bridge ------------------------------------------------


class TabBridge:
    """``js_api`` of the tab window. Every public method is callable from JS."""

    #: Re-read usage on the page's own poll when nothing refreshed for this long
    #: (the full window is hidden, so its page is not driving refreshes).
    LAZY_REFRESH_AFTER = 120.0

    def __init__(self, bridge, model: TabModel, controller=None) -> None:
        self._bridge = bridge
        self._model = model
        self._controller = controller
        self._refreshing = threading.Lock()

    def attach_controller(self, controller) -> None:
        self._controller = controller

    def _dark(self) -> bool:
        from spend_app.wealthfront_window import resolve_dark

        effective = getattr(self._bridge, "effective_dark", None)
        return bool(effective) if effective is not None else resolve_dark(self._bridge.theme)

    def build_payload(self) -> dict:
        return self._model.payload(dark=self._dark(), demo=self._bridge.demo)

    def get_state(self) -> dict:
        """The current payload; kicks one background refresh when it is stale."""
        if self._model.age > self.LAZY_REFRESH_AFTER and self._refreshing.acquire(blocking=False):
            threading.Thread(target=self._lazy_refresh, name="burnrate-tab-refresh", daemon=True).start()
        return self.build_payload()

    def _lazy_refresh(self) -> None:
        try:
            self._bridge.fetch_usage(self._bridge.current_period, "all")
        except Exception:  # noqa: BLE001 -- the model already holds the error
            LOGGER.debug("Tab refresh failed", exc_info=True)
        finally:
            self._refreshing.release()

    def open_full(self) -> dict:
        if self._controller is not None:
            self._controller.show_full()
        return {"ok": True}

    def begin_drag(self) -> None:
        if self._controller is not None:
            self._controller.tab.begin_drag()

    def expand(self, width: float, height: float) -> dict:
        if self._controller is None:
            return {"ok": False}
        return {"ok": True, **self._controller.tab.expand(width, height)}

    def collapse(self) -> dict:
        if self._controller is not None:
            self._controller.tab.collapse()
        return {"ok": True}

    def resize_to_content(self, width: float) -> dict:
        if self._controller is not None:
            self._controller.tab.set_width(width)
        return {"ok": True}


# --- the window ------------------------------------------------------------


def _scaled(value: float, dpi: int) -> int:
    return int(round(float(value) * max(dpi, 1) / 96))


def clamp_number(value, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    if number != number:
        return low
    return max(low, min(number, high))


class TabHost:
    """Owns the tab's pywebview window: placement, size, drag, show and hide."""

    def __init__(self, window, *, store, hwnd_box: list[int] | None = None, icon_path: Path | None = None, persist: bool = True) -> None:
        self.window = window
        self.hwnd_box = hwnd_box if hwnd_box is not None else [0]
        self._store = store
        self._icon_path = icon_path
        self._persist = persist
        self.tracker = native_geometry.GeometryTracker(
            store,
            lambda: self.hwnd_box[0],
            snapshotter=self._snapshot,
            key=TAB_PREFERENCE_KEY,
        )
        if not persist:
            self.tracker.disable()
        self._base = (LOADING_WIDTH + HOME_WIDTH + OPEN_WIDTH, TAB_HEIGHT)  # logical (w, h)
        self._expanded: tuple[float, float] | None = None
        self._origin_before_expand: tuple[int, int] | None = None
        self.visible = False
        self.quitting = False
        self._lock = threading.RLock()

    @property
    def hwnd(self) -> int:
        return int(self.hwnd_box[0] or 0)

    def _snapshot(self, hwnd: int):
        """Never save while a hover card has stretched (or shifted) the window."""
        if self._expanded is not None:
            return None
        return native_geometry.snapshot(hwnd)

    # -- physical placement --------------------------------------------------

    def _dpi(self) -> int:
        return native_geometry.window_dpi(self.hwnd)

    def _apply(self, x: int | None, y: int | None, width: int, height: int) -> None:
        """SetWindowPos without activating or showing (pywebview's resize shows)."""
        if sys.platform != "win32" or not self.hwnd:
            return
        import ctypes

        flags = 0x0004 | 0x0010 | 0x4000  # NOZORDER | NOACTIVATE | ASYNCWINDOWPOS
        if x is None or y is None:
            flags |= 0x0002  # SWP_NOMOVE
            x = y = 0
        ctypes.windll.user32.SetWindowPos(self.hwnd, 0, x, y, width, height, flags)

    def _target_size(self) -> tuple[int, int]:
        dpi = self._dpi()
        width, height = self._expanded or self._base
        return _scaled(width, dpi), _scaled(height, dpi)

    def _current(self) -> native_geometry.Rect | None:
        return native_geometry.window_rect(self.hwnd)

    def fit(self, *, keep_visible: bool = True) -> None:
        """Re-apply the wanted size (DPI changes rescale it) and pull on screen."""
        width, height = self._target_size()
        rect = self._current()
        if rect is None:
            self._apply(None, None, width, height)
            return
        x, y = rect.left, rect.top
        if keep_visible:
            x, y = native_geometry.keep_on_screen(
                (x, y, width, height), native_geometry.enum_monitors()
            )
        self._apply(x, y, width, height)

    def set_base(self, width: float, height: float = TAB_HEIGHT) -> None:
        with self._lock:
            self._base = (float(width), float(height))
            self.fit()

    def set_width(self, width: float) -> None:
        width = clamp_number(width, 120, 2400)
        with self._lock:
            self._base = (width, self._base[1])
            if self._expanded is None:
                self.fit()
            else:
                self._expanded = (max(width, self._expanded[0]), self._expanded[1])
                self.fit()

    def expand(self, width: float, height: float) -> dict:
        """Grow downward (or upward near the bottom edge) to hold a hover card."""
        width = clamp_number(width, self._base[0], 2400)
        height = clamp_number(height, self._base[1], 1200)
        with self._lock:
            rect = self._current()
            self._expanded = (width, height)
            dpi = self._dpi()
            new_w, new_h = _scaled(width, dpi), _scaled(height, dpi)
            if rect is None:
                self._apply(None, None, new_w, new_h)
                return {"up": False}
            self._origin_before_expand = (rect.left, rect.top)
            x, y, up = native_geometry.place_expanded(
                (rect.left, rect.top, rect.width, rect.height),
                (new_w, new_h),
                native_geometry.enum_monitors(),
            )
            self._apply(x, y, new_w, new_h)
            return {"up": up}

    def collapse(self) -> None:
        with self._lock:
            if self._expanded is None:
                return
            self._expanded = None
            origin = self._origin_before_expand
            self._origin_before_expand = None
            width, height = self._target_size()
            if origin is None:
                self._apply(None, None, width, height)
            else:
                self._apply(origin[0], origin[1], width, height)

    def begin_drag(self) -> None:
        if sys.platform != "win32" or not self.hwnd:
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            user32.ReleaseCapture()
            user32.PostMessageW(self.hwnd, 0xA1, 2, 0)  # WM_NCLBUTTONDOWN, HTCAPTION
        except OSError:
            pass

    # -- show / hide ---------------------------------------------------------

    def restore_position(self, saved) -> None:
        """Place the tab at its saved spot (or the top-right of the work area)."""
        width, height = self._target_size()
        monitors = native_geometry.enum_monitors()
        position = native_geometry.plan_tab_position(
            native_geometry.parse_saved(saved), monitors, self._dpi(), (width, height)
        )
        if position is None:
            self._apply(None, None, width, height)
        else:
            self._apply(position[0], position[1], width, height)

    def show(self, *, activate: bool = True) -> None:
        with self._lock:
            self.fit()
            self.visible = True
        try:
            if activate:
                self.window.show()
            elif sys.platform == "win32" and self.hwnd:
                import ctypes

                ctypes.windll.user32.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
            else:
                self.window.show()
        except Exception:  # noqa: BLE001
            LOGGER.debug("Tab show skipped", exc_info=True)

    def hide(self) -> None:
        self.tracker.flush()
        with self._lock:
            self.visible = False
            self._expanded = None
            self._origin_before_expand = None
        try:
            self.window.hide()
        except Exception:  # noqa: BLE001
            LOGGER.debug("Tab hide skipped", exc_info=True)

    def destroy(self) -> None:
        self.tracker.disable()
        self.quitting = True
        try:
            self.window.destroy()
        except Exception:  # noqa: BLE001
            LOGGER.debug("Tab destroy skipped", exc_info=True)

    def push(self, payload: dict) -> None:
        """Hand a payload to the page on a worker (never on the UI thread)."""
        script = (
            "window.burnrateTab && window.burnrateTab.update && "
            f"window.burnrateTab.update({json.dumps(payload)})"
        )

        def run() -> None:
            try:
                self.window.evaluate_js(script)
            except Exception as exc:  # noqa: BLE001 -- the page may be loading or gone
                LOGGER.debug("Tab push skipped: %s", exc)

        threading.Thread(target=run, name="burnrate-tab-push", daemon=True).start()

    # -- form events (UI thread) ---------------------------------------------

    def on_before_show(self, *, on_close_request, dark: bool) -> None:
        from spend_app.wealthfront_window import _apply_win11_chrome

        native = getattr(self.window, "native", None)
        try:
            self.hwnd_box[0] = int(native.Handle.ToInt64())
        except (AttributeError, OSError, RuntimeError):
            return
        try:
            native.ShowInTaskbar = False
        except Exception:  # noqa: BLE001 -- the tab still works with a taskbar button
            LOGGER.debug("Could not hide the tab's taskbar button", exc_info=True)
        _apply_win11_chrome(self.window, self._icon_path, dark)

        def on_closing(_sender, args) -> None:
            if self.quitting:
                return
            args.Cancel = True
            on_close_request()

        try:
            native.FormClosing += on_closing
            native.DpiChanged += lambda *_args: self.fit()
        except Exception:  # noqa: BLE001
            LOGGER.debug("Tab form hooks skipped", exc_info=True)


# --- switching ---------------------------------------------------------------


class SurfaceController:
    """Decides which of the two windows is up and remembers it."""

    def __init__(self, main_host, tab: TabHost, bridge, store) -> None:
        self.main = main_host
        self.tab = tab
        self.bridge = bridge
        self.store = store

    @property
    def surface(self) -> str:
        return self.bridge.surface

    def show_tab(self) -> None:
        self.bridge._remember_surface("tab")
        self.main.hide()
        self.tab.show()

    def show_full(self) -> None:
        self.bridge._remember_surface("full")
        self.tab.hide()
        self.main.show()

    def toggle(self) -> None:
        """Tray click: hide whatever is up, or bring the remembered surface back."""
        if self.tab.visible:
            self.tab.hide()
        elif self.main.is_visible():
            self.main.hide()
        elif self.surface == "tab":
            self.tab.show()
        else:
            self.main.show()

    def restore_on_launch(self) -> None:
        """The main window is already up for a ``full`` launch; bring the tab up
        (without stealing focus) for a ``tab`` one."""
        if self.surface == "tab":
            self.tab.show(activate=False)

    def close_tab(self) -> None:
        """Alt+F4 on the tab: park in the tray, or quit when there is none."""
        if self.main.can_hide_to_tray():
            self.tab.hide()
        else:
            self.main.quit()
