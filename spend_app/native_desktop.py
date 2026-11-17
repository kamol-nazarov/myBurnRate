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
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from spend_app import native_theme as theme
from spend_app.config import Settings, load_settings, load_user_env
from spend_app.error_codes import HUB_PROTOCOL_VERSION, format_error, map_http_error
from spend_app.native_frame import NativeFrame
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
    def __init__(self, message, status_code=None, code=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


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
        self._session_token = None
        self._auth_lock = threading.Lock()

    def request(self, path: str, body: dict | None = None) -> dict:
        if not path.startswith("/api/"):
            raise ValueError("Only local BURNRATE API paths are supported")
        headers = {"Origin": self.base_url, "X-BURNRATE-Request": "1"}
        token = os.getenv("BURNRATE_ACCESS_TOKEN", "").strip()
        try:
            with httpx.Client(
                timeout=httpx.Timeout(
                    45
                    if path.startswith(
                        (
                            "/api/spend/usage",
                            "/api/spend/quotas/refresh",
                            # H11 settings
                            "/api/setup/scan",
                            "/api/setup/notifications/test",
                            "/api/setup/diagnostics",
                            "/api/sync/trigger",
                        )
                    )
                    else 20,
                    connect=2,
                ),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                if token:
                    # Exchange recovery authority once; ordinary UI calls use
                    # an independently expiring and revocable session.
                    with self._auth_lock:
                        if self._session_token is None:
                            bootstrap = client.post(self.base_url + "/api/auth/bootstrap", json={},
                                                    headers={**headers, "Authorization": "Bearer " + token})
                            if bootstrap.status_code != 200:
                                try:
                                    boot_data = bootstrap.json()
                                except ValueError:
                                    boot_data = None
                                mapped = map_http_error(
                                    bootstrap.status_code,
                                    boot_data,
                                    headers=getattr(bootstrap, "headers", None),
                                    endpoint_path="/api/auth/bootstrap",
                                    has_machine_token=token.startswith("br_m_"),
                                    local_service=True,
                                )
                                raise DesktopError(
                                    format_error(mapped.code, retry_after=mapped.retry_after),
                                    bootstrap.status_code,
                                    code=mapped.code,
                                )
                            from spend_app.auth_model import extract_cookie
                            session_token = extract_cookie(bootstrap)
                            if not session_token:
                                raise DesktopError(
                                    "The tracker returned an invalid session response.",
                                    bootstrap.status_code,
                                    code="APP-901",
                                )
                            self._session_token = session_token
                    headers["Authorization"] = "Bearer " + self._session_token
                response = (
                    client.get(self.base_url + path, headers=headers)
                    if body is None
                    else client.post(self.base_url + path, json=body, headers=headers)
                )
                try:
                    data = response.json()
                except ValueError:
                    data = None
                if not response.is_success:
                    mapped = map_http_error(
                        response.status_code,
                        data,
                        headers=getattr(response, "headers", None),
                        endpoint_path=path,
                        has_machine_token=token.startswith("br_m_"),
                        local_service=True,
                    )
                    raise DesktopError(
                        format_error(mapped.code, retry_after=mapped.retry_after),
                        response.status_code,
                        code=mapped.code,
                    )
                if not isinstance(data, dict):
                    raise DesktopError(
                        format_error("APP-901"),
                        response.status_code,
                        code="APP-901",
                    )
                return data
        except httpx.HTTPError:
            raise DesktopError(
                format_error("APP-901"),
                503,
                code="APP-901",
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
                    protocol = data.get("protocolVersion")
                    if type(protocol) is not int or protocol != HUB_PROTOCOL_VERSION:
                        if type(protocol) is int and protocol > HUB_PROTOCOL_VERSION:
                            raise DesktopError(
                                format_error("APP-902"),
                                426,
                                code="APP-902",
                            )
                        raise DesktopError(
                            "An older BURNRATE background service is still running. "
                            "Quit the old app and stop the old service, then reopen BURNRATE.",
                            code="APP-901",
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
    # Sample spans and a $200/month plan set give a plausible same-span ratio.
    span_hours = {"today": 10.0, "week": 168.0, "month": 312.0, "all": 1440.0}[period]
    accrued = round(200 / 30 * span_hours / 24, 2)
    now = datetime.now(UTC)
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
        "generatedAt": now.isoformat(),
        "activeFrom": (now - timedelta(hours=span_hours)).isoformat(),
        "rate": {
            "perHourUsd": round(127.34 / span_hours, 4),
            "perDayUsd": round(127.34 / span_hours * 24, 2),
            "spanHours": span_hours,
            "complete": True,
        },
        "plans": {
            "accruedUsd": accrued,
            "multiple": round(127.34 / accrued, 2),
            "basis": "ratio",
            "method": "Sample plan cost accrued over the same span.",
        },
        "machines": [
            {"id": "local", "name": "Desktop", "online": True},
            {"id": "example-laptop", "name": "Laptop", "online": True},
        ],
        "series": _demo_series(now, span_hours, period),
    }


def _demo_series(now: datetime, span_hours: float, period: str) -> list[dict]:
    # Uneven sample buckets that still sum exactly to the demo total.
    weights = (2, 5, 9, 14, 6, 3, 11, 16, 12, 8, 4, 10)
    scale = sum(weights)
    step = span_hours / len(weights)
    points, running_usd, running_tok = [], 0.0, 0
    for idx, weight in enumerate(weights):
        bucket_usd = round(127.34 * weight / scale, 2)
        bucket_tok = int(8_200_000 * weight / scale)
        if idx == len(weights) - 1:
            bucket_usd = round(127.34 - running_usd, 2)
            bucket_tok = 8_200_000 - running_tok
        running_usd = round(running_usd + bucket_usd, 2)
        running_tok += bucket_tok
        end = now - timedelta(hours=step * (len(weights) - idx - 1))
        label = (
            end.strftime("%I:%M %p").lstrip("0")
            if period == "today"
            else end.strftime("%b %d").replace(" 0", " ")
        )
        points.append(
            {
                "label": label,
                "bucketUsd": bucket_usd,
                "cumulativeUsd": running_usd,
                "tokens": bucket_tok,
                "cumulativeTokens": running_tok,
            }
        )
    return points


def _iso_z(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _demo_forecast(
    now: datetime, resets_at: datetime, hours_to_dry: float, burn: float
) -> dict:
    """A run-dry forecast shaped like ``capacity_forecast``'s, from fixed pace."""
    projected = now + timedelta(hours=hours_to_dry)
    before = (resets_at - projected).total_seconds() / 3600
    span = f"{max(1, round(hours_to_dry))}h"
    return {
        "state": "tight" if before <= 12 else "dry",
        "projectedRunDry": projected.replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "hoursRemaining": hours_to_dry,
        "hoursBeforeReset": round(before, 4),
        "burnPctPerHour": burn,
        "line": f"pace → dry in ~{span} ({max(1, round(before))}h before reset)",
    }


def demo_hud() -> dict:
    now = datetime.now(UTC)
    in_3h = (now + timedelta(hours=3, minutes=18)).isoformat().replace("+00:00", "Z")
    in_4d = (now + timedelta(days=4, hours=14)).isoformat().replace("+00:00", "Z")
    in_9d = (now + timedelta(days=9, hours=6)).isoformat().replace("+00:00", "Z")
    stamp = now.isoformat().replace("+00:00", "Z")
    # The sample the tab and the full window are designed against: Claude Code
    # runs dry within the hour, Codex and OpenCode Go within the day or two.
    cc_5h_reset = now + timedelta(hours=2, minutes=14)
    cc_week_reset = now + timedelta(days=3, hours=6)
    week_reset = now + timedelta(days=4, hours=14)
    return {
        "generatedAt": stamp,
        "status": "live",
        "capacity": [
            {
                "providerKey": "claude-code",
                "title": "Claude Code",
                "plan": "Claude Max",
                "inUse": True,
                "rows": [
                    {
                        "limitKey": "5h",
                        "label": "5-hour window",
                        "pct": 46.0,
                        "unit": "pct",
                        "resetsAt": _iso_z(cc_5h_reset),
                        "forecast": _demo_forecast(now, cc_5h_reset, 1.17, 39.3),
                    },
                    {
                        "limitKey": "weekly",
                        "label": "Weekly window",
                        "pct": 61.0,
                        "unit": "pct",
                        "resetsAt": _iso_z(cc_week_reset),
                        "forecast": _demo_forecast(now, cc_week_reset, 27.0, 1.44),
                    },
                ],
            },
            {
                "providerKey": "claude-code-work",
                "title": "Claude Code · Work",
                "accountLabel": "Work",
                "plan": "Claude Max",
                "inUse": False,
                "rows": [
                    {
                        "limitKey": "5h",
                        "label": "5-hour window",
                        "pct": 4.0,
                        "unit": "pct",
                        "resetsAt": in_3h,
                    },
                    {
                        "limitKey": "weekly",
                        "label": "Weekly window",
                        "pct": 12.0,
                        "unit": "pct",
                        "resetsAt": in_4d,
                    },
                ],
            },
            {
                "providerKey": "opencode-go",
                "title": "OpenCode Go",
                "plan": "Go",
                "inUse": False,
                "rows": [
                    {
                        "limitKey": "5h",
                        "label": "5-hour window",
                        "pct": 8.0,
                        "unit": "pct",
                        "resetsAt": in_3h,
                    },
                    {
                        "limitKey": "weekly",
                        "label": "Weekly window",
                        "pct": 44.0,
                        "unit": "pct",
                        "resetsAt": in_4d,
                        "forecast": _demo_forecast(now, week_reset, 32.0, 1.75),
                    },
                    {
                        "limitKey": "monthly",
                        "label": "Monthly window",
                        "pct": 80.0,
                        "unit": "pct",
                        "resetsAt": in_9d,
                    },
                ],
            },
            {
                "providerKey": "opencode-go-2",
                "title": "OpenCode Go · Profile 2",
                "accountLabel": "Profile 2",
                "plan": "Go",
                "inUse": False,
                "rows": [
                    {
                        "limitKey": "5h",
                        "label": "5-hour window",
                        "pct": 0.0,
                        "unit": "pct",
                        "resetsAt": in_3h,
                    },
                    {
                        "limitKey": "weekly",
                        "label": "Weekly window",
                        "pct": 5.0,
                        "unit": "pct",
                        "resetsAt": in_4d,
                    },
                    {
                        "limitKey": "monthly",
                        "label": "Monthly window",
                        "pct": 7.0,
                        "unit": "pct",
                        "resetsAt": in_9d,
                    },
                ],
            },
            {
                "providerKey": "opencode",
                "title": "Z.AI Coding Plan",
                "plan": "Max",
                "inUse": False,
                "rows": [
                    {
                        "limitKey": "weekly",
                        "label": "Weekly credits",
                        "pct": 6.0,
                        "used": 8570,
                        "allowance": 140000,
                        "unit": "credits",
                        "resetsAt": in_4d,
                    }
                ],
            },
            {
                "providerKey": "grok",
                "title": "Grok Build",
                "plan": "SuperGrok",
                "inUse": False,
                "rows": [
                    {
                        "limitKey": "weekly",
                        "label": "Weekly",
                        "pct": 12.0,
                        "unit": "pct",
                        "resetsAt": in_4d,
                    }
                ],
            },
            {
                "providerKey": "codex",
                "title": "Codex",
                "plan": "ChatGPT Pro",
                "inUse": True,
                "rows": [
                    {
                        "limitKey": "weekly",
                        "label": "Weekly",
                        "pct": 38.0,
                        "unit": "pct",
                        "resetsAt": in_4d,
                        "forecast": _demo_forecast(now, week_reset, 14.0, 4.43),
                    }
                ],
            },
            {
                "providerKey": "openrouter",
                "title": "OpenRouter",
                "plan": "PAYG",
                "isPayg": True,
                "fundsRemainingUsd": 23.40,
                "fundsRunway": {
                    "state": "estimate",
                    "days": 42.0,
                    "runsOutAt": _iso_z(now + timedelta(days=42)),
                    "line": "Runway ~42d at recent pace",
                },
                "rows": [
                    {
                        "limitKey": "balance",
                        "label": "Balance",
                        "used": 23.40,
                        "unit": "usd",
                        "runway": {"line": "Runway ~42d at recent pace"},
                    }
                ],
            },
            {
                "providerKey": "deepseek",
                "title": "DeepSeek",
                "plan": "PAYG",
                "isPayg": True,
                "fundsRemainingUsd": 54.82,
                "fundsRunway": {"line": "Prepaid USD balance"},
                "rows": [
                    {
                        "limitKey": "balance",
                        "label": "Balance",
                        "used": 54.82,
                        "unit": "usd",
                    }
                ],
            },
            {
                "providerKey": "muse",
                "title": "Muse Code",
                "plan": "Power Usage",
                "inUse": False,
                "rows": [
                    {
                        "limitKey": "weekly",
                        "label": "Weekly window",
                        "pct": 4.0,
                        "unit": "pct",
                        "resetsAt": in_4d,
                    }
                ],
            },
        ],
    }


def demo_hud_usage() -> dict:
    return {
        "generatedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "windowTokens": {
            "claude-code": {
                "5h": {"tokens": 640_000},
                "weekly": {"tokens": 9_400_000, "today": {"tokens": 2_900_000}},
            },
            "claude-code-work": {
                "5h": {"tokens": 120_000},
                "weekly": {"tokens": 1_850_000, "today": {"tokens": 410_000}},
            },
            "opencode-go": {
                "5h": {"tokens": 210_000},
                "weekly": {"tokens": 3_200_000, "today": {"tokens": 580_000}},
                "monthly": {"tokens": 18_400_000},
            },
            "opencode-go-2": {
                "5h": {"tokens": 0},
                "weekly": {"tokens": 480_000, "today": {"tokens": 95_000}},
                "monthly": {"tokens": 1_100_000},
            },
            "opencode": {
                "weekly": {"tokens": 4_600_000, "today": {"tokens": 1_400_000}},
            },
            "grok": {
                "weekly": {"tokens": 390_000, "today": {"tokens": 100_000}},
            },
            "codex": {
                "weekly": {"tokens": 11_200_000, "today": {"tokens": 3_100_000}},
            },
            "muse": {
                "weekly": {"tokens": 1_900_000, "today": {"tokens": 700_000}},
            },
        },
    }


# H9 analytics
_DEMO_WINDOW_SCALE = {
    "15m": (0.04, "15m", 15),
    "30m": (0.07, "30m", 15),
    "1h": (0.12, "1h", 20),
    "3h": (0.28, "3h", 18),
    "6h": (0.52, "6h", 24),
    "12h": (0.76, "12h", 24),
    "1d": (1.0, "1d", 24),
    "1w": (4.2, "1w", 14),
    "1mo": (12.4, "1mo", 15),
    "mtd": (9.6, "MTD", 14),
    "ytd": (28.0, "YTD", 16),
    "all": (42.0, "All", 16),
}

_DEMO_TOOL_DEFS = (
    ("claude-code", "Claude Code", "Claude Max", True, 2_900_000, 52.10, 62.0, 22),
    ("codex", "Codex", "ChatGPT Pro", True, 3_100_000, 41.82, 54.0, 19),
    ("opencode", "OpenCode", "Z.AI Coding Plan", False, 1_400_000, 18.60, 46.0, 11),
    ("muse", "Muse Code", "Power Usage", False, 700_000, 9.72, 39.0, 7),
    ("grok", "Grok Build", "SuperGrok", False, 100_000, 5.10, 28.0, 4),
)

_DEMO_MODEL_DEFS = (
    ("claude-opus-4-6", "Claude Opus 4.6", "claude-code", True, 34.40, 1_650_000, 64.0, 12),
    ("claude-sonnet-4-6", "Claude Sonnet 4.6", "claude-code", True, 17.70, 1_250_000, 59.0, 10),
    ("gpt-5.3-codex", "GPT-5.3 Codex", "codex", True, 41.82, 3_100_000, 54.0, 19),
    ("glm-4.7", "GLM-4.7", "opencode", False, 18.60, 1_400_000, 46.0, 11),
    ("llama-4-maverick", "Llama 4 Maverick", "muse", False, 9.72, 700_000, 39.0, 7),
    ("grok-4-fast", "Grok 4 Fast", "grok", False, 5.10, 100_000, 28.0, 4),
)


def demo_summary(window_key: str = "1d", tool: str = "all", machine: str = "all") -> dict:
    from spend_app.aggregate import TOOL_COLORS

    win = window_key if window_key in _DEMO_WINDOW_SCALE else "1d"
    scale, win_label, bucket_count = _DEMO_WINDOW_SCALE[win]
    use_tool = (tool or "all").strip() or "all"
    now = datetime.now(UTC)

    tool_rows = []
    for key, name, plan, exact, tok, val, cache_pct, runs in _DEMO_TOOL_DEFS:
        if use_tool != "all" and key != use_tool:
            continue
        t_tok = max(1, int(round(tok * scale)))
        t_val = round(val * scale, 2)
        tool_rows.append(
            {
                "key": key,
                "name": name,
                "plan": plan,
                "color": TOOL_COLORS.get(key, "#4840bb"),
                "tokens": t_tok,
                "value": t_val,
                "isExact": exact,
                "valueMarker": "" if exact else "≈",
                "cachePct": cache_pct,
                "runs": max(1, int(round(runs * max(scale, 0.25)))),
            }
        )

    model_rows = []
    for key, name, t_key, exact, val, tok, cache_pct, runs in _DEMO_MODEL_DEFS:
        if use_tool != "all" and t_key != use_tool:
            continue
        m_tok = max(1, int(round(tok * scale)))
        m_val = round(val * scale, 2)
        model_rows.append(
            {
                "key": key,
                "name": name,
                "toolKey": t_key,
                "value": m_val,
                "isExact": exact,
                "valueMarker": "" if exact else "≈",
                "tokens": m_tok,
                "cachePct": cache_pct,
                "runs": max(1, int(round(runs * max(scale, 0.25)))),
            }
        )

    total_tokens = sum(r["tokens"] for r in tool_rows)
    priced_val = round(sum(r["value"] for r in tool_rows if r["isExact"]), 2)
    pub_val = round(sum(r["value"] for r in tool_rows if not r["isExact"]), 2)
    tracked_val = round(priced_val + pub_val, 2)
    cached_tok = int(round(total_tokens * 0.50))
    fresh_tok = int(round(total_tokens * 0.36))
    output_tok = max(0, total_tokens - cached_tok - fresh_tok)
    write_tok = int(round(total_tokens * 0.04))
    mix_denom = max(1, cached_tok + fresh_tok + output_tok + write_tok)
    prompt_denom = max(1, cached_tok + fresh_tok + write_tok)
    cache_reuse_pct = round(cached_tok / prompt_denom * 100.0, 1) if total_tokens else None
    eff_rate = round(tracked_val / total_tokens * 1_000_000.0, 2) if total_tokens > 0 else None
    sessions = max(1, sum(r["runs"] for r in tool_rows)) if tool_rows else 0
    mean_session = round(tracked_val / sessions, 2) if sessions else None

    series = []
    weights = [((i * 5 + 3) % 11) + 2 for i in range(bucket_count)]
    w_sum = max(1, sum(weights))
    for i, w in enumerate(weights):
        end = now - timedelta(hours=max(1, bucket_count - i - 1))
        label = (
            end.strftime("%I:%M %p").lstrip("0")
            if win in {"15m", "30m", "1h", "3h", "6h", "12h", "1d"}
            else end.strftime("%b %d").replace(" 0", " ")
        )
        by_tool = {}
        b_total = 0
        for row in tool_rows:
            part = int(round(row["tokens"] * w / w_sum))
            by_tool[row["key"]] = part
            b_total += part
        series.append(
            {
                "bucketStart": end.isoformat().replace("+00:00", "Z"),
                "bucketKey": f"{win}-{i}",
                "label": label,
                "total": b_total,
                "byTool": by_tool,
            }
        )

    waste_items = [
        {
            "key": "cold-prompt",
            "toolKey": "claude-code",
            "title": "Claude Code — repeated uncached prefixes",
            "detail": "Long system prompt turns missed cache warm window",
            "fix": "Keep prefix instructions stable across turns",
            "perDay": round(2.85 * max(0.5, min(scale, 2.0)), 2),
        },
        {
            "key": "codex-context",
            "toolKey": "codex",
            "title": "Codex — wide repo reads without reuse",
            "detail": "Fresh input spikes across multi-file patch runs",
            "fix": "Narrow targeted file reads in long sessions",
            "perDay": round(1.40 * max(0.5, min(scale, 2.0)), 2),
        },
    ]
    if use_tool != "all":
        waste_items = [item for item in waste_items if item["toolKey"] == use_tool]
    waste_day = round(sum(item["perDay"] for item in waste_items), 2)

    heatmap = []
    for day in range(7):
        for hour in range(24):
            peak_boost = 1.8 if (day < 5 and 9 <= hour <= 18) else 0.45
            wave = ((day * 7 + hour * 3) % 9) + 1
            val = round(0.18 * peak_boost * wave * min(scale, 3.0), 2) if tool_rows else 0.0
            heatmap.append({"weekday": day, "hour": hour, "value": val, "unit": "usd"})

    exact_names = [r["name"] for r in tool_rows if r["isExact"]]
    derived_names = [r["name"] for r in tool_rows if not r["isExact"]]
    plan_cost = round(200.0 * 0.43, 2)
    mtd_val = round(tracked_val * (1.0 if win == "mtd" else 2.1), 2)
    return {
        "window": {"key": win, "label": win_label, "from": (now - timedelta(days=7)).isoformat(), "to": now.isoformat()},
        "generatedAt": now.isoformat().replace("+00:00", "Z"),
        "displayTimezone": "UTC",
        "cadenceSeconds": 60,
        "status": "ok",
        "failingSource": None,
        "machines": demo_report()["machines"],
        "selectedMachine": machine,
        "coverage": {
            "exactProviders": exact_names,
            "derivedProviders": derived_names,
            "note": f"{len(exact_names)} of {len(tool_rows)} providers publish exact per-token rates. Every figure derived from published rates instead of a billed amount is marked ≈. Unpriced usage is counted in tokens and never estimated in dollars.",
        },
        "totals": {
            "trackedValue": tracked_val,
            "trackedValueMarker": "≈" if derived_names else "",
            "priced": priced_val,
            "publishedRate": pub_val,
            "subscriptionUsd": 200.0,
            "knownValue": tracked_val,
            "unpricedModels": [],
            "effectiveCostPerMillionTokens": eff_rate,
            "knownEffectiveCostPerMillionTokens": eff_rate,
            "effectiveCostPricedTokens": total_tokens,
            "effectiveCostCoveragePct": 100.0 if total_tokens else 0.0,
            "effectiveCostComplete": True,
            "tokens": total_tokens,
            "cachedInputTokens": cached_tok,
            "freshInputTokens": fresh_tok,
            "outputTokens": output_tok,
            "records": max(1, len(model_rows) * 8) if model_rows else 0,
            "sessions": sessions,
            "meanSessionValue": mean_session,
            "cacheReusePct": cache_reuse_pct,
        },
        "waste": {"perDay": waste_day, "perMonth": round(waste_day * 30, 2), "items": waste_items},
        "cacheSavings": round(tracked_val * 0.34, 2),
        "projected": {
            "value": mtd_val,
            "planCost": plan_cost,
            "fullMonthPlanCost": 200.0,
            "multiple": round(mtd_val / plan_cost, 2) if plan_cost > 0 else None,
            "multipleBasis": "ratio",
            "method": "Month-to-date reference usage versus configured accrual over the same elapsed days. A comparison at published rates, not a bill.",
        },
        "mix": [
            {"key": "cached_input", "label": "Cached input", "tokens": cached_tok, "share": round(cached_tok / mix_denom * 100, 1)},
            {"key": "fresh_input", "label": "Fresh input", "tokens": fresh_tok, "share": round(fresh_tok / mix_denom * 100, 1)},
            {"key": "output", "label": "Output", "tokens": output_tok, "share": round(output_tok / mix_denom * 100, 1)},
            {"key": "cache_write", "label": "Cache writes", "tokens": write_tok, "share": round(write_tok / mix_denom * 100, 1)},
        ],
        "series": series,
        "tools": tool_rows,
        "models": model_rows,
        "heatmap": heatmap,
        "heatmapUnit": "reference usage (USD at documented rates)",
        "heatmapFallback": win in {"15m", "30m", "1h", "3h", "6h", "12h", "1d"},
    }


def demo_entity(kind: str, key: str, window_key: str = "1d", machine: str = "all") -> dict:
    summary = demo_summary(window_key, "all", machine)
    totals = summary["totals"]
    if kind == "model":
        match = next((m for m in summary["models"] if m["key"] == key), summary["models"][0])
        tool_key = match["toolKey"]
        tool_row = next((t for t in summary["tools"] if t["key"] == tool_key), summary["tools"][0])
        name = match["name"]
        value = match["value"]
        tokens_count = match["tokens"]
        cache_pct = match["cachePct"]
        runs = match["runs"]
        is_exact = match["isExact"]
    else:
        match = next((t for t in summary["tools"] if t["key"] == key), summary["tools"][0])
        tool_key = match["key"]
        tool_row = match
        name = match["name"]
        value = match["value"]
        tokens_count = match["tokens"]
        cache_pct = match["cachePct"]
        runs = match["runs"]
        is_exact = match["isExact"]

    share_val = round((value / totals["trackedValue"] * 100.0), 1) if totals["trackedValue"] else 0.0
    share_tok = round((tokens_count / totals["tokens"] * 100.0), 1) if totals["tokens"] else 0.0
    output_tok = int(round(tokens_count * 0.10))
    cached_tok = int(round(tokens_count * 0.50))
    fresh_tok = int(round(tokens_count * 0.36))
    write_tok = max(0, tokens_count - cached_tok - fresh_tok - output_tok)
    mix_denom = max(1, cached_tok + fresh_tok + output_tok + write_tok)
    hud_match = next((c for c in demo_hud().get("capacity", []) if c.get("providerKey") == tool_key), None)
    series = [
        {
            "bucketStart": b["bucketStart"],
            "bucketKey": b["bucketKey"],
            "label": b["label"],
            "tokens": int(round(b["byTool"].get(tool_key, b["total"] * 0.3))),
        }
        for b in summary["series"]
    ]
    sess_rows = [
        {
            "id": f"{tool_key}-run-{i+1}",
            "project": proj,
            "durationMin": dur,
            "tokens": int(round(tokens_count * frac)),
            "cachePct": round(max(12.0, cache_pct - i * 4.5), 1),
            "value": round(value * frac, 2),
            "sharePct": round(frac * 100.0, 1),
            "isExact": is_exact,
        }
        for i, (proj, dur, frac) in enumerate((("myburnrate-core", 28, 0.34), ("desktop-analytics", 19, 0.24), ("pricing-sync", 12, 0.16)))
    ]
    return {
        "window": summary["window"],
        "kind": kind,
        "key": match["key"],
        "name": name,
        "providerKey": tool_key,
        "providerName": tool_row["name"],
        "plan": tool_row.get("plan", "Usage"),
        "isExact": is_exact,
        "valueMarker": "" if is_exact else "≈",
        "color": tool_row.get("color", "#4840bb"),
        "value": value,
        "shareOfTrackedValue": share_val,
        "tokens": tokens_count,
        "shareOfTokens": share_tok,
        "cachePct": cache_pct,
        "cachedInputTokens": cached_tok,
        "outputTokens": output_tok,
        "runs": runs,
        "valuePerRun": round(value / max(1, runs), 2),
        "tokensPerRun": int(round(tokens_count / max(1, runs))),
        "series": series,
        "mix": [
            {"key": "cached_input", "label": "Cached input", "tokens": cached_tok, "share": round(cached_tok / mix_denom * 100, 1)},
            {"key": "fresh_input", "label": "Fresh input", "tokens": fresh_tok, "share": round(fresh_tok / mix_denom * 100, 1)},
            {"key": "output", "label": "Output", "tokens": output_tok, "share": round(output_tok / mix_denom * 100, 1)},
            {"key": "cache_write", "label": "Cache writes", "tokens": write_tok, "share": round(write_tok / mix_denom * 100, 1)},
        ],
        "opportunity": {
            "kind": "cache_gap" if cache_pct < 65 else "healthy",
            "amount": round(value * 0.18, 2),
            "detail": "Keeping system prompts warm across turns would trim uncached reads.",
            "fix": "Reuse conversation context instead of starting fresh threads",
            "alreadySaved": round(value * 0.31, 2),
        },
        "providerLimits": (hud_match or {}).get("rows", []),
        "sessions": {"rows": sess_rows, "shownTotal": round(sum(r["value"] for r in sess_rows), 2)},
        "isPayg": bool((hud_match or {}).get("isPayg", False)),
    }


class DemoClient:
    def ensure_service(self):
        pass

    def snapshot(self, period, machine):
        return copy.deepcopy(demo_report(period, machine))

    def request(self, path, body=None):
        if body is not None:
            raise DesktopError("Settings changes are disabled in the sample-data demo.")
        if path.startswith("/api/spend/summary"):
            from urllib.parse import parse_qs, urlsplit

            qs = parse_qs(urlsplit(path).query)
            return copy.deepcopy(
                demo_summary(
                    (qs.get("window") or ["1d"])[0],
                    (qs.get("tool") or ["all"])[0],
                    (qs.get("machine") or ["all"])[0],
                )
            )
        if path.startswith("/api/spend/entity"):
            from urllib.parse import parse_qs, urlsplit

            qs = parse_qs(urlsplit(path).query)
            return copy.deepcopy(
                demo_entity(
                    (qs.get("kind") or ["tool"])[0],
                    (qs.get("key") or ["claude-code"])[0],
                    (qs.get("window") or ["1d"])[0],
                    (qs.get("machine") or ["all"])[0],
                )
            )
        if path.startswith("/api/spend/health"):
            return {"status": "ok", "pricingGaps": []}
        if path.startswith("/api/spend/hud/usage"):
            return copy.deepcopy(demo_hud_usage())
        if path.startswith("/api/spend/hud"):
            return copy.deepcopy(demo_hud())
        if path.startswith("/api/connections"):
            return {
                "connections": [
                    {
                        "source": r["key"],
                        "name": r["name"],
                        "kind": "local",
                        "state": "receiving_usage",
                        "location": "Example location",
                        "supportsProfiles": True,
                        "profiles": [
                            {
                                "key": "default",
                                "name": "Default",
                                "location": f"~/.{r['key']}",
                                "origin": "default",
                                "state": "receiving",
                                "revision": 1,
                                "plan": None,
                                "sharesLimitsWith": None,
                            }
                        ],
                        "detected": [],
                    }
                    for r in demo_report()["tools"]
                ]
                + [
                    {
                        "source": "openrouter",
                        "name": "OpenRouter balance",
                        "kind": "api",
                        "capabilities": ["balance"],
                        "state": "connected",
                        "detail": "Prepaid credits connected",
                        "supportsProfiles": False,
                        "profiles": [
                            {
                                "key": "default",
                                "name": "Default",
                                "location": "",
                                "origin": "default",
                                "state": "receiving",
                                "revision": 1,
                                "plan": None,
                                "sharesLimitsWith": None,
                            }
                        ],
                        "detected": [],
                    },
                    {
                        "source": "deepseek",
                        "name": "DeepSeek balance",
                        "kind": "api",
                        "capabilities": ["balance"],
                        "state": "connected",
                        "detail": "Prepaid USD balance connected",
                        "supportsProfiles": False,
                        "profiles": [
                            {
                                "key": "default",
                                "name": "Default",
                                "location": "",
                                "origin": "default",
                                "state": "receiving",
                                "revision": 1,
                                "plan": None,
                                "sharesLimitsWith": None,
                            }
                        ],
                        "detected": [],
                    },
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
            resource_root / "assets" / "icons" / "icon.ico",
        ):
            if sys.platform == "win32" and icon.is_file():
                root.iconbitmap(str(icon))
                break
        root.protocol("WM_DELETE_WINDOW", self.close)
        theme.install(root)
        self.chrome = NativeFrame(root)
        scale = theme.ui_scale(root)

        frame = ttk.Frame(root, padding=(22, 18, 22, 16))
        frame.pack(fill="both", expand=True)

        # Top navigation bar on Wealthfront grouped canvas (#F5F5FA)
        header = ttk.Frame(frame)
        self.header_frame = header
        header.pack(fill="x", pady=(0, 14))
        theme.brand(header).pack(side="left")
        if self.chrome.enabled:
            controls = ttk.Frame(header)
            controls.pack(side="right", padx=(8, 0))
            self.minimize_button = ttk.Button(
                controls,
                text="−",
                width=2,
                style="Window.TButton",
                command=self.chrome.minimize,
            )
            self.minimize_button.pack(side="left")
            self.close_button = ttk.Button(
                controls,
                text="×",
                width=2,
                style="Close.Window.TButton",
                command=self.close,
            )
            self.close_button.pack(side="left", padx=(2, 0))
        ttk.Button(
            header, text="Settings", style="Quiet.TButton", command=self.settings
        ).pack(side="right", padx=(8, 0))
        self.live_status = ttk.Label(
            header, text="●  Starting tracker", style="Live.TLabel"
        )
        self.live_status.pack(side="right")

        # Elevated white Hero Portfolio Card (#FFFFFF with 1px #E4E4EE border)
        hero_shell, hero = theme.card(frame, padding=(20, 16, 20, 16))
        hero_shell.pack(fill="x", pady=(0, 12))

        hero_top = ttk.Frame(hero, style="Card.TFrame")
        self.heading_frame = hero_top
        hero_top.pack(fill="x", pady=(0, 6))
        ttk.Label(
            hero_top,
            text="API-EQUIVALENT VALUE",
            style="CardEyebrow.TLabel",
        ).pack(side="left")
        self.machine_picker = ttk.Combobox(
            hero_top,
            textvariable=self.computer,
            values=["All computers"],
            state="readonly",
            width=18,
        )
        self.machine_picker.pack(side="right")
        self.machine_picker.bind("<<ComboboxSelected>>", lambda _: self.change_scope())

        value_row = ttk.Frame(hero, style="Card.TFrame")
        value_row.pack(fill="x", pady=(0, 2))
        self.value = ttk.Label(value_row, text="Loading…", style="Value.TLabel")
        self.value.pack(side="left")
        ttk.Label(value_row, text="USD", style="CardMuted.TLabel").pack(
            side="left", anchor="s", padx=(10, 0), pady=(0, 10)
        )
        self.count = ttk.Label(
            hero,
            text="Measured tokens, valued at API rates.",
            style="CardCaption.TLabel",
        )
        self.count.pack(anchor="w")

        # Wealthfront-style proportional provider allocation bar
        alloc_frame = ttk.Frame(hero, style="Card.TFrame")
        self.alloc_frame = alloc_frame
        alloc_frame.pack(fill="x", pady=(12, 4))
        self.alloc_canvas = tk.Canvas(
            alloc_frame,
            height=max(6, round(8 * scale)),
            background=theme.SURFACE,
            highlightthickness=0,
            bd=0,
        )
        self.alloc_canvas.pack(fill="x")
        self.alloc_canvas.bind("<Configure>", lambda _: self._paint_allocation())

        metrics = ttk.Frame(hero, style="Card.TFrame")
        self.metrics_frame = metrics
        metrics.pack(fill="x", pady=(10, 4))
        self.metric_labels = []
        for index, label in enumerate(
            ("Recorded tokens", "Active tools", "Computers in view")
        ):
            cell = ttk.Frame(metrics, style="Card.TFrame")
            cell.grid(row=0, column=index, sticky="w")
            metrics.columnconfigure(index, weight=1, uniform="metrics")
            number = ttk.Label(cell, text="—", style="Metric.TLabel")
            number.pack(anchor="w")
            ttk.Label(cell, text=label, style="CardCaption.TLabel").pack(
                anchor="w", pady=(2, 0)
            )
            self.metric_labels.append(number)

        periods_row = ttk.Frame(hero, style="Card.TFrame")
        periods_row.pack(fill="x", pady=(12, 0))
        periods = ttk.Frame(periods_row, padding=3, style="Segment.TFrame")
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

        notice_row = ttk.Frame(frame)
        notice_row.pack(fill="x")
        self.notice = theme.notice(notice_row)

        # Reserve footer before the expandable Providers Card so small heights never clip Refresh
        bottom = ttk.Frame(frame)
        self.status = ttk.Label(
            bottom, text="Starting tracker…", style="Caption.TLabel"
        )
        self.status.pack(side="left", fill="x", expand=True)
        self.refresh_button = ttk.Button(
            bottom, text="↻  Refresh", command=lambda: self.refresh(restart=True)
        )
        self.refresh_button.pack(side="right", padx=(8, 0))
        bottom.pack(side="bottom", fill="x", pady=(10, 0))

        # Elevated white Providers Card (#FFFFFF with 1px #E4E4EE border)
        providers_shell, providers = theme.card(frame, padding=(16, 12, 16, 12))
        providers_shell.pack(fill="both", expand=True)

        self.detail = ttk.Label(
            providers,
            text="Expand a tool for model and token details.",
            wraplength=540,
            style="CardCaption.TLabel",
        )
        self.detail.pack(side="bottom", fill="x", pady=(8, 0))
        ttk.Separator(providers).pack(side="bottom", fill="x", pady=(6, 0))

        table_frame = ttk.Frame(providers, style="Card.TFrame")
        self.tree = ttk.Treeview(
            table_frame,
            columns=("tokens", "value"),
            style="Usage.Treeview",
            show="tree headings",
            selectmode="browse",
        )
        self.tree.heading("#0", text="PROVIDERS", anchor="w")
        self.tree.heading("tokens", text="TOKENS", anchor="e")
        self.tree.heading("value", text="API VALUE", anchor="e")
        self.tree.column("#0", width=290, minwidth=185)
        self.tree.column(
            "tokens",
            width=round(108 * scale),
            minwidth=80,
            anchor="e",
            stretch=False,
        )
        self.tree.column(
            "value",
            width=round(108 * scale),
            minwidth=90,
            anchor="e",
            stretch=False,
        )
        self.tree.tag_configure("model", foreground=theme.MUTED, font=(theme.FONT, 9))
        self.tool_icons = {}
        scroll = ttk.Scrollbar(table_frame, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.selection)
        table_frame.pack(fill="both", expand=True)

        def resize(event):
            if event.widget == root:
                for label in (self.notice, self.detail):
                    label.configure(wraplength=max(240, event.width - 84))
                self._adapt_layout(event.height)

        root.bind("<Configure>", resize, add="+")
        self.chrome.drag_region(header)
        self.chrome.drag_region(hero_top)
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
        alloc = getattr(self, "alloc_frame", None)
        if compact:
            if alloc is not None:
                alloc.pack_forget()
            self.metrics_frame.pack_forget()
        else:
            if alloc is not None:
                alloc.pack(fill="x", pady=(12, 4), after=self.count)
                self.metrics_frame.pack(fill="x", pady=(10, 4), after=alloc)
            else:
                self.metrics_frame.pack(fill="x", pady=(10, 4), after=self.count)
        self._paint_value()
        if self.report is not None:
            self._paint_summary()
            self._paint_allocation()

    def _paint_value(self):
        base = 30 if self.compact else 42
        size = max(22, base - max(0, len(self.value_text) - 9) * 2)
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

    def _paint_allocation(self):
        canvas = getattr(self, "alloc_canvas", None)
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.delete("all")
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width <= 4 or height <= 1:
            return
        canvas.create_rectangle(0, 0, width, height, fill=theme.SEGMENT_BG, outline="")
        if self.report is None:
            return
        tools = [
            item
            for item in (self.report.get("tools") or [])
            if not item.get("organization")
        ] or list(self.report.get("tools") or [])
        if not tools:
            return
        weights = [max(0.0, float(item.get("valueUsd") or 0.0)) for item in tools]
        if sum(weights) <= 0:
            weights = [max(0.0, float(item.get("tokens") or 0.0)) for item in tools]
        total_weight = sum(weights)
        if total_weight <= 0:
            return
        active = [(item, w) for item, w in zip(tools, weights) if w > 0]
        gap = 2 if len(active) > 1 else 0
        usable = max(1, width - gap * (len(active) - 1))
        cursor = 0.0
        for idx, (item, weight) in enumerate(active):
            seg_w = (
                width - round(cursor)
                if idx == len(active) - 1
                else max(3, round(usable * (weight / total_weight)))
            )
            x0 = round(cursor)
            x1 = min(width, x0 + seg_w)
            if x1 > x0:
                canvas.create_rectangle(
                    x0,
                    0,
                    x1,
                    height,
                    fill=theme.tool_color(item.get("key")),
                    outline="",
                )
            cursor = x1 + gap

    def _set_live_status(self, text: str, *, warn: bool = False):
        self.live_status.configure(
            text=text,
            foreground=theme.WARNING if warn else theme.GREEN,
            style="WarnLive.TLabel" if warn else "Live.TLabel",
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
                            self._set_live_status("●  Unavailable", warn=True)
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
        self._paint_allocation()
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
                self._set_live_status("●  Refresh failed", warn=True)
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
        self._paint_allocation()
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
        self.machine_ids = {"This computer" if data.get("scope") == "local-fallback" else "All computers": "all"}
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
        warn = scope in {"unavailable", "local-fallback"}
        self._set_live_status(
            "●  Sample data"
            if self.demo
            else "●  Hub unavailable"
            if scope == "unavailable"
            else "●  Local copy"
            if scope == "local-fallback"
            else "●  Tracking",
            warn=warn,
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
        from tkinter import filedialog, messagebox, ttk

        tk = app.tk
        window.title("BURNRATE Settings")
        window.transient(app.root)
        window.attributes("-topmost", app.topmost.get())
        theme.size_window(window, 650, 620, 580, 520)

        header = ttk.Frame(window, padding=(24, 20, 24, 14))
        header.pack(fill="x")
        ttk.Label(header, text="Settings", style="Title.TLabel").pack(side="left")
        ttk.Button(
            header, text="Done", style="Accent.TButton", command=window.destroy
        ).pack(side="right")

        notebook = ttk.Notebook(window)
        notebook.pack(fill="both", expand=True, padx=24, pady=(0, 22))
        sources_tab, computers_tab, general_tab = (
            ttk.Frame(notebook, padding=(0, 4, 0, 0)) for _ in range(3)
        )
        for label, frame in (
            ("Sources", sources_tab),
            ("Computers", computers_tab),
            ("General", general_tab),
        ):
            notebook.add(frame, text=label)

        sources_shell, sources = theme.card(sources_tab, padding=(18, 16, 18, 16))
        sources_shell.pack(fill="both", expand=True)
        ttk.Label(
            sources,
            text="USAGE & BALANCE SOURCES ON THIS COMPUTER",
            style="CardEyebrow.TLabel",
        ).pack(anchor="w", pady=(0, 8))
        listing = ttk.Treeview(
            sources, columns=("state",), show="tree headings", height=8
        )
        listing.heading("#0", text="PROVIDER", anchor="w")
        listing.heading("state", text="STATUS", anchor="w")
        listing.column("#0", width=270)
        listing.column("state", width=180)
        details = ttk.Label(
            sources,
            text="Select a source to change its location or connection.",
            wraplength=520,
            style="CardCaption.TLabel",
        )
        rows = {}
        disconnect_change = SourceMutation(app.client)

        computers_shell, computers = theme.card(computers_tab, padding=(18, 16, 18, 16))
        computers_shell.pack(fill="both", expand=True)
        ttk.Label(computers, text="PAIRED COMPUTERS", style="CardEyebrow.TLabel").pack(
            anchor="w", pady=(0, 8)
        )
        machines = ttk.Treeview(
            computers, columns=("state",), show="tree headings", height=8
        )
        machines.heading("#0", text="COMPUTER", anchor="w")
        machines.heading("state", text="STATUS", anchor="w")
        feedback = ttk.Label(
            computers,
            text="All paired computers contribute to the combined usage view.",
            wraplength=510,
            style="CardCaption.TLabel",
        )

        general_shell, general = theme.card(general_tab, padding=(18, 18, 18, 18))
        general_shell.pack(fill="both", expand=True)

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
                )

            def show(data):
                if not window.winfo_exists():
                    return
                listing.delete(*listing.get_children())
                rows.clear()
                for row in data[0].get("connections") or []:
                    profiles = [p for p in (row.get("profiles") or []) if isinstance(p, dict)]
                    extra_count = max(0, len(profiles) - 1)
                    label = row["name"] if not extra_count else f"{row['name']} (+{extra_count})"
                    key = listing.insert(
                        "",
                        "end",
                        text=label,
                        values=(
                            str(row.get("state") or "Unknown")
                            .replace("_", " ")
                            .capitalize(),
                        ),
                        image=app._tool_image(row["source"]),
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

            app.submit(read, show, failure)

        def selected():
            selection = listing.selection()
            return rows.get(selection[0]) if selection else None

        def describe(_=None):
            row = selected()
            if not row:
                details.configure(text="")
                return
            base = (
                row.get("detail")
                or row.get("location")
                or row.get("capabilityNote")
                or ""
            )
            profiles = [p for p in (row.get("profiles") or []) if isinstance(p, dict)]
            if len(profiles) > 1:
                names = ", ".join(str(p.get("name") or p.get("key") or "") for p in profiles)
                base = f"{base} \u00b7 Profiles: {names}" if base else f"Profiles: {names}"
            details.configure(text=base)

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
            outer = ttk.Frame(dialog, padding=18)
            outer.pack(fill="both", expand=True)
            card_shell, form = theme.card(outer, padding=(18, 16, 18, 16))
            card_shell.pack(fill="both", expand=True)
            api = row.get("kind") == "api"
            ttk.Label(
                form,
                text=row.get("credentialHelp") if api else "Usage location",
                wraplength=480,
                style="Card.TLabel",
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
                style="CardCaption.TLabel",
            ).pack(anchor="w", pady=10)

            def connect_source():
                profiles = [p for p in (row.get("profiles") or []) if isinstance(p, dict)]
                primary_profile = next((p.get("key") for p in profiles if p.get("key")), "default")
                revision = row.get("revision")
                if revision is None and profiles:
                    revision = profiles[0].get("revision", 0)
                body = {
                    "source": row["source"],
                    "profile": primary_profile,
                    "operation": "connect",
                    "revision": revision or 0,
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
            profiles = [p for p in (row.get("profiles") or []) if isinstance(p, dict)]
            primary_profile = next((p.get("key") for p in profiles if p.get("key")), "default")
            revision = row.get("revision")
            if revision is None and profiles:
                revision = profiles[0].get("revision", 0)
            body = {
                "source": row["source"],
                "profile": primary_profile,
                "operation": "disable",
                "revision": revision or 0,
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

        actions = ttk.Frame(sources, style="Card.TFrame")
        actions.pack(side="bottom", fill="x")
        ttk.Button(actions, text="Refresh", command=load).pack(side="left")
        ttk.Button(actions, text="Connect / change…", command=configure_source).pack(
            side="left", padx=8
        )
        disconnect_button = ttk.Button(actions, text="Disconnect", command=disconnect)
        disconnect_button.pack(side="right")
        details.pack(side="bottom", fill="x", pady=(8, 12))
        listing.pack(fill="both", expand=True, pady=6)

        feedback.pack(side="bottom", fill="x", pady=(8, 12))
        machines.pack(fill="both", expand=True, pady=(0, 10))

        def pin():
            app.root.attributes("-topmost", app.topmost.get())
            app.chrome.refresh()
            app.save_preferences()

        ttk.Label(general, text="WINDOW PREFERENCES", style="CardEyebrow.TLabel").pack(
            anchor="w", pady=(0, 8)
        )
        ttk.Checkbutton(
            general, text="Keep usage window on top", variable=app.topmost, command=pin
        ).pack(anchor="w", pady=(0, 14))
        ttk.Label(
            general,
            text="Collection continues in the background when this window closes. Reopen BURNRATE to see usage.",
            wraplength=500,
            style="CardCaption.TLabel",
        ).pack(anchor="w")
        if app.demo:
            ttk.Label(
                general,
                text="Sample-data demo: connections and account sync are disabled.",
                style="CardCaption.TLabel",
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
    if os.getenv("BURNRATE_TK_FALLBACK") != "1":
        if smoke_test and os.getenv("GITHUB_ACTIONS") == "true":
            # GitHub Actions Windows runners execute in headless Session 0 where
            # Edge WebView2 has no interactive desktop compositor; verify bundled
            # Wealthfront HTML, embedded Manrope WOFF2, and bridge APIs,
            # then run the native Tk window smoke path.
            from spend_app.tab_window import load_tab_html
            from spend_app.wealthfront_window import DesktopBridge, load_ui_html

            bundled_html = load_ui_html()
            if '<symbol id="claude"' not in load_tab_html():
                return 1
            bridge = DesktopBridge(
                DemoClient() if demo else NativeClient(settings, port), demo=demo
            )
            probe = bridge.fetch_usage("today", "all")
            if (
                "Manrope" not in bundled_html
                or "data:font/woff2;base64," not in bundled_html
                or not probe.get("ok")
            ):
                return 1
        else:
            try:
                from webview.errors import WebViewException

                from spend_app.wealthfront_window import run_webview
            except ImportError:
                run_webview = None
                WebViewException = RuntimeError
            if run_webview is not None:
                try:
                    return run_webview(
                        DemoClient() if demo else NativeClient(settings, port),
                        preferences=None
                        if demo
                        else settings.database_path.parent / "native-window.json",
                        demo=demo,
                        smoke_test=smoke_test,
                    )
                except WebViewException:
                    pass
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass
    try:
        root = tk.Tk()
        root.withdraw()
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
    root.deiconify()
    root.mainloop()
    return result[0]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="BURNRATE native usage window")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--port", type=int)
    options = parser.parse_args()
    raise SystemExit(main(demo=options.demo, port=options.port))
