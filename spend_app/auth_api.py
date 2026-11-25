"""Owner credential management. The middleware supplies the principal."""
import secrets
import hmac
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from spend_app.auth_model import (
    COOKIE, LEGACY_COOKIE, Principal, audit, create_session, digest, owner_id, parse,
    revoke_session, session_is_live, set_session_cookie,
)
from spend_app.db import connect
from spend_app.error_codes import error_response


def auth_router(settings):
    router = APIRouter()

    def spoke_enabled() -> bool:
        from spend_app.setup_wizard import read_setup_state
        with connect(settings.database_path) as connection:
            state = read_setup_state(connection)
        return state.get("role") == "spoke" and bool(state["spokeConfig"].get("enabled"))

    @router.get("/api/auth/context")
    def context(request: Request):
        from spend_app.setup_wizard import _read_accounts, read_setup_state
        with connect(settings.database_path) as connection:
            state = read_setup_state(connection)
            has_owner = bool(_read_accounts(connection)["users"])
        spoke = state.get("role") == "spoke" and bool(state["spokeConfig"].get("enabled"))
        return {"spoke": spoke, "hasOwner": has_owner, "localSession": False}

    @router.post("/api/auth/bootstrap")
    def bootstrap(request: Request):
        actor = request.state.principal
        if actor.via != "break-glass":
            return JSONResponse({"error": "Use the configured break-glass bearer."}, status_code=403)
        with connect(settings.database_path) as connection:
            data = create_session(connection, actor, "Recovery session", subject_id=owner_id(connection))
            audit(connection, actor, "login", "bootstrap")
        return set_session_cookie(JSONResponse(
            {"ok": True, "sessionId": data["sessionId"], "expiresAt": data["expiresAt"]},
            headers={"Cache-Control": "no-store"},
        ), request, data)

    @router.get("/api/auth/sessions")
    def sessions(request: Request):
        if spoke_enabled():
            return error_response("AUTH-107", status_code=401)
        if not request.state.principal.permits("credentials.manage"):
            return JSONResponse({"error": "Owner session required."}, status_code=403)
        with connect(settings.database_path) as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT id,role,created_at,last_used_at,expires_at,revoked_at,device_label,machine_id,auth_time "
                "FROM owner_sessions ORDER BY created_at DESC"
            )]
        for row in rows:
            row["machine_binding"] = "bound" if row["machine_id"] else "not tied to a computer"
        return {"sessions": rows, "currentSessionId": request.state.principal.session_id}

    @router.post("/api/auth/sessions/revoke")
    async def revoke(request: Request):
        from spend_app.setup_wizard import _bounded_json
        body = await _bounded_json(request)
        if spoke_enabled():
            return error_response("AUTH-107", status_code=401)
        actor = request.state.principal
        if not actor.permits("credentials.manage"):
            return JSONResponse({"error": "Owner session required."}, status_code=403)
        with connect(settings.database_path) as connection:
            sid = str(body.get("id") or "")
            changed = revoke_session(connection, actor, sid)
            request.state.audit_recorded = changed
        if not changed:
            return JSONResponse({"error": "Session not found or already revoked."}, status_code=404)
        return {"ok": True}

    @router.post("/api/auth/logout")
    def logout(request: Request):
        if spoke_enabled():
            return error_response("AUTH-107", status_code=401)
        actor = request.state.principal
        with connect(settings.database_path) as connection:
            if actor.session_id:
                request.state.audit_recorded = revoke_session(connection, actor, actor.session_id)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True)
        response.delete_cookie(LEGACY_COOKIE, path="/", secure=request.url.scheme == "https", httponly=True)
        return response

    @router.post("/api/auth/session/rotate")
    def rotate_session(request: Request):
        if spoke_enabled():
            return error_response("AUTH-107", status_code=401)
        actor = request.state.principal
        if not actor.session_id:
            return JSONResponse({"error": "Current owner session required."}, status_code=401)
        with connect(settings.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM owner_sessions WHERE id=?", (actor.session_id,)).fetchone()
            if not session_is_live(row) or not revoke_session(connection, actor, actor.session_id):
                return JSONResponse({"error": "Session already rotated."}, status_code=401)
            data = create_session(
                connection, actor, row["device_label"], auth_time=parse(row["auth_time"]),
                machine_id=row["machine_id"], password_authenticated=bool(row["password_authenticated"]),
            )
        return set_session_cookie(JSONResponse(
            {"ok": True, "sessionId": data["sessionId"], "expiresAt": data["expiresAt"]},
            headers={"Cache-Control": "no-store"},
        ), request, data)

    @router.post("/api/auth/rotate")
    def rotate_credentials(request: Request):
        # The break-glass credential is hub authority; a paired computer must
        # never relay it. Rotate it on the hub directly.
        if spoke_enabled():
            return JSONResponse(
                {"error": "Rotate the break-glass credential on the hub, not from a paired computer."},
                status_code=403,
            )
        actor = request.state.principal
        if not actor.permits("credentials.manage"):
            return JSONResponse({"error": "Owner credential required."}, status_code=403)
        token = "br_bg_" + secrets.token_urlsafe(32)
        with connect(settings.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if actor.via == "break-glass":
                row = connection.execute("SELECT value FROM app_meta WHERE key='auth.break_glass.v1'").fetchone()
                expected = row[0] if row else digest(os.getenv("BURNRATE_ACCESS_TOKEN", "").strip())
                presented = request.headers.get("authorization", "")[7:].strip()
                if not hmac.compare_digest(digest(presented), expected):
                    audit(connection, actor, "token.rotate", "break-glass", "failure")
                    request.state.audit_recorded = True
                    return JSONResponse({"error": "Recovery credential already rotated."}, status_code=401)
            connection.execute("INSERT INTO app_meta(key,value) VALUES('auth.break_glass.v1',?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (digest(token),))
            audit(connection, actor, "token.rotate", "break-glass")
            request.state.audit_recorded = True
        return {"ok": True, "token": token, "warning": "Save this credential securely; it is shown once. The old environment credential is invalid."}

    @router.get("/api/auth/audit")
    def audit_log(request: Request):
        if spoke_enabled():
            return error_response("AUTH-107", status_code=401)
        if not request.state.principal.permits("credentials.manage"):
            return JSONResponse({"error": "Owner session required."}, status_code=403)
        with connect(settings.database_path) as connection:
            rows = [dict(row) for row in connection.execute("SELECT * FROM admin_audit ORDER BY id DESC LIMIT 500")]
        return {"actions": rows}

    @router.post("/api/auth/machine/rotate")
    async def rotate_machine(request: Request):
        from spend_app.setup_wizard import _bounded_json
        from spend_app.sync import rotate_machine_token
        body = await _bounded_json(request)
        actor = request.state.principal
        if actor.kind != "machine":
            return error_response("DEV-301", status_code=401)
        if body.get("id", actor.id) != actor.id:
            return error_response("AUTH-107", status_code=403)
        request.state.audit_target = actor.id
        result = rotate_machine_token(settings.database_path, request.headers["authorization"][7:].strip(), actor=actor)
        if not result:
            return error_response("DEV-301", status_code=401)
        request.state.audit_recorded = True
        result.pop("machines", None)
        return result

    return router
