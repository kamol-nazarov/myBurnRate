"""Headless checks for the native Tk client and its local HTTP boundary."""

from typing import ClassVar

import pytest

from spend_app.native_desktop import (
    DemoClient,
    DesktopError,
    NativeClient,
    UsageWindow,
    money,
    tokens,
)
from tests_spend.test_api import make_settings


class Response:
    def __init__(self, status=200, payload=None, *, invalid_json=False):
        self.status_code = status
        self.payload = payload
        self.invalid_json = invalid_json
        self.is_success = 200 <= status < 300

    def json(self):
        if self.invalid_json:
            raise ValueError("not JSON")
        return self.payload


class FakeHttpClient:
    instances: ClassVar[list] = []
    response = Response(payload={"total": {"events": 0}, "tools": []})

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
        return self.response

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.response


def _client(tmp_path, port=17331):
    settings = make_settings(tmp_path / "native-client.db")
    return NativeClient(settings, port=port)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0"),
        (999, "999"),
        (1000, "1.00K"),
        (1_250_000, "1.25M"),
        (-2_500_000, "-2.50M"),
        (float("nan"), "—"),
        (None, "—"),
    ],
)
def test_token_formatting(value, expected):
    assert tokens(value) == expected


def test_money_formatting_distinguishes_zero_and_partial_values():
    assert money(0) == "$0.00"
    assert money(0.001) == "<$0.01"
    assert money(0.001, complete=False) == "Partial <$0.01"
    assert money(12.345, complete=True) == "$12.35"
    assert money(12.345, complete=False) == "≥$12.35"
    assert money(None) == "Unpriced"
    assert money(float("inf")) == "Unpriced"


def test_demo_client_returns_independent_sample_data_and_disables_writes():
    demo = DemoClient()
    first = demo.snapshot("today", "all")
    assert first["scope"] == "all-computers"
    assert first["total"]["valueUsd"] > 0
    first["tools"].clear()
    assert len(demo.snapshot("today", "all")["tools"]) == 5
    assert demo.request("/api/connections")["connections"]
    with pytest.raises(DesktopError, match="disabled"):
        demo.request("/api/connections", {"operation": "disable"})


def test_native_client_uses_loopback_and_disables_redirects_and_proxy_environment(
    tmp_path, monkeypatch
):
    import spend_app.native_desktop as desktop

    FakeHttpClient.instances.clear()
    FakeHttpClient.response = Response(payload={"total": {}, "tools": []})
    monkeypatch.setattr(desktop.httpx, "Client", FakeHttpClient)
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "local-secret")
    client = _client(tmp_path, port=17432)
    result = client.snapshot("all", "all")
    assert result["tools"] == []
    transport = FakeHttpClient.instances[-1]
    method, url, kwargs = transport.calls[0]
    assert method == "GET"
    assert url.startswith("http://127.0.0.1:17432/api/spend/usage?")
    assert "period=all" in url and "machine=all" in url
    assert kwargs["headers"] == {
        "Origin": "http://127.0.0.1:17432",
        "X-BURNRATE-Request": "1",
        "Authorization": "Bearer local-secret",
    }
    assert transport.options["trust_env"] is False
    assert transport.options["follow_redirects"] is False


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (Response(status=401, payload={}), "authenticate"),
        (Response(status=302, payload={}), "busy"),
        (Response(status=200, payload=[]), "invalid response"),
        (Response(status=200, invalid_json=True), "invalid response"),
        (Response(status=503, payload={"error": "maintenance"}), "maintenance"),
    ],
)
def test_native_client_reports_http_and_payload_failures(
    tmp_path, monkeypatch, response, message
):
    import spend_app.native_desktop as desktop

    FakeHttpClient.instances.clear()
    FakeHttpClient.response = response
    monkeypatch.setattr(desktop.httpx, "Client", FakeHttpClient)
    client = _client(tmp_path)
    with pytest.raises(DesktopError, match=message):
        client.snapshot("today", "all")


def test_native_client_rejects_non_api_paths_without_network(tmp_path, monkeypatch):
    import spend_app.native_desktop as desktop

    def unexpected(*args, **kwargs):
        raise AssertionError("HTTP client must not be created")

    monkeypatch.setattr(desktop.httpx, "Client", unexpected)
    with pytest.raises(ValueError, match="Only local BURNRATE API"):
        _client(tmp_path).request("https://example.invalid/api/spend/usage")


def test_machine_bearer_must_be_valid_for_native_usage_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import spend_app.native_desktop as desktop
    from spend_app.api import create_app
    from spend_app.db import initialize
    from spend_app.sync import register_machine

    monkeypatch.delenv("BURNRATE_ACCESS_TOKEN", raising=False)
    settings = make_settings(tmp_path / "machine-auth.db")
    initialize(settings.database_path)
    valid_token = register_machine(
        settings.database_path, name="Paired", os_name="test"
    )["token"]
    application = TestClient(create_app(settings, enable_scheduler=False))

    class LocalClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            return application.get(url, **kwargs)

    monkeypatch.setattr(desktop.httpx, "Client", lambda **kwargs: LocalClient())
    client = _client(tmp_path)
    try:
        monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "br_m_bogus-revoked")
        with pytest.raises(DesktopError, match="authenticate"):
            client.snapshot("all", "all")
        monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", valid_token)
        assert client.snapshot("all", "all")["scope"] == "all-computers"
    finally:
        application.close()


def test_service_health_refuses_another_process_without_spawning(tmp_path, monkeypatch):
    import spend_app.native_desktop as desktop

    FakeHttpClient.instances.clear()
    FakeHttpClient.response = Response(payload={"service": "other-app"})
    monkeypatch.setattr(desktop.httpx, "Client", FakeHttpClient)
    monkeypatch.setattr(
        desktop.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail(
            "must not spawn over a foreign local service"
        ),
    )
    with pytest.raises(DesktopError, match="Another service"):
        _client(tmp_path).ensure_service()
    request = FakeHttpClient.instances[0]
    assert request.calls[0][1] == "http://127.0.0.1:17331/healthz?supervisor=1"


def test_service_health_accepts_real_burnrate_supervisor_response(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient

    import spend_app.native_desktop as desktop
    from spend_app.api import create_app
    from spend_app.db import initialize

    settings = make_settings(tmp_path / "owned-service.db")
    initialize(settings.database_path)
    application = TestClient(create_app(settings, enable_scheduler=False))

    class LocalClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, **kwargs):
            return application.get(url, **kwargs)

    monkeypatch.setattr(desktop.httpx, "Client", lambda **kwargs: LocalClient())
    monkeypatch.setattr(
        desktop.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("healthy BURNRATE service must be reused"),
    )
    try:
        _client(tmp_path).ensure_service()
    finally:
        application.close()


class Label:
    def __init__(self):
        self.text = ""

    def configure(self, **kwargs):
        if "text" in kwargs:
            self.text = kwargs["text"]


class Tree:
    def __init__(self):
        self.rows = {}
        self.roots = []
        self.selected = ()
        self.next_id = 0

    def get_children(self, item=""):
        if item:
            return self.rows[item]["children"]
        return self.roots

    def item(self, item, option=None, **kwargs):
        row = self.rows[item]
        if kwargs:
            row.update(kwargs)
        return row.get(option) if option else row

    def delete(self, *items):
        for item in items:
            if item in self.roots:
                self.roots.remove(item)
            self.rows.pop(item, None)

    def insert(self, parent, _index, *, text, values, open=False, image=None, tags=()):
        self.next_id += 1
        key = str(self.next_id)
        self.rows[key] = {
            "parent": parent,
            "text": text,
            "values": values,
            "open": open,
            "children": [],
        }
        (self.rows[parent]["children"] if parent else self.roots).append(key)
        return key

    def selection(self):
        return self.selected

    def selection_set(self, item):
        self.selected = (item,)


class Picker:
    def configure(self, **kwargs):
        self.values = kwargs.get("values", [])


class StringVar:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def test_usage_window_renders_local_totals_and_selected_reasoning_without_tk_root():
    app = object.__new__(UsageWindow)
    app.tree = Tree()
    app.items = {}
    app.machine_ids = {"All computers": "all"}
    app.selected_machine = "all"
    app.computer = StringVar("All computers")
    app.machine_picker = Picker()
    app.value = Label()
    app.count = Label()
    app.notice = Label()
    app.detail = Label()
    app.status = Label()
    app.live_status = Label()
    app.metric_labels = [Label() for _ in range(3)]
    app._tool_image = lambda key: ""
    app._fit_numeric_columns = lambda: None
    app.compact = False
    app.demo = False
    app.report = None
    tool = {
        "key": "codex",
        "name": "Codex",
        "tokens": 1_200_000,
        "valueUsd": 1.23,
        "complete": False,
        "events": 2,
        "unpricedEvents": 1,
        "input": 500,
        "cached": 200,
        "writes": 30,
        "output": 100,
        "reasoning": 40,
        "organization": False,
        "models": [
            {
                "key": "gpt-test",
                "name": "GPT Test",
                "tokens": 1_200_000,
                "valueUsd": 1.23,
                "complete": False,
                "input": 500,
                "cached": 200,
                "writes": 30,
                "output": 100,
                "reasoning": 40,
                "unpricedEvents": 1,
            }
        ],
    }
    report = {
        "total": {
            "valueUsd": 1.23,
            "complete": False,
            "events": 2,
            "tokens": 1_200_000,
        },
        "tools": [tool],
        "generatedAt": "2026-11-13T12:00:00Z",
        "machines": [{"id": "local", "name": "Desktop", "online": True}],
    }

    app.render(report)
    root = app.tree.get_children()[0]
    assert app.value.text == "≥$1.23"
    assert app.count.text == "Measured tokens, valued at API rates."
    assert [label.text for label in app.metric_labels] == ["1.20M", "1", "1"]
    assert "known subtotal" in app.notice.text
    assert "1 offline" not in app.status.text
    assert app.tree.rows[root]["text"] == "Codex"
    model = app.tree.get_children(root)[0]
    app.tree.selection_set(model)
    app.selection()
    assert "Reasoning within output 40" in app.detail.text
    assert "1 unpriced records" in app.detail.text
    assert "Desktop" in app.machine_ids


def _headless_window():
    app = object.__new__(UsageWindow)
    app.tree = Tree()
    app.items = {}
    app.machine_ids = {"All computers": "all"}
    app.selected_machine = "all"
    app.computer = StringVar("All computers")
    app.machine_picker = Picker()
    app.value = Label()
    app.count = Label()
    app.notice = Label()
    app.detail = Label()
    app.status = Label()
    app.live_status = Label()
    app.metric_labels = [Label() for _ in range(3)]
    app._tool_image = lambda key: ""
    app._fit_numeric_columns = lambda: None
    app.compact = False
    app.demo = False
    app.report = None
    return app


def test_selected_machine_id_survives_rename_removal_and_duplicate_labels():
    app = _headless_window()

    def report(machines):
        return {
            "total": {"valueUsd": 1.0, "complete": True, "events": 1, "tokens": 10},
            "tools": [],
            "generatedAt": "2026-11-13T12:00:00Z",
            "machines": machines,
        }

    app.selected_machine = "laptop-b"
    app.render(
        report(
            [
                {"id": "laptop-a", "name": "Laptop", "online": True},
                {"id": "laptop-b", "name": "Laptop", "online": True},
            ]
        )
    )
    assert app.computer.get() == "Laptop (laptop-b)"
    assert app.selected_machine == "laptop-b"

    app.render(
        report(
            [
                {"id": "laptop-a", "name": "Laptop", "online": True},
                {"id": "laptop-b", "name": "Work laptop", "online": True},
            ]
        )
    )
    assert app.computer.get() == "Work laptop"
    assert app.selected_machine == "laptop-b"

    app.render(report([{"id": "laptop-a", "name": "Laptop", "online": True}]))
    assert app.computer.get() == "Previously paired computer"
    assert app.selected_machine == "laptop-b"
    assert "no longer paired" in app.notice.text


def test_drain_isolates_closed_tk_dialog_and_schedules_next_poll():
    import queue
    from concurrent.futures import Future
    from types import SimpleNamespace

    class TclError(Exception):
        pass

    class Root:
        def __init__(self):
            self.scheduled = []

        def after(self, delay, callback):
            self.scheduled.append((delay, callback))

    app = object.__new__(UsageWindow)
    app.tk = SimpleNamespace(TclError=TclError)
    app.closed = False
    app.root = Root()
    app.messages = queue.Queue()
    failed = []
    future = Future()
    future.set_result({"ready": True})

    def closed_dialog(_result):
        raise TclError("settings dialog was destroyed before callback")

    app.messages.put((future, closed_dialog, lambda exc: failed.append(exc)))
    app._drain()
    assert failed == []
    assert len(app.root.scheduled) == 1
    assert app.root.scheduled[0][0] == 80
    assert app.root.scheduled[0][1].__self__ is app


def test_desktop_cli_check_only_and_demo_use_native_path_without_browser(
    tmp_path, monkeypatch, capsys
):
    import sys
    import types
    import webbrowser

    import spend_app.native_desktop as desktop
    from spend_app import cli

    settings = make_settings(tmp_path / "cli.db")
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    fake_tk = types.ModuleType("tkinter")
    fake_tk.TkVersion = 8.6
    fake_tk.ttk = object()
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    monkeypatch.setattr(sys, "argv", ["burnrate", "desktop", "--check-only"])
    assert cli._main() == 0
    assert '"interface": "native"' in capsys.readouterr().out

    calls = []
    monkeypatch.setattr(
        desktop,
        "main",
        lambda settings, **kwargs: calls.append((settings, kwargs)) or 0,
    )
    monkeypatch.setattr(
        webbrowser,
        "open",
        lambda *args, **kwargs: pytest.fail("native desktop must not launch a browser"),
    )
    monkeypatch.setattr(sys, "argv", ["burnrate", "desktop", "--demo", "--smoke-test"])
    assert cli._main() == 0
    assert calls == [(settings, {"demo": True, "port": None, "smoke_test": True})]


def test_source_mutation_retries_committed_connection_with_same_id_and_rotates_on_change(
    tmp_path,
):
    from fastapi.testclient import TestClient

    from spend_app.api import create_app
    from spend_app.db import initialize
    from spend_app.native_desktop import SourceMutation

    settings = make_settings(tmp_path / "source-retry.db")
    initialize(settings.database_path)
    server = TestClient(
        create_app(settings, enable_scheduler=False), base_url="http://127.0.0.1:17331"
    )

    class CommitThenLoseFirstReply:
        def __init__(self):
            self.bodies = []

        def request(self, path, body=None):
            self.bodies.append(dict(body))
            response = server.post(
                path,
                json=body,
                headers={
                    "Origin": "http://127.0.0.1:17331",
                    "X-BURNRATE-Request": "1",
                },
            )
            assert response.status_code == 200, response.text
            if len(self.bodies) == 1:
                # The server has committed and stored its idempotency receipt,
                # but the desktop process never received the successful body.
                raise DesktopError("synthetic lost response")
            return response.json()

    transport = CommitThenLoseFirstReply()
    mutation = SourceMutation(transport)
    values = {
        "source": "codex_local",
        "operation": "disable",
        "revision": 0,
        "mode": "manual",
    }
    try:
        with pytest.raises(DesktopError, match="lost response"):
            mutation.send(values)
        retry_result = mutation.send(values)
        first_id, retry_id = [body["requestId"] for body in transport.bodies]
        assert first_id == retry_id == retry_result["requestId"]

        # A changed dialog value starts a distinct mutation with a fresh ID.
        changed_result = mutation.send(
            {**values, "revision": 1, "location": "changed path"}
        )
        changed_id = transport.bodies[2]["requestId"]
        assert changed_id != first_id
        assert changed_result["requestId"] == changed_id
    finally:
        server.close()


def test_usage_only_scheduler_keeps_collection_jobs_and_omits_dashboard_jobs(
    tmp_path, monkeypatch
):
    from spend_app import collector, scheduler
    from spend_app.pricing import PricingEngine

    settings = make_settings(tmp_path / "scheduler.db")
    monkeypatch.setenv("BURNRATE_USAGE_ONLY", "1")
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "0")
    monkeypatch.setattr(collector, "enabled", lambda: False)
    scheduled = scheduler.create_scheduler(
        settings, PricingEngine.load(settings.pricing_path)
    )
    job_ids = {job.id for job in scheduled.get_jobs()}
    assert {"local-ingest", "provider-admin"} <= job_ids
    assert not {"subscriptions", "quota-poll", "activity"} & job_ids


def test_provider_logos_resolve_all_tools_and_admin_sources():
    from spend_app import native_theme as theme
    from spend_app.provider_logos import ICONS_1X, ICONS_2X, ICONS_15X, resolve_logo_key

    assert theme.BG == "#f5f5fa"
    assert theme.SURFACE == "#ffffff"
    assert theme.ACCENT == "#4840bb"

    expected = {
        "claude-code": "claude",
        "claude_local": "claude",
        "api:anthropic_admin": "anthropic",
        "codex": "codex",
        "api:openai_admin": "openai",
        "opencode": "opencode",
        "muse": "muse",
        "grok": "grok",
        "zcode": "zcode",
        "antigravity": "antigravity",
        "cursor": "cursor",
        "traycer": "traycer",
        "openrouter": "openrouter",
        "gemini": "gemini",
        "unrecognized-source": "custom",
    }
    for source_key, logo_key in expected.items():
        assert resolve_logo_key(source_key) == logo_key
        assert logo_key in ICONS_1X
        assert logo_key in ICONS_15X
        assert logo_key in ICONS_2X
        assert theme.tool_color(source_key).startswith("#")
