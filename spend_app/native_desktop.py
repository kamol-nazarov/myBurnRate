"""Native BURNRATE usage window. Uses Tk/ttk controls, never a browser.

HTTP is internal IPC with the existing local collector and paired hub. Every
request runs on a worker; only the Tk thread reads or changes widgets.
"""

from __future__ import annotations

import copy
import json
import math
import os
import queue
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from spend_app import native_theme as theme
from spend_app.config import Settings, load_settings, load_user_env
from spend_app.platform_paths import NO_WINDOW
from spend_app.timeutil import parse_utc

PERIOD_LABELS = {
    "today": "Today",
    "week": "7 days",
    "month": "This month",
    "all": "All time",
}


def tokens(value) -> str:
    try:
        number = float(value)
    except (ValueError, TypeError):
        return "—"
    if not math.isfinite(number):
        return "—"
    for size, label in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(number) >= size:
            return f"{number / size:.2f}{label}"
    return f"{number:,.0f}"


def money(value, complete=True) -> str:
    try:
        number = float(value) if value is not None else math.nan
    except (ValueError, TypeError):
        return "Unpriced"
    if not math.isfinite(number) or number < 0:
        return "Unpriced"
    text = "<$0.01" if 0 < number < 0.005 else f"${number:,.2f}"
    if complete:
        return text
    return ("Partial " if text.startswith("<") else "≥") + text


class DesktopError(RuntimeError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class SourceMutation:
    """One dialog's retryable source change; secrets stay in memory only."""

    def __init__(self, client):
        self.client = client
        self.values = None
        self.request_id = None

    def send(self, values):
        if values != self.values:
            self.values = dict(values)
            self.request_id = str(uuid.uuid4())
        return self.client.request(
            "/api/connections", {**self.values, "requestId": self.request_id}
        )


class NativeClient:
    def __init__(self, settings: Settings, port=17331):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid local tracker port")
        self.settings = settings
        self.base_url = f"http://127.0.0.1:{port}"
        self.port = port
        self.process = None

    def request(self, path: str, body: dict | None = None) -> dict:
        if not path.startswith("/api/"):
            raise ValueError("Only local BURNRATE API paths are supported")
        headers = {"Origin": self.base_url, "X-BURNRATE-Request": "1"}
        token = os.getenv("BURNRATE_ACCESS_TOKEN", "").strip()
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            with httpx.Client(
                timeout=httpx.Timeout(20, connect=2),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = (
                    client.get(self.base_url + path, headers=headers)
                    if body is None
                    else client.post(self.base_url + path, json=body, headers=headers)
                )
                if response.status_code == 404 and path.startswith("/api/spend/usage"):
                    raise DesktopError(
                        "Restart BURNRATE's local tracker with this version to load the native usage view."
                    )
                if response.status_code in (401, 403):
                    raise DesktopError(
                        "BURNRATE could not authenticate this window. Check the local access-token configuration."
                    )
                try:
                    data = response.json()
                except ValueError:
                    raise DesktopError(
                        "The tracker returned an invalid response."
                    ) from None
                if not response.is_success:
                    message = data.get("error") if isinstance(data, dict) else None
                    raise DesktopError(
                        message
                        if isinstance(message, str)
                        else "The tracker is busy. Please retry.",
                        response.status_code,
                    )
                if not isinstance(data, dict):
                    raise DesktopError("The tracker returned an invalid response.")
                return data
        except httpx.HTTPError:
            raise DesktopError(
                "The local tracker is unavailable. Showing the last recorded result."
            ) from None

    def snapshot(self, period: str, machine: str) -> dict:
        from urllib.parse import urlencode

        if period not in PERIOD_LABELS:
            raise ValueError("Invalid usage period")
        data = self.request(
            "/api/spend/usage?" + urlencode({"period": period, "machine": machine})
        )
        if not isinstance(data.get("total"), dict) or not isinstance(
            data.get("tools"), list
        ):
            raise DesktopError("The tracker returned an invalid usage result.")
        for tool in data["tools"]:
            if not isinstance(tool, dict) or not isinstance(tool.get("models"), list):
                raise DesktopError("The tracker returned an invalid tool result.")
            if any(not isinstance(model, dict) for model in tool["models"]):
                raise DesktopError("The tracker returned an invalid model result.")
        return data

    def ensure_service(self) -> None:
        def health():
            try:
                with httpx.Client(
                    timeout=2, trust_env=False, follow_redirects=False
                ) as client:
                    result = client.get(self.base_url + "/healthz?supervisor=1")
                    if result.status_code != 200:
                        raise DesktopError(
                            "Another service is using BURNRATE's local port."
                        )
                    data = result.json()
                    if (
                        not isinstance(data, dict)
                        or data.get("application") != "burnrate"
                        or data.get("status") != "ok"
                        or type(data.get("pid")) is not int
                        or data["pid"] <= 0
                    ):
                        raise DesktopError(
                            "Another service is using BURNRATE's local port."
                        )
                    return True
            except (httpx.ConnectError, httpx.ConnectTimeout):
                return False
            except (httpx.HTTPError, ValueError):
                raise DesktopError(
                    "BURNRATE's local tracker is not answering correctly."
                ) from None

        if health():
            return
        env = dict(
            os.environ,
            BURNRATE_PORT=str(self.port),
            SPEND_DATABASE_PATH=str(self.settings.database_path),
            BURNRATE_USAGE_ONLY="1",
        )
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--service", "--port", str(self.port)]
        else:
            python = Path(sys.executable)
            windowless = (
                python.with_name("pythonw.exe") if sys.platform == "win32" else python
            )
            command = [
                str(windowless if windowless.is_file() else python),
                "-m",
                "spend_app.service",
                "--port",
                str(self.port),
            ]
        self.process = subprocess.Popen(
            command,
            env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
            start_new_session=sys.platform != "win32",
        )
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if health():
                return
            if self.process.poll() is not None:
                # An existing service can have won its startup mutex.
                if health():
                    return
                raise DesktopError(
                    "The tracker could not start. See BURNRATE's dashboard.log."
                )
            time.sleep(0.25)
        raise DesktopError(
            "The tracker is taking longer to start. Please retry shortly."
        )


def demo_report(period="today", machine="all") -> dict:
    rows = []
    for key, name, count, value in (
        ("claude-code", "Claude Code", 2_900_000, 52.10),
        ("codex", "Codex", 3_100_000, 41.82),
        ("opencode", "OpenCode", 1_400_000, 18.60),
        ("muse", "Muse Code", 700_000, 9.72),
        ("grok", "Grok Build", 100_000, 5.10),
    ):
        row = {
            "key": key,
            "name": name,
            "tokens": count,
            "valueUsd": value,
            "complete": True,
            "events": 10,
            "unpricedEvents": 0,
            "organization": False,
            "input": int(count * 0.4),
            "cached": int(count * 0.5),
            "writes": 0,
            "output": int(count * 0.1),
            "unclassified": 0,
            "reasoning": None,
            "reasoningComplete": False,
        }
        row["models"] = [{**row, "key": key + ":sample", "name": "Example model"}]
        rows.append(row)
    return {
        "period": period,
        "periodLabel": PERIOD_LABELS[period],
        "scope": "all-computers",
        "total": {
            "valueUsd": 127.34,
            "tokens": 8_200_000,
            "events": 50,
            "complete": True,
            "unpricedEvents": 0,
        },
        "tools": rows,
        "timezone": "UTC",
        "generatedAt": datetime.now(UTC).isoformat(),
        "machines": [
            {"id": "local", "name": "Desktop", "online": True},
            {"id": "example-laptop", "name": "Laptop", "online": True},
        ],
    }


class DemoClient:
    def ensure_service(self):
        pass

    def snapshot(self, period, machine):
        return copy.deepcopy(demo_report(period, machine))

    def request(self, path, body=None):
        if body is not None:
            raise DesktopError("Settings changes are disabled in the sample-data demo.")
        if path.startswith("/api/connections"):
            return {
                "connections": [
                    {
                        "source": r["key"],
                        "name": r["name"],
                        "state": "receiving_usage",
                        "location": "Example location",
                    }
                    for r in demo_report()["tools"]
                ]
            }
        if path.startswith("/api/machines"):
            return {"machines": demo_report()["machines"]}
        return {"setup": {"role": "hub"}}


class UsageWindow:
    def __init__(self, root, client, *, preferences: Path | None = None, demo=False):
        import tkinter as tk
        from tkinter import ttk

        self.tk, self.ttk, self.root, self.client = tk, ttk, root, client
        self.preferences = preferences
        self.demo = demo
        self.closed = False
        self.worker = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="burnrate-native"
        )
        self.messages = queue.Queue()
        self.generation = 0
        self.pending = False
        self.again = False
        self.report = None
        self.items = {}
        self.machine_ids = {"All computers": "all"}
        self.selected_machine = "all"
        self.settings_window = None
        self.compact = False
        self.value_text = "Loading…"
        saved = self._read_preferences()
        self.period = tk.StringVar(
            value=saved.get("period", "today")
            if saved.get("period") in PERIOD_LABELS
            else "today"
        )
        self.computer = tk.StringVar(value="All computers")
        self.topmost = tk.BooleanVar(value=bool(saved.get("topmost", False)))
        root.title("BURNRATE" + (" · Sample data" if demo else ""))
        theme.size_window(root, 640, 800, 580, 620)
        root.attributes("-topmost", self.topmost.get())
        resource_root = Path(__file__).resolve().parents[1]
        for icon in (
            resource_root / "burnrate.ico",
            resource_root / "burnrate-assets" / "burnrate.ico",
            resource_root / "src-tauri" / "icons" / "icon.ico",
        ):
            if sys.platform == "win32" and icon.is_file():
                root.iconbitmap(str(icon))
                break
        root.protocol("WM_DELETE_WINDOW", self.close)
        theme.install(root)
        frame = ttk.Frame(root, padding=(28, 24, 28, 18))
        frame.pack(fill="both", expand=True)
        header = ttk.Frame(frame)
        self.header_frame = header
        header.pack(fill="x", pady=(0, 12))
        theme.brand(header).pack(side="left")
        self.live_status = ttk.Label(
            header, text="●  Starting tracker", style="Live.TLabel"
        )
        self.live_status.pack(side="right")
        ttk.Button(
            header, text="Settings", style="Quiet.TButton", command=self.settings
        ).pack(side="right", padx=(0, 10))
        heading = ttk.Frame(frame)
        self.heading_frame = heading
        heading.pack(fill="x", pady=(0, 12))
        ttk.Label(heading, text="Your usage", style="Title.TLabel").pack(side="left")
        filters = ttk.Frame(frame)
        filters.pack(fill="x", pady=(0, 18))
        periods = ttk.Frame(filters, padding=4, style="Segment.TFrame")
        periods.pack(side="left")
        for index, (key, label) in enumerate(PERIOD_LABELS.items()):
            ttk.Radiobutton(
                periods,
                text=label,
                variable=self.period,
                value=key,
                command=self.change_scope,
                style="Segment.TRadiobutton",
            ).grid(row=0, column=index, sticky="nsew")
        self.machine_picker = ttk.Combobox(
            filters,
            textvariable=self.computer,
            values=["All computers"],
            state="readonly",
            width=18,
        )
        self.machine_picker.pack(side="right")
        self.machine_picker.bind("<<ComboboxSelected>>", lambda _: self.change_scope())
        ttk.Label(frame, text="API-equivalent value", style="Muted.TLabel").pack(
            anchor="w"
        )
        value_row = ttk.Frame(frame)
        value_row.pack(fill="x", pady=(0, 2))
        self.value = ttk.Label(value_row, text="Loading…", style="Value.TLabel")
        self.value.pack(side="left")
        ttk.Label(value_row, text="USD", style="Muted.TLabel").pack(
            side="left", anchor="s", padx=(10, 0), pady=(0, 11)
        )
        self.count = ttk.Label(
            frame, text="Measured tokens, valued at API rates.", style="Caption.TLabel"
        )
        self.count.pack(anchor="w")
        metrics = ttk.Frame(frame)
        self.metrics_frame = metrics
        metrics.pack(fill="x", pady=(16, 16))
        self.metric_labels = []
        for index, label in enumerate(
            ("Recorded tokens", "Active tools", "Computers in view")
        ):
            cell = ttk.Frame(metrics)
            cell.grid(row=0, column=index, sticky="w")
            metrics.columnconfigure(index, weight=1, uniform="metrics")
            number = ttk.Label(cell, text="—", style="Metric.TLabel")
            number.pack(anchor="w")
            ttk.Label(cell, text=label, style="Caption.TLabel").pack(
                anchor="w", pady=(3, 0)
            )
            self.metric_labels.append(number)
        ttk.Separator(frame).pack(fill="x", pady=(0, 10))
        notice_row = ttk.Frame(frame)
        notice_row.pack(fill="x")
        self.notice = theme.notice(notice_row)
        table_frame = ttk.Frame(frame)
        self.tree = ttk.Treeview(
            table_frame,
            columns=("tokens", "value"),
            style="Usage.Treeview",
            show="tree headings",
            selectmode="browse",
        )
        self.tree.heading("#0", text="Usage by tool", anchor="w")
        self.tree.heading("tokens", text="Tokens")
        self.tree.heading("value", text="API value")
        self.tree.heading("tokens", anchor="e")
        self.tree.heading("value", anchor="e")
        self.tree.column("#0", width=300, minwidth=190)
        self.tree.column(
            "tokens",
            width=round(110 * theme.ui_scale(root)),
            minwidth=80,
            anchor="e",
            stretch=False,
        )
        self.tree.column(
            "value",
            width=round(110 * theme.ui_scale(root)),
            minwidth=90,
            anchor="e",
            stretch=False,
        )
        self.tree.tag_configure("model", foreground=theme.MUTED, font=(theme.FONT, 10))
        self.tool_icons = {}
        scroll = ttk.Scrollbar(table_frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.selection)
        self.detail = ttk.Label(
            frame,
            text="Expand a tool for model and token details.",
            wraplength=580,
            style="Caption.TLabel",
        )
        bottom = ttk.Frame(frame)
        self.status = ttk.Label(
            bottom, text="Starting tracker…", style="Caption.TLabel"
        )
        self.status.pack(side="left", fill="x", expand=True)
        self.refresh_button = ttk.Button(
            bottom, text="↻  Refresh", command=lambda: self.refresh(restart=True)
        )
        self.refresh_button.pack(side="right", padx=(8, 0))
        # Reserve the footer before letting the history list consume space.
        # Otherwise Tk's packing order clips Settings/Refresh at small sizes.
        bottom.pack(side="bottom", fill="x", pady=(12, 0))
        ttk.Separator(frame).pack(side="bottom", fill="x", pady=(12, 0))
        self.detail.pack(side="bottom", fill="x", pady=(10, 0))
        table_frame.pack(fill="both", expand=True)

        def resize(event):
            if event.widget == root:
                for label in (self.notice, self.detail):
                    label.configure(wraplength=max(240, event.width - 56))
                self._adapt_layout(event.height)

        root.bind("<Configure>", resize)
        root.after(50, self._drain)
        root.after(
            50,
            lambda: self.submit(self.client.ensure_service, lambda _: self.refresh()),
        )
        root.after(15_000, self._poll)

    def _read_preferences(self):
        try:
            data = (
                json.loads(self.preferences.read_text(encoding="utf-8"))
                if self.preferences
                else {}
            )
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _adapt_layout(self, height):
        compact = height < round(760 * theme.ui_scale(self.root))
        if compact == self.compact:
            return
        self.compact = compact
        if compact:
            self.heading_frame.pack_forget()
            self.metrics_frame.pack_forget()
        else:
            self.heading_frame.pack(fill="x", pady=(0, 12), after=self.header_frame)
            self.metrics_frame.pack(fill="x", pady=(16, 16), after=self.count)
        self._paint_value()
        if self.report is not None:
            self._paint_summary()

    def _paint_value(self):
        base = 32 if self.compact else 44
        size = max(24, base - max(0, len(self.value_text) - 9) * 2)
        self.value.configure(text=self.value_text, font=(theme.DISPLAY, size, "bold"))

    def _paint_summary(self):
        data = self.report
        ordinary = [item for item in data["tools"] if not item.get("organization")]
        computers = (
            len(data.get("machines") or []) if self.selected_machine == "all" else 1
        )
        self.count.configure(
            text="Selected computer unavailable"
            if data.get("scope") == "unavailable"
            else f"{tokens(data['total'].get('tokens'))} tokens · {len(ordinary)} tools · {computers} computers"
            if self.compact
            else "Measured tokens, valued at API rates."
        )

    def save_preferences(self):
        if self.preferences is None:
            return
        try:
            self.preferences.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.preferences.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {"period": self.period.get(), "topmost": self.topmost.get()}
                ),
                encoding="utf-8",
            )
            temporary.replace(self.preferences)
        except OSError:
            pass

    def submit(self, work, done, failed=None):
        if self.closed:
            return
        future = self.worker.submit(work)
        future.add_done_callback(
            lambda result: self.messages.put((result, done, failed))
        )

    def _drain(self):
        if self.closed:
            return
        try:
            while True:
                future, done, failed = self.messages.get_nowait()
                try:
                    result = future.result()
                    done(result)
                except self.tk.TclError:
                    # A settings dialog may close while its worker is running.
                    continue
                except Exception as exc:  # noqa: BLE001 -- isolate background task failures from Tk
                    try:
                        if failed:
                            failed(exc)
                        else:
                            self.status.configure(text="Tracker unavailable")
                            self.live_status.configure(
                                text="●  Unavailable", foreground=theme.WARNING
                            )
                            if self.report is None:
                                self.value_text = "Unavailable"
                                self._paint_value()
                            self.notice.configure(
                                text=str(exc)
                                if isinstance(exc, DesktopError)
                                else "Could not read usage. Please retry."
                            )
                    except self.tk.TclError:
                        pass
        except queue.Empty:
            pass
        finally:
            if not self.closed:
                self.root.after(80, self._drain)

    def change_scope(self):
        self.selected_machine = self.machine_ids.get(
            self.computer.get(), self.selected_machine
        )
        self.generation += 1
        self.report = None
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self.value_text = "Loading…"
        self._paint_value()
        self.count.configure(text="Loading the selected usage period")
        for label in self.metric_labels:
            label.configure(text="—")
        self.detail.configure(text="")
        self.notice.configure(text="")
        self.refresh()
        self.save_preferences()

    def refresh(self, *, restart=False):
        if self.pending:
            self.again = True
            return
        self.pending = True
        self.refresh_button.configure(state="disabled")
        generation = self.generation
        period = self.period.get()
        machine = self.selected_machine

        def finish():
            self.pending = False
            self.refresh_button.configure(state="normal")
            if self.again:
                self.again = False
                self.refresh()

        def done(data):
            if generation == self.generation:
                try:
                    self.render(data)
                except (DesktopError, KeyError, TypeError, ValueError) as exc:
                    failed(exc)
                    return
            finish()

        def failed(error):
            if generation == self.generation:
                self.status.configure(text="Refresh failed")
                self.live_status.configure(
                    text="●  Refresh failed", foreground=theme.WARNING
                )
                self.notice.configure(
                    text=str(error)
                    if isinstance(error, DesktopError)
                    else "Could not refresh usage. Retry shortly."
                )
                if self.report is None:
                    self.value_text = "Unavailable"
                    self._paint_value()
            finish()

        def read():
            if restart:
                self.client.ensure_service()
            return self.client.snapshot(period, machine)

        self.submit(read, done, failed)

    def _poll(self):
        if self.closed:
            return
        self.refresh()
        self.root.after(15_000, self._poll)

    def render(self, data):
        if not isinstance(data.get("total"), dict) or not isinstance(
            data.get("tools"), list
        ):
            raise DesktopError("The tracker returned an invalid usage result.")
        self.report = data
        total = data["total"]
        self.value_text = (
            "Unavailable"
            if data.get("scope") == "unavailable"
            else money(total.get("valueUsd"), total.get("complete", False))
            if total.get("events")
            else "No usage yet"
        )
        self._paint_value()
        ordinary = [item for item in data["tools"] if not item.get("organization")]
        self._paint_summary()
        for label, text in zip(
            self.metric_labels,
            (
                tokens(total.get("tokens")),
                str(len(ordinary)),
                str(len(data.get("machines") or []))
                if self.selected_machine == "all"
                else "1",
            ),
        ):
            label.configure(text=text)
        notice = data.get("warning") or ""
        if not total.get("complete", True):
            notice += (
                (" " if notice else "")
                + "Some recorded usage has missing rates or token details. The value shown is a known subtotal."
            )
        if any(item.get("organization") for item in data["tools"]):
            notice += (
                (" " if notice else "")
                + "Organization API usage is listed separately because it may overlap tool usage."
            )
        self.notice.configure(text=notice)
        opened = {
            self.items[key].get("key")
            for key in self.tree.get_children()
            if self.tree.item(key, "open") and key in self.items
        }
        selected = self.tree.selection()
        selected_key = (
            self.items.get(selected[0], {}).get("_identity") if selected else None
        )
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        restore = None
        for tool in data["tools"]:
            key = self.tree.insert(
                "",
                "end",
                text=(tool.get("name") or "Unknown tool")
                + (" · separate" if tool.get("organization") else ""),
                values=(
                    tokens(tool.get("tokens")),
                    money(tool.get("valueUsd"), tool.get("complete", False)),
                ),
                open=tool.get("key") in opened,
                image=self._tool_image(tool.get("key")),
            )
            identity = (tool.get("key"), None)
            self.items[key] = {**tool, "_identity": identity}
            if identity == selected_key:
                restore = key
            for model in tool.get("models") or []:
                child = self.tree.insert(
                    key,
                    "end",
                    text=model.get("name") or "Unknown model",
                    values=(
                        tokens(model.get("tokens")),
                        money(model.get("valueUsd"), model.get("complete", False)),
                    ),
                    tags=("model",),
                )
                identity = (tool.get("key"), model.get("key"))
                self.items[child] = {**model, "_identity": identity}
                if identity == selected_key:
                    restore = child
        if restore:
            self.tree.selection_set(restore)
            self.selection()
        else:
            self.detail.configure(
                text="Expand a tool for model and token details."
                if data["tools"]
                else "No recorded usage in this period. Check Sources in Settings to connect a tool."
            )
        self.machine_ids = {"All computers": "all"}
        for machine in data.get("machines") or []:
            label = str(machine.get("name") or machine["id"])
            if label in self.machine_ids:
                label += " (" + str(machine["id"]) + ")"
            self.machine_ids[label] = machine["id"]
        self.machine_picker.configure(values=list(self.machine_ids))
        label = next(
            (
                name
                for name, key in self.machine_ids.items()
                if key == self.selected_machine
            ),
            None,
        )
        if label is None:
            authoritative = data.get("scope") not in {"local-fallback", "unavailable"}
            label = (
                "Previously paired computer"
                if authoritative
                else "Selected computer (offline)"
            )
            self.machine_ids[label] = self.selected_machine
            self.notice.configure(
                text=(
                    notice
                    + (
                        " This computer is no longer paired; showing its recorded history."
                        if authoritative
                        else ""
                    )
                ).strip()
            )
            self.machine_picker.configure(values=list(self.machine_ids))
        self.computer.set(label)
        self._fit_numeric_columns()
        self._paint_status()

    def _fit_numeric_columns(self):
        from tkinter import font

        table_font = font.Font(
            root=self.root,
            font=self.ttk.Style(self.root).lookup("Usage.Treeview", "font"),
        )
        for column, values in (
            ("tokens", [tokens(item.get("tokens")) for item in self.items.values()]),
            (
                "value",
                [
                    money(item.get("valueUsd"), item.get("complete", False))
                    for item in self.items.values()
                ],
            ),
        ):
            required = max(
                (table_font.measure(value) for value in values), default=0
            ) + round(26 * theme.ui_scale(self.root))
            current = self.tree.column(column, "width")
            self.tree.column(column, minwidth=required, width=max(current, required))

    def _tool_image(self, key):
        if key not in self.tool_icons:
            self.tool_icons[key] = theme.tool_icon(self.root, key)
        return self.tool_icons[key]

    def _paint_status(self):
        if self.report is None:
            return
        stamp = parse_utc(self.report.get("generatedAt"))
        age = (
            max(0, int((datetime.now(UTC) - stamp).total_seconds())) if stamp else None
        )
        fresh = (
            "Updated just now"
            if age is not None and age < 30
            else f"Data {age // 60}m old"
            if age is not None
            else "Collection time unavailable"
        )
        offline = sum(
            not m.get("online", True) for m in self.report.get("machines") or []
        )
        if offline:
            fresh += f" · {offline} offline"
        if self.demo:
            fresh = "Sample data"
        timezone = self.report.get("timezone")
        self.status.configure(text=fresh + (f" · {timezone}" if timezone else ""))
        scope = self.report.get("scope")
        self.live_status.configure(
            text="●  Sample data"
            if self.demo
            else "●  Hub unavailable"
            if scope == "unavailable"
            else "●  Local copy"
            if scope == "local-fallback"
            else "●  Tracking",
            foreground=theme.WARNING
            if scope in {"unavailable", "local-fallback"}
            else theme.GREEN,
        )

    def selection(self, _event=None):
        selected = self.tree.selection()
        item = self.items.get(selected[0]) if selected else None
        if item is None:
            return
        text = f"Fresh input {tokens(item.get('input'))} · Cached {tokens(item.get('cached'))} · Writes {tokens(item.get('writes'))} · Output {tokens(item.get('output'))}"
        if item.get("reasoning") is not None:
            text += f" · Reasoning within output {tokens(item['reasoning'])}"
        if item.get("unpricedEvents"):
            text += f" · {item['unpricedEvents']} unpriced records"
        self.detail.configure(text=text)

    def settings(self):
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            return
        self.settings_window = SettingsWindow(self)

    def close(self):
        self.closed = True
        self.save_preferences()
        self.worker.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


class SettingsWindow:
    def __new__(cls, app):
        import tkinter as tk

        # Actual controls live in a native Toplevel; no HTML is loaded.
        window = tk.Toplevel(app.root)
        window.configure(background=theme.BG)
        if sys.platform == "win32" and app.root.iconbitmap():
            window.iconbitmap(app.root.iconbitmap())
        cls._build(window, app)
        window.after(0, lambda: theme.dark_titlebar(window))
        return window

    @staticmethod
    def _build(window, app):
        from tkinter import filedialog, messagebox, simpledialog, ttk

        tk = app.tk
        window.title("BURNRATE Settings")
        window.transient(app.root)
        window.attributes("-topmost", app.topmost.get())
        theme.size_window(window, 640, 600, 580, 510)
        header = ttk.Frame(window, padding=(24, 22, 24, 16))
        header.pack(fill="x")
        ttk.Label(header, text="Settings", style="Title.TLabel").pack(side="left")
        ttk.Button(
            header, text="Done", style="Quiet.TButton", command=window.destroy
        ).pack(side="right")
        notebook = ttk.Notebook(window)
        notebook.pack(fill="both", expand=True, padx=24, pady=(0, 24))
        sources, computers, general = (
            ttk.Frame(notebook, padding=(0, 8, 0, 0)) for _ in range(3)
        )
        for label, frame in (
            ("Sources", sources),
            ("Computers", computers),
            ("General", general),
        ):
            notebook.add(frame, text=label)
        ttk.Label(
            sources, text="Usage sources on this computer", style="Section.TLabel"
        ).pack(anchor="w", pady=(0, 8))
        listing = ttk.Treeview(
            sources, columns=("state",), show="tree headings", height=8
        )
        listing.heading("#0", text="Source", anchor="w")
        listing.heading("state", text="Status", anchor="w")
        listing.column("#0", width=250)
        listing.column("state", width=190)
        details = ttk.Label(
            sources,
            text="Select a source to change its location or connection.",
            wraplength=530,
            style="Caption.TLabel",
        )
        rows = {}
        disconnect_change = SourceMutation(app.client)
        machines = ttk.Treeview(
            computers, columns=("state",), show="tree headings", height=8
        )
        machines.heading("#0", text="Computer")
        machines.heading("state", text="Status")
        pair_state = {}
        feedback = ttk.Label(
            computers,
            text="All paired computers contribute to the combined usage view.",
            wraplength=520,
        )

        def failure(exc):
            if window.winfo_exists():
                messagebox.showerror(
                    "BURNRATE",
                    str(exc)
                    if isinstance(exc, DesktopError)
                    else "Could not complete this action.",
                    parent=window,
                )

        def load():
            if not window.winfo_exists():
                return

            def read():
                return (
                    app.client.request("/api/connections"),
                    app.client.request("/api/machines"),
                    app.client.request("/api/setup/state"),
                )

            def show(data):
                if not window.winfo_exists():
                    return
                listing.delete(*listing.get_children())
                rows.clear()
                for row in data[0].get("connections") or []:
                    # Balance-only integrations have no token usage to display.
                    if row.get("capabilities") == ["balance"]:
                        continue
                    key = listing.insert(
                        "",
                        "end",
                        text=row["name"],
                        values=(str(row.get("state") or "Unknown").replace("_", " "),),
                    )
                    rows[key] = row
                machines.delete(*machines.get_children())
                for row in data[1].get("machines") or []:
                    machines.insert(
                        "",
                        "end",
                        text=row.get("name") or row["id"],
                        values=("Online" if row.get("online") else "Last synced copy",),
                    )
                pair_state.update(data[2].get("setup") or {})

            app.submit(read, show, failure)

        def selected():
            selection = listing.selection()
            return rows.get(selection[0]) if selection else None

        def describe(_=None):
            row = selected()
            details.configure(
                text=(
                    row.get("detail")
                    or row.get("location")
                    or row.get("capabilityNote")
                    or ""
                )
                if row
                else ""
            )

        listing.bind("<<TreeviewSelect>>", describe)

        def configure_source():
            row = selected()
            if row is None or app.demo:
                return
            if row.get("kind") == "external":
                messagebox.showinfo(
                    "Source",
                    row.get("detail") or "This integration is managed externally.",
                    parent=window,
                )
                return
            dialog = tk.Toplevel(window)
            dialog.configure(background=theme.BG)
            dialog.after(0, lambda: theme.dark_titlebar(dialog))
            change = SourceMutation(app.client)
            dialog.title("Connect " + row["name"])
            dialog.transient(window)
            form = ttk.Frame(dialog, padding=16)
            form.pack(fill="both", expand=True)
            api = row.get("kind") == "api"
            ttk.Label(
                form,
                text=row.get("credentialHelp") if api else "Usage location",
                wraplength=480,
            ).pack(anchor="w")
            text = tk.StringVar(value="" if api else row.get("location", ""))
            entry = ttk.Entry(
                form, textvariable=text, width=64, show="•" if api else ""
            )
            entry.pack(fill="x", pady=8)
            if not api:

                def browse():
                    path = (
                        filedialog.askopenfilename(parent=dialog)
                        if row.get("kind") in {"sqlite", "opencode", "zcode"}
                        else filedialog.askdirectory(parent=dialog)
                    )
                    if path:
                        text.set(path)

                ttk.Button(form, text="Browse…", command=browse).pack(anchor="w")
            ttk.Label(
                form,
                text="Connecting permits BURNRATE to read usage from this source.",
                wraplength=480,
            ).pack(anchor="w", pady=10)

            def connect_source():
                body = {
                    "source": row["source"],
                    "operation": "connect",
                    "revision": row.get("revision", 0),
                    "mode": "api" if api else "manual",
                    "secret" if api else "location": text.get(),
                }
                button.configure(state="disabled")

                def done(_):
                    text.set("")
                    if dialog.winfo_exists():
                        dialog.destroy()
                    load()
                    app.refresh()

                def failed(exc):
                    if dialog.winfo_exists():
                        if isinstance(exc, DesktopError) and exc.status_code == 409:

                            def reload_revision(data):
                                fresh = next(
                                    (
                                        item
                                        for item in data.get("connections", [])
                                        if item["source"] == row["source"]
                                    ),
                                    None,
                                )
                                if fresh:
                                    row.update(fresh)
                                if dialog.winfo_exists():
                                    button.configure(state="normal")

                            app.submit(
                                lambda: app.client.request("/api/connections"),
                                reload_revision,
                                lambda error: (
                                    button.configure(state="normal")
                                    if dialog.winfo_exists()
                                    else None,
                                    failure(error),
                                ),
                            )
                        else:
                            button.configure(state="normal")
                    failure(exc)

                app.submit(lambda: change.send(body), done, failed)

            button = ttk.Button(
                form,
                text="Test and connect",
                command=connect_source,
                style="Accent.TButton",
            )
            button.pack(anchor="e")
            entry.focus_set()

        def disconnect():
            if disconnect_button.instate(["disabled"]):
                return
            row = selected()
            if row is None or app.demo:
                return
            if not messagebox.askyesno(
                "Disconnect source",
                "Stop collecting this source? Recorded history is kept.",
                parent=window,
            ):
                return
            body = {
                "source": row["source"],
                "operation": "disable",
                "revision": row.get("revision", 0),
            }
            disconnect_button.configure(state="disabled")

            def done(_):
                if window.winfo_exists():
                    disconnect_button.configure(state="normal")
                    load()

            def failed(exc):
                if window.winfo_exists():
                    disconnect_button.configure(state="normal")
                    if isinstance(exc, DesktopError) and exc.status_code == 409:
                        load()
                failure(exc)

            app.submit(
                lambda: disconnect_change.send(body),
                done,
                failed,
            )

        actions = ttk.Frame(sources)
        actions.pack(side="bottom", fill="x")
        ttk.Button(actions, text="Refresh", command=load).pack(side="left")
        ttk.Button(actions, text="Connect / change…", command=configure_source).pack(
            side="left", padx=8
        )
        disconnect_button = ttk.Button(actions, text="Disconnect", command=disconnect)
        disconnect_button.pack(side="right")
        details.pack(side="bottom", fill="x", pady=(8, 12))
        listing.pack(fill="both", expand=True, pady=8)

        pair_pending = [False]

        def pairing_busy(busy):
            pair_pending[0] = busy
            if window.winfo_exists():
                for button in pair_buttons:
                    button.configure(state="disabled" if busy else "normal")

        def pairing_failed(exc):
            pairing_busy(False)
            failure(exc)

        def join():
            if app.demo or pair_pending[0]:
                return
            code = simpledialog.askstring(
                "Join my computers",
                "Paste the pairing code from your main computer:",
                parent=window,
            )
            if code:

                def done(_):
                    pairing_busy(False)
                    if window.winfo_exists():
                        feedback.configure(
                            text="Connected. Usage from your paired computers will sync in the background."
                        )
                        load()
                    app.change_scope()

                pairing_busy(True)
                app.submit(
                    lambda: app.client.request(
                        "/api/setup/connect-spoke",
                        {"pairingCode": code, "testSync": True},
                    ),
                    done,
                    pairing_failed,
                )

        def create_code():
            if app.demo or pair_pending[0]:
                return
            if pair_state.get("role") == "spoke":
                messagebox.showinfo(
                    "Pair a computer",
                    "Create a pairing code on your main computer, then paste it on the new computer.",
                    parent=window,
                )
                return
            address = simpledialog.askstring(
                "Main computer address",
                "Address the other computer can reach (for example your private HTTPS hub):",
                parent=window,
            )
            if not address:
                return
            parsed = urlsplit(address)
            if (
                parsed.scheme not in {"https", "http"}
                or not parsed.hostname
                or parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            ):
                messagebox.showerror(
                    "Pair a computer",
                    "Enter a reachable hub address, not this computer's loopback address.",
                    parent=window,
                )
                return
            name = simpledialog.askstring(
                "New computer", "Name of the computer you want to add:", parent=window
            )
            if not name:
                return

            def done(data):
                pairing_busy(False)
                if not window.winfo_exists():
                    return
                dialog = tk.Toplevel(window)
                dialog.configure(background=theme.BG)
                dialog.after(0, lambda: theme.dark_titlebar(dialog))
                dialog.title("Pairing code")
                body = ttk.Frame(dialog, padding=16)
                body.pack(fill="both", expand=True)
                ttk.Label(
                    body,
                    text="Paste this code into BURNRATE on the other computer. Keep it private.",
                    wraplength=460,
                ).pack()
                text = ttk.Entry(body, width=70)
                text.insert(0, data["pairingCode"])
                text.configure(state="readonly")
                text.pack(fill="x", pady=12)

                def copy_code():
                    dialog.clipboard_clear()
                    dialog.clipboard_append(data["pairingCode"])

                ttk.Button(body, text="Copy code", command=copy_code).pack()
                load()

            pairing_busy(True)
            app.submit(
                lambda: app.client.request(
                    "/api/setup/pair-hub", {"hubUrl": address, "name": name}
                ),
                done,
                pairing_failed,
            )

        pair_actions = ttk.Frame(computers)
        pair_actions.pack(side="bottom", fill="x")
        pair_buttons = [
            ttk.Button(pair_actions, text="Create pairing code…", command=create_code),
            ttk.Button(pair_actions, text="Join with code…", command=join),
        ]
        for button in pair_buttons:
            button.pack(side="left", padx=(0, 8))
        feedback.pack(side="bottom", fill="x", pady=(8, 12))
        machines.pack(fill="both", expand=True, pady=(0, 12))

        def pin():
            app.root.attributes("-topmost", app.topmost.get())
            app.save_preferences()

        ttk.Checkbutton(
            general, text="Keep usage window on top", variable=app.topmost, command=pin
        ).pack(anchor="w", pady=(0, 14))
        ttk.Label(
            general,
            text="Collection continues in the background when this window closes. Reopen BURNRATE to see usage.",
            wraplength=500,
        ).pack(anchor="w")
        if app.demo:
            ttk.Label(
                general,
                text="Sample-data demo: connections and computer pairing are disabled.",
            ).pack(anchor="w", pady=15)
        load()


def main(
    settings: Settings | None = None,
    *,
    demo=False,
    port: int | None = None,
    smoke_test=False,
) -> int:
    import tkinter as tk

    if smoke_test and not demo:
        raise ValueError("Native smoke tests require --demo")
    if not demo:
        load_user_env()
    settings = settings or load_settings()
    port = port if port is not None else int(os.getenv("BURNRATE_PORT", "17331"))
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise SystemExit(
            "A desktop display with Tcl/Tk is required for the native window."
        ) from exc
    window = UsageWindow(
        root,
        DemoClient() if demo else NativeClient(settings, port),
        preferences=None
        if demo
        else settings.database_path.parent / "native-window.json",
        demo=demo,
    )
    result = [0]
    if smoke_test:
        deadline = time.monotonic() + 5
        minimum_checked = [False]

        def check():
            if window.report is not None or time.monotonic() >= deadline:
                result[0] = 0 if window.report is not None else 1
                root.update_idletasks()
                if (
                    not window.refresh_button.winfo_ismapped()
                    or window.refresh_button.winfo_rooty()
                    + window.refresh_button.winfo_height()
                    > root.winfo_rooty() + root.winfo_height()
                ):
                    result[0] = 1
                rows = window.tree.get_children()
                bounds = window.tree.bbox(rows[0]) if rows else ()
                if not bounds or bounds[1] + bounds[3] > window.tree.winfo_height():
                    result[0] = 1
                if result[0] == 0 and not minimum_checked[0]:
                    minimum_checked[0] = True
                    width, height = root.minsize()
                    root.geometry(f"{width}x{height}")
                    root.after(150, check)
                    return
                window.close()
            else:
                root.after(50, check)

        root.after(100, check)
    root.mainloop()
    return result[0]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="BURNRATE native usage window")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--port", type=int)
    options = parser.parse_args()
    raise SystemExit(main(demo=options.demo, port=options.port))
