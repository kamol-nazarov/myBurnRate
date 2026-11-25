"""Explicitly authenticated in-process clients for local application tests."""
from pathlib import Path

from fastapi.testclient import TestClient

from spend_app.local_access import read_local_key


def local_client(app, database: Path, **kwargs) -> TestClient:
    """Use the same per-install credential as the native desktop."""
    key = read_local_key(database)
    if not key:
        raise AssertionError("A local test app must create an installation credential.")
    headers = dict(kwargs.pop("headers", {}))
    if any(name.casefold() == "authorization" for name in headers):
        raise AssertionError("Authentication boundary tests should supply their own client.")
    return TestClient(app, headers={**headers, "Authorization": f"Bearer {key}"}, **kwargs)
