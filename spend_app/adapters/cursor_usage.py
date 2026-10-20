"""Experimental: near-real-time Cursor usage from undocumented DashboardService.

``POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetFilteredUsageEvents``
is an undocumented Cursor interface and may change without notice. The same
local bearer token Cursor uses for its own dashboard is sent only to that
allowlisted host (``trust_env=False``). Responses contain model, timestamp,
conversation id, exact token classes, and charged cents; no prompts or output
content are requested or stored.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from spend_app.adapters.common import UsageRow, failed_result, persist_rows, skipped_result, stable_id
from spend_app.adapters.local_common import optional_number, positive_cost
from spend_app.db import connect, initialize
from spend_app.limits import (
    CURSOR_DASHBOARD_ALLOWED_HOSTS,
    _assert_allowed_https_host,
    _cursor_access_token,
)
from spend_app.pricing import PricingEngine


SOURCE = "cursor_usage_service"
TOOL_KEY = "cursor"
BASE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/"
AUTHORITY_START = datetime(2026, 9, 2, tzinfo=UTC)
PAGE_SIZE = 100
# Each poll re-reads only the recent tail. The overlap catches events Cursor
# publishes late; stable raw ids make the re-read idempotent. The first poll
# after a restart looks further back to heal any gap while the app was off.
# Re-reading the full history every poll took minutes per poll once it grew to
# hundreds of pages and stalled every other local source behind it.
INCREMENTAL_OVERLAP = timedelta(hours=3)
RESTART_LOOKBACK = timedelta(hours=72)
_NEXT_POLL_AT = 0.0
_CAUGHT_UP = False


def reset_state() -> None:
    global _NEXT_POLL_AT, _CAUGHT_UP
    _NEXT_POLL_AT = 0.0
    _CAUGHT_UP = False


def fetch_start(database_path: Path) -> datetime:
    """Oldest event time this poll must request: full history only when empty."""
    initialize(database_path)
    with connect(database_path) as connection:
        newest = connection.execute(
            "SELECT MAX(occurred_at) FROM ("
            "SELECT occurred_at FROM usage_events WHERE source=? "
            "UNION ALL SELECT occurred_at FROM unpriced_usage_events WHERE source=?)",
            (SOURCE, SOURCE),
        ).fetchone()[0]
    if not newest:
        return AUTHORITY_START
    try:
        newest_at = datetime.fromisoformat(str(newest).replace("Z", "+00:00"))
    except ValueError:
        return AUTHORITY_START
    if newest_at.tzinfo is None:
        newest_at = newest_at.replace(tzinfo=UTC)
    lookback = INCREMENTAL_OVERLAP if _CAUGHT_UP else RESTART_LOOKBACK
    return max(AUTHORITY_START, newest_at - lookback)


def _count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _occurred_at(value: object) -> datetime | None:
    milliseconds = _count(value)
    if milliseconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def parse_events(events: list[dict]) -> list[UsageRow]:
    rows: list[UsageRow] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        usage = event.get("tokenUsage")
        if not isinstance(usage, dict):
            continue
        occurred_at = _occurred_at(event.get("timestamp"))
        if occurred_at is None or occurred_at < AUTHORITY_START:
            continue
        fresh = _count(usage.get("inputTokens"))
        cached = _count(usage.get("cacheReadTokens"))
        writes = _count(usage.get("cacheWriteTokens"))
        output = _count(usage.get("outputTokens"))
        if fresh + cached + writes + output <= 0:
            continue
        model = str(event.get("model") or "unknown").strip().lower()
        conversation_id = str(event.get("conversationId") or "") or None
        charged_cents = event.get("chargedCents", usage.get("totalCents"))
        try:
            cost_usd = positive_cost(float(charged_cents) / 100)
        except (TypeError, ValueError, OverflowError):
            cost_usd = None
        rows.append(
            UsageRow(
                source=SOURCE,
                tool_key=TOOL_KEY,
                model_key=f"cursor:{model}",
                occurred_at=occurred_at,
                session_id=conversation_id,
                project=None,
                # Cursor's service reports fresh input and cache reads as
                # additive fields, matching its SDK turnEnded metadata.
                input_tokens=fresh + cached,
                cached_input_tokens=cached,
                cache_write_tokens=writes,
                cache_write_1h_tokens=0,
                output_tokens=output,
                reasoning_tokens=optional_number(usage.get("reasoningTokens")),
                cost_usd=cost_usd,
                raw_id=stable_id(
                    "cursor-usage-service",
                    event.get("timestamp"),
                    conversation_id,
                    model,
                    fresh,
                    cached,
                    writes,
                    output,
                    event.get("kind"),
                ),
            )
        )
    return rows


def ingest(*, database_path: Path, pricing: PricingEngine) -> dict:
    from spend_app.integration_policy import authorize, identity_headers
    allowed, reason = authorize("cursor_usage_service")
    if not allowed:
        return skipped_result(database_path=database_path, source=SOURCE, reason=reason)
    global _NEXT_POLL_AT, _CAUGHT_UP
    current_tick = time.monotonic()
    if current_tick < _NEXT_POLL_AT:
        return {"source": SOURCE, "status": "skipped", "reason": "adaptive cadence"}
    token = _cursor_access_token()
    if not token:
        return skipped_result(
            database_path=database_path,
            source=SOURCE,
            reason="Experimental Cursor DashboardService: Cursor is not signed in locally or its access token is unavailable.",
        )
    now = datetime.now(UTC)
    start = fetch_start(database_path)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
        **identity_headers(),
    }
    events: list[dict] = []
    try:
        _assert_allowed_https_host(BASE_URL, CURSOR_DASHBOARD_ALLOWED_HOSTS)
        with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
            page = 1
            while True:
                response = client.post(
                    BASE_URL + "GetFilteredUsageEvents",
                    headers=headers,
                    json={
                        "teamId": 0,
                        "startDate": str(int(start.timestamp() * 1000)),
                        "endDate": str(int(now.timestamp() * 1000)),
                        "page": page,
                        "pageSize": PAGE_SIZE,
                    },
                )
                response.raise_for_status()
                payload = response.json()
                batch = payload.get("usageEventsDisplay") or []
                events.extend(item for item in batch if isinstance(item, dict))
                total = _count(payload.get("totalUsageEventsCount"))
                if len(batch) < PAGE_SIZE or page >= math.ceil(total / PAGE_SIZE):
                    break
                page += 1
    except (httpx.HTTPError, ValueError, RuntimeError) as exc:
        _NEXT_POLL_AT = current_tick + 60
        return failed_result(
            database_path=database_path,
            source=SOURCE,
            reason=f"Experimental Cursor DashboardService lookup failed ({type(exc).__name__}).",
        )

    rows = parse_events(events)
    newest = max((row.occurred_at for row in rows), default=None)
    active = newest is not None and now - newest <= timedelta(minutes=10)
    _NEXT_POLL_AT = current_tick + (15 if active else 60)
    result = persist_rows(
        database_path=database_path,
        pricing=pricing,
        source=SOURCE,
        usage_rows=rows,
    )
    _CAUGHT_UP = True
    return {
        **result,
        "eventsReturned": len(events),
        "pages": max(1, math.ceil(len(events) / PAGE_SIZE)),
        "fetchedSince": start.isoformat().replace("+00:00", "Z"),
    }
