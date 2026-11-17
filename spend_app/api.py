from __future__ import annotations

import asyncio

import hashlib
import logging
import os
import time
from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime
from importlib.resources import as_file, files
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.gzip import GZipMiddleware

from spend_app import __version__
from spend_app.bounded_reads import BoundedReadExecutor, ReadCapacityExceeded
from spend_app.build import build_id
from spend_app.error_codes import HUB_PROTOCOL_VERSION, error_payload
from spend_app.native_launch import evaluate as evaluate_native_binary
from spend_app.native_launch import locked_binary


ASSET_IMMUTABLE_CACHE = "public, max-age=31536000, immutable"


from spend_app.db import UserFacingError  # re-exported
ASSET_REVALIDATE_CACHE = "no-cache"
GZIP_MINIMUM_SIZE = 1024
ASSET_SETTLE_SECONDS = 5
# How long a paired Spoke's HUD may show its last Hub copy while renewing it.
HUD_HUB_STALE_SECONDS = 300
# The usage aggregate on the Hub can take much longer than a live meter read.
HUD_USAGE_HUB_READ_TIMEOUT = 60.0
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; "
    "base-uri 'none'; object-src 'none'; frame-ancestors 'none'"
)
_ASSET_VERSIONS: dict[str, tuple[tuple[int, int], str]] = {}


def _stamp_hub_age(payload: dict, *, now, stamp: str | None = None) -> dict:
    """Add a data-age stamp to a Hub payload that predates the fields.

    A spoke can be upgraded before its Hub. The HUD must not silently trust
    that older payload: derive the age from the payload's own timestamps so
    the footer and the stale alarm keep working during the rollout.
    """
    if not isinstance(payload, dict) or "lastIngestAgeSeconds" in payload:
        return payload
    from spend_app.hud_data import DATA_STALE_AFTER_SECONDS
    from spend_app.aggregate import _parse

    if not stamp:
        stamps = [
            row.get("polledAt")
            for card in payload.get("capacity") or ()
            for row in card.get("rows") or ()
            if isinstance(row, dict) and row.get("polledAt")
        ]
        if stamps:
            # Use the newest meter: one deliberately stale optional lane
            # (for example a disconnected Antigravity host) must not make the
            # whole live feed look days old. Per-lane held state is shown on
            # the row itself and in the freshness report.
            try:
                stamp = max(stamps, key=_parse)
            except (TypeError, ValueError):
                stamp = None
        if not stamp:
            stamp = payload.get("generatedAt")
    if not stamp:
        return payload
    try:
        age = max(0, int((now - _parse(stamp)).total_seconds()))
    except (TypeError, ValueError):
        return payload
    stamped = {**payload}
    stamped["lastIngestAt"] = stamp
    stamped["lastIngestAgeSeconds"] = age
    stamped["stale"] = age > DATA_STALE_AFTER_SECONDS
    stamped["dataSource"] = "hub"
    return stamped


def asset_version(path: Path) -> str:
    """Content hash of a static asset, cached by file mtime and size.

    The HTML references ``spend.js?v=<hash>`` so browsers may cache assets
    immutably: any edit changes the hash, which changes the URL, so a stale
    file can never be pinned by a cache.
    """
    stat = path.stat()
    signature = (stat.st_mtime_ns, stat.st_size)
    cached = _ASSET_VERSIONS.get(str(path))
    # A file edited within the last few seconds is always re-hashed: two
    # same-size writes inside one mtime tick would otherwise share a stale hash.
    settled = time.time() - stat.st_mtime > ASSET_SETTLE_SECONDS
    if cached and cached[0] == signature and settled:
        return cached[1]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    if settled:
        _ASSET_VERSIONS[str(path)] = (signature, digest)
    return digest


def render_index(web_root: Path) -> str:
    html = (web_root / "index.html").read_text(encoding="utf-8")
    for name, marker in (("spend.css", "43"), ("spend.js", "42"), ("request-state.js", "1"), ("product.js", "2"), ("dialogs.js", "1"), ("favicon.svg", "1"), ("logos.svg", "1")):
        html = html.replace(f"/{name}?v={marker}", f"/{name}?v={asset_version(web_root / name)}")
    return html


def _asset_cache_header(request: Request, path: Path) -> str:
    requested = request.query_params.get("v")
    if requested and requested == asset_version(path):
        return ASSET_IMMUTABLE_CACHE
    return ASSET_REVALIDATE_CACHE

from spend_app.aggregate import (
    WINDOW_SPECS,
    aggregate_entity,
    aggregate_health_cached,
    aggregate_nav,
    aggregate_summary,
    aggregate_summary_cached,
    canonicalize_window,
    data_clock,
    record_summary_request,
)
from spend_app.config import Settings, dashboard_enabled, demo_mode, load_settings
from spend_app.db import initialize
from spend_app.pricing import PricingEngine
from spend_app.limits import snapshot_limits
from spend_app.scheduler import HistoryDemand, create_scheduler


SPEND_WINDOWS = set(WINDOW_SPECS) | {"7d", "30d", "MTD", "YTD", "All"}


def _content_security_policy() -> str:
    policy = CONTENT_SECURITY_POLICY
    if os.getenv("BURNRATE_DEV", "").strip() != "1":
        return policy
    try:
        import json
        meta = json.loads((Path(__file__).resolve().parents[1] / ".variate" / "server.json").read_text(encoding="utf-8"))
        origin = f"http://127.0.0.1:{int(meta['port'])}"
    except (OSError, ValueError, TypeError, KeyError):
        return policy
    return (
        policy.replace("script-src 'self' 'unsafe-inline'", f"script-src 'self' 'unsafe-inline' {origin}")
        .replace("connect-src 'self'", f"connect-src 'self' {origin}")
    )


def _live_hud_html() -> str:
    """Render the HUD page, refreshing automatically when hud.py or hud.js changes on disk."""
    import spend_app.hud as hud

    return hud.render_hud(dashboard=dashboard_enabled())


# With the dashboard off, "/" redirects to the browser HUD.
HUD_LAUNCHER_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>BURNRATE</title>
<script>
location.replace("/hud");
</script></head><body></body></html>
"""


def create_app(
    settings: Settings | None = None,
    *,
    enable_scheduler: bool = True,
    now: datetime | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    initialize(settings.database_path)
    from spend_app.db import connect
    from spend_app.setup_wizard import setup_token_warning

    with connect(settings.database_path) as connection:
        stale_setup_token = setup_token_warning(connection)
    if stale_setup_token:
        logging.getLogger("burnrate.setup").warning(stale_setup_token)
    pricing = PricingEngine.load(settings.pricing_path)
    bounded_reads = BoundedReadExecutor(max_workers=1)

    async def _run_bounded_read(function, *args):
        try:
            return await bounded_reads.run(function, *args)
        except ReadCapacityExceeded:
            raise HTTPException(status_code=503, detail="Spend data is temporarily busy; retry shortly.",
                                headers={"Retry-After": "1"}) from None

    history_demand = HistoryDemand()
    scheduler = create_scheduler(settings, pricing, history_active=history_demand.active) if enable_scheduler else None

    def request_collection():
        if history_demand.touch() and scheduler:
            from spend_app.freshness import collection_resumed

            collection_resumed()
            for job_id in ("local-ingest", "provider-admin"):
                scheduler.modify_job(job_id, next_run_time=datetime.now(UTC))

    def collect_now():
        # The setup wizard's "check again": run the next collection pass and
        # quota poll now instead of waiting for their interval.
        if scheduler is None:
            return
        from spend_app.freshness import collection_resumed

        history_demand.touch()
        collection_resumed()
        for job_id in ("local-ingest", "quota-poll"):
            try:
                scheduler.modify_job(job_id, next_run_time=datetime.now(UTC))
            except Exception:
                pass

    def request_history(request: Request):
        # Dashboard views opt in explicitly. The HUD's separate usage read
        # also keeps collection active, without building dashboard summaries.
        if request.headers.get("x-burnrate-view") == "dashboard":
            request_collection()
    from spend_app.diagnostics import detect_local_sources, source_reports

    def source_guidance():
        presence = {}
        with connect(settings.database_path) as connection:
            return source_reports(connection, current_now(), presence, settings=settings, pricing=pricing)
    resource_stack = ExitStack()
    web_root = Path(resource_stack.enter_context(as_file(files("spend_web"))))

    def current_now() -> datetime:
        return (now or datetime.now(UTC)).astimezone(UTC)

    def slot_now() -> datetime:
        # Live requests describe the data as of the latest completed ingest
        # cycle, so every viewer and the pre-warm job share one clock until
        # the next cycle; an injected clock (tests) is passed through untouched.
        if now is not None:
            return current_now()
        return data_clock(settings.database_path, settings.local_ingest_interval_seconds)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if scheduler:
            scheduler.start()
        try:
            yield
        finally:
            try:
                if scheduler:
                    await asyncio.to_thread(scheduler.shutdown, wait=False)
            finally:
                try:
                    await asyncio.to_thread(bounded_reads.close)
                finally:
                    resource_stack.close()

    app = FastAPI(title="BURNRATE", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.history_demand = history_demand
    app.state.scheduler = scheduler
    app.state.collector_worker = getattr(scheduler, "collector_worker", None)
    from spend_app.connections import Store
    Store(settings.database_path).bootstrap()
    from spend_app.connection_api import connection_router
    app.include_router(connection_router(settings))
    from spend_app.product_api import product_router
    app.include_router(product_router(settings, pricing=pricing))
    from spend_app.attention_api import attention_router
    app.include_router(attention_router(settings, pricing))
    from spend_app.capacity_forecast import forecast_router
    app.include_router(forecast_router(settings))
    from spend_app.sync import sync_router
    app.include_router(sync_router(settings, pricing))
    from spend_app.auth_api import auth_router
    app.include_router(auth_router(settings))
    from spend_app.redact import install_log_redaction
    install_log_redaction()
    from spend_app.setup_wizard import setup_router
    app.include_router(setup_router(settings, pricing, collect_now=collect_now))
    app.state._resource_stack = resource_stack
    # Compress HTML, CSS, JS and JSON for clients that accept it (143 KB of
    # static assets otherwise travel uncompressed over the tailnet).
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MINIMUM_SIZE)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = _content_security_policy()
        if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    def boundary_error(
        status: int,
        message: str,
        *,
        retry_after: str | None = None,
        code: str | None = None,
    ) -> JSONResponse:
        """Boundary rejections still carry the security headers."""
        headers = {
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": _content_security_policy(),
            "Cache-Control": "no-store",
        }
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        payload = error_payload(code) if code is not None else {"error": message}
        return JSONResponse(payload, status_code=status, headers=headers)

    @app.middleware("http")
    async def host_boundary(request, call_next):
        """Exact Host allowlist for reads and writes (DNS-rebinding defence)."""
        from spend_app.connection_api import host_allowed

        if not host_allowed(request.headers.get("host", "")):
            return boundary_error(403, "Request rejected: unrecognized Host header.", code="NET-002")
        return await call_next(request)

    # Explicit application boundary (Release A A13): when BURNRATE_ACCESS_TOKEN
    # is configured, every /api read and write requires it as a bearer token.
    # The default remains loopback-only with no token; a request claiming a
    # trusted Host is never authentication by itself. A central server without
    # a token fails closed instead of serving anonymous reads.
    _access_token = os.getenv("BURNRATE_ACCESS_TOKEN", "").strip()
    from spend_app.connection_api import central_server

    _central = central_server()
    if _central:
        _setup_token = os.getenv("BURNRATE_SETUP_TOKEN", "").strip()
        if _setup_token and len(_setup_token) < 32:
            raise RuntimeError("BURNRATE_SETUP_TOKEN must be at least 32 characters.")
    with connect(settings.database_path) as connection:
        _protected = bool(_access_token or connection.execute(
            "SELECT 1 FROM app_meta WHERE key='auth.break_glass.v1'"
        ).fetchone())

    @app.middleware("http")
    async def access_boundary(request, call_next):
        from spend_app.auth_model import (
            COOKIE, Principal, authenticate_session, audit, break_glass_matches,
            create_session, owner_id, scoped_machine_read, set_session_cookie,
        )
        from spend_app.connection_api import (
            _MACHINE_READ_PATHS, access_allowed, csrf_mutation_allowed, host_allowed, trusted,
        )
        if not host_allowed(request.headers.get("host", "")):
            return boundary_error(403, "Request rejected: unrecognized Host header.", code="NET-002")
        auth = request.headers.get("authorization", "")
        bearer = auth[7:].strip() if auth.startswith("Bearer ") else ""
        cookie = request.cookies.get(COOKIE, "")
        principal = Principal()
        request.state.principal = principal
        request.state.machine_id = None

        async def recorded(response):
            path = request.url.path
            action = None
            if request.method == "POST":
                if path in {"/api/account/login", "/api/account/register"}:
                    action = "login" if path.endswith("login") else "account.register"
                elif path.startswith("/api/setup/"):
                    action = "setup.change"
                elif path == "/api/machines":
                    action = getattr(request.state, "audit_action", "machine.change")
                elif path == "/api/auth/machine/rotate":
                    action = "machine.rotate"
                elif path in {"/api/auth/logout", "/api/auth/sessions/revoke"}:
                    action = "session.revoke"
                elif path == "/api/auth/rotate":
                    action = "token.rotate"
                elif path == "/api/auth/session/rotate" and response.status_code >= 400:
                    action = "session.rotate"
                elif path == "/api/auth/bootstrap" and response.status_code >= 400:
                    action = "login"
            if getattr(request.state, "audit_recorded", False):
                action = None
            if action:
                with connect(settings.database_path) as connection:
                    audit(connection, request.state.principal, action,
                          getattr(request.state, "audit_target", "setup" if action == "setup.change" else path),
                          "success" if response.status_code < 400 else "failure")
            return response

        if bearer:
            if await asyncio.to_thread(break_glass_matches, settings.database_path, bearer, _access_token):
                automation = os.getenv("BURNRATE_ALLOW_ADMIN_BEARER", "0").lower() in {"1", "true", "yes"}
                if automation or request.url.path in {"/api/auth/bootstrap", "/api/auth/rotate"}:
                    principal = Principal("owner", "break-glass", "owner", via="break-glass")
            elif bearer.startswith("br_m_"):
                from spend_app.sync import authenticate_machine_token
                mid = await asyncio.to_thread(authenticate_machine_token, settings.database_path, bearer)
                if mid:
                    principal = Principal("machine", mid, via="bearer")
            elif bearer.startswith("br_s_"):
                principal = await asyncio.to_thread(authenticate_session, settings.database_path, bearer)
            if not principal or principal.kind == "unauthenticated":
                err_code = "DEV-301" if bearer.startswith("br_m_") else "AUTH-107"
                return await recorded(boundary_error(401, "Invalid, expired or revoked credential.", code=err_code))
        elif cookie:
            principal = await asyncio.to_thread(authenticate_session, settings.database_path, cookie, via="cookie")
            if not principal:
                # Login remains available when a browser's previous session expires.
                if request.url.path not in {"/api/account/login", "/api/account/register"} and request.url.path.startswith("/api/"):
                    return await recorded(boundary_error(401, "Session expired or revoked. Sign in again.", code="AUTH-107"))
                principal = Principal()
        request.state.principal = principal
        request.state.machine_id = principal.id if principal.kind == "machine" else None
        request.state.auth_kind = "machine" if principal.kind == "machine" else principal.kind
        if (_central and principal.kind == "unauthenticated" and request.method == "GET"
                and request.url.path in {"/", "/hud", "/setup"}
                and not request.query_params.get("token", "").strip()):
            # The shells carry no data, but a hub should not hand them to an
            # anonymous visitor. /auth is the public entry point; the ?token
            # exchange still needs the page it lands on.
            return await recorded(RedirectResponse("/auth", status_code=303))
        bearer_ok = principal.via in {"bearer", "break-glass"}
        if not csrf_mutation_allowed(request, bearer_ok=bearer_ok):
            return await recorded(boundary_error(403, "Request rejected: use the configured BURNRATE origin.", code="NET-002"))
        if (principal.via == "cookie" and request.method not in {"GET", "HEAD", "OPTIONS"}
                and (not trusted(request) or urlsplit(request.headers.get("origin", "")).scheme != request.url.scheme)):
            return await recorded(boundary_error(403, "Cookie mutations require the same origin."))
        protected = _protected
        if not protected:
            with connect(settings.database_path) as connection:
                protected = bool(connection.execute("SELECT 1 FROM app_meta WHERE key='auth.break_glass.v1'").fetchone())
        if not access_allowed(request, protected, central=_central, principal=principal):
            return await recorded(boundary_error(
                403 if principal.kind != "unauthenticated" else 401,
                "Permission denied." if principal.kind != "unauthenticated" else "Authentication required for the API",
                code="DEV-301" if principal.kind == "machine" else "AUTH-107",
            ))
        if principal.kind == "machine" and request.method in {"GET", "HEAD"} and request.url.path in _MACHINE_READ_PATHS:
            data = await asyncio.to_thread(scoped_machine_read, settings, pricing, principal, request)
            return JSONResponse(data, headers={"Cache-Control": "no-store"})
        if request.method == "GET" and request.url.path in {"/", "/hud", "/setup"}:
            candidate = request.query_params.get("token", "").strip()
            if candidate:
                remaining = [(k, v) for k, v in request.query_params.multi_items() if k != "token"]
                clean_url = request.url.path + ("?" + urlencode(remaining) if remaining else "")
                if principal.kind != "unauthenticated":
                    return await recorded(RedirectResponse(clean_url, status_code=303))
                if trusted(request, mutation=False) and await asyncio.to_thread(
                        break_glass_matches, settings.database_path, candidate, _access_token):
                    with connect(settings.database_path) as connection:
                        actor = Principal("owner", "break-glass", "owner", via="break-glass")
                        data = create_session(connection, actor, "Bootstrap browser", subject_id=owner_id(connection))
                        audit(connection, actor, "login", "bootstrap")
                    redirect = RedirectResponse(clean_url, status_code=303)
                    set_session_cookie(redirect, request, data)
                    return await recorded(redirect)
        response = await call_next(request)
        return await recorded(response)

    @app.exception_handler(ValueError)
    async def value_error_handler(_request, exc: ValueError):
        if isinstance(exc, UserFacingError):
            return JSONResponse(status_code=400, content={"error": str(exc)})
        logging.getLogger("burnrate.api").exception("Unhandled ValueError in request handler")
        return JSONResponse(status_code=400, content={"error": "invalid_request"})

    def _window_or_400(window: str) -> str:
        if window not in SPEND_WINDOWS:
            raise HTTPException(status_code=400, detail="unsupported spend window")
        return canonicalize_window(window)

    def _spend_summary_sync(
        request: Request,
        window: str = Query(default="1d"),
        tool: str = Query(default="all"),
        machine: str = Query(default="all"),
    ) -> dict:
        resolved = _window_or_400(window)
        # An already-open HUD may still run the previous embedded script
        # after a backend upgrade. Keep its collection and lightweight reads.
        referrer = urlsplit(request.headers.get("referer", ""))
        if referrer.path == "/hud" or parse_qs(referrer.query).get("hud") == ["1"]:
            return {**_spend_hud_usage_sync(machine), **_spend_hud_live_sync(machine)}
        request_history(request)
        record_summary_request(resolved, tool)
        from spend_app.sync import fetch_from_configured_hub
        hub_data = fetch_from_configured_hub(
            settings,
            "/api/spend/summary",
            {"window": resolved, "tool": tool, "machine": machine},
        )
        if hub_data is not None:
            return hub_data
        local_machine, scope = _hud_fallback_scope(machine)
        return {**aggregate_summary_cached(
            database_path=settings.database_path,
            pricing=pricing,
            window_key=resolved,
            tool=tool,
            timezone=settings.timezone,
            cache_threshold=settings.cache_hit_threshold,
            cadence_seconds=settings.local_ingest_interval_seconds,
            now=slot_now(),
            machine=local_machine,
        ), **scope}


    @app.get("/api/spend/summary")
    async def spend_summary(
        request: Request,
        window: str = Query(default="1d"),
        tool: str = Query(default="all"),
        machine: str = Query(default="all"),
    ) -> dict:
        return await _run_bounded_read(_spend_summary_sync, request, window, tool, machine)

    @app.get("/api/spend/nav")
    def spend_nav(request: Request) -> dict:
        request_history(request)
        from spend_app.sync import fetch_from_configured_hub
        hub_data = fetch_from_configured_hub(settings, "/api/spend/nav")
        if hub_data is not None:
            return hub_data
        return aggregate_nav(
            database_path=settings.database_path,
            pricing=pricing,
            timezone=settings.timezone,
            cadence_seconds=settings.local_ingest_interval_seconds,
            now=current_now(),
        )

    def _native_usage_sync(period: str, machine: str) -> dict:
        from spend_app.native_usage import usage_report
        from spend_app.setup_wizard import read_setup_state
        from spend_app.sync import fetch_from_configured_hub

        request_collection()
        hub = fetch_from_configured_hub(settings, "/api/spend/usage",
                                        {"period": period, "machine": machine}, read_timeout=20.0)
        if hub is not None and isinstance(hub.get("total"), dict):
            return {**hub, "scope": "all-computers" if machine == "all" else "computer"}
        with connect(settings.database_path) as connection:
            setup = read_setup_state(connection)
        if setup.get("role") == "spoke" and setup.get("spokeConfig", {}).get("enabled"):
            own_id = str(setup.get("account", {}).get("machineId") or "")
            own_selected = machine == "all" or bool(own_id and machine == own_id)
            # On the Hub, 'local' means the Hub itself, never this Spoke.
            # A cold/offline cache must not put this machine's totals under
            # another machine's name, or count imports left from a former Hub.
            report = usage_report(settings, pricing, now=slot_now(), period=period,
                                  machine="local" if own_selected else "_unavailable")
            own = next((row for row in report["machines"] if row["id"] == "local"), None)
            machines = [{**own, "id": own_id}] if own and own_id else []
            return {**report, "machine": machine, "machines": machines,
                    "scope": "local-fallback" if own_selected else "unavailable",
                    "warning": (
                        "Showing this computer's recorded usage only. Sign in at /auth for All computers; your hub may also be offline."
                        if own_selected else "This computer's history is unavailable until the hub reconnects."
                    )}
        return usage_report(settings, pricing, now=slot_now(), period=period, machine=machine)

    @app.get("/api/spend/usage")
    async def native_usage(period: str = Query(default="today", pattern="^(today|week|month|all)$"),
                           machine: str = Query(default="all")) -> dict:
        return await _run_bounded_read(_native_usage_sync, period, machine)

    @app.get("/api/spend/entity")
    def spend_entity(
        request: Request,
        kind: str = Query(..., pattern="^(model|tool)$"),
        key: str = Query(..., min_length=1),
        window: str = Query(default="1d"),
        machine: str = Query(default="all"),
    ) -> dict:
        resolved = _window_or_400(window)
        request_history(request)
        from spend_app.sync import fetch_from_configured_hub
        hub_data = fetch_from_configured_hub(
            settings,
            "/api/spend/entity",
            {"kind": kind, "key": key, "window": resolved, "machine": machine},
        )
        if hub_data is not None:
            return hub_data
        local_machine, scope = _hud_fallback_scope(machine)
        return {**aggregate_entity(
            database_path=settings.database_path,
            pricing=pricing,
            kind=kind,
            key=key,
            window_key=resolved,
            timezone=settings.timezone,
            cache_threshold=settings.cache_hit_threshold,
            now=slot_now(),
            machine=local_machine,
        ), **scope}

    @app.get("/api/spend/health")
    def spend_health(request: Request) -> dict:
        request_history(request)
        payload = aggregate_health_cached(
            database_path=settings.database_path,
            timezone=settings.timezone,
            now=slot_now(),
        )
        return {**payload, "sources": source_guidance()}

    @app.get("/api/spend/freshness")
    def spend_freshness(view: str = Query(default="dashboard", pattern="^(dashboard|hud)$")) -> dict:
        # This computer's own collection, quota and sync state. Never proxied:
        # on a spoke the hub cannot see that a local source or meter stalled.
        from spend_app.freshness import report

        # Collection is unconditional; the heartbeat is never masked, so the
        # freshness report and the watchdog always see collection as it is.
        collecting = True
        return report(settings, history_active=collecting if scheduler else True,
                      include_history=view != "hud" or collecting)

    def _hud_fallback_scope(machine):
        from spend_app.setup_wizard import read_setup_state
        with connect(settings.database_path) as connection:
            setup = read_setup_state(connection)
        if setup.get("role") != "spoke" or not setup["spokeConfig"].get("enabled"):
            return machine, {}
        own = str(setup.get("account", {}).get("machineId") or "")
        own_selected = machine in {"", "all", "local", own}
        return ("local" if own_selected else "_unavailable"), {
            "scope": "local-fallback" if own_selected else "unavailable",
            "warning": "Showing this computer only. Sign in for All computers, or reconnect your hub.",
            "authUrl": "/auth",
        }

    def _spend_hud_live_sync(machine: str = Query(default="all")) -> dict:
        from spend_app.hud_data import live_snapshot
        from spend_app.sync import fetch_from_configured_hub

        # The HUD polls every few seconds: serve the last Hub copy at once
        # and renew it in the background rather than wait on the network.
        hub = fetch_from_configured_hub(settings, "/api/spend/hud", {"machine": machine},
                                        stale_seconds=HUD_HUB_STALE_SECONDS)
        if hub is not None:
            return _stamp_hub_age(hub, now=current_now())
        local_machine, scope = _hud_fallback_scope(machine)
        return {**live_snapshot(settings, now=current_now(), machine=local_machine), **scope}


    @app.get("/api/spend/hud")
    async def spend_hud_live(machine: str = Query(default="all")) -> dict:
        return await _run_bounded_read(_spend_hud_live_sync, machine)

    def _spend_hud_usage_sync(machine: str = Query(default="all")) -> dict:
        from spend_app.hud_data import local_usage_machine, recorded_usage
        from spend_app.sync import fetch_from_configured_hub

        # Renew on every poll, including on paired spokes before the hub
        # read. The collector runs asynchronously; return recorded data now.
        request_collection()
        hub = fetch_from_configured_hub(settings, "/api/spend/hud/usage", {"machine": machine},
                                        stale_seconds=HUD_HUB_STALE_SECONDS,
                                        read_timeout=HUD_USAGE_HUB_READ_TIMEOUT)
        if hub is not None:
            return _stamp_hub_age(hub, now=current_now(), stamp=hub.get("historyUpdatedAt"))
        local_machine, scope = _hud_fallback_scope(machine)
        return {**recorded_usage(settings, pricing, now=current_now(),
                                machine=local_usage_machine(settings, local_machine)), **scope}


    @app.get("/api/spend/hud/usage")
    async def spend_hud_usage(machine: str = Query(default="all")) -> dict:
        return await _run_bounded_read(_spend_hud_usage_sync, machine)

    @app.get("/api/spend/share/week")
    def spend_share_week(machine: str = Query(default="all")) -> dict:
        from spend_app.hud_data import local_usage_machine, week_card
        from spend_app.sync import fetch_from_configured_hub

        hub = fetch_from_configured_hub(settings, "/api/spend/share/week", {"machine": machine})
        if hub is not None and "usd" in hub:
            return hub
        local_machine, scope = _hud_fallback_scope(machine)
        return {**week_card(settings, pricing, now=current_now(), machine=local_usage_machine(settings, local_machine)), **scope}

    @app.get("/api/onboarding")
    def onboarding():
        from spend_app.diagnostics import integration_reports, HELP_URL
        from spend_app.connections import Store
        from spend_app.sync import fetch_from_configured_hub, list_machines
        from spend_app.setup_wizard import public_setup_state, read_setup_state
        with connect(settings.database_path) as connection:
            history = connection.execute("SELECT EXISTS(SELECT 1 FROM usage_events) OR EXISTS(SELECT 1 FROM unpriced_usage_events)").fetchone()[0]
            machines = list_machines(connection)
            raw_setup = read_setup_state(connection)
            setup_completed = bool(raw_setup.get("completed"))
            public_setup = public_setup_state(raw_setup)
        hub_onboarding = fetch_from_configured_hub(settings, "/api/onboarding")
        if hub_onboarding is not None:
            if isinstance(hub_onboarding.get("machines"), list):
                machines = hub_onboarding["machines"]
            history = bool(history or hub_onboarding.get("hasHistory"))
        return {"timezone": settings.timezone, "timezoneSetting": "SPEND_TIMEZONE in your BURNRATE environment configuration",
                "hasHistory": bool(history), "sources": source_guidance(),
                "integrations": integration_reports(settings, bindings=Store(settings.database_path).read()["bindings"]),
                "machines": machines,
                "setupCompleted": setup_completed,
                "setup": public_setup,
                "documentation": HELP_URL,
                "detail": "BURNRATE stores usage locally. Provider network integrations are optional and remain under your existing configuration."}

    @app.post("/api/spend/quotas/refresh")
    def refresh_quotas() -> dict:
        from spend_app.quotas import nudge_quota_lanes, poll_quotas
        from spend_app.setup_wizard import maybe_run_configured_spoke_sync
        from spend_app.sync import STREAM_BROKER, expire_hub_proxy_cache

        now_val = current_now()
        stamp = now_val.astimezone(UTC).isoformat().replace("+00:00", "Z")
        if demo_mode():
            # The demo's meters are made up; polling would read this computer's accounts.
            return {"ok": True, "refreshedAt": stamp, "providersPolled": 0, "polledProviders": [], "syncResult": None}
        try:
            nudge_quota_lanes(database_path=settings.database_path)
        except Exception:
            pass
        try:
            outcomes = poll_quotas(database_path=settings.database_path, now=lambda: stamp, settings=settings)
        except Exception:
            outcomes = {}
        sync_res = maybe_run_configured_spoke_sync(settings, pricing)
        expire_hub_proxy_cache()
        from spend_app import aggregate as _agg
        _agg.reset_memo()
        polled_list = outcomes.get("polledProviders", []) if isinstance(outcomes, dict) else []
        STREAM_BROKER.publish({"type": "quota_refresh", "at": stamp})
        return {
            "ok": True,
            "refreshedAt": stamp,
            "providersPolled": len(polled_list),
            "polledProviders": polled_list,
            "syncResult": sync_res,
        }

    @app.get("/api/spend/limits")
    def spend_limits() -> dict:
        from spend_app.sync import fetch_from_configured_hub
        hub_data = fetch_from_configured_hub(settings, "/api/spend/limits")
        if hub_data is not None:
            return hub_data
        # Persisted-snapshot read only (Release A A01): a GET never launches
        # credential-bearing probes; the scheduled poller stores snapshots.
        return snapshot_limits(settings.database_path)

    @app.get("/api/spend")
    def spend_root() -> dict:
        return {
            "service": "BURNRATE",
            "version": __version__,
            "build": build_id(),
            "endpoints": [
                "/api/spend/summary",
                "/api/spend/nav",
                "/api/spend/entity",
                "/api/spend/health",
                "/api/spend/limits",
            ],
        }

    @app.get("/api/hub/info")
    async def hub_info(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "service": "burnrate",
                "protocolVersion": HUB_PROTOCOL_VERSION,
            },
            headers={
                "Cache-Control": "no-store",
                "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
            },
        )

    @app.get("/healthz")
    async def healthz(request: Request, supervisor: bool = False) -> dict:
        if not supervisor:
            return {"status": "ok"}
        if _central and not request.state.principal.permits("hub.read"):
            # Liveness stays public; process and ingest detail is owner-only on a hub.
            raise HTTPException(status_code=401, detail="Authentication required for supervisor health")
        from spend_app.freshness import heartbeat

        return {"status": "ok", "application": "burnrate", "pid": os.getpid(),
                "collectorEnabled": scheduler is not None,
                "ingest": heartbeat(current_now()) if scheduler is not None else None}

    @app.get("/")
    def spend_frontend(
        hud: int = Query(default=0),
        setup: int = Query(default=0),
        app_launch: int = Query(default=0, alias="app"),
    ):
        if hud == 1:
            return HTMLResponse(_live_hud_html(), headers={"Cache-Control": ASSET_REVALIDATE_CACHE})
        if setup == 1:
            from spend_app.setup_wizard import render_setup_wizard
            return HTMLResponse(render_setup_wizard(), headers={"Cache-Control": ASSET_REVALIDATE_CACHE})
        if app_launch == 1:
            from spend_app.setup_wizard import read_setup_state, render_setup_wizard
            try:
                initialize(settings.database_path)
                with connect(settings.database_path) as connection:
                    state = read_setup_state(connection)
                # "Set up later" counts: the wizard stays reachable from the HUD and tray.
                completed = bool(state.get("completed") or state.get("deferredAt"))
            except Exception:
                completed = False
            if not completed:
                return HTMLResponse(render_setup_wizard(), headers={"Cache-Control": ASSET_REVALIDATE_CACHE})
        if not dashboard_enabled():
            return HTMLResponse(HUD_LAUNCHER_HTML, headers={"Cache-Control": "no-store"})
        return HTMLResponse(
            render_index(web_root),
            headers={"Cache-Control": ASSET_REVALIDATE_CACHE},
        )

    @app.get("/hud")
    def spend_hud():
        return HTMLResponse(_live_hud_html(), headers={"Cache-Control": ASSET_REVALIDATE_CACHE})

    @app.post("/api/hud/native")
    async def launch_native_hud(request: Request) -> dict:
        import subprocess
        import sys

        if os.getenv("BURNRATE_CENTRAL_SERVER", "").strip().lower() in {"1", "true", "yes"}:
            return {"ok": False, "reason": "central_server"}
        # A demo in a browser must not open or control the real desktop HUD.
        if demo_mode():
            return {"ok": False, "reason": "demo"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        action = str((body or {}).get("action") or "open").strip().lower()
        if action not in {"open", "show", "pin"}:
            return {"ok": False, "reason": "unsupported_action"}
        local_app_data = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        programs_exe = local_app_data / "Programs" / "BURNRATE" / "BURNRATE.exe"
        exe_path = programs_exe if programs_exe.is_file() else None
        if exe_path is not None:
            try:
                # Hold the file without write sharing so the binary checked
                # below is the binary CreateProcess runs.
                with locked_binary(exe_path):
                    trusted, signature = await asyncio.to_thread(evaluate_native_binary, exe_path)
                    if not trusted:
                        return {"ok": False, "reason": "binary_refused", "state": signature}
                    si = None
                    if sys.platform == "win32":
                        si = subprocess.STARTUPINFO()
                        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                        si.wShowWindow = 5
                    subprocess.Popen(
                        [str(exe_path)],
                        cwd=str(exe_path.parent),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        startupinfo=si,
                    )
                    return {"ok": True, "state": signature}
            except OSError:
                return {"ok": False, "reason": "binary_busy"}
            except Exception:
                return {"ok": False, "reason": "launch_failed"}
        return {"ok": False, "reason": "not_found"}

    @app.get("/spend.css")
    def spend_css(request: Request):
        path = web_root / "spend.css"
        return FileResponse(
            path,
            media_type="text/css",
            headers={"Cache-Control": _asset_cache_header(request, path)},
        )

    @app.get("/spend.js")
    def spend_js(request: Request):
        path = web_root / "spend.js"
        return FileResponse(
            path,
            media_type="application/javascript",
            headers={"Cache-Control": _asset_cache_header(request, path)},
        )

    @app.get("/request-state.js")
    def request_state_js(request: Request):
        path = web_root / "request-state.js"
        return FileResponse(
            path,
            media_type="application/javascript",
            headers={"Cache-Control": _asset_cache_header(request, path)},
        )

    @app.get("/dialogs.js")
    def product_support_js(request: Request):
        path = web_root / request.url.path.removeprefix("/")
        return FileResponse(path, media_type="application/javascript",
                            headers={"Cache-Control": _asset_cache_header(request, path)})

    @app.get("/product.js")
    def product_js(request: Request):
        path = web_root / "product.js"
        return FileResponse(path, media_type="application/javascript",
                            headers={"Cache-Control": _asset_cache_header(request, path)})

    @app.get("/favicon.svg")
    def favicon(request: Request):
        path = web_root / "favicon.svg"
        return FileResponse(
            path,
            media_type="image/svg+xml",
            headers={"Cache-Control": _asset_cache_header(request, path)},
        )

    @app.get("/logos.svg")
    def provider_logos(request: Request):
        # One sprite of provider marks; pages reference symbols with <use href>.
        path = web_root / "logos.svg"
        return FileResponse(
            path,
            media_type="image/svg+xml",
            headers={"Cache-Control": _asset_cache_header(request, path)},
        )

    @app.get("/favicon.ico")
    def favicon_ico(request: Request):
        ico_path = Path(__file__).resolve().parents[1] / "assets" / "icons" / "icon.ico"
        if ico_path.is_file():
            return FileResponse(
                ico_path,
                media_type="image/x-icon",
                headers={"Cache-Control": _asset_cache_header(request, ico_path)},
            )
        path = web_root / "favicon.svg"
        return FileResponse(
            path,
            media_type="image/svg+xml",
            headers={"Cache-Control": _asset_cache_header(request, path)},
        )

    return app


_DEFAULT_APP: FastAPI | None = None


def __getattr__(name: str):
    global _DEFAULT_APP
    if name == "app":
        if _DEFAULT_APP is None:
            _DEFAULT_APP = create_app()
        return _DEFAULT_APP
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
