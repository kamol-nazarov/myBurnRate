"""Unified credential and personal-data redactor for errors, evidence, and logs."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

_MAX_UNBOUNDED_INPUT = 64 * 1024

_FIELD_NAMES = (
    r"api[_-]?key|access[_-]?(?:key|token)|refresh[_-]?token|client[_-]?secret|"
    r"token|secret|password|passwd|pwd|authorization|set-cookie|cookie|session[_-]?key|x-api-key"
)

# When a separator crosses a newline, a value only starts if what follows is NOT
# itself another key:value / key=value field (so an empty `key:` at EOL never
# swallows the next line's field name).
_NEXT_FIELD_LOOKAHEAD = (
    r'(?!\\"[^"\r\n]+\\"\s*[:=]|"[^"\r\n]+"\s*[:=]|'
    r"'[^'\r\n]+'\s*[:=]|[\w.-]+\s*[:=])"
)
_FIELD_SEP = (
    rf"\s*[:=](?:[ \t]*(?![\r\n])|[ \t]*[\r\n]\s*{_NEXT_FIELD_LOOKAHEAD})"
)

# Header-to-EOL stays bounded to a single line.
_HEADER_TO_EOL = re.compile(
    r"(?i)(?<![\"'\\])\b(?:set-cookie|cookie|authorization)([ \t]*:[ \t]*)\S[^\r\n]*"
)

# Capture prefix+separator separately from the closing quote so replacements
# preserve surrounding quotes/JSON structure while removing the entire value.
_SECRET_FIELD_QUOTED_ESCAPED = re.compile(
    rf'(?i)(\b(?:{_FIELD_NAMES})\b(?:\\?["\'])?{_FIELD_SEP}\\")(?:\\\\\\"|\\\\|\\[^"\r\n]|[^\\"\r\n])*(\\"|(?=\r|\n|$))'
)
_SECRET_FIELD_QUOTED_DOUBLE = re.compile(
    rf'(?i)(\b(?:{_FIELD_NAMES})\b["\']?{_FIELD_SEP}")(?:\\.|[^"\\\r\n])*("|(?=\r|\n|$))'
)
_SECRET_FIELD_QUOTED_SINGLE = re.compile(
    rf"(?i)(\b(?:{_FIELD_NAMES})\b[\"']?{_FIELD_SEP}')(?:\\.|[^'\\\r\n])*('|(?=\r|\n|$))"
)

# Unquoted field values:
# - With '=' or across a newline, there is no minimum length and no allowlist:
#   always redact the value (e.g. password=abc12, token=x9, api_key=short).
# - With ':' on the same line, always redact unless the value starts with an
#   exact known-safe status phrase in _SAFE_STATUS_PHRASES (case-insensitive)
#   and the remainder of the consumed span (and glued tail) consists strictly of
#   harmless trailing punctuation [.,;:!?)\]] in a linear check.
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
_SAFE_STATUS_ALTERNATION = "|".join(
    r"[ \t]+".join(re.escape(part) for part in phrase.split(" "))
    for phrase in sorted(_SAFE_STATUS_PHRASES, key=len, reverse=True)
)
_SAFE_PHRASE_START = re.compile(rf"(?i)(?:{_SAFE_STATUS_ALTERNATION})")
_SAFE_TRAILING_PUNCT_CHARS = ".,;:!?)]"
_CLAUSE_STOP_CHARS = " \t,;}{\"'"

_SECRET_FIELD_UNQUOTED = re.compile(
    rf"(?i)(\b(?:{_FIELD_NAMES})\b[\"']?)"
    rf"({_FIELD_SEP})"
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


def _line_end(text: str, start: int) -> int:
    nl_r = text.find("\r", start)
    nl_n = text.find("\n", start)
    if nl_r == -1:
        return len(text) if nl_n == -1 else nl_n
    if nl_n == -1:
        return nl_r
    return nl_r if nl_r < nl_n else nl_n


def _replace_unquoted_secret(match: re.Match[str]) -> str:
    prefix, sep, raw_val = match.group(1), match.group(2), match.group(3)
    if "=" in sep or "\n" in sep or "\r" in sep:
        return f"{prefix}{sep}[redacted]"
    if re.match(r"(?i)^(?:bearer|basic)[ \t]+", raw_val):
        return f"{prefix}{sep}[redacted]"

    text = match.string
    start = match.start(3)
    nl_pos = _line_end(text, start)
    phrase_match = _SAFE_PHRASE_START.match(text, start, min(nl_pos, start + 64))
    if not phrase_match:
        return f"{prefix}{sep}[redacted]"

    phrase_end = phrase_match.end()
    # Linear check of the remainder of the consumed span and any non-whitespace
    # characters glued onto the phrase before a whitespace or clause delimiter.
    tail_end = phrase_end
    while tail_end < nl_pos and text[tail_end] not in _CLAUSE_STOP_CHARS:
        tail_end += 1
    glued_tail = text[phrase_end:tail_end]
    if glued_tail.strip(_SAFE_TRAILING_PUNCT_CHARS):
        return f"{prefix}{sep}[redacted]"

    if tail_end < nl_pos and text[tail_end] in " \t":
        if not any(ch in ".!?)]" for ch in glued_tail):
            rest_pos = tail_end
            while rest_pos < nl_pos and text[rest_pos] in " \t":
                rest_pos += 1
            if rest_pos < nl_pos and text[rest_pos] not in ",;}{\"'":
                return f"{prefix}{sep}[redacted]"

    return match.group(0)


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
    max_input = max(0, limit) * 4 if limit is not None else _MAX_UNBOUNDED_INPUT
    if max_input == 0:
        return ""
    if len(value) > max_input:
        value = value[:max_input]

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
