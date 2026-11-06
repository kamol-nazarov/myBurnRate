"""Offline post-install gate; never opens a user database or calls a provider."""

import inspect
from importlib.metadata import version

from httpx import create_ssl_context
from spend_app.bounded_reads import BoundedReadExecutor

if tuple(map(int, version("certifi").split("."))) < (2026, 10, 23):
    raise SystemExit("certifi is below the reviewed security floor")
if "emSign Root CA - G3" in repr(
    [cert["subject"] for cert in create_ssl_context().get_ca_certs()]
):
    raise SystemExit("Effective HTTPX trust still contains the removed certificate")
assert inspect.iscoroutinefunction(BoundedReadExecutor.run)
print(
    "Installed certificate trust and bounded read executor verified; no provider calls"
)
