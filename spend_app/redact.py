"""Unified credential and personal-data redactor for errors, evidence, and logs."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

_MAX_LINE_CHARS = 8192
_MAX_DECODED_BYTES = 256 * 1024

_LINE_SPLIT = re.compile(r"(\r\n|\r|\n)")

_FIELD_NAMES = (
    r"api[_-]?key|access[_-]?(?:key|token)|refresh[_-]?token|client[_-]?secret|"
    r"token|secret|password|passwd|pwd|authorization|set-cookie|cookie|session[_-]?key|x-api-key"
)

# Single key grammar shared by same-line field matchers, empty-key pending, and
# key-only pending matchers: bare, "...", '...', \"...\", and \'...\'.
_FIELD_KEY = (
    rf'(?:\\"(?:{_FIELD_NAMES})\\"'
    rf"|\\'(?:{_FIELD_NAMES})\\'"
    rf'|"(?:{_FIELD_NAMES})"'
    rf"|'(?:{_FIELD_NAMES})'"
    rf'|\b(?:{_FIELD_NAMES})\b)'
)

# Per-line secret-field matchers (never cross line boundaries; cross-line values
# are handled by the explicit line-level state machine in redact()).
_HEADER_TO_EOL = re.compile(
    r"(?i)(?<![\"'\\])\b(?:set-cookie|cookie|authorization)([^\S\r\n]*:[^\S\r\n]*)\S[^\r\n]*"
)

# Capture prefix+separator separately from the closing quote so replacements
# preserve surrounding quotes/JSON structure while removing the entire value.
# Disjoint single/two-character alternatives with bounded repetition prevent
# backtracking on unclosed or mismatched quotes; trailing lone backslash at EOL
# is also consumed so unclosed quotes never leak.
_SECRET_FIELD_QUOTED_ESCAPED_DOUBLE = re.compile(
    rf'(?i)({_FIELD_KEY}[^\S\r\n]*[:=][^\S\r\n]*\\")(?:[^\\\r\n]|\\[^"\r\n]){{0,8192}}(\\"|\\?$)'
)
_SECRET_FIELD_QUOTED_ESCAPED_SINGLE = re.compile(
    rf"(?i)({_FIELD_KEY}[^\S\r\n]*[:=][^\S\r\n]*\\')(?:[^\\\r\n]|\\[^'\r\n]){{0,8192}}(\\'|\\?$)"
)
_SECRET_FIELD_QUOTED_DOUBLE = re.compile(
    rf'(?i)({_FIELD_KEY}[^\S\r\n]*[:=][^\S\r\n]*")(?:[^"\\\r\n]|\\[^\r\n]){{0,8192}}("|\\?$)'
)
_SECRET_FIELD_QUOTED_SINGLE = re.compile(
    rf"(?i)({_FIELD_KEY}[^\S\r\n]*[:=][^\S\r\n]*')(?:[^'\\\r\n]|\\[^\r\n]){{0,8192}}('|\\?$)"
)

# Unquoted field values:
# - With '=', there is no minimum length and no allowlist: always redact the
#   value (e.g. password=abc12, token=x9, api_key=short).
# - With ':', always redact regardless of length unless the value starts with an
#   exact known-safe status phrase in _SAFE_STATUS_PHRASES (case-insensitive)
#   and the remainder of the consumed span (and glued tail) consists strictly of
#   harmless trailing punctuation [.,;:!?)\\]] in a linear check.
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
    r"[^\S\r\n]+".join(re.escape(part) for part in phrase.split(" "))
    for phrase in sorted(_SAFE_STATUS_PHRASES, key=len, reverse=True)
)
_SAFE_PHRASE_START = re.compile(rf"(?i)(?:{_SAFE_STATUS_ALTERNATION})")
_SAFE_TRAILING_PUNCT_CHARS = ".,;:!?)]"
_CLAUSE_DELIM_CHARS = ",;}{\"'"

_SECRET_FIELD_UNQUOTED = re.compile(
    rf"(?i)({_FIELD_KEY})([^\S\r\n]*[:=][^\S\r\n]*)"
    r"(?!\\?[\"'])"
    r"((?:(?:bearer|basic)[^\S\r\n]+)?[^\s\"',;}{]+)"
)

# Cross-line pending-state matchers (evaluated on a small trailing window of the
# whitespace-trimmed raw line):
# - empty secret field ending in ':' or '=' (e.g. 'password:', '"password":', '\'password\':', 'token=')
# - key-only line using the same _FIELD_KEY grammar (e.g. '"password"', '{"password"', 'password')
# - line ending with auth scheme 'Bearer' or 'Basic' (case-insensitive)
_TRAILING_FIELD_KEY = re.compile(rf"(?i){_FIELD_KEY}$")
_TRAILING_SCHEME = re.compile(r"(?i)\b(?:bearer|basic)$")

_URL_USERINFO = re.compile(r"(://)[^\s/:@]+:[^\s/:@]+(@)")

_CREDENTIAL_TOKENS = re.compile(
    r"(?i)(?:bearer|basic)[^\S\r\n]+\S+"
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

# Bounded local/domain lengths ensure linear scanning; callers also gate on '@' in line.
_EMAIL = re.compile(r"(?i)[A-Z0-9._%+-]{1,64}@[A-Z0-9.-]{1,253}\.[A-Z]{2,63}")
_USER_DIR = re.compile(r"(?i)([A-Z]:[\\/]+Users[\\/]+)[^\\/\s\"']+")
_UNIX_HOME = re.compile(r"(/(?:home|Users)/)[^/\s\"']+")


def _replace_quoted_secret(match: re.Match[str]) -> str:
    closing = match.group(2)
    if closing == "\\":
        closing = ""
    return f"{match.group(1)}[redacted]{closing}"


def _replace_unquoted_secret(match: re.Match[str]) -> str:
    prefix, sep, raw_val = match.group(1), match.group(2), match.group(3)
    if "=" in sep:
        return f"{prefix}{sep}[redacted]"
    if re.match(r"(?i)^(?:bearer|basic)[^\S\r\n]+", raw_val):
        return f"{prefix}{sep}[redacted]"

    line = match.string
    start = match.start(3)
    line_len = len(line)
    phrase_match = _SAFE_PHRASE_START.match(line, start, min(line_len, start + 64))
    if not phrase_match:
        return f"{prefix}{sep}[redacted]"

    phrase_end = phrase_match.end()
    # Linear check of the remainder of the consumed span and any non-whitespace
    # characters glued onto the phrase before a whitespace or clause delimiter.
    tail_end = phrase_end
    while (
        tail_end < line_len
        and line[tail_end] not in _CLAUSE_DELIM_CHARS
        and not line[tail_end].isspace()
    ):
        tail_end += 1
    glued_tail = line[phrase_end:tail_end]
    if glued_tail.strip(_SAFE_TRAILING_PUNCT_CHARS):
        return f"{prefix}{sep}[redacted]"

    if tail_end < line_len and line[tail_end].isspace():
        if not any(ch in ".!?)]" for ch in glued_tail):
            rest_pos = tail_end
            while rest_pos < line_len and line[rest_pos].isspace():
                rest_pos += 1
            if rest_pos < line_len and line[rest_pos] not in _CLAUSE_DELIM_CHARS:
                return f"{prefix}{sep}[redacted]"

    return match.group(0)


def _line_sets_pending(line: str) -> bool:
    """Return True if a non-blank raw line ends with an unvalued secret key, key-only line, or Bearer/Basic."""
    tail = line.rstrip()
    if not tail:
        return False

    if tail[-1] in ":=":
        head = tail[:-1].rstrip()
        return bool(_TRAILING_FIELD_KEY.search(head[-64:]))

    if _TRAILING_SCHEME.search(tail[-16:]):
        return True

    window = tail[-64:]
    match = _TRAILING_FIELD_KEY.search(window)
    if not match:
        return False
    prefix_start = len(tail) - len(window) + match.start()
    prefix = tail[:prefix_start].rstrip()
    return not prefix or prefix.endswith(("{", ","))


def _is_separator_only_line(line: str) -> bool:
    """Return True if a non-blank line is ':' or '=', optionally followed by ',' or '}' and whitespace."""
    s = line.strip()
    if not s or s[0] not in ":=":
        return False
    rest = s[1:].strip()
    return rest in ("", ",", "}")


def _redact_line(line: str, home_forms: list[str]) -> str:
    for form in home_forms:
        line = re.sub(re.escape(form), "~", line, flags=re.IGNORECASE)

    line = _URL_USERINFO.sub(r"\1[redacted]\2", line)
    line = _SECRET_FIELD_QUOTED_ESCAPED_DOUBLE.sub(_replace_quoted_secret, line)
    line = _SECRET_FIELD_QUOTED_ESCAPED_SINGLE.sub(_replace_quoted_secret, line)
    line = _SECRET_FIELD_QUOTED_DOUBLE.sub(_replace_quoted_secret, line)
    line = _SECRET_FIELD_QUOTED_SINGLE.sub(_replace_quoted_secret, line)
    line = _HEADER_TO_EOL.sub(lambda m: m.group(0)[: m.start(1) - m.start(0)] + m.group(1) + "[redacted]", line)
    line = _SECRET_FIELD_UNQUOTED.sub(_replace_unquoted_secret, line)
    line = _CREDENTIAL_TOKENS.sub("[redacted]", line)
    if "@" in line:
        line = _EMAIL.sub("[redacted]", line)
    line = _USER_DIR.sub(r"\1[user]", line)
    line = _UNIX_HOME.sub(r"\1[user]", line)
    return line


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
    """Percent-decode, scrub credentials/emails/home paths per line, and cap length."""
    value = "" if text is None else str(text)
    value = _percent_decode(value, max_passes=3)

    home_forms: list[str] = []
    if home is not None:
        home_text = str(home)
        home_forms = [
            form
            for form in sorted(
                {
                    home_text,
                    home_text.replace("\\", "/"),
                    home_text.replace("/", "\\"),
                    home_text.replace("\\", "\\\\"),
                },
                key=len,
                reverse=True,
            )
            if len(form) > 3
        ]

    parts = _LINE_SPLIT.split(value)
    out_parts: list[str] = []
    total_bytes = 0
    truncated = False
    expect_crossline_secret = False

    n_parts = len(parts)
    for idx in range(0, n_parts, 2):
        line = parts[idx]
        sep = parts[idx + 1] if idx + 1 < n_parts else ""
        line_bytes = len(line.encode("utf-8", "surrogatepass"))
        sep_bytes = len(sep.encode("utf-8", "surrogatepass"))

        if idx > 0 and total_bytes + line_bytes > _MAX_DECODED_BYTES:
            truncated = True
            break

        total_bytes += line_bytes + sep_bytes

        if not line.strip():
            scrubbed_line = "" if len(line) > _MAX_LINE_CHARS else line
        elif len(line) > _MAX_LINE_CHARS:
            expect_crossline_secret = _line_sets_pending(line)
            scrubbed_line = "[line omitted]"
        elif expect_crossline_secret and _is_separator_only_line(line):
            scrubbed_line = line
        else:
            next_pending = _line_sets_pending(line)
            if expect_crossline_secret:
                scrubbed_line = "[redacted]"
            else:
                scrubbed_line = _redact_line(line, home_forms)
            expect_crossline_secret = next_pending

        out_parts.append(scrubbed_line)
        if sep:
            if idx + 2 < n_parts and total_bytes > _MAX_DECODED_BYTES:
                out_parts.append(sep)
                truncated = True
                break
            out_parts.append(sep)

    if truncated:
        out_parts.append("[truncated]")

    result = "".join(out_parts)
    if limit is not None:
        return result[: max(0, limit)]
    return result
