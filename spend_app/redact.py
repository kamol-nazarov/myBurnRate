"""Unified credential and personal-data redactor for errors, evidence, and logs."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

_FIELD_NAMES = (
    r"api[_-]?key|access[_-]?(?:key|token)|refresh[_-]?token|client[_-]?secret|"
    r"token|secret|password|passwd|pwd|authorization|set-cookie|cookie|session[_-]?key|x-api-key"
)

# Header-to-EOL stays bounded to a single line.
_HEADER_TO_EOL = re.compile(
    r"(?i)(?<![\"'\\])\b(?:set-cookie|cookie|authorization)([ \t]*:[ \t]*)\S[^\r\n]*"
)

# Capture prefix+separator separately from the closing quote so replacements
# preserve surrounding quotes/JSON structure while removing the entire value.
# Allow any whitespace (including newlines/CRLF) between key, separator, and value
# for multiline JSON and decoded multi-line payloads.
_SECRET_FIELD_QUOTED_ESCAPED = re.compile(
    rf'(?i)(\b(?:{_FIELD_NAMES})\b(?:\\?["\'])?\s*[:=]\s*\\")(?:\\\\\\"|\\\\|\\[^"\r\n]|[^\\"\r\n])*(\\"|(?=\r|\n|$))'
)
_SECRET_FIELD_QUOTED_DOUBLE = re.compile(
    rf'(?i)(\b(?:{_FIELD_NAMES})\b["\']?\s*[:=]\s*")(?:\\.|[^"\\\r\n])*("|(?=\r|\n|$))'
)
_SECRET_FIELD_QUOTED_SINGLE = re.compile(
    rf"(?i)(\b(?:{_FIELD_NAMES})\b[\"']?\s*[:=]\s*')(?:\\.|[^'\\\r\n])*('|(?=\r|\n|$))"
)

# Unquoted field values:
# - With '=', there is no minimum length and no allowlist: always redact the
#   value (e.g. password=abc12, token=x9, api_key=short).
# - With ':', always redact the value regardless of length unless it is on the
#   same line as ':' and the entire consumed span + clause up to a clean
#   boundary is an exact, complete known-safe status phrase in
#   _SAFE_STATUS_PHRASES (case-insensitive) plus harmless trailing punctuation.
#   Never exempt a match whose consumed value carries an unverified suffix
#   (e.g. token: missing]password=x, token: missing):password=x).
#   Bare prose without '=' or ':' (e.g. "password is required", "invalid token")
#   never matches _SECRET_FIELD_UNQUOTED and stays intact.
_SAFE_STATUS_PHRASES = frozenset(
    {
        "missing",
        "none",
        "null",
        "empty",
        "unset",
        "invalid",
        "expired",
        "required",
        "configured",
        "not configured",
        "not provided",
        "not found",
        "not set",
        "not available",
        "not present",
        "no token",
        "no key",
        "no secret",
        "no password",
        "file is missing",
        "file missing",
        "file not found",
        "revoked",
        "rejected",
        "denied",
    }
)
_SAFE_CLAUSE_RE = re.compile(
    r"([^\r\n,;}{\"']+?)"
    r"(?=[ \t]*(?:[.,;:!?)\]]*(?=$|[\r\n,;}{\"'])|(?:[)\]]+[.,;:!?]*|\.(?=\s|$))[ \t]+))"
)
_SAFE_TRAILING_PUNCT = re.compile(r"^[.,;:!?)\]]*$")

_SECRET_FIELD_UNQUOTED = re.compile(
    rf"(?i)(\b(?:{_FIELD_NAMES})\b[\"']?)"
    r"(\s*[:=]\s*)"
    r"(?!\\?[\"'])"
    r"((?:(?:bearer|basic)[ \t]+)?[^\s\"',;}{]+)"
)

_URL_USERINFO = re.compile(r"(://)[^\s/:@]+:[^\s/:@]+(@)")

_CREDENTIAL_TOKENS = re.compile(
    r"(?i)(?:bearer|basic)\s+\S+"
    r"|(?<![A-Za-z0-9])sk-[a-z0-9_-]+"
    r"|(?<![A-Za-z0-9])(?-i:zai_)(?!quota_endpoint(?![A-Za-z0-9_-]))[A-Za-z0-9_-]+"
    r"|(?<![A-Za-z0-9])xai-[a-z0-9_-]+"
    r"|(?<![A-Za-z0-9])or-v1-[a-z0-9_-]+"
    r"|(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9_]{8,}"
    r"|(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{8,}"
    r"|(?<![A-Za-z0-9])br_(?:m|s|p|bg|l)_[A-Za-z0-9_-]{6,}"
    r"|(?<![A-Za-z0-9])brpair_[A-Za-z0-9_=-]{6,}"
    r"|(?<![A-Za-z0-9])(?-i:eyJ)[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_-]*)*"
    r"|[A-Fa-f0-9]{32,}"
)

_EMAIL = re.compile(r"(?i)[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}")
_USER_DIR = re.compile(r"(?i)([A-Z]:[\\/]+Users[\\/]+)[^\\/\s\"']+")
_UNIX_HOME = re.compile(r"(/(?:home|Users)/)[^/\s\"']+")


def _replace_unquoted_secret(match: re.Match[str]) -> str:
    prefix, sep, raw_val = match.group(1), match.group(2), match.group(3)
    if "=" in sep or "\n" in sep or "\r" in sep:
        return f"{prefix}{sep}[redacted]"
    if re.match(r"(?i)^(?:bearer|basic)[ \t]+", raw_val):
        return f"{prefix}{sep}[redacted]"
    clause_match = _SAFE_CLAUSE_RE.match(match.string, match.start(3))
    if clause_match:
        phrase = re.sub(r"[ \t]+", " ", clause_match.group(1).strip()).lower()
        # Only exempt when the entire consumed span is the verified safe phrase
        # plus harmless trailing punctuation (no unverified suffix like ']password=x').
        consumed_tail = match.string[clause_match.end(1) : match.end(3)]
        if phrase in _SAFE_STATUS_PHRASES and _SAFE_TRAILING_PUNCT.match(consumed_tail):
            return match.group(0)
    return f"{prefix}{sep}[redacted]"


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
    value = _SECRET_FIELD_QUOTED_ESCAPED.sub(lambda m: m.group(1) + "[redacted]" + m.group(2), value)
    value = _SECRET_FIELD_QUOTED_DOUBLE.sub(lambda m: m.group(1) + "[redacted]" + m.group(2), value)
    value = _SECRET_FIELD_QUOTED_SINGLE.sub(lambda m: m.group(1) + "[redacted]" + m.group(2), value)
    value = _HEADER_TO_EOL.sub(lambda m: m.group(0)[: m.start(1) - m.start(0)] + m.group(1) + "[redacted]", value)
    value = _SECRET_FIELD_UNQUOTED.sub(_replace_unquoted_secret, value)
    value = _CREDENTIAL_TOKENS.sub("[redacted]", value)
    value = _EMAIL.sub("[redacted]", value)
    value = _USER_DIR.sub(r"\1[user]", value)
    value = _UNIX_HOME.sub(r"\1[user]", value)

    if limit is not None:
        return value[: max(0, limit)]
    return value
