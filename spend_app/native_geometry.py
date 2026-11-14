"""Remember and restore the native window's rectangle across launches.

Units. pywebview's WinForms backend takes ``x/y/width/height`` in *logical*
pixels and converts with ``int(value * scale)`` where ``scale`` is the DPI of
the window handle at that moment; ``moved``/``resized`` events convert back with
``int(value / scale)``. Both directions truncate, negative coordinates on a
monitor left of or above the primary round toward zero, and ``scale`` can differ
between creation and the later events. None of that round-trips exactly, so this
module never uses pywebview's numbers. It stores the **physical** rectangle that
``GetWindowRect`` returns and puts it back with ``SetWindowPos`` before the
window is first shown. The rectangle is saved together with the window's DPI so
a changed display scale between launches rescales the size instead of
mis-sizing the window.
"""

from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass

from spend_app.native_prefs import PreferenceStore

LOGGER = logging.getLogger(__name__)

PREFERENCE_KEY = "window"
DEFAULT_SIZE = (380, 556)
MIN_SIZE = (336, 440)
#: A restored window must overlap a monitor by at least this much (physical px)
#: in both directions, otherwise the position is treated as off-screen.
MIN_VISIBLE = (48, 32)
SAVE_DELAY = 0.6
_LIMIT = 100_000


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True)
class Monitor:
    rect: Rect
    work: Rect
    primary: bool = False


@dataclass(frozen=True)
class SavedGeometry:
    x: int
    y: int
    width: int
    height: int
    dpi: int

    def as_preference(self) -> dict:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "dpi": self.dpi,
        }


@dataclass(frozen=True)
class RestorePlan:
    width: int
    height: int
    #: ``None`` when the saved position is gone; the window then centres.
    position: tuple[int, int] | None


def _whole(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    number = int(value)
    return number if abs(number) <= _LIMIT else None


def parse_saved(raw) -> SavedGeometry | None:
    """Validate the stored dict; anything malformed means "nothing saved"."""
    if not isinstance(raw, dict):
        return None
    values = {k: _whole(raw.get(k)) for k in ("x", "y", "width", "height", "dpi")}
    if any(v is None for v in values.values()):
        return None
    if values["width"] <= 0 or values["height"] <= 0 or values["dpi"] <= 0:
        return None
    return SavedGeometry(**values)


def min_size_for(dpi: int) -> tuple[int, int]:
    """Minimum size in physical pixels, using the same truncation as pywebview."""
    scale = max(dpi, 1) / 96
    return int(MIN_SIZE[0] * scale), int(MIN_SIZE[1] * scale)


def default_size_for(dpi: int) -> tuple[int, int]:
    scale = max(dpi, 1) / 96
    return int(DEFAULT_SIZE[0] * scale), int(DEFAULT_SIZE[1] * scale)


def _overlap(a: Rect, b: Rect) -> tuple[int, int]:
    return (
        max(0, min(a.right, b.right) - max(a.left, b.left)),
        max(0, min(a.bottom, b.bottom) - max(a.top, b.top)),
    )


def _clamp(value: int, low: int, high: int) -> int:
    # A window larger than the work area pins to its top/left edge.
    return low if high < low else max(low, min(value, high))


def plan_restore(
    saved: SavedGeometry | None, monitors: list[Monitor], dpi: int
) -> RestorePlan | None:
    """Decide the size/position to apply for ``saved`` on today's monitor layout.

    ``dpi`` is the current DPI of the window. Returns ``None`` when nothing was
    saved (the caller keeps pywebview's default size and centring).
    """
    if saved is None:
        return None
    dpi = dpi if dpi > 0 else 96
    ratio = dpi / saved.dpi
    width, height = saved.width, saved.height
    x, y = saved.x, saved.y
    if ratio != 1:
        width, height = round(width * ratio), round(height * ratio)
        x, y = round(x * ratio), round(y * ratio)

    min_w, min_h = min_size_for(dpi)
    width, height = max(width, min_w), max(height, min_h)

    rect = Rect(x, y, x + width, y + height)
    best: Monitor | None = None
    best_area = 0
    for monitor in monitors:
        ow, oh = _overlap(rect, monitor.rect)
        if ow >= MIN_VISIBLE[0] and oh >= MIN_VISIBLE[1] and ow * oh > best_area:
            best, best_area = monitor, ow * oh

    if best is None:
        # Off-screen now (monitor unplugged / layout changed): keep the size,
        # drop the position, and never exceed the primary work area.
        primary = next((m for m in monitors if m.primary), None) or (
            monitors[0] if monitors else None
        )
        if primary is not None:
            width = max(min_w, min(width, primary.work.width))
            height = max(min_h, min(height, primary.work.height))
        return RestorePlan(width, height, None)

    work = best.work
    width = max(min_w, min(width, work.width))
    height = max(min_h, min(height, work.height))
    x = _clamp(x, work.left, work.right - width)
    y = _clamp(y, work.top, work.bottom - height)
    return RestorePlan(width, height, (x, y))


# --- Win32 plumbing (exercised by the unit tests only through fakes) ---------


def enum_monitors() -> list[Monitor]:
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found: list[Monitor] = []
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )

    def callback(hmonitor, _hdc, _rect, _data):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            r, w = info.rcMonitor, info.rcWork
            found.append(
                Monitor(
                    Rect(r.left, r.top, r.right, r.bottom),
                    Rect(w.left, w.top, w.right, w.bottom),
                    bool(info.dwFlags & 1),  # MONITORINFOF_PRIMARY
                )
            )
        return True

    user32.EnumDisplayMonitors.argtypes = [
        wintypes.HDC,
        ctypes.c_void_p,
        callback_type,
        wintypes.LPARAM,
    ]
    user32.EnumDisplayMonitors(None, None, callback_type(callback), 0)
    return found


def window_dpi(hwnd: int) -> int:
    if sys.platform != "win32" or not hwnd:
        return 96
    import ctypes

    try:
        dpi = int(ctypes.windll.user32.GetDpiForWindow(hwnd))
    except (OSError, AttributeError):
        return 96
    return dpi if dpi > 0 else 96


def window_rect(hwnd: int) -> Rect | None:
    if sys.platform != "win32" or not hwnd:
        return None
    import ctypes
    from ctypes import wintypes

    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return Rect(rect.left, rect.top, rect.right, rect.bottom)


def is_restorable_state(hwnd: int) -> bool:
    """True only for a visible, normal (not minimized, not maximized) window."""
    if sys.platform != "win32" or not hwnd:
        return False
    import ctypes

    user32 = ctypes.windll.user32
    return bool(
        user32.IsWindow(hwnd)
        and user32.IsWindowVisible(hwnd)
        and not user32.IsIconic(hwnd)
        and not user32.IsZoomed(hwnd)
    )


def snapshot(hwnd: int) -> SavedGeometry | None:
    """The window's current rectangle, or ``None`` when it must not be saved."""
    if not is_restorable_state(hwnd):
        return None
    rect = window_rect(hwnd)
    if rect is None or rect.width <= 0 or rect.height <= 0:
        return None
    return SavedGeometry(rect.left, rect.top, rect.width, rect.height, window_dpi(hwnd))


def apply_before_show(native, plan: RestorePlan | None) -> None:
    """Place the WinForms ``native`` form; call on the UI thread before ``Show``.

    ``StartPosition`` is switched to Manual when a position is restored, since
    WinForms otherwise re-centres the form during its first ``Show``.
    """
    if plan is None:
        return
    import System.Drawing  # type: ignore[import-not-found]
    import System.Windows.Forms as WinForms  # type: ignore[import-not-found]

    native.Size = System.Drawing.Size(plan.width, plan.height)
    if plan.position is not None:
        native.StartPosition = WinForms.FormStartPosition.Manual
        native.Location = System.Drawing.Point(*plan.position)
        _set_window_pos(int(native.Handle.ToInt64()), plan)


def _set_window_pos(hwnd: int, plan: RestorePlan) -> None:
    if sys.platform != "win32" or plan.position is None:
        return
    import ctypes

    swp_nozorder, swp_noactivate = 0x0004, 0x0010
    ctypes.windll.user32.SetWindowPos(
        hwnd,
        0,
        plan.position[0],
        plan.position[1],
        plan.width,
        plan.height,
        swp_nozorder | swp_noactivate,
    )


class GeometryTracker:
    """Debounced writer of the window rectangle into the preference store."""

    def __init__(
        self,
        store: PreferenceStore,
        hwnd_getter,
        *,
        snapshotter=snapshot,
        delay: float = SAVE_DELAY,
    ) -> None:
        self._store = store
        self._hwnd_getter = hwnd_getter
        self._snapshotter = snapshotter
        self._delay = delay
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._last: SavedGeometry | None = None
        self._enabled = True

    def disable(self) -> None:
        with self._lock:
            self._enabled = False
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def notify_changed(self) -> None:
        """Called on every move/resize; coalesces into one write."""
        with self._lock:
            if not self._enabled:
                return
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._delay, self.flush)
            self._timer.daemon = True
            self._timer.start()

    def flush(self) -> bool:
        """Write the current rectangle now. False when it must not be saved."""
        with self._lock:
            if not self._enabled:
                return False
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        current = self._snapshotter(int(self._hwnd_getter() or 0))
        if current is None:
            return False
        if current != self._last:
            self._store.update({PREFERENCE_KEY: current.as_preference()})
            self._last = current
        return True
