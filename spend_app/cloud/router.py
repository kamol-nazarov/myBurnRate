"""Cloud authentication, registration OTP, pairing, password reset, email change, and account deletion routes."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from spend_app.auth_throttle import Throttle
from spend_app.cloud.auth_store import CloudAuthError, CloudAuthStore, CloudPrincipal
from spend_app.cloud.email_sender import (
    EmailDeliveryError,
    EmailSender,
    account_deleted_receipt,
    email_change_verification_email,
    email_changed_alert,
    existing_account_signup_alert,
    password_changed_alert,
    password_reset_email,
    verification_email,
)
from spend_app.cloud.tenant import TenantManager
from spend_app.db import connect, utc_now
from spend_app.error_codes import error_payload, error_response
from spend_app.exposure import resolve_client_ip
from spend_app.plan_service import PlanError
from spend_app.setup_wizard import _materialize_transferred_plans, normalize_email
from spend_app.subscription_transfer import export_history, import_history
from spend_app.sync import _read_state, _write_state

LOGGER = logging.getLogger("burnrate.cloud")
_MAX_AUTH_BODY_BYTES = 65536


def _mirror_machine_to_tenant_db(
    db_path: Path,
    machine: dict,
    *,
    revoked: bool = False,
    now_iso: str | None = None,
) -> None:
    """Mirror display metadata (never token hashes) into a user's isolated spend.db."""
    mid = str(machine.get("id") or "").strip()
    if not mid:
        return
    stamp = now_iso or utc_now()
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        state = _read_state(connection)
        rec = state["machines"].get(mid) or {
            "id": mid,
            "created_at": stamp,
            "events_pushed": 0,
        }
        rec["id"] = mid
        rec["name"] = str(machine.get("name") or rec.get("name") or mid)[:64]
        rec["owner_name"] = rec["name"]
        rec["reported_name"] = str(machine.get("reportedName") or rec.get("reported_name") or rec["name"])[:64]
        rec["os"] = str(machine.get("os") or rec.get("os") or "")[:32]
        rec["token_hash"] = ""  # Cloud tokens live exclusively in auth.db
        rec["last_seen_at"] = stamp
        if machine.get("expiresAt"):
            rec["expires_at"] = machine["expiresAt"]
        if revoked:
            rec["revoked"] = True
            rec["revoked_at"] = stamp
        else:
            rec["revoked"] = False
            rec.pop("revoked_at", None)
        state["machines"][mid] = rec
        _write_state(connection, state)


def _mark_all_tenant_machines_revoked(db_path: Path, *, now_iso: str | None = None) -> None:
    if not db_path.is_file():
        return
    stamp = now_iso or utc_now()
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        state = _read_state(connection)
        for rec in state["machines"].values():
            if isinstance(rec, dict) and not rec.get("revoked"):
                rec["revoked"] = True
                rec["revoked_at"] = stamp
                rec["token_hash"] = ""
        _write_state(connection, state)


def _merge_subscription_history(db_path: Path, timezone: str, raw_history: object) -> dict | None:
    today = datetime.now(UTC).astimezone(ZoneInfo(timezone)).date()
    with connect(db_path) as connection:
        if raw_history is not None:
            connection.execute("BEGIN IMMEDIATE")
            if import_history(connection, raw_history):
                _materialize_transferred_plans(connection, timezone)
        return export_history(connection, today)


async def _parse_cloud_json(request: Request, *, max_bytes: int = _MAX_AUTH_BODY_BYTES) -> dict:
    if request.headers.get("x-burnrate-request") != "1":
        raise CloudAuthError("NET-002", "Missing BURNRATE request header.", status_code=403)
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise CloudAuthError("APP-901", "Content-Type must be application/json.", status_code=415)
    buf = bytearray()
    async for chunk in request.stream():
        if len(buf) + len(chunk) > max_bytes:
            raise CloudAuthError("REQ-401", "Request body is too large.", status_code=413)
        buf.extend(chunk)
    try:
        data = json.loads(
            buf or b"{}",
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid JSON constant")),
        )
    except ValueError:
        raise CloudAuthError("APP-901", "Invalid JSON payload.", status_code=422) from None
    if not isinstance(data, dict):
        raise CloudAuthError("APP-901", "Expected a JSON object.", status_code=422)
    return data


def _cloud_error_response(exc: CloudAuthError) -> JSONResponse:
    payload = error_payload(exc.code, include_message=True)
    payload["error"]["detail"] = exc.message
    return JSONResponse(payload, status_code=exc.status_code, headers={"Cache-Control": "no-store"})


def cloud_auth_router(
    auth_store: CloudAuthStore,
    tenant_manager: TenantManager,
    email_sender: EmailSender,
    *,
    timezone: str = "UTC",
    now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> APIRouter:
    router = APIRouter()
    register_ip_throttle = Throttle(window=15 * 60, max_failures=10, lockout=15 * 60)
    register_email_throttle = Throttle(window=15 * 60, max_failures=5, lockout=15 * 60)
    otp_throttle = Throttle(window=15 * 60, max_failures=10, lockout=15 * 60)
    login_throttle = Throttle(window=15 * 60, max_failures=5, lockout=15 * 60)
    pair_throttle = Throttle(window=15 * 60, max_failures=10, lockout=15 * 60)
    reset_throttle = Throttle(window=15 * 60, max_failures=5, lockout=15 * 60)

    def _require_owner(request: Request, *, sensitive: bool = False) -> CloudPrincipal:
        principal: CloudPrincipal = request.state.principal
        if principal.kind != "owner" or not principal.user_id:
            raise CloudAuthError("AUTH-107", "Owner sign-in required.", status_code=401)
        if sensitive and not principal.permits("account.sensitive"):
            raise CloudAuthError(
                "AUTH-107",
                "Sign in with your account password to perform this sensitive account action.",
                status_code=403,
            )
        return principal

    @router.post("/api/cloud/register")
    async def register_start(request: Request):
        ip = resolve_client_ip(request)
        if wait := register_ip_throttle.retry_after(ip):
            return error_response("AUTH-102", status_code=429, retry_after=wait, include_message=True)
        try:
            body = await _parse_cloud_json(request)
        except CloudAuthError as exc:
            return _cloud_error_response(exc)

        raw_email = str(body.get("email") or "")
        password = str(body.get("password") or "")
        clean_email = normalize_email(raw_email)
        if clean_email is not None:
            if wait := register_email_throttle.retry_after(clean_email):
                return error_response("AUTH-102", status_code=429, retry_after=wait, include_message=True)
            register_email_throttle.fail(clean_email)
        register_ip_throttle.fail(ip)

        try:
            state, code = await run_in_threadpool(
                auth_store.start_registration,
                raw_email,
                password,
                client_ip=ip,
                now=now_fn(),
            )
        except CloudAuthError as exc:
            return _cloud_error_response(exc)

        assert clean_email is not None
        if state == "pending" and code is not None:
            try:
                await run_in_threadpool(email_sender.send, verification_email(clean_email, code))
            except EmailDeliveryError:
                LOGGER.warning("Resend delivery failed during registration start")
                return error_response("CLOUD-503", status_code=503, include_message=True)
        elif state == "existing":
            try:
                await run_in_threadpool(email_sender.send, existing_account_signup_alert(clean_email))
            except EmailDeliveryError:
                LOGGER.warning("Resend alert failed for existing account signup attempt")

        return JSONResponse(
            {
                "ok": True,
                "verificationRequired": True,
                "message": "Check your email for a 6-digit verification code.",
            },
            status_code=202,
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/cloud/register/verify")
    async def register_verify(request: Request):
        ip = resolve_client_ip(request)
        try:
            body = await _parse_cloud_json(request)
        except CloudAuthError as exc:
            return _cloud_error_response(exc)

        raw_email = str(body.get("email") or "")
        clean_email = normalize_email(raw_email) or raw_email.strip().lower()
        tkey = f"{ip}|{clean_email}"
        if wait := otp_throttle.retry_after(tkey):
            return error_response("AUTH-102", status_code=429, retry_after=wait, include_message=True)

        code = str(body.get("code") or "")
        computer_name = str(body.get("computerName") or "Connected Computer")
        os_name = str(body.get("os") or "")
        device_id = body.get("deviceId")

        try:
            verified = await run_in_threadpool(
                auth_store.verify_registration,
                raw_email,
                code,
                computer_name=computer_name,
                os_name=os_name,
                device_id=device_id,
                client_ip=ip,
                now=now_fn(),
            )
        except CloudAuthError as exc:
            wait = otp_throttle.fail(tkey)
            if wait:
                return error_response("AUTH-102", status_code=429, retry_after=wait, include_message=True)
            return _cloud_error_response(exc)

        otp_throttle.succeed(tkey)
        uid = verified["user"]["id"]
        db_path = await run_in_threadpool(tenant_manager.provision_user_db, uid)
        await run_in_threadpool(
            _mirror_machine_to_tenant_db,
            db_path,
            verified["machine"],
            now_iso=now_fn().astimezone(UTC).isoformat().replace("+00:00", "Z"),
        )
        try:
            sub_history = await run_in_threadpool(
                _merge_subscription_history,
                db_path,
                timezone,
                body.get("subscriptionHistory"),
            )
        except PlanError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422, headers={"Cache-Control": "no-store"})

        return JSONResponse(
            {"ok": True, **verified, "subscriptionHistory": sub_history},
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/cloud/login")
    async def cloud_login(request: Request):
        ip = resolve_client_ip(request)
        try:
            body = await _parse_cloud_json(request)
        except CloudAuthError as exc:
            return _cloud_error_response(exc)

        raw_email = str(body.get("email") or "")
        clean_email = normalize_email(raw_email) or raw_email.strip().lower()
        tkey = f"{ip}|{clean_email}"
        if wait := login_throttle.retry_after(tkey):
            return error_response("AUTH-102", status_code=429, retry_after=wait, include_message=True)

        password = str(body.get("password") or "")
        computer_name = str(body.get("computerName") or "Connected Computer")
        os_name = str(body.get("os") or "")
        device_id = body.get("deviceId")

        try:
            logged = await run_in_threadpool(
                auth_store.login,
                raw_email,
                password,
                computer_name=computer_name,
                os_name=os_name,
                device_id=device_id,
                client_ip=ip,
                now=now_fn(),
            )
        except CloudAuthError as exc:
            if exc.status_code == 401:
                wait = login_throttle.fail(tkey)
                if wait:
                    return error_response("AUTH-102", status_code=429, retry_after=wait, include_message=True)
            return _cloud_error_response(exc)

        login_throttle.succeed(tkey)
        uid = logged["user"]["id"]
        try:
            db_path = await run_in_threadpool(tenant_manager.existing_user_db, uid)
        except FileNotFoundError:
            db_path = await run_in_threadpool(tenant_manager.provision_user_db, uid)
        await run_in_threadpool(
            _mirror_machine_to_tenant_db,
            db_path,
            logged["machine"],
            now_iso=now_fn().astimezone(UTC).isoformat().replace("+00:00", "Z"),
        )
        try:
            sub_history = await run_in_threadpool(
                _merge_subscription_history,
                db_path,
                timezone,
                body.get("subscriptionHistory"),
            )
        except PlanError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422, headers={"Cache-Control": "no-store"})

        return JSONResponse(
            {"ok": True, **logged, "subscriptionHistory": sub_history},
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/cloud/pairing-code")
    async def create_pairing_code(request: Request):
        try:
            principal = _require_owner(request, sensitive=False)
            await _parse_cloud_json(request)
        except CloudAuthError as exc:
            return _cloud_error_response(exc)

        ip = resolve_client_ip(request)
        assert principal.user_id is not None
        data = await run_in_threadpool(
            auth_store.create_pairing_code,
            principal.user_id,
            principal.session_id,
            client_ip=ip,
            now=now_fn(),
        )
        return JSONResponse({"ok": True, **data}, headers={"Cache-Control": "no-store"})

    @router.post("/api/cloud/pair")
    async def redeem_pairing_code(request: Request):
        ip = resolve_client_ip(request)
        if wait := pair_throttle.retry_after(ip):
            return error_response("AUTH-102", status_code=429, retry_after=wait, include_message=True)
        try:
            body = await _parse_cloud_json(request)
        except CloudAuthError as exc:
            return _cloud_error_response(exc)

        code = str(body.get("code") or "")
        computer_name = str(body.get("computerName") or "Connected Computer")
        os_name = str(body.get("os") or "")
        device_id = body.get("deviceId")

        try:
            paired = await run_in_threadpool(
                auth_store.redeem_pairing_code,
                code,
                computer_name=computer_name,
                os_name=os_name,
                device_id=device_id,
                client_ip=ip,
                now=now_fn(),
            )
        except CloudAuthError as exc:
            if exc.status_code == 401:
                wait = pair_throttle.fail(ip)
                if wait:
                    return error_response("AUTH-102", status_code=429, retry_after=wait, include_message=True)
            return _cloud_error_response(exc)

        pair_throttle.succeed(ip)
        uid = paired["user"]["id"]
        db_path = await run_in_threadpool(tenant_manager.existing_user_db, uid)
        await run_in_threadpool(
            _mirror_machine_to_tenant_db,
            db_path,
            paired["machine"],
            now_iso=now_fn().astimezone(UTC).isoformat().replace("+00:00", "Z"),
        )
        try:
            sub_history = await run_in_threadpool(
                _merge_subscription_history,
                db_path,
                timezone,
                body.get("subscriptionHistory"),
            )
        except PlanError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422, headers={"Cache-Control": "no-store"})

        return JSONResponse(
            {"ok": True, "paired": True, **paired, "subscriptionHistory": sub_history},
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/cloud/password-reset/request")
    async def password_reset_request(request: Request):
        ip = resolve_client_ip(request)
        try:
            body = await _parse_cloud_json(request)
        except CloudAuthError as exc:
            return _cloud_error_response(exc)

        raw_email = str(body.get("email") or "")
        clean_email = normalize_email(raw_email) or raw_email.strip().lower()
        tkey = f"{ip}|{clean_email}"
        if wait := reset_throttle.retry_after(tkey):
            return error_response("AUTH-102", status_code=429, retry_after=wait, include_message=True)
        reset_throttle.fail(tkey)

        uid, code = await run_in_threadpool(
            auth_store.request_password_reset,
            raw_email,
            client_ip=ip,
            now=now_fn(),
        )
        if uid is not None and code is not None and normalize_email(raw_email):
            try:
                await run_in_threadpool(email_sender.send, password_reset_email(clean_email, code))
            except EmailDeliveryError:
                LOGGER.warning("Resend delivery failed for password reset request")
                return error_response("CLOUD-503", status_code=503, include_message=True)

        return JSONResponse(
            {
                "ok": True,
                "message": "If an account exists for that email, a 6-digit reset code has been sent.",
            },
            status_code=202,
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/cloud/password-reset/confirm")
    async def password_reset_confirm(request: Request):
        ip = resolve_client_ip(request)
        try:
            body = await _parse_cloud_json(request)
        except CloudAuthError as exc:
            return _cloud_error_response(exc)

        raw_email = str(body.get("email") or "")
        clean_email = normalize_email(raw_email) or raw_email.strip().lower()
        tkey = f"{ip}|{clean_email}"
        if wait := otp_throttle.retry_after(tkey):
            return error_response("AUTH-102", status_code=429, retry_after=wait, include_message=True)

        code = str(body.get("code") or "")
        new_password = str(body.get("newPassword") or "")
        disconnect_devices = bool(body.get("disconnectDevices", False))

        try:
            result = await run_in_threadpool(
                auth_store.confirm_password_reset,
                raw_email,
                code,
                new_password,
                disconnect_devices=disconnect_devices,
                client_ip=ip,
                now=now_fn(),
            )
        except CloudAuthError as exc:
            if exc.status_code == 401:
                wait = otp_throttle.fail(tkey)
                if wait:
                    return error_response("AUTH-102", status_code=429, retry_after=wait, include_message=True)
            return _cloud_error_response(exc)

        otp_throttle.succeed(tkey)
        reset_uid = result.pop("userId", None)
        if disconnect_devices and reset_uid:
            try:
                db_path = tenant_manager.existing_user_db(reset_uid)
                await run_in_threadpool(_mark_all_tenant_machines_revoked, db_path)
            except FileNotFoundError:
                pass
        try:
            await run_in_threadpool(
                email_sender.send,
                password_changed_alert(result["email"], disconnected_devices=disconnect_devices),
            )
        except EmailDeliveryError:
            LOGGER.warning("Password changed alert email failed")

        return JSONResponse(result, status_code=200, headers={"Cache-Control": "no-store"})

    @router.post("/api/cloud/password/change")
    async def password_change(request: Request):
        try:
            principal = _require_owner(request, sensitive=True)
            body = await _parse_cloud_json(request)
        except CloudAuthError as exc:
            return _cloud_error_response(exc)

        ip = resolve_client_ip(request)
        assert principal.user_id is not None
        try:
            result = await run_in_threadpool(
                auth_store.change_password,
                principal.user_id,
                str(body.get("currentPassword") or ""),
                str(body.get("newPassword") or ""),
                current_session_id=principal.session_id,
                client_ip=ip,
                now=now_fn(),
            )
        except CloudAuthError as exc:
            return _cloud_error_response(exc)

        try:
            await run_in_threadpool(
                email_sender.send,
                password_changed_alert(result["email"], disconnected_devices=False),
            )
        except EmailDeliveryError:
            LOGGER.warning("Password changed alert email failed")

        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    @router.post("/api/cloud/email-change/request")
    async def email_change_request(request: Request):
        try:
            principal = _require_owner(request, sensitive=True)
            body = await _parse_cloud_json(request)
        except CloudAuthError as exc:
            return _cloud_error_response(exc)

        ip = resolve_client_ip(request)
        assert principal.user_id is not None
        try:
            target_email, code = await run_in_threadpool(
                auth_store.request_email_change,
                principal.user_id,
                str(body.get("password") or ""),
                str(body.get("newEmail") or ""),
                client_ip=ip,
                now=now_fn(),
            )
        except CloudAuthError as exc:
            return _cloud_error_response(exc)

        try:
            await run_in_threadpool(
                email_sender.send,
                email_change_verification_email(target_email, code),
            )
        except EmailDeliveryError:
            return error_response("CLOUD-503", status_code=503, include_message=True)

        return JSONResponse(
            {"ok": True, "targetEmail": target_email, "message": "Check your new email for a 6-digit code."},
            status_code=202,
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/cloud/email-change/confirm")
    async def email_change_confirm(request: Request):
        try:
            principal = _require_owner(request, sensitive=True)
            body = await _parse_cloud_json(request)
        except CloudAuthError as exc:
            return _cloud_error_response(exc)

        ip = resolve_client_ip(request)
        assert principal.user_id is not None
        try:
            old_email, new_email = await run_in_threadpool(
                auth_store.confirm_email_change,
                principal.user_id,
                str(body.get("code") or ""),
                client_ip=ip,
                now=now_fn(),
            )
        except CloudAuthError as exc:
            return _cloud_error_response(exc)

        for recipient in (old_email, new_email):
            try:
                await run_in_threadpool(
                    email_sender.send,
                    email_changed_alert(recipient, new_email=new_email),
                )
            except EmailDeliveryError:
                LOGGER.warning("Email change alert delivery failed")

        return JSONResponse({"ok": True, "email": new_email}, headers={"Cache-Control": "no-store"})

    @router.post("/api/cloud/account/delete")
    async def delete_account(request: Request):
        try:
            principal = _require_owner(request, sensitive=True)
            body = await _parse_cloud_json(request)
        except CloudAuthError as exc:
            return _cloud_error_response(exc)

        if str(body.get("confirm") or "").strip() != "DELETE":
            return _cloud_error_response(
                CloudAuthError("APP-901", "Type DELETE to confirm permanent account deletion.", status_code=422)
            )
        ip = resolve_client_ip(request)
        assert principal.user_id is not None
        try:
            email = await run_in_threadpool(
                auth_store.delete_account,
                principal.user_id,
                str(body.get("password") or ""),
                client_ip=ip,
                now=now_fn(),
            )
        except CloudAuthError as exc:
            return _cloud_error_response(exc)

        await run_in_threadpool(tenant_manager.delete_user_storage, principal.user_id)
        try:
            await run_in_threadpool(email_sender.send, account_deleted_receipt(email))
        except EmailDeliveryError:
            LOGGER.warning("Account deletion receipt email failed")

        return JSONResponse({"ok": True, "deleted": True}, headers={"Cache-Control": "no-store"})

    @router.get("/api/cloud/me")
    async def cloud_me(request: Request):
        try:
            principal = _require_owner(request, sensitive=False)
        except CloudAuthError as exc:
            return _cloud_error_response(exc)
        assert principal.user_id is not None
        profile = await run_in_threadpool(auth_store.get_user_profile, principal.user_id)
        if profile is None:
            return error_response("AUTH-107", status_code=401, include_message=True)
        db_bytes = await run_in_threadpool(tenant_manager.db_size_bytes, principal.user_id)
        return JSONResponse(
            {
                **profile,
                "dbBytes": db_bytes,
                "passwordAuthenticated": principal.password_authenticated,
                "currentSessionId": principal.session_id,
                "currentMachineId": principal.machine_id,
            },
            headers={"Cache-Control": "no-store"},
        )

    return router
