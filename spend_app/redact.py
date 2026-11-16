"""Unified credential and personal-data redactor for errors, evidence, and logs."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

_SECRET_FIELD = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|"
    r"passwd|authorization|cookie|session[_-]?key)|x-api-key)"
    r"(\s*[\"']?\s*[:=]\s*[\"']?)(?:(?:bearer|basic)\s+)?[^\s\"',;}]+"
)

_URL_USERINFO = re.compile(r"(://)[^/\s:@]+:[^/\s:@]+(@)")

_CREDENTIAL_TOKENS = re.compile(
    r"(?i)(?:bearer|basic)\s+\S+"
    r"|(?<![a-z0-9_-])sk-[a-z0-9_-]+"
    r"|\bzai_[a-z0-9_-]+"
    r"|\bxai-[a-z0-9_-]+"
    r"|\bor-v1-[a-z0-9_-]+"
    r"|\bgh[pousr]_[A-Za-z0-9_]{8,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{8,}"
    r"|\bbr_(?:m|s|p|bg|l)_[A-Za-z0-9_-]{6,}"
    r"|\bbrpair_[A-Za-z0-9_=-]{6,}"
    r"|(?-i:\beyJ)[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_-]*)*"
    r"|[A-Fa-f0-9]{32,}"
)

_EMAIL = re.compile(r"(?i)[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}")
_USER_DIR = re.compile(r"(?i)([A-Z]:[\\/]+Users[\\/]+)[^\\/\s\"']+")
_UNIX_HOME = re.compile(r"(/(?:home|Users)/)[^/\s\"']+")


def _percent_decode(text: str, *, max_passes: int = 3) -> str:
    value = text
    for _ in range(max_passes):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    return value


def redact(
    text: object,
    limit: int | None = None,
    *,
    home: Path | None = None,
) -> str:
    """Percent-decode, scrub credentials/emails/home paths, and cap length."""
    value = "" if text is None else str(text)
    value = _percent_decode(value, max_passes=3)

    if home is not None:
        home_text = str(home)
        for form in sorted(
            {
                home_text,
                home_text.replace("\\", "/"),
                home_text.replace("/", "\\"),
                home_text.replace("\\", "\\\\"),
            },
            key=len,
            reverse=True,
        ):
            if len(form) > 3:
                value = re.sub(re.escape(form), "~", value, flags=re.IGNORECASE)

    value = _URL_USERINFO.sub(r"\1[redacted]\2", value)
    value = _SECRET_FIELD.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", value)
    value = _CREDENTIAL_TOKENS.sub("[redacted]", value)
    value = _EMAIL.sub("[redacted]", value)
    value = _USER_DIR.sub(r"\1[user]", value)
    value = _UNIX_HOME.sub(r"\1[user]", value)

    if limit is not None:
        return value[: max(0, limit)]
    return value
