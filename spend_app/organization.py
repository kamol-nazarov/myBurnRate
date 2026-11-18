"""Provider organization evidence shared by collection, pricing and sync."""

from __future__ import annotations

import hashlib
import re
from uuid import UUID

from spend_app.timeutil import EPOCH, parse_utc

ORGANIZATION_SOURCES = frozenset({"openai_admin", "anthropic_admin"})
_SCOPE = re.compile(r"^[a-f0-9]{64}$")
_BUCKET = re.compile(
    r"^(?:openai|anthropic)-(?:usage|cost):[a-f0-9]{64}:org:[a-f0-9]{64}$"
)


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
    return (
        value
        if isinstance(source, str)
        and source in ORGANIZATION_SOURCES
        and isinstance(value, str)
        and _SCOPE.fullmatch(value)
        else None
    )


def scoped_raw_id(raw_id: str, scope: str | None) -> str:
    return f"{raw_id}:org:{scope}" if scope else raw_id


def verified_bucket_id(row: dict) -> str | None:
    """Return a provider bucket identity, independent of reporting machine."""
    source = row.get("source")
    scope = clean_scope(source, row.get("organization_scope"))
    raw_id = row.get("raw_id") or ""
    if raw_id.startswith("m:"):
        raw_id = raw_id.split(":", 2)[-1]
    if scope and _BUCKET.fullmatch(raw_id) and raw_id.endswith(f":org:{scope}"):
        provider, _ = source.split("_", 1)
        if raw_id.startswith(provider + "-"):
            return raw_id
    return None


def bucket_observation_order(row: dict) -> tuple:
    """Stable ordering of competing observations of one provider bucket."""
    return (parse_utc(row.get("ingested_at")) or EPOCH, row.get("raw_id") or "")


def clean_request_count(value: object) -> int | None:
    return value if type(value) is int and 0 < value <= 2**63 - 1 else None


def clean_context_window(source: str, value: object) -> str | None:
    if (
        source == "anthropic_admin"
        and isinstance(value, str)
        and value.lower() in {"0-200k", "200k-1m"}
    ):
        return value.lower()
    return None


def admin_event_metadata(row) -> dict:
    source = row.get("source")
    if not isinstance(source, str) or source not in ORGANIZATION_SOURCES:
        return {}
    return {
        "admin_request_count": clean_request_count(row.get("admin_request_count")),
        "admin_context_window": clean_context_window(
            source, row.get("admin_context_window")
        ),
        "organization_scope": clean_scope(source, row.get("organization_scope")),
    }


def admin_pricing_args(source: str, request_count: int | None = None) -> dict:
    if not isinstance(source, str) or source not in ORGANIZATION_SOURCES:
        return {}
    return {
        "request_bucket": True,
        "request_count": clean_request_count(request_count),
    }


def admin_row_pricing_args(row) -> dict:
    return admin_pricing_args(row.get("source"), row.get("admin_request_count"))
