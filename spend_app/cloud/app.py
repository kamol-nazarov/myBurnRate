"""Multi-user hosted cloud FastAPI application factory (BURNRATE_CLOUD_SERVER=1)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware

from spend_app import __version__
from spend_app import aggregate as aggregate_mod
from spend_app.aggregate import (
    WINDOW_SPECS,
    aggregate_entity,
    aggregate_nav,
    aggregate_summary_cached,
    canonicalize_window,
    evict_database_memo,
)
from spend_app.auth_model import scoped_machine_read
from spend_app.auth_throttle import Throttle
from spend_app.bounded_reads import BoundedReadExecutor, ReadCapacityExceeded
from spend_app.cloud import cloud_auth_db_path, cloud_data_root
from spend_app.cloud.auth_store import (
    CLOUD_MACHINE_PREFIX,
    CLOUD_SESSION_PREFIX,
    CloudAuthError,
    CloudAuthStore,
    CloudPrincipal,
)
from spend_app.cloud.email_sender import EmailSender, default_email_sender
from spend_app.cloud.router import (
    _cloud_error_response,
    _mirror_machine_to_tenant_db,
    _parse_cloud_json,
    cloud_auth_router,
)
from spend_app.cloud.tenant import StorageQuotaExceeded, TenantManager
from spend_app.config import Settings, load_settings
from spend_app.connection_api import _MACHINE_READ_PATHS, csrf_mutation_allowed, host_allowed
from spend_app.db import connect
from spend_app.error_codes import (
    HUB_PROTOCOL_VERSION,
    check_protocol_version,
    error_payload,
    error_response,
)
from spend_app.exposure import TrustedProxyHeadersMiddleware, resolve_client_ip, trusted_proxies
from spend_app.hud_data import live_snapshot, recorded_usage, week_card
from spend_app.limits import snapshot_limits
from spend_app.machine_data import delete_machine_data
from spend_app.native_usage import usage_report
from spend_app.plan_service import PlanConflict, PlanError, apply_mutation, effective_terms, list_plans
from spend_app.plans_value_periods import SUPPORTED_PERIODS
from spend_app.plans_value_store import evict_plans_value_cache, fetch_plans_value_report
from spend_app.pricing import PricingEngine
from spend_app.redact import install_log_redaction
from spend_app.setup_wizard import _apply_subscription_presets_and_custom, _materialize_transferred_plans
from spend_app.subscription_transfer import export_history, import_history
from spend_app.sync import _read_state, apply_sync_push
from spend_app.sync_validation import reject_json_constant

LOGGER = logging.getLogger("burnrate.cloud")
SPEND_WINDOWS = set(WINDOW_SPECS) | {"7d", "30d", "MTD", "YTD", "All"}
MAX_SYNC_PUSH_BYTES = 16 * 1024 * 1024  # 16 MB
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; base-uri 'none'; form-action 'none'; "
    "object-src 'none'; frame-ancestors 'none'"
)
_PUBLIC_CLOUD_POST_PATHS = frozenset(
    {
        "/api/cloud/register",
        "/api/cloud/register/verify",
        "/api/cloud/login",
        "/api/cloud/pair",
        "/api/cloud/password-reset/request",
        "/api/cloud/password-reset/confirm",
    }
)
_MACHINE_POST_PATHS = frozenset(
    {
        "/api/sync/push",
        "/api/auth/machine/rotate",
        "/api/machines/self/revoke",
        "/api/machines",
    }
)
_PURGE_INTERVAL_SECONDS = 6 * 3600


def create_cloud_app(
    settings: Settings | None = None,
    *,
    data_root: Path | None = None,
    auth_store: CloudAuthStore | None = None,
    tenant_manager: TenantManager | None = None,
    email_sender: EmailSender | None = None,
    now: datetime | None = None,
    enable_purge_loop: bool = True,
) -> FastAPI:
    """Build the multi-tenant hosted myBurnRate Cloud FastAPI application."""
    base_settings = settings or load_settings()
    trusted_proxies()
    install_log_redaction()

    root = Path(data_root) if data_root is not None else cloud_data_root()
    store = auth_store or CloudAuthStore(cloud_auth_db_path(root))
    tenants = tenant_manager or TenantManager(root)
    mailer = email_sender or default_email_sender()
    pricing = PricingEngine.load(base_settings.pricing_path)
    machine_fail_throttle = Throttle(window=60, max_failures=30, lockout=60)
    sync_push_throttle = Throttle(window=60, max_failures=61, lockout=60)
    bounded_reads = BoundedReadExecutor(max_workers=4)

    async def _run_bounded_read(fn, *args):
        try:
            return await bounded_reads.run(fn, *args)
        except ReadCapacityExceeded:
            raise HTTPException(
                status_code=503,
                detail="Cloud analytics temporarily busy; retry shortly.",
                headers={"Retry-After": "1"},
            ) from None

    def current_now() -> datetime:
        return (now or datetime.now(UTC)).astimezone(UTC)

    def slot_now() -> datetime:
        """Quantize live timestamps to 15-second slots for high summary cache hit rates."""
        if now is not None:
            return current_now()
        cadence = max(1, int(base_settings.local_ingest_interval_seconds))
        ts = int(datetime.now(UTC).timestamp())
        return datetime.fromtimestamp((ts // cadence) * cadence, tz=UTC)

    def _user_settings(user_id: str) -> Settings:
        db_path = tenants.existing_user_db(user_id)
        return replace(base_settings, database_path=db_path)

    def _window_or_400(window: str) -> str:
        if window not in SPEND_WINDOWS:
            raise HTTPException(status_code=400, detail="unsupported spend window")
        return canonicalize_window(window)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        purge_task: asyncio.Task | None = None
        if enable_purge_loop:
            try:
                await run_in_threadpool(store.purge_expired)
            except Exception:
                LOGGER.exception("Initial cloud expiry purge failed")

            async def _loop():
                while True:
                    await asyncio.sleep(_PURGE_INTERVAL_SECONDS)
                    try:
                        await run_in_threadpool(store.purge_expired)
                    except Exception:
                        LOGGER.exception("Periodic cloud expiry purge failed")

            purge_task = asyncio.create_task(_loop())
        try:
            yield
        finally:
            if purge_task is not None:
                purge_task.cancel()
                try:
                    await purge_task
                except asyncio.CancelledError:
                    pass
            await asyncio.to_thread(bounded_reads.close)

    app = FastAPI(
        title="myBurnRate Cloud",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.auth_store = store
    app.state.tenant_manager = tenants
    app.state.email_sender = mailer

    app.include_router(
        cloud_auth_router(
            store,
            tenants,
            mailer,
            timezone=base_settings.timezone,
            now_fn=current_now,
        )
    )

    app.add_middleware(GZipMiddleware, minimum_size=1024)

    def _boundary_error(
        status: int,
        message: str,
        *,
        code: str | None = None,
        retry_after: int | None = None,
    ) -> JSONResponse:
        headers = {
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": CONTENT_SECURITY_POLICY,
            "Cache-Control": "no-store",
        }
        if retry_after is not None:
            headers["Retry-After"] = str(max(1, int(retry_after)))
        if code is not None:
            payload = error_payload(code, retry_after=retry_after, include_message=True)
            payload["error"]["detail"] = message
        else:
            payload = {"error": message}
        return JSONResponse(payload, status_code=status, headers=headers)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.middleware("http")
    async def cloud_access_boundary(request: Request, call_next):
        if not host_allowed(request.headers.get("host", "")):
            return _boundary_error(403, "Request rejected: unrecognized Host header.", code="NET-002")

        proto_err = check_protocol_version(request.headers.get("x-burnrate-protocol"))
        if proto_err is not None:
            return _boundary_error(426, "Protocol version mismatch.", code=proto_err)

        path = request.url.path
        method = request.method
        auth_header = request.headers.get("authorization", "")
        bearer = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
        principal = CloudPrincipal()
        request.state.principal = principal
        request.state.raw_bearer = bearer

        if bearer:
            if bearer.startswith(CLOUD_MACHINE_PREFIX):
                ip = resolve_client_ip(request)
                if wait := machine_fail_throttle.retry_after(ip):
                    return _boundary_error(429, "Too many invalid machine credentials.", code="AUTH-102", retry_after=wait)
                found = await run_in_threadpool(store.authenticate_machine, bearer, now=current_now())
                if found is None:
                    machine_fail_throttle.fail(ip)
                    return _boundary_error(401, "Invalid, expired, or revoked device credential.", code="DEV-301")
                principal = found
            elif bearer.startswith(CLOUD_SESSION_PREFIX):
                found = await run_in_threadpool(store.authenticate_session, bearer, now=current_now())
                if found is None:
                    return _boundary_error(401, "Session expired or revoked. Sign in again.", code="AUTH-107")
                principal = found
            else:
                return _boundary_error(401, "Invalid cloud credential.", code="AUTH-107")

        request.state.principal = principal
        request.state.machine_id = principal.machine_id

        if not csrf_mutation_allowed(request, bearer_ok=bool(bearer)):
            return _boundary_error(403, "Cross-origin browser requests are not allowed.", code="NET-002")

        # Public liveness and metadata
        if path in {"/healthz", "/api/hub/info"} and method in {"GET", "HEAD"}:
            return await call_next(request)

        # Public unauthenticated cloud auth endpoints
        if path in _PUBLIC_CLOUD_POST_PATHS and method == "POST":
            return await call_next(request)

        if not path.startswith("/api/"):
            return await call_next(request)

        if principal.kind == "unauthenticated" or not principal.user_id:
            return _boundary_error(401, "Authentication required.", code="AUTH-107")

        if principal.kind == "machine":
            if method == "POST" and path in _MACHINE_POST_PATHS:
                return await call_next(request)
            if method in {"GET", "HEAD"} and path == "/api/machines":
                return await call_next(request)
            if method in {"GET", "HEAD"} and path in _MACHINE_READ_PATHS:
                if request.query_params.getlist("machine") != [principal.machine_id]:
                    return _boundary_error(403, "Machine credentials can only read their own history.", code="DEV-301")
                user_settings = await run_in_threadpool(_user_settings, principal.user_id)

                def _scoped_read():
                    report = usage_report(
                        user_settings, pricing, now=current_now(), period="all", machine=principal.machine_id
                    )
                    report.pop("plans", None)
                    report.pop("machines", None)
                    report["scope"] = "computer"
                    return report

                data = await run_in_threadpool(_scoped_read)
                return JSONResponse(data, headers={"Cache-Control": "no-store"})
            return _boundary_error(403, "Machine credential is not permitted for this route.", code="DEV-301")

        # Owner principal: proceed to tenant-isolated handler
        return await call_next(request)

    # Outermost proxy header normalization (must wrap HTTP middleware)
    app.add_middleware(TrustedProxyHeadersMiddleware)

    @app.exception_handler(StarletteHTTPException)
    async def http_not_found_handler(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404 and not request.url.path.startswith("/api/"):
            return error_response("REQ-404", status_code=404, include_message=True)
        return await http_exception_handler(request, exc)

    @app.exception_handler(FileNotFoundError)
    async def missing_tenant_handler(_request: Request, _exc: FileNotFoundError):
        return error_response("AUTH-107", status_code=401, include_message=True)

    # -- Public Health & Hub Info -----------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "mode": "cloud"}

    @app.get("/api/hub/info")
    async def hub_info() -> JSONResponse:
        return JSONResponse(
            {
                "service": "burnrate",
                "protocolVersion": HUB_PROTOCOL_VERSION,
                "mode": "cloud",
            },
            headers={
                "Cache-Control": "no-store",
                "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
            },
        )

    # -- Sync Push (Machine Bearer, Quota Checked) ------------------------------

    @app.post("/api/sync/push")
    async def cloud_sync_push(request: Request):
        principal: CloudPrincipal = request.state.principal
        if principal.kind != "machine" or not principal.user_id or not principal.machine_id:
            return error_response("DEV-301", status_code=403, include_message=True)

        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return JSONResponse({"error": "Use application/json."}, status_code=415)

        uid = principal.user_id
        mid = principal.machine_id
        push_key = f"{uid}:{mid}"
        if wait := sync_push_throttle.retry_after(push_key):
            return error_response("AUTH-102", status_code=429, retry_after=wait, include_message=True)
        if wait := sync_push_throttle.fail(push_key):
            return error_response("AUTH-102", status_code=429, retry_after=wait, include_message=True)

        buf = bytearray()
        async for chunk in request.stream():
            if len(buf) + len(chunk) > MAX_SYNC_PUSH_BYTES:
                return error_response("REQ-401", status_code=413, include_message=True)
            buf.extend(chunk)
        try:
            body = json.loads(buf or b"{}", parse_constant=reject_json_constant)
        except ValueError:
            return JSONResponse({"error": "Invalid JSON sync payload."}, status_code=422)
        if not isinstance(body, dict):
            return JSONResponse({"error": "Expected a JSON object."}, status_code=422)

        def _do_push() -> dict:
            with tenants.lock_for(uid):
                tenants.check_storage_quota(uid, max_bytes=principal.max_db_bytes)
                db_path = tenants.existing_user_db(uid)
                result = apply_sync_push(
                    db_path,
                    pricing,
                    machine_id=mid,
                    body=body,
                    now=current_now(),
                    expected_token_hash=None,
                )
            tenants.maybe_prune_user_db(uid)
            return result

        try:
            result = await run_in_threadpool(_do_push)
        except StorageQuotaExceeded:
            return error_response("CLOUD-413", status_code=413, include_message=True)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422, headers={"Cache-Control": "no-store"})
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    # -- Tenant-Scoped Spend, HUD & Analytics Routes ----------------------------

    @app.get("/api/spend/summary")
    async def spend_summary(
        request: Request,
        window: str = Query(default="1d"),
        tool: str = Query(default="all"),
        machine: str = Query(default="all"),
    ) -> dict:
        resolved = _window_or_400(window)
        uid = request.state.principal.user_id
        assert uid is not None

        def _read():
            us = _user_settings(uid)
            return aggregate_summary_cached(
                database_path=us.database_path,
                pricing=pricing,
                window_key=resolved,
                tool=tool,
                timezone=us.timezone,
                cache_threshold=us.cache_hit_threshold,
                cadence_seconds=us.local_ingest_interval_seconds,
                now=slot_now(),
                machine=machine,
            )

        return await _run_bounded_read(_read)

    @app.get("/api/spend/nav")
    async def spend_nav(request: Request) -> dict:
        uid = request.state.principal.user_id
        assert uid is not None

        def _read():
            us = _user_settings(uid)
            return aggregate_nav(
                database_path=us.database_path,
                pricing=pricing,
                timezone=us.timezone,
                cadence_seconds=us.local_ingest_interval_seconds,
                now=current_now(),
            )

        return await run_in_threadpool(_read)

    @app.get("/api/spend/usage")
    async def native_usage(
        request: Request,
        period: str = Query(default="today", pattern="^(today|week|month|all)$"),
        machine: str = Query(default="all"),
    ) -> dict:
        uid = request.state.principal.user_id
        assert uid is not None

        def _read():
            us = _user_settings(uid)
            return usage_report(us, pricing, now=slot_now(), period=period, machine=machine)

        return await _run_bounded_read(_read)

    @app.get("/api/spend/entity")
    async def spend_entity(
        request: Request,
        kind: str = Query(..., pattern="^(model|tool)$"),
        key: str = Query(..., min_length=1),
        window: str = Query(default="1d"),
        machine: str = Query(default="all"),
        tool: str | None = Query(default=None, min_length=1),
    ) -> dict:
        resolved = _window_or_400(window)
        selected_tool = tool if kind == "model" and tool != "all" else None
        uid = request.state.principal.user_id
        assert uid is not None

        def _read():
            us = _user_settings(uid)
            return aggregate_entity(
                database_path=us.database_path,
                pricing=pricing,
                kind=kind,
                key=key,
                window_key=resolved,
                timezone=us.timezone,
                cache_threshold=us.cache_hit_threshold,
                now=current_now(),
                machine=machine,
                tool=selected_tool,
            )

        return await run_in_threadpool(_read)

    @app.get("/api/spend/hud")
    async def spend_hud(request: Request, machine: str = Query(default="all")) -> dict:
        uid = request.state.principal.user_id
        assert uid is not None

        def _read():
            us = _user_settings(uid)
            return live_snapshot(us, now=current_now(), machine=machine)

        return await run_in_threadpool(_read)

    @app.get("/api/spend/hud/usage")
    async def spend_hud_usage(request: Request, machine: str = Query(default="all")) -> dict:
        uid = request.state.principal.user_id
        assert uid is not None

        def _read():
            us = _user_settings(uid)
            return recorded_usage(us, pricing, now=current_now(), machine=machine)

        return await run_in_threadpool(_read)

    @app.get("/api/spend/limits")
    async def spend_limits(request: Request) -> dict:
        uid = request.state.principal.user_id
        assert uid is not None

        def _read():
            return snapshot_limits(tenants.existing_user_db(uid))

        return await run_in_threadpool(_read)

    @app.get("/api/spend/share/week")
    async def spend_share_week(request: Request, machine: str = Query(default="all")) -> dict:
        uid = request.state.principal.user_id
        assert uid is not None

        def _read():
            us = _user_settings(uid)
            return week_card(us, pricing, now=current_now(), machine=machine)

        return await run_in_threadpool(_read)

    @app.get("/api/onboarding")
    async def cloud_onboarding(request: Request) -> dict:
        uid = request.state.principal.user_id
        assert uid is not None

        def _read():
            db_path = tenants.existing_user_db(uid)
            with connect(db_path) as conn:
                has_history = bool(
                    conn.execute(
                        "SELECT EXISTS(SELECT 1 FROM usage_events) OR EXISTS(SELECT 1 FROM unpriced_usage_events)"
                    ).fetchone()[0]
                )
            machines = _combined_machines(uid)
            return {
                "timezone": base_settings.timezone,
                "hasHistory": has_history,
                "machines": machines,
                "mode": "cloud",
            }

        return await run_in_threadpool(_read)

    # -- Machine Registry & Per-Machine Data Deletion ---------------------------

    def _combined_machines(user_id: str) -> list[dict]:
        machines = store.list_machines(user_id, now=current_now())
        counts: dict[str, int] = {}
        try:
            db_path = tenants.existing_user_db(user_id)
            with connect(db_path) as conn:
                state = _read_state(conn)
                for mid, rec in (state.get("machines") or {}).items():
                    if isinstance(rec, dict):
                        counts[mid] = int(rec.get("events_pushed") or 0)
        except FileNotFoundError:
            pass
        for item in machines:
            item["eventsPushed"] = counts.get(item["id"], 0)
        return [item for item in machines if not item.get("revoked")]

    @app.get("/api/machines")
    async def get_machines(request: Request):
        principal: CloudPrincipal = request.state.principal
        assert principal.user_id is not None
        machines = await run_in_threadpool(_combined_machines, principal.user_id)
        if principal.kind == "machine":
            machines = [m for m in machines if m["id"] == principal.machine_id]
        return JSONResponse({"machines": machines}, headers={"Cache-Control": "no-store"})

    @app.post("/api/machines")
    async def mutate_machines(request: Request):
        principal: CloudPrincipal = request.state.principal
        assert principal.user_id is not None
        ip = resolve_client_ip(request)
        try:
            body = await _parse_cloud_json(request)
        except CloudAuthError as exc:
            return _cloud_error_response(exc)

        action = str(body.get("action") or "register").strip().lower()
        if principal.kind == "machine":
            target_id = str(body.get("machineId") or body.get("id") or "")
            if action not in {"rename", "revoke"} or target_id != principal.machine_id:
                return error_response("DEV-301", status_code=403, include_message=True)

        uid = principal.user_id
        try:
            if action == "register":
                res = await run_in_threadpool(
                    store.register_machine,
                    uid,
                    computer_name=str(body.get("name") or body.get("computerName") or "Connected Computer"),
                    os_name=str(body.get("os") or ""),
                    device_id=body.get("deviceId"),
                    client_ip=ip,
                    now=current_now(),
                )
                db_path = await run_in_threadpool(tenants.existing_user_db, uid)
                await run_in_threadpool(_mirror_machine_to_tenant_db, db_path, res["machine"])
                machines = await run_in_threadpool(_combined_machines, uid)
                return JSONResponse({**res, "machines": machines}, headers={"Cache-Control": "no-store"})

            if action == "rename":
                mid = str(body.get("machineId") or body.get("id") or "")
                new_name = str(body.get("name") or "")
                res = await run_in_threadpool(
                    store.rename_machine,
                    uid,
                    mid,
                    new_name,
                    actor_type=principal.kind,
                    actor_id=principal.id,
                    client_ip=ip,
                    now=current_now(),
                )
                db_path = await run_in_threadpool(tenants.existing_user_db, uid)
                await run_in_threadpool(
                    _mirror_machine_to_tenant_db, db_path, {"id": mid, "name": res["name"]}
                )
                machines = await run_in_threadpool(_combined_machines, uid)
                return JSONResponse({**res, "machines": machines}, headers={"Cache-Control": "no-store"})

            if action == "revoke":
                mid = str(body.get("machineId") or body.get("id") or "")
                changed = await run_in_threadpool(
                    store.revoke_machine,
                    uid,
                    mid,
                    actor_type=principal.kind,
                    actor_id=principal.id,
                    client_ip=ip,
                    now=current_now(),
                )
                if not changed:
                    return JSONResponse({"error": "Unknown or already revoked computer."}, status_code=404)
                db_path = await run_in_threadpool(tenants.existing_user_db, uid)
                await run_in_threadpool(_mirror_machine_to_tenant_db, db_path, {"id": mid}, revoked=True)
                machines = await run_in_threadpool(_combined_machines, uid)
                return JSONResponse({"ok": True, "machines": machines}, headers={"Cache-Control": "no-store"})

            return JSONResponse({"error": "Unsupported machine action."}, status_code=422)
        except CloudAuthError as exc:
            return _cloud_error_response(exc)

    @app.post("/api/machines/self/revoke")
    async def self_revoke_machine(request: Request):
        principal: CloudPrincipal = request.state.principal
        if principal.kind != "machine" or not principal.user_id or not principal.machine_id:
            return error_response("DEV-301", status_code=403, include_message=True)
        ip = resolve_client_ip(request)
        changed = await run_in_threadpool(
            store.revoke_machine,
            principal.user_id,
            principal.machine_id,
            actor_type="machine",
            actor_id=principal.machine_id,
            client_ip=ip,
            now=current_now(),
        )
        if not changed:
            return error_response("DEV-301", status_code=401, include_message=True)
        db_path = await run_in_threadpool(tenants.existing_user_db, principal.user_id)
        await run_in_threadpool(
            _mirror_machine_to_tenant_db, db_path, {"id": principal.machine_id}, revoked=True
        )
        return JSONResponse({"ok": True, "revoked": True}, headers={"Cache-Control": "no-store"})

    @app.post("/api/machines/{machine_id}/data/delete")
    async def cloud_delete_machine_data(request: Request, machine_id: str):
        principal: CloudPrincipal = request.state.principal
        if principal.kind != "owner" or not principal.user_id:
            return error_response("AUTH-107", status_code=403, include_message=True)
        ip = resolve_client_ip(request)
        uid = principal.user_id

        all_machines = await run_in_threadpool(store.list_machines, uid, now=current_now())
        target = next((m for m in all_machines if m["id"] == machine_id), None)
        if target is None:
            return JSONResponse({"error": "Computer not found on this account."}, status_code=404)
        if not target.get("revoked"):
            return JSONResponse({"error": "Revoke this computer before removing its data."}, status_code=422)

        def _delete():
            with tenants.lock_for(uid):
                db_path = tenants.existing_user_db(uid)
                _mirror_machine_to_tenant_db(db_path, target, revoked=True)
                res = delete_machine_data(
                    db_path,
                    machine_id,
                    actor=principal,
                    create_backup=False,
                    source=ip,
                )
                evict_database_memo(db_path)
                evict_plans_value_cache(db_path)
                return res

        try:
            result = await run_in_threadpool(_delete)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    # -- Sessions & Token Rotation ----------------------------------------------

    @app.get("/api/auth/sessions")
    async def list_sessions(request: Request):
        principal: CloudPrincipal = request.state.principal
        assert principal.user_id is not None
        rows = await run_in_threadpool(store.list_sessions, principal.user_id)
        for r in rows:
            r["machine_binding"] = "bound" if r.get("machine_id") else "not tied to a computer"
        return JSONResponse(
            {"sessions": rows, "currentSessionId": principal.session_id},
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/auth/sessions/revoke")
    async def revoke_session(request: Request):
        principal: CloudPrincipal = request.state.principal
        assert principal.user_id is not None
        try:
            body = await _parse_cloud_json(request)
        except CloudAuthError as exc:
            return _cloud_error_response(exc)
        sid = str(body.get("id") or "")
        changed = await run_in_threadpool(
            store.revoke_session,
            principal.user_id,
            sid,
            client_ip=resolve_client_ip(request),
            now=current_now(),
        )
        if not changed:
            return JSONResponse({"error": "Session not found or already revoked."}, status_code=404)
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        principal: CloudPrincipal = request.state.principal
        if principal.user_id and principal.session_id:
            await run_in_threadpool(
                store.revoke_session,
                principal.user_id,
                principal.session_id,
                client_ip=resolve_client_ip(request),
                now=current_now(),
            )
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    @app.post("/api/auth/session/rotate")
    async def rotate_session(request: Request):
        principal: CloudPrincipal = request.state.principal
        data = await run_in_threadpool(
            store.rotate_session,
            principal,
            client_ip=resolve_client_ip(request),
            now=current_now(),
        )
        if data is None:
            return error_response("AUTH-107", status_code=401, include_message=True)
        return JSONResponse(
            {
                "ok": True,
                "sessionToken": data["sessionToken"],
                "sessionId": data["sessionId"],
                "expiresAt": data["expiresAt"],
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/auth/machine/rotate")
    async def rotate_machine(request: Request):
        principal: CloudPrincipal = request.state.principal
        raw_bearer = getattr(request.state, "raw_bearer", "")
        data = await run_in_threadpool(
            store.rotate_machine,
            principal,
            raw_bearer,
            client_ip=resolve_client_ip(request),
            now=current_now(),
        )
        if data is None:
            return error_response("DEV-301", status_code=401, include_message=True)
        return JSONResponse(data, headers={"Cache-Control": "no-store"})

    # -- Tenant-Scoped Subscriptions --------------------------------------------

    @app.get("/api/subscriptions")
    async def get_subscriptions(request: Request):
        uid = request.state.principal.user_id
        assert uid is not None

        def _read():
            db_path = tenants.existing_user_db(uid)
            today = current_now().astimezone(ZoneInfo(base_settings.timezone)).date()
            with connect(db_path) as conn:
                return {**list_plans(conn, today), "timezone": base_settings.timezone}

        return await run_in_threadpool(_read)

    @app.get("/api/subscriptions/value")
    async def get_subscriptions_value(request: Request, period: str = "this_month"):
        if period not in SUPPORTED_PERIODS:
            return JSONResponse(
                {"error": f"Invalid period '{period}'. Use 'this_month', 'last_month', or 'this_cycle'."},
                status_code=422,
            )
        uid = request.state.principal.user_id
        assert uid is not None

        def _read():
            us = _user_settings(uid)
            with connect(us.database_path) as conn:
                return fetch_plans_value_report(
                    connection=conn,
                    settings=us,
                    pricing=pricing,
                    period=period,
                    as_of=current_now(),
                )

        return JSONResponse(await run_in_threadpool(_read), headers={"Cache-Control": "no-store"})

    @app.post("/api/account/subscriptions")
    async def mutate_subscriptions(request: Request):
        uid = request.state.principal.user_id
        assert uid is not None
        try:
            body = await _parse_cloud_json(request)
        except CloudAuthError as exc:
            return _cloud_error_response(exc)

        def _apply():
            today = current_now().astimezone(ZoneInfo(base_settings.timezone)).date()
            with tenants.lock_for(uid):
                db_path = tenants.existing_user_db(uid)
                with connect(db_path) as conn:
                    if body.get("action") == "import-history":
                        expected_store = body.get("expectedStore")
                        if not isinstance(expected_store, str) or not re.fullmatch(r"[a-f0-9]{32}", expected_store):
                            raise PlanError("The subscription history source is required.")
                        conn.execute("BEGIN IMMEDIATE")
                        if import_history(conn, body.get("subscriptionHistory"), expected_store=expected_store):
                            _materialize_transferred_plans(conn, base_settings.timezone)
                        aggregate_mod.reset_memo()
                        return {
                            "subscriptionHistory": export_history(conn, today),
                            "subscriptions": effective_terms(conn),
                        }
                    if "action" in body or "operation" in body:
                        conn.execute("BEGIN IMMEDIATE")
                        res = apply_mutation(
                            conn, {k: v for k, v in body.items() if k != "action"}, today
                        )
                        aggregate_mod.reset_memo()
                        return res
                    preset_keys = [str(k) for k in (body.get("presetKeys") or []) if k]
                    custom_plans = body.get("customPlans") if isinstance(body.get("customPlans"), list) else []
                    _apply_subscription_presets_and_custom(
                        conn,
                        base_settings.timezone,
                        preset_keys,
                        custom_plans,
                    )
                    aggregate_mod.reset_memo()
                    return {**list_plans(conn, today), "timezone": base_settings.timezone}

        try:
            res = await run_in_threadpool(_apply)
            return JSONResponse(res, headers={"Cache-Control": "no-store"})
        except PlanConflict as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except (PlanError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    return app
