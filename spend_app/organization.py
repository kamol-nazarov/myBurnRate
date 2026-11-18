"""Provider organization evidence shared by collection, pricing and sync."""

from __future__ import annotations

import hashlib
import re
from uuid import UUID


ORGANIZATION_SOURCES = frozenset({"openai_admin", "anthropic_admin"})
_SCOPE = re.compile(r"^[a-f0-9]{64}$")


def organization_scope(source: str, provider_identifier: object) -> str | None:
    """Use only proven provider identifiers, never credential material."""
    if not isinstance(provider_identifier, str):
        return None
    if source == "openai_admin":
        if not re.fullmatch(r"org-[A-Za-z0-9]{1,96}", provider_identifier):
            return None
    elif source == "anthropic_admin":
        try:
            provider_identifier = str(UUID(provider_identifier))
        except ValueError:
            return None
    else:
        return None
    return hashlib.sha256(f"{source}:{provider_identifier}".encode("ascii")).hexdigest()


def clean_scope(source: str, value: object) -> str | None:
    return value if source in ORGANIZATION_SOURCES and isinstance(value, str) and _SCOPE.fullmatch(value) else None


def scoped_raw_id(raw_id: str, scope: str | None) -> str:
    return f"{raw_id}:org:{scope}" if scope else raw_id


def clean_request_count(value: object) -> int | None:
    return value if type(value) is int and 0 < value <= 2**63 - 1 else None


def clean_context_window(source: str, value: object) -> str | None:
    if source == "anthropic_admin" and isinstance(value, str) and value.lower() in {"0-200k", "200k-1m"}:
        return value.lower()
    return None


def admin_event_metadata(row) -> dict:
    source = row.get("source")
    if source not in ORGANIZATION_SOURCES:
        return {}
    return {
        "admin_request_count": clean_request_count(row.get("admin_request_count")),
        "admin_context_window": clean_context_window(source, row.get("admin_context_window")),
        "organization_scope": clean_scope(source, row.get("organization_scope")),
    }


def admin_pricing_args(source: str, request_count: int | None = None) -> dict:
    if source not in ORGANIZATION_SOURCES:
        return {}
    return {
        "request_bucket": True,
        "request_count": clean_request_count(request_count),
    }


def admin_row_pricing_args(row) -> dict:
    return admin_pricing_args(row.get("source"), row.get("admin_request_count"))
