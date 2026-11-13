"""BURNRATE's Wealthfront-inspired light fintech design system for native desktop widgets."""

from __future__ import annotations

import sys

from spend_app.provider_logos import ICONS_1X, ICONS_2X, ICONS_15X, resolve_logo_key

# Wealthfront iOS v2026 light fintech palette
BG = "#f5f5fa"
SURFACE = "#ffffff"
RAISED = "#eeedf9"
HOVER = "#e3e1f5"
LINE = "#e4e4ee"
DIVIDER = "#ececf4"
TEXT = "#18172b"
MUTED = "#6e6d7e"
SUBTLE = "#8a899b"
ACCENT = "#4840bb"
ACCENT_HOVER = "#3d35a8"
ACCENT_PRESSED = "#332c91"
SEGMENT_BG = "#ebeaf5"
GREEN = "#1f7a63"
GREEN_BG = "#e6f4f0"
WARNING = "#9a4d14"
WARNING_BG = "#fdf1e6"

FONT = "Segoe UI Variable Text" if sys.platform == "win32" else "Helvetica"
DISPLAY = "Segoe UI Variable Display" if sys.platform == "win32" else FONT

TOOL_COLORS = {
    "claude-code": "#d97757",
    "claude": "#d97757",
    "anthropic": "#c96442",
    "codex": "#4840bb",
    "openai": "#10a37f",
    "opencode": "#1f7a63",
    "grok": "#18172b",
    "xai": "#36344d",
    "muse": "#0064e0",
    "cursor": "#5b53d6",
    "zcode": "#2563eb",
    "antigravity": "#4285f4",
    "gemini": "#4285f4",
    "traycer": "#7c3aed",
    "openrouter": "#6366f1",
    "custom": "#8a899b",
}


def tool_color(key: object) -> str:
    name = str(key or "").strip().lower()
    if name in TOOL_COLORS:
        return TOOL_COLORS[name]
    resolved = resolve_logo_key(name)
    return TOOL_COLORS.get(resolved, ACCENT)


def dark_titlebar(window):
    """Apply Windows 11 light DWM surface, rounded corners, and subtle hairline border."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        window.update_idletasks()
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        handle = user32.GetParent(window.winfo_id()) or window.winfo_id()
        setter = ctypes.windll.dwmapi.DwmSetWindowAttribute
        setter.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        # DWMWA_USE_IMMERSIVE_DARK_MODE = 20 (19 on older Win10): 0 = light surface
        light_mode = ctypes.c_int(0)
        if setter(handle, 20, ctypes.byref(light_mode), ctypes.sizeof(light_mode)) != 0:
            setter(handle, 19, ctypes.byref(light_mode), ctypes.sizeof(light_mode))
        # DWMWA_WINDOW_CORNER_PREFERENCE = 33 -> DWMWCP_ROUND (2)
        round_corners = ctypes.c_int(2)
        setter(handle, 33, ctypes.byref(round_corners), ctypes.sizeof(round_corners))
        # DWMWA_BORDER_COLOR = 34 -> COLORREF 0x00BBGGRR for #E4E4EE
        border_color = ctypes.c_int(0x00EEE4E4)
        setter(handle, 34, ctypes.byref(border_color), ctypes.sizeof(border_color))
    except (OSError, AttributeError):
        pass


def install(root):
    from tkinter import ttk

    scale = ui_scale(root)
    root.configure(background=BG)
    root.option_add("*TCombobox*Listbox.font", (FONT, 10))
    root.option_add("*TCombobox*Listbox.background", SURFACE)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", RAISED)
    root.option_add("*TCombobox*Listbox.selectForeground", ACCENT)
    root.option_add("*TCombobox*Listbox.highlightThickness", 0)

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(
        ".", background=BG, foreground=TEXT, font=(FONT, 10), bordercolor=LINE
    )
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=SURFACE)

    # Canvas-level labels
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED)
    style.configure("Caption.TLabel", background=BG, foreground=MUTED, font=(FONT, 9))
    style.configure(
        "Title.TLabel", background=BG, foreground=TEXT, font=(DISPLAY, 20, "bold")
    )
    style.configure(
        "Section.TLabel", background=BG, foreground=TEXT, font=(FONT, 11, "bold")
    )

    # Elevated white card labels
    style.configure("Card.TLabel", background=SURFACE, foreground=TEXT)
    style.configure(
        "CardEyebrow.TLabel",
        background=SURFACE,
        foreground=SUBTLE,
        font=(FONT, 9, "bold"),
    )
    style.configure(
        "CardSection.TLabel",
        background=SURFACE,
        foreground=TEXT,
        font=(DISPLAY, 12, "bold"),
    )
    style.configure(
        "CardMuted.TLabel",
        background=SURFACE,
        foreground=MUTED,
        font=(FONT, 10, "bold"),
    )
    style.configure(
        "CardCaption.TLabel",
        background=SURFACE,
        foreground=MUTED,
        font=(FONT, 9),
    )
    style.configure(
        "Value.TLabel",
        background=SURFACE,
        foreground=TEXT,
        font=(DISPLAY, 42, "bold"),
    )
    style.configure(
        "Metric.TLabel",
        background=SURFACE,
        foreground=TEXT,
        font=(DISPLAY, 16, "bold"),
    )

    # Status pills & notice banner
    style.configure(
        "Live.TLabel",
        background=GREEN_BG,
        foreground=GREEN,
        font=(FONT, 9, "bold"),
        padding=(10, 4),
    )
    style.configure(
        "WarnLive.TLabel",
        background=WARNING_BG,
        foreground=WARNING,
        font=(FONT, 9, "bold"),
        padding=(10, 4),
    )
    style.configure(
        "Notice.TLabel",
        background=WARNING_BG,
        foreground=WARNING,
        font=(FONT, 9),
        padding=(12, 8),
    )

    # Buttons
    style.configure(
        "TButton",
        background=SURFACE,
        foreground=TEXT,
        bordercolor=LINE,
        lightcolor=SURFACE,
        darkcolor=SURFACE,
        padding=(13, 7),
        font=(FONT, 9, "bold"),
        focuscolor=ACCENT,
        focusthickness=1,
        borderwidth=1,
    )
    style.map(
        "TButton",
        background=[("disabled", BG), ("pressed", HOVER), ("active", RAISED)],
        foreground=[("disabled", SUBTLE), ("active", ACCENT)],
        bordercolor=[("focus", ACCENT), ("active", "#cbc8ee")],
    )
    style.configure(
        "Quiet.TButton",
        background=SURFACE,
        bordercolor=LINE,
        lightcolor=SURFACE,
        darkcolor=SURFACE,
        foreground=TEXT,
        font=(FONT, 9, "bold"),
        padding=(11, 5),
        borderwidth=1,
    )
    style.map(
        "Quiet.TButton",
        background=[("active", RAISED), ("pressed", HOVER)],
        foreground=[("active", ACCENT)],
        bordercolor=[("focus", ACCENT), ("active", "#cbc8ee")],
    )
    style.configure(
        "Window.TButton",
        background=BG,
        foreground=MUTED,
        bordercolor=BG,
        lightcolor=BG,
        darkcolor=BG,
        borderwidth=0,
        padding=(7, 2),
        font=(FONT, 12),
    )
    style.map(
        "Window.TButton",
        background=[("active", SEGMENT_BG), ("pressed", HOVER)],
        foreground=[("active", TEXT)],
        bordercolor=[("focus", ACCENT), ("active", SEGMENT_BG)],
    )
    style.configure(
        "Close.Window.TButton",
        background=BG,
        foreground=MUTED,
        bordercolor=BG,
        lightcolor=BG,
        darkcolor=BG,
        borderwidth=0,
        padding=(7, 2),
        font=(FONT, 12),
    )
    style.map(
        "Close.Window.TButton",
        background=[("pressed", "#c93b3b"), ("active", "#e5484d")],
        foreground=[("active", "#ffffff"), ("pressed", "#ffffff")],
        bordercolor=[("focus", ACCENT), ("active", "#e5484d")],
    )
    style.configure(
        "Accent.TButton",
        background=ACCENT,
        foreground="#ffffff",
        bordercolor=ACCENT,
        lightcolor=ACCENT,
        darkcolor=ACCENT,
        font=(FONT, 9, "bold"),
        padding=(14, 8),
    )
    style.map(
        "Accent.TButton",
        background=[
            ("active", ACCENT_HOVER),
            ("pressed", ACCENT_PRESSED),
            ("disabled", SEGMENT_BG),
        ],
        foreground=[("disabled", SUBTLE)],
        bordercolor=[("focus", TEXT), ("active", ACCENT_HOVER)],
    )

    # Wealthfront Governor Bay segmented time-range pills
    style.configure(
        "Segment.TRadiobutton",
        background=SEGMENT_BG,
        foreground=MUTED,
        padding=(13, 6),
        font=(FONT, 9, "bold"),
        focuscolor=ACCENT,
        borderwidth=0,
    )
    style.layout(
        "Segment.TRadiobutton",
        [
            (
                "Radiobutton.padding",
                {
                    "sticky": "nswe",
                    "children": [
                        (
                            "Radiobutton.focus",
                            {
                                "sticky": "nswe",
                                "children": [("Radiobutton.label", {"sticky": "nswe"})],
                            },
                        )
                    ],
                },
            )
        ],
    )
    style.map(
        "Segment.TRadiobutton",
        background=[("selected", ACCENT), ("active", HOVER)],
        foreground=[("selected", "#ffffff"), ("active", TEXT)],
    )
    style.configure("Segment.TFrame", background=SEGMENT_BG)

    # Inputs & dropdowns
    style.configure(
        "TCombobox",
        fieldbackground=SURFACE,
        background=SURFACE,
        foreground=TEXT,
        arrowcolor=MUTED,
        bordercolor=LINE,
        lightcolor=LINE,
        darkcolor=LINE,
        padding=(10, 6),
        arrowsize=11,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", SURFACE)],
        foreground=[("readonly", TEXT)],
        selectbackground=[("readonly", SURFACE)],
        selectforeground=[("readonly", TEXT)],
        bordercolor=[("focus", ACCENT), ("active", "#cbc8ee")],
    )
    style.configure(
        "TEntry",
        fieldbackground=SURFACE,
        foreground=TEXT,
        insertcolor=TEXT,
        bordercolor=LINE,
        lightcolor=LINE,
        darkcolor=LINE,
        padding=9,
    )
    style.map(
        "TEntry",
        bordercolor=[("focus", ACCENT)],
        fieldbackground=[("readonly", BG)],
    )
    style.configure(
        "TCheckbutton",
        background=SURFACE,
        foreground=TEXT,
        indicatorbackground=SURFACE,
        indicatorforeground=ACCENT,
        focuscolor=ACCENT,
        padding=(0, 7),
    )
    style.map(
        "TCheckbutton",
        background=[("active", SURFACE)],
        indicatorbackground=[("selected", RAISED)],
    )

    # White card tables with soft lavender active selection
    style.configure(
        "Treeview",
        background=SURFACE,
        fieldbackground=SURFACE,
        foreground=TEXT,
        borderwidth=0,
        relief="flat",
        rowheight=round(44 * scale),
        font=(FONT, 10),
    )
    style.map(
        "Treeview",
        background=[("selected", RAISED)],
        foreground=[("selected", TEXT)],
    )
    style.configure(
        "Treeview.Heading",
        background=SURFACE,
        foreground=SUBTLE,
        borderwidth=0,
        relief="flat",
        font=(FONT, 8, "bold"),
        padding=(8, 8),
    )
    style.map("Treeview.Heading", background=[("active", SURFACE)])
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.configure(
        "Usage.Treeview",
        background=SURFACE,
        fieldbackground=SURFACE,
        foreground=TEXT,
        rowheight=round(46 * scale),
        font=(FONT, 10),
    )
    style.configure(
        "Usage.Treeview.Heading",
        background=SURFACE,
        foreground=SUBTLE,
        borderwidth=0,
        relief="flat",
        font=(FONT, 8, "bold"),
        padding=(6, 8),
    )
    style.map("Usage.Treeview.Heading", background=[("active", SURFACE)])

    # Scrollbar
    style.configure(
        "Vertical.TScrollbar",
        background="#d5d4e4",
        troughcolor=SURFACE,
        borderwidth=0,
        arrowcolor=MUTED,
        arrowsize=10,
        width=6,
    )
    style.map("Vertical.TScrollbar", background=[("active", "#b4b2cc")])
    style.layout(
        "Vertical.TScrollbar",
        [
            (
                "Vertical.Scrollbar.trough",
                {
                    "sticky": "ns",
                    "children": [
                        ("Vertical.Scrollbar.thumb", {"sticky": "nswe", "expand": True})
                    ],
                },
            )
        ],
    )

    # Notebook tabs in Settings
    style.configure(
        "TNotebook",
        background=BG,
        borderwidth=0,
        bordercolor=BG,
        lightcolor=BG,
        darkcolor=BG,
        tabmargins=(0, 0, 0, 12),
    )
    style.configure(
        "TNotebook.Tab",
        background=SEGMENT_BG,
        foreground=MUTED,
        padding=(16, 8),
        font=(FONT, 9, "bold"),
        borderwidth=0,
        bordercolor=BG,
        lightcolor=BG,
        darkcolor=BG,
        focuscolor=ACCENT,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", ACCENT), ("active", HOVER)],
        foreground=[("selected", "#ffffff"), ("active", TEXT)],
    )

    style.configure(
        "TSeparator",
        background=DIVIDER,
        lightcolor=DIVIDER,
        darkcolor=DIVIDER,
        bordercolor=DIVIDER,
    )
    root.after(0, lambda: dark_titlebar(root))


def card(parent, *, padding=(18, 16, 18, 16)):
    """Create an elevated white Wealthfront card with a crisp 1px #E4E4EE border."""
    import tkinter as tk
    from tkinter import ttk

    shell = tk.Frame(
        parent,
        background=SURFACE,
        highlightbackground=LINE,
        highlightcolor=LINE,
        highlightthickness=1,
        bd=0,
    )
    body = ttk.Frame(shell, style="Card.TFrame", padding=padding)
    body.pack(fill="both", expand=True)
    return shell, body


def _logo_bucket(scale: float) -> dict[str, str]:
    if scale >= 1.75:
        return ICONS_2X
    if scale >= 1.25:
        return ICONS_15X
    return ICONS_1X


def brand(parent):
    import tkinter as tk
    from tkinter import ttk

    row = ttk.Frame(parent)
    bucket = _logo_bucket(ui_scale(parent))
    image = tk.PhotoImage(master=parent, data=bucket["brand"])
    mark = ttk.Label(row, image=image)
    mark.image = image
    mark.pack(side="left", padx=(0, 9))
    ttk.Label(
        row,
        text="BURNRATE",
        font=(DISPLAY, 11, "bold"),
        foreground=TEXT,
    ).pack(side="left")
    return row


def ui_scale(root):
    return max(1.0, float(root.tk.call("tk", "scaling")) / (96 / 72))


def size_window(root, width, height, minimum_width, minimum_height):
    # Fonts use points; geometry uses pixels. Fit both high-DPI displays and
    # smaller work areas without making the footer unreachable.
    scale = ui_scale(root)
    available_width = max(320, root.winfo_screenwidth() - 80)
    available_height = max(360, root.winfo_screenheight() - 90)
    root.geometry(
        f"{min(round(width * scale), available_width)}x{min(round(height * scale), available_height)}"
    )
    root.minsize(
        min(round(minimum_width * scale), available_width),
        min(round(minimum_height * scale), available_height),
    )


def notice(parent):
    import tkinter as tk
    from tkinter import ttk

    shell = tk.Frame(
        parent,
        background=WARNING_BG,
        highlightbackground="#f3d5b5",
        highlightcolor="#f3d5b5",
        highlightthickness=1,
        bd=0,
    )

    class Notice(ttk.Label):
        def configure(self, cnf=None, **kwargs):
            result = super().configure(cnf, **kwargs)
            if "text" in kwargs:
                if kwargs["text"]:
                    shell.pack(fill="x", pady=(0, 10))
                else:
                    shell.pack_forget()
            return result

    label = Notice(shell, text="", wraplength=540, style="Notice.TLabel")
    label.pack(fill="x")
    return label


def tool_icon(root, key):
    """Return a crisp, DPI-matched vector-rendered provider brand badge."""
    import tkinter as tk

    bucket = _logo_bucket(ui_scale(root))
    logo_key = resolve_logo_key(key)
    data = bucket.get(logo_key) or bucket["custom"]
    return tk.PhotoImage(master=root, data=data)
