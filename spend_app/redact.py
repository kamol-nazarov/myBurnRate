"""Unified credential and personal-data redactor for errors, evidence, and logs."""

from __future__ import annotations

import importlib
import ipaddress
import logging
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

_MAX_LINE_CHARS = 8192
_MAX_DECODED_BYTES = 256 * 1024
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_LINE_SPLIT = re.compile(r"(\r\n|\r|\n)")

_FIELD_NAMES = (
    r"api[_-]?key|access[_-]?(?:key|token)|refresh[_-]?token|client[_-]?secret|"
    r"proxy-authorization|authorization|x-api-key|x-auth-token|x-access-token|private-token|"
    r"token|secret|password|passwd|pwd|set-cookie|cookie|session[_-]?key"
)

# Single key grammar shared by same-line field matching, empty-key pending, and
# key-only pending matchers: bare, "...", '...', \"...\", and \'...\'.
_FIELD_KEY = (
    rf'(?:\\"(?:{_FIELD_NAMES})\\"'
    rf"|\\'(?:{_FIELD_NAMES})\\'"
    rf'|"(?:{_FIELD_NAMES})"'
    rf"|'(?:{_FIELD_NAMES})'"
    rf'|\b(?:{_FIELD_NAMES})\b)'
)

# Any recognised secret field key followed by ':' or '=' on the same line.
# Matching values redact from value start to end of line via linear slicing.
_SECRET_FIELD_PREFIX = re.compile(rf"(?i){_FIELD_KEY}[^\S\r\n]*[:=]")

# Exact known-safe status phrases exempted only when the separator is ':' and the
# entire remainder of the line, stripped, is one of these phrases plus optional
# trailing punctuation.
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
_SAFE_TRAILING_PUNCT_CHARS = ".,;:!?)]"

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


def _is_loopback_host(hostname: str) -> bool:
    clean = (hostname or "").strip().lower()
    if clean in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(clean).is_loopback
    except ValueError:
        return False


def configured_hub_terms() -> list[str]:
    """Return configured/built-in hub origins, netlocs, and non-loopback hosts."""
    raw_values: list[str] = []
    env_url = os.getenv("BURNRATE_HUB_URL", "").strip()
    if env_url:
        raw_values.append(env_url)
    try:
        mod = importlib.import_module("spend_app._build_config")
        builtin_url = str(getattr(mod, "BUILTIN_HUB_URL", "") or "").strip()
        if builtin_url:
            raw_values.append(builtin_url)
    except ImportError:
        pass

    terms: set[str] = set()
    for raw in raw_values:
        clean = raw.strip().rstrip("/")
        if not clean:
            continue
        terms.add(clean)
        try:
            parsed = urlsplit(clean)
        except ValueError:
            continue
        if parsed.scheme and parsed.netloc:
            terms.add(f"{parsed.scheme}://{parsed.netloc}")
        if parsed.netloc:
            terms.add(parsed.netloc)
        host = (parsed.hostname or "").strip()
        if host and len(host) > 3 and not _is_loopback_host(host):
            terms.add(host)
    return sorted(terms, key=len, reverse=True)


def redact_hub_urls(text: str, terms: list[str] | None = None) -> str:
    """Replace any configured/built-in hub URL or host in ``text``."""
    hub_terms = configured_hub_terms() if terms is None else terms
    if not hub_terms or not text:
        return text
    out = text
    for term in hub_terms:
        out = re.sub(re.escape(term), "[redacted]", out, flags=re.IGNORECASE)
    return out


def _is_safe_status_value(rest: str) -> bool:
    """Return True if rest (after ':') is strictly a known-safe phrase plus optional trailing punctuation."""
    candidate = rest.rstrip(_SAFE_TRAILING_PUNCT_CHARS).rstrip()
    if not candidate:
        return False
    normalized = " ".join(candidate.lower().split())
    return normalized in _SAFE_STATUS_PHRASES


def _redact_secret_field_to_eol(line: str) -> str:
    """Redact from the start of a recognised secret field's value to the end of the line."""
    match = _SECRET_FIELD_PREFIX.search(line)
    if not match:
        return line

    sep_char = line[match.end() - 1]
    val_start = match.end()
    line_len = len(line)
    while val_start < line_len and line[val_start].isspace():
        val_start += 1
    if val_start >= line_len:
        return line

    if sep_char == ":" and _is_safe_status_value(line[val_start:]):
        return line

    return f"{line[:val_start]}[redacted]"


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


def _redact_line(
    line: str,
    home_forms: list[str],
    hub_terms: list[str] | None = None,
) -> str:
    line = _redact_secret_field_to_eol(line)
    for form in home_forms:
        line = re.sub(re.escape(form), "~", line, flags=re.IGNORECASE)

    line = redact_hub_urls(line, hub_terms if hub_terms is not None else [])
    line = _URL_USERINFO.sub(r"\1[redacted]\2", line)
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
    """Percent-decode, scrub credentials/emails/home paths/hub URLs per line, and cap length."""
    value = "" if text is None else str(text)
    value = _percent_decode(value, max_passes=3)
    hub_terms = configured_hub_terms()

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
        elif expect_crossline_secret and _is_separator_only_line(line):
            scrubbed_line = "[line omitted]" if len(line) > _MAX_LINE_CHARS else line
        elif len(line) > _MAX_LINE_CHARS:
            expect_crossline_secret = _line_sets_pending(line)
            scrubbed_line = "[line omitted]"
        else:
            next_pending = _line_sets_pending(line)
            if expect_crossline_secret:
                scrubbed_line = "[redacted]"
            else:
                scrubbed_line = _redact_line(line, home_forms, hub_terms)
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


class RedactingFormatter(logging.Formatter):
    """Log formatter that scrubs credentials, home paths, and configured hub URLs."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class HubUrlRedactingFilter(logging.Filter):
    """Scrub configured/built-in hub URLs from log records before formatting."""

    def filter(self, record: logging.LogRecord) -> bool:
        terms = configured_hub_terms()
        if not terms:
            return True
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        lowered = [t.lower() for t in terms]
        if any(t in message.lower() for t in lowered):
            record.msg = redact_hub_urls(message, terms)
            record.args = ()
        if record.exc_text and any(t in record.exc_text.lower() for t in lowered):
            record.exc_text = redact_hub_urls(record.exc_text, terms)
        if record.exc_info:
            formatted_exc = logging.Formatter().formatException(record.exc_info)
            if any(t in formatted_exc.lower() for t in lowered):
                record.exc_text = redact_hub_urls(formatted_exc, terms)
                record.exc_info = None
        return True


_HUB_LOG_FILTER = HubUrlRedactingFilter()


def install_log_redaction() -> None:
    """Attach the hub-URL log filter to root, handlers, and known HTTP/service loggers."""
    root = logging.getLogger()
    if _HUB_LOG_FILTER not in root.filters:
        root.addFilter(_HUB_LOG_FILTER)
    for handler in root.handlers:
        if _HUB_LOG_FILTER not in handler.filters:
            handler.addFilter(_HUB_LOG_FILTER)
    for name in (
        "burnrate",
        "burnrate.api",
        "burnrate.watchdog",
        "spend_app",
        "httpx",
        "httpcore",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
    ):
        logger = logging.getLogger(name)
        if _HUB_LOG_FILTER not in logger.filters:
            logger.addFilter(_HUB_LOG_FILTER)
