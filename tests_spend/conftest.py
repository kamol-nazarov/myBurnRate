"""Hermetic hooks for the spend suite.

Collection is limited by pytest.ini `testpaths = tests_spend`. This module
does not rewrite Path.home() globally — CLI tests assert default glob strings
that include the real home path without ingesting it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import os
import tempfile

import pytest

FROZEN_NOW = datetime(2026, 8, 30, 23, tzinfo=UTC)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})
_FAKE_TRANSPORTS = frozenset(
    {
        "MockTransport",
        "ASGITransport",
        "WSGITransport",
        "ASGI2Transport",
        "_TestClientTransport",
    }
)


class FrozenDateTime(datetime):
    """`datetime.now(UTC)` always returns the frozen acceptance clock."""

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return FROZEN_NOW.replace(tzinfo=None)
        return FROZEN_NOW.astimezone(tz)


def pytest_configure(config) -> None:
    # Module-level ASGI app imports must never initialize a user's default DB.
    config._burnrate_test_data = tempfile.TemporaryDirectory(prefix="burnrate-tests-")
    config._burnrate_previous_db = os.environ.get("SPEND_DATABASE_PATH")
    os.environ["SPEND_DATABASE_PATH"] = str(Path(config._burnrate_test_data.name) / "default.db")
    config.addinivalue_line(
        "markers",
        "isolated_home: set HOME/USERPROFILE to a tmp_path (does not patch Path.home)",
    )


def pytest_unconfigure(config) -> None:
    if not hasattr(config, "_burnrate_test_data"):
        return
    if config._burnrate_previous_db is None:
        os.environ.pop("SPEND_DATABASE_PATH", None)
    else:
        os.environ["SPEND_DATABASE_PATH"] = config._burnrate_previous_db
    config._burnrate_test_data.cleanup()


def pytest_ignore_collect(collection_path, config) -> bool | None:
    """Refuse files that are not under tests_spend (belt-and-suspenders with testpaths)."""
    tests_root = Path(__file__).resolve().parent
    path = Path(collection_path).resolve()
    try:
        path.relative_to(tests_root)
    except ValueError:
        return True
    return None


@pytest.fixture(autouse=True)
def _legacy_bearer_for_hermetic_tests(monkeypatch):
    """Keep pre-0.5.7 admin-automation behavior for tests that use the bearer.

    Production defaults the static bearer off; a dedicated test removes this
    variable to assert the current default.
    """
    monkeypatch.setenv("BURNRATE_ALLOW_ADMIN_BEARER", "1")


@pytest.fixture(autouse=True)
def _test_allowed_hosts(monkeypatch):
    """Fake hub hostnames used by hermetic sync tests are explicitly trusted.

    The production allowlist stays loopback-only; a test that probes an
    untrusted host (for example ``attacker.invalid``) must still be refused.
    """
    monkeypatch.setenv(
        "BURNRATE_ALLOWED_HOSTS",
        ",".join(
            (
                "hub.test",
                "hub.invalid",
                "hub.example.invalid",
                "pairing-hub.invalid",
                "vps-hub.tailnet.ts.net",
                "dashboard.example.invalid",
            )
        ),
    )


@pytest.fixture
def frozen_clock(monkeypatch):
    """Opt-in freeze of spend_app datetime.now to FROZEN_NOW."""
    monkeypatch.setattr("spend_app.aggregate.datetime", FrozenDateTime)
    monkeypatch.setattr("spend_app.api.datetime", FrozenDateTime, raising=False)
    return FrozenDateTime


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Opt-in HOME/USERPROFILE rewrite. Does not patch Path.home()."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture(autouse=True)
def _opt_in_isolated_home(request):
    if request.node.get_closest_marker("isolated_home"):
        request.getfixturevalue("isolated_home")


def _transport_name(client) -> str:
    transport = getattr(client, "_transport", None)
    return type(transport).__name__ if transport is not None else ""


def _request_host(client, url) -> str:
    import httpx

    merged = url if isinstance(url, httpx.URL) else httpx.URL(str(url))
    if not merged.is_absolute_url:
        merged = client.base_url.join(merged)
    return (merged.host or "").lower()


@pytest.fixture(autouse=True)
def _ignore_user_env(monkeypatch):
    """The developer's per-user BURNRATE .env must never leak into tests."""
    monkeypatch.setattr("spend_app.config.load_user_env", lambda: None)


@pytest.fixture(autouse=True)
def _no_desktop_notifications(monkeypatch):
    """Only the desktop service sends Windows toasts; a test never does."""
    monkeypatch.delenv("BURNRATE_DESKTOP_NOTIFICATIONS", raising=False)


@pytest.fixture(autouse=True)
def _no_real_opencode_profiles(monkeypatch):
    """Extra OpenCode profiles in the developer's home must not become quota lanes in tests."""
    monkeypatch.setattr("spend_app.opencode_profiles.extra_profiles", lambda: ())


@pytest.fixture(autouse=True)
def _no_real_claude_accounts(monkeypatch):
    """Extra Claude accounts in the developer's home must not become quota lanes or profiles in tests."""
    monkeypatch.setattr("spend_app.claude_accounts.extra_accounts", lambda: ())


class _InMemoryTestVault:
    def __init__(self, store: dict[str, str], service: str) -> None:
        self._store = store
        self.service = service

    def available(self) -> bool:
        return True

    def put(self, secret: str, ref: str | None = None) -> str:
        from spend_app.connection_paths import LocationError

        if not isinstance(secret, str) or not secret:
            raise LocationError("Invalid secret.")
        use_ref = ref or ("burnrate-" + os.urandom(16).hex())
        self._store[use_ref] = secret
        return use_ref

    def get(self, ref: str) -> str:
        from spend_app.connection_paths import LocationError

        if not isinstance(ref, str) or not ref.startswith("burnrate-"):
            raise LocationError("Invalid credential reference.")
        if ref not in self._store:
            raise LocationError("Saved credential unavailable.")
        return self._store[ref]

    def delete(self, ref: str) -> None:
        from spend_app.connection_paths import LocationError

        if not isinstance(ref, str) or not ref.startswith("burnrate-"):
            raise LocationError("Not a BURNRATE-owned credential.")
        self._store.pop(ref, None)


@pytest.fixture(autouse=True)
def _in_memory_vault_factory():
    """Route get_vault() to an in-memory per-DB vault so tests never touch Windows Credential Manager."""
    from spend_app.connection_secrets import set_vault_factory

    stores: dict[str, dict[str, str]] = {}

    def _factory(database_path):
        key = os.path.normcase(str(database_path))
        store = stores.setdefault(key, {})
        return _InMemoryTestVault(store, f"BURNRATE/test/{key}")

    previous = set_vault_factory(_factory)
    try:
        yield stores
    finally:
        set_vault_factory(previous)


@pytest.fixture(autouse=True)
def _block_live_httpx(monkeypatch):
    """Fail httpx calls whose host is not loopback, unless the client is mocked.

    MockTransport and Starlette TestClient (`_TestClientTransport` / host
    `testserver`) never hit the network. Default HTTPTransport is allowed
    only for 127.0.0.1 / localhost.
    """
    import httpx

    original = httpx.Client.request

    def request(self, method, url, *args, **kwargs):
        if _transport_name(self) in _FAKE_TRANSPORTS:
            return original(self, method, url, *args, **kwargs)
        host = _request_host(self, url)
        if host not in _LOOPBACK_HOSTS:
            raise RuntimeError(f"blocked live network request to host {host!r}")
        return original(self, method, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "request", request)
