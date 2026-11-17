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
    display_order,
    focus_window,
    format_hours,
    format_reset,
    grade_window,
)

LOGGER = logging.getLogger(__name__)

SURFACES = ("full", "tab")
TAB_PREFERENCE_KEY = "tabWindow"
SURFACE_KEY = "surface"

# Layout, in logical pixels. The page uses the same numbers (they ride along
# in every payload), so the window is exactly as wide as its content.
TAB_HEIGHT = 60
MAX_SEGMENTS = 5
HOME_WIDTH = 42
SEGMENT_WIDTH = 124
#: A segment that names its profile ("Primary · 5-hour") needs a longer label line.
PROFILE_EXTRA = 36
#: An idle segment says "Awaiting reading", which needs a little more room.
IDLE_EXTRA = 20
FINE_WIDTH = 60
OPEN_WIDTH = 36
CHIP_WIDTH = 212
#: The red "+N urgent" pill (urgent providers that do not fit the monitor).
URGENT_PILL_WIDTH = 92
#: Never plan a layout for less than this, nor closer than this to the screen edge.
MIN_BUDGET = 480
BUDGET_MARGIN = 16
#: Gap between the strip and its hover card, and the two corner radii.
CARD_GAP = 6
TAB_RADIUS = 14
CARD_RADIUS = 12
#: Longer idle reasons show as "Awaiting reading" (the full reason is the tooltip).
IDLE_STATUS_LIMIT = 22
LOADING_WIDTH = 150
EMPTY_WIDTH = 340
CARD_WIDTH = 300
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
    status = detail = str(urgency.get("text") or "")
    if level == "idle":
        big, unit_label, fuel = str(provider.get("name") or provider.get("key") or ""), profile or "", 0.0
        status = status or "Awaiting reading"
        if len(status) > IDLE_STATUS_LIMIT:
            status = "Awaiting reading"
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
        "width": SEGMENT_WIDTH + (PROFILE_EXTRA if profile else 0) + (IDLE_EXTRA if level == "idle" else 0),
        "limitKey": urgency.get("limitKey"),
        "big": big,
        "unit": unit_label,
        "fuel": round(fuel, 1),
        "status": status,
        "detail": detail or status,
        "muted": level == "idle",
        "aria": aria,
        "card": {
            "rows": _card_rows(provider, now),
            "balance": _balance_card(provider) if provider.get("fundsRemainingUsd") is not None else None,
            "summary": str((provider.get("remaining") or {}).get("summary") or ""),
        },
    }


def tab_providers(providers) -> list[dict]:
    """Providers the tab can say something about, most urgent first.

    The order is the one Python stamps on every provider (``displayRank``), so
    the tab and the full window can never disagree; providers without a rank
    (a hand-built list) are ordered by the same rule here.
    """
    usable = [
        p
        for p in providers or []
        if isinstance(p, dict) and (p.get("remaining") or {}).get("kind") != "usage_only"
    ]
    if usable and all(isinstance(p.get("displayRank"), int) for p in usable):
        return sorted(usable, key=lambda p: p["displayRank"])
    return display_order(usable)


def split_overflow(segments: list[dict], cap: int = MAX_SEGMENTS) -> tuple[list[dict], list[dict]]:
    """Keep every urgent segment; fill to ``cap`` from the rest; fold the remainder."""
    shown, folded, _ = plan_segments(segments, cap=cap)
    return shown, folded


def plan_segments(
    segments: list[dict],
    *,
    budget: float | None = None,
    fixed: int = 0,
    cap: int = MAX_SEGMENTS,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Choose what the strip shows: ``(shown, folded, urgent_hidden)``.

    Out/soon/warn segments always show, even beyond ``cap``. The rest fill the
    remaining slots and fold into "+N fine". ``budget`` is the logical width the
    monitor can spare and ``fixed`` the width of everything that is not a
    segment: more calm segments fold until it fits, and if the urgent ones
    alone do not fit, the last of them move into a "+N urgent" pill. Nothing
    is ever sized past the budget.
    """
    urgent = [s for s in segments if s["level"] not in FOLDABLE_LEVELS]
    calm = [s for s in segments if s["level"] in FOLDABLE_LEVELS]

    def total(shown_urgent, shown_calm, folded, urgent_more) -> int:
        width = fixed + sum(s["width"] for s in shown_urgent) + sum(s["width"] for s in shown_calm)
        return width + (FINE_WIDTH if folded else 0) + (URGENT_PILL_WIDTH if urgent_more else 0)

    room = len(calm) if len(segments) <= cap else max(0, cap - len(urgent))
    for kept in range(min(room, len(calm)), -1, -1):
        shown_calm, folded = calm[:kept], calm[kept:]
        if budget is None or total(urgent, shown_calm, folded, False) <= budget:
            keep = {id(s) for s in urgent + shown_calm}
            return [s for s in segments if id(s) in keep], folded, []
    for count in range(len(urgent) - 1, -1, -1):
        shown_urgent, hidden = urgent[:count], urgent[count:]
        if count == 0 or total(shown_urgent, [], calm, True) <= budget:
            return shown_urgent, calm, hidden
    return [], calm, urgent


def layout_width(
    shown: int,
    fine: bool,
    *,
    chip: bool = False,
    status: int = 0,
    extra: int = 0,
    urgent_pill: bool = False,
) -> int:
    """Window width for ``shown`` segments, the pills, an error chip and status
    text (``status`` is that text's width in pixels)."""
    width = HOME_WIDTH + shown * SEGMENT_WIDTH + extra + OPEN_WIDTH + status
    if fine:
        width += FINE_WIDTH
    if urgent_pill:
        width += URGENT_PILL_WIDTH
    if chip:
        width += CHIP_WIDTH
    return width


def _pill_info(items: list[dict]) -> dict | None:
    if not items:
        return None
    return {
        "count": len(items),
        "items": [
            {"key": s["key"], "title": s["title"], "level": s["level"], "text": f"{s['big']} {s['unit']}".strip()}
            for s in items
        ],
    }


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

    def payload(
        self,
        *,
        dark: bool,
        demo: bool = False,
        now: datetime | None = None,
        budget: float | None = None,
    ) -> dict:
        """What the tab page draws. ``budget`` is the logical width the current
        monitor can spare; the layout never exceeds it."""
        now = now or datetime.now(UTC)
        with self._lock:
            providers = list(self._providers)
            loaded = self._loaded
            error = dict(self._error) if self._error else None
        segments = [build_segment(p, now) for p in tab_providers(providers)]
        if not loaded and error is None:
            state = "loading"
        elif not segments:
            state = "error" if error else "empty"
        else:
            state = "ready"
        status = {"loading": LOADING_WIDTH, "empty": EMPTY_WIDTH}.get(state, 0)
        fixed = HOME_WIDTH + OPEN_WIDTH + status + (CHIP_WIDTH if error else 0)
        limit = max(budget, MIN_BUDGET) if budget is not None else None
        shown, fine, urgent_more = plan_segments(segments, budget=limit, fixed=fixed)
        urgent = sum(1 for s in segments if s["level"] in URGENT_LEVELS)
        extra = sum(s["width"] - SEGMENT_WIDTH for s in shown)
        width = layout_width(
            len(shown), bool(fine), chip=bool(error), status=status, extra=extra,
            urgent_pill=bool(urgent_more),
        )
        return {
            "state": state,
            "dark": bool(dark),
            "demo": bool(demo),
            "stale": bool(error) and bool(segments),
            "error": error,
            "segments": shown,
            "fine": _pill_info(fine),
            "urgentMore": _pill_info(urgent_more),
            "urgentCount": urgent,
            "layout": {
                "height": TAB_HEIGHT,
                "width": width,
                "budget": limit,
                "segmentWidth": SEGMENT_WIDTH,
                "cardWidth": CARD_WIDTH,
                "cardGap": CARD_GAP,
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
    """``js_api`` of the tab window. Every public method is callable from JS.

    The tab never refreshes anything: the full window's page owns refreshing
    (it keeps polling while hidden), and every result lands in the model that
    this bridge only reads.
    """

    #: Longest a getter waits for a refresh the main window already started.
    REFRESH_WAIT = 3.0

    def __init__(self, bridge, model: TabModel, controller=None) -> None:
        self._bridge = bridge
        self._model = model
        self._controller = controller

    def attach_controller(self, controller) -> None:
        self._controller = controller

    def _dark(self) -> bool:
        from spend_app.wealthfront_window import resolve_dark

        effective = getattr(self._bridge, "effective_dark", None)
        return bool(effective) if effective is not None else resolve_dark(self._bridge.theme)

    def build_payload(self) -> dict:
        budget = self._controller.tab.logical_budget() if self._controller is not None else None
        return self._model.payload(dark=self._dark(), demo=self._bridge.demo, budget=budget)

    def get_state(self) -> dict:
        """The current payload. If a refresh is already in flight, wait for its
        result instead of reading stale data; never start one."""
        self._bridge._wait_for_refresh(self.REFRESH_WAIT)
        return self.build_payload()

    def open_full(self) -> dict:
        if self._controller is not None:
            self._controller.show_full()
        return {"ok": True}

    def begin_drag(self) -> None:
        if self._controller is not None:
            self._controller.tab.begin_drag()

    def expand(
        self,
        width: float,
        height: float,
        card_x: float | None = None,
        card_width: float | None = None,
        card_height: float | None = None,
    ) -> dict:
        """Grow for a hover card; ``card_*`` is the card's rectangle in CSS pixels
        so only the strip and the card stay visible."""
        if self._controller is None:
            return {"ok": False}
        card = None
        if None not in (card_x, card_width, card_height):
            card = (
                clamp_number(card_x, 0, 2400),
                clamp_number(card_width, 1, 2400),
                clamp_number(card_height, 1, 1200),
            )
        return {"ok": True, **self._controller.tab.expand(width, height, card)}

    def collapse(self) -> dict:
        if self._controller is not None:
            self._controller.tab.collapse()
        return {"ok": True}


# --- the window ------------------------------------------------------------


def _scaled(value: float, dpi: int) -> int:
    return int(round(float(value) * max(dpi, 1) / 96))


def shape_rects(
    size: tuple[int, int],
    scale: float,
    card: tuple[float, float, float] | None = None,
    up: bool = False,
) -> list[tuple[int, int, int, int, int]]:
    """The visible parts of the tab window as ``(left, top, right, bottom, radius)``
    in physical pixels: the strip, plus the hover card while one is open.

    ``card`` is ``(x, width, height)`` in logical pixels. Everything outside
    these rectangles is cut away by the window region, so it is invisible and
    clicks go through to whatever is underneath.
    """
    width, height = size
    tab_h = min(height, int(round(TAB_HEIGHT * scale)))
    tab_radius = int(round(TAB_RADIUS * scale))
    if card is None:
        return [(0, 0, width, height, tab_radius)]
    card_x, card_w, card_h = card
    tab_top = height - tab_h if up else 0
    card_top = 0 if up else tab_h + int(round(CARD_GAP * scale))
    return [
        (0, tab_top, width, tab_top + tab_h, tab_radius),
        (
            int(round(card_x * scale)),
            card_top,
            min(width, int(round((card_x + card_w) * scale))),
            min(height, card_top + int(round(card_h * scale))),
            int(round(CARD_RADIUS * scale)),
        ),
    ]


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
        self._card: tuple[float, float, float] | None = None
        self._up = False
        #: Physical top-left of the collapsed tab for the whole expansion session.
        self._anchor: tuple[int, int] | None = None
        self.on_reflow = None
        self._last_budget: float | None = None
        self._reflowing = False
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

    def _set_region(self, rects: list[tuple[int, int, int, int, int]]) -> None:
        """Cut the window down to ``rects`` (rounded), invisible outside them."""
        if sys.platform != "win32" or not self.hwnd or not rects:
            return
        import ctypes

        gdi32, user32 = ctypes.windll.gdi32, ctypes.windll.user32
        gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
        gdi32.CombineRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        region = None
        for left, top, right, bottom, radius in rects:
            part = gdi32.CreateRoundRectRgn(left, top, right + 1, bottom + 1, radius * 2, radius * 2)
            if region is None:
                region = part
            else:
                gdi32.CombineRgn(region, region, part, 2)  # RGN_OR
                gdi32.DeleteObject(part)
        if not user32.SetWindowRgn(self.hwnd, region, True):
            gdi32.DeleteObject(region)  # the window only owns a region it accepted

    def _apply_shape(self) -> None:
        width, height = self._target_size()
        self._set_region(shape_rects((width, height), self._dpi() / 96, self._card, self._up))

    def _target_size(self) -> tuple[int, int]:
        dpi = self._dpi()
        width, height = self._expanded or self._base
        return _scaled(width, dpi), _scaled(height, dpi)

    def _current(self) -> native_geometry.Rect | None:
        return native_geometry.window_rect(self.hwnd)

    def _base_size(self) -> tuple[int, int]:
        dpi = self._dpi()
        return _scaled(self._base[0], dpi), _scaled(self._base[1], dpi)

    def _place_session(self) -> tuple[int, int, bool]:
        """Position of the grown window, always computed from the collapsed anchor."""
        width, height = self._target_size()
        base_w, base_h = self._base_size()
        return native_geometry.place_expanded(
            (self._anchor[0], self._anchor[1], base_w, base_h),
            (width, height),
            native_geometry.enum_monitors(),
        )

    def fit(self, *, keep_visible: bool = True) -> None:
        """Re-apply the wanted size (DPI changes rescale it) and pull on screen."""
        width, height = self._target_size()
        if self._anchor is not None and self._expanded is not None:
            x, y, self._up = self._place_session()
            self._apply(x, y, width, height)
            self._apply_shape()
            self.check_budget()
            return
        rect = self._current()
        if rect is None:
            self._apply(None, None, width, height)
            self._apply_shape()
            return
        x, y = rect.left, rect.top
        if keep_visible:
            x, y = native_geometry.keep_on_screen(
                (x, y, width, height), native_geometry.enum_monitors()
            )
        self._apply(x, y, width, height)
        self._apply_shape()
        self.check_budget()

    def set_base(self, width: float, height: float = TAB_HEIGHT) -> None:
        """The window's content width changed (a refresh added or dropped a segment)."""
        width = clamp_number(width, 120, 2400)
        with self._lock:
            self._base = (width, float(height))
            if self._expanded is not None:
                self._expanded = (max(width, self._expanded[0]), self._expanded[1])
            self.fit()

    # -- the monitor's width budget ------------------------------------------

    def _budget_now(self) -> float | None:
        monitors = native_geometry.enum_monitors()
        if not monitors:
            return None
        rect = self._current()
        if self._anchor is not None:
            base_w, base_h = self._base_size()
            rect = native_geometry.Rect(
                self._anchor[0], self._anchor[1], self._anchor[0] + base_w, self._anchor[1] + base_h
            )
        if rect is not None:
            monitor = native_geometry.monitor_for(rect, monitors)
        else:
            monitor = next((m for m in monitors if m.primary), None) or monitors[0]
        scale = max(self._dpi(), 1) / 96
        return monitor.work.width / scale - BUDGET_MARGIN

    def logical_budget(self) -> float | None:
        """Logical width the tab may use on its current monitor (None: unknown)."""
        budget = self._budget_now()
        self._last_budget = budget
        return budget

    def check_budget(self) -> None:
        """A move or DPI change may give the tab a different monitor budget:
        ask for a fresh layout when it did."""
        callback = self.on_reflow
        if callback is None or self._reflowing:
            return
        budget = self._budget_now()
        if budget is None or (self._last_budget is not None and abs(budget - self._last_budget) < 1):
            return
        self._reflowing = True
        try:
            callback()
        except Exception:  # noqa: BLE001 -- a stale layout beats a crashed handler
            LOGGER.debug("Tab reflow failed", exc_info=True)
        finally:
            self._reflowing = False

    def expand(
        self,
        width: float,
        height: float,
        card: tuple[float, float, float] | None = None,
    ) -> dict:
        """Grow downward (or upward near the bottom edge) to hold a hover card.

        ``card`` is the card's ``(x, width, height)`` in logical pixels; the
        window region then shows only the strip and the card. Every expansion
        in one session is placed from the same collapsed anchor, so repeated or
        switching cards never drift the tab.
        """
        width = clamp_number(width, self._base[0], 2400)
        height = clamp_number(height, self._base[1], 1200)
        with self._lock:
            if self._anchor is None:
                rect = self._current()
                if rect is not None:
                    self._anchor = (rect.left, rect.top)
            self._expanded = (width, height)
            self._card = card
            new_w, new_h = self._target_size()
            if self._anchor is None:
                self._up = False
                self._apply(None, None, new_w, new_h)
                self._apply_shape()
                return {"up": False}
            x, y, self._up = self._place_session()
            self._apply(x, y, new_w, new_h)
            self._apply_shape()
            return {"up": self._up}

    def collapse(self) -> None:
        """End the session: back to the strip at the anchor."""
        with self._lock:
            if self._expanded is None:
                return
            anchor = self._anchor
            self._expanded = None
            self._card = None
            self._up = False
            self._anchor = None
            width, height = self._target_size()
            if anchor is None:
                self._apply(None, None, width, height)
            else:
                self._apply(anchor[0], anchor[1], width, height)
            self._apply_shape()

    def drop_dwm_frame(self) -> None:
        """No DWM border or corner rounding: the window region draws the shape.

        Otherwise Windows outlines the whole (stretched) window rectangle.
        """
        if sys.platform != "win32" or not self.hwnd:
            return
        try:
            import ctypes
            from ctypes import wintypes

            setter = ctypes.windll.dwmapi.DwmSetWindowAttribute
            setter.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            for attribute, value in ((33, 1), (34, 0xFFFFFFFE)):  # DONOTROUND, no border
                number = ctypes.c_uint(value)
                setter(self.hwnd, attribute, ctypes.byref(number), ctypes.sizeof(number))
        except (OSError, AttributeError):
            LOGGER.debug("Could not drop the tab's DWM frame", exc_info=True)

    def hide_taskbar_button(self) -> None:
        """Make the tab a tool window (no taskbar button, no Alt+Tab entry).

        ``Form.ShowInTaskbar`` recreates the window handle, which aborts
        WebView2's start-up, so the extended style is changed directly.
        """
        if sys.platform != "win32" or not self.hwnd:
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            gwl_exstyle, tool_window, app_window = -20, 0x00000080, 0x00040000
            style = user32.GetWindowLongW(self.hwnd, gwl_exstyle)
            user32.SetWindowLongW(
                self.hwnd, gwl_exstyle, (style | tool_window) & ~app_window
            )
        except OSError:
            LOGGER.debug("Could not hide the tab's taskbar button", exc_info=True)

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
        if self.quitting:
            return
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
        if self.quitting:
            return
        self.collapse()  # back to the anchor, so the next show is not displaced
        self.tracker.flush()
        with self._lock:
            self.visible = False
        try:
            self.window.hide()
        except Exception:  # noqa: BLE001
            LOGGER.debug("Tab hide skipped", exc_info=True)

    def destroy(self) -> None:
        self.quitting = True
        self.tracker.disable()
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
        self.hide_taskbar_button()
        _apply_win11_chrome(self.window, self._icon_path, dark)
        self.drop_dwm_frame()

        def on_closing(_sender, args) -> None:
            if self.quitting:
                return
            if args.CloseReason in user_closing:
                args.Cancel = True  # Alt+F4 or Close: park the tab (or quit) instead
                on_close_request()
                return
            # Windows shutdown, application exit, owner close: let it through.
            self.quitting = True
            self.tracker.flush()
            self.tracker.disable()

        try:
            import System.Windows.Forms as WinForms  # type: ignore[import-not-found]

            user_closing = (
                WinForms.CloseReason.UserClosing,
                WinForms.CloseReason.TaskManagerClosing,
            )
            native.FormClosing += on_closing
            native.DpiChanged += lambda *_args: self.fit()
        except Exception:  # noqa: BLE001
            LOGGER.debug("Tab form hooks skipped", exc_info=True)


# --- switching ---------------------------------------------------------------


class SurfaceController:
    """Decides which of the two windows is up and remembers it.

    Every transition (shrink to tab, open full, tray toggle, launch restore,
    closing the tab) is a request on one queue that a single worker applies in
    order. The latest absolute request (tab/full) supersedes everything queued
    before it, so the last click wins and the windows can never end up both
    visible. Callers never block and no lock is held across a GUI call, so a
    request made from the UI thread cannot deadlock against the worker.
    """

    ABSOLUTE = ("tab", "full")

    def __init__(self, main_host, tab: TabHost, bridge, store) -> None:
        self.main = main_host
        self.tab = tab
        self.bridge = bridge
        self.store = store
        self._lock = threading.Lock()
        self._pending: list[str] = []
        self._worker_alive = False
        self._closing = False
        self._idle = threading.Event()
        self._idle.set()

    @property
    def surface(self) -> str:
        return self.bridge.surface

    # -- requests (any thread, never blocking) ---------------------------------

    def show_tab(self) -> None:
        self._request("tab")

    def show_full(self) -> None:
        self._request("full")

    def toggle(self) -> None:
        """Tray click: hide whatever is up, or bring the remembered surface back."""
        self._request("toggle")

    def restore_on_launch(self) -> None:
        """The main window is already up for a ``full`` launch; bring the tab up
        (without stealing focus) for a ``tab`` one."""
        self._request("restore")

    def close_tab(self) -> None:
        """Alt+F4 on the tab: park in the tray, or quit when there is none."""
        self._request("close-tab")

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until every queued transition has been applied (tests, shutdown)."""
        return self._idle.wait(timeout)

    def shutdown(self) -> None:
        """Drop queued transitions and take the tab down with the app."""
        with self._lock:
            self._closing = True
            self._pending.clear()
        self.tab.destroy()

    # -- the queue --------------------------------------------------------------

    def _request(self, action: str) -> None:
        with self._lock:
            if self._closing:
                return
            self._pending.append(action)
            self._idle.clear()
            if self._worker_alive:
                return
            self._worker_alive = True
        threading.Thread(target=self._drain, name="burnrate-surface", daemon=True).start()

    def _next(self) -> str | None:
        with self._lock:
            if self._closing or not self._pending:
                self._worker_alive = False
                self._idle.set()
                return None
            last_absolute = max(
                (i for i, a in enumerate(self._pending) if a in self.ABSOLUTE), default=-1
            )
            if last_absolute > 0:
                del self._pending[:last_absolute]  # the later absolute request wins
            return self._pending.pop(0)

    def _drain(self) -> None:
        while True:
            action = self._next()
            if action is None:
                return
            try:
                self._apply(action)
            except Exception:  # noqa: BLE001 -- one bad transition must not stop the rest
                LOGGER.warning("Surface transition %s failed", action, exc_info=True)

    def _apply(self, action: str) -> None:
        if action == "tab":
            self.bridge._remember_surface("tab")
            self.main.hide()
            self.tab.show()
        elif action == "full":
            self.bridge._remember_surface("full")
            self.tab.hide()
            self.main.show()
        elif action == "toggle":
            if self.tab.visible:
                self.tab.hide()
            elif self.main.is_visible():
                self.main.hide()
            elif self.surface == "tab":
                self.tab.show()
            else:
                self.main.show()
        elif action == "restore":
            if self.surface == "tab":
                self.tab.show(activate=False)
        elif action == "close-tab":
            if self.main.can_hide_to_tray():
                self.tab.hide()
            else:
                self.main.quit()
