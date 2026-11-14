"""The one place that decides which URLs the native window may open.

Shared by the bridge's ``open_external`` and, later, the update module, so the
pinned repository lives in exactly one constant.
"""

from __future__ import annotations

from urllib.parse import urlsplit

try:  # the update module owns the repository name; one source of truth
    from spend_app.native_update import REPO as PINNED_REPO
except ImportError:  # builds without the update module keep the same pin
    PINNED_REPO = "kamol-nazarov/myBurnRate"

PINNED_REPO_HOST = "github.com"
PINNED_REPO_PATH_PREFIX = f"/{PINNED_REPO}/"


def is_allowed_external_url(url: object) -> bool:
    """True only for ``https://github.com/kamol-nazarov/myBurnRate/...``.

    Strict on purpose: no surrounding or embedded whitespace or control
    characters, no userinfo or port, no backslashes, no ``..`` segments, and
    the repository prefix must match exactly.
    """
    if not isinstance(url, str) or not url or len(url) > 2048:
        return False
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        return False
    if "\\" in url:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https" or parts.netloc != PINNED_REPO_HOST:
        return False
    if not parts.path.startswith(PINNED_REPO_PATH_PREFIX):
        return False
    return ".." not in parts.path.split("/")
