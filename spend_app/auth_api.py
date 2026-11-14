"""Owner credential management. The middleware supplies the principal."""
import secrets
import hmac
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from spend_app.auth_model import (
    COOKIE, Principal, audit, create_session, digest, owner_id, revoke_session, set_session_cookie,
)
from spend_app.db import connect


def auth_router(settings):
    router = APIRouter()

    def relay(path, body=None):
        from spend_app.setup_wizard import read_setup_state, write_setup_state
        from spend_app.sync import HubMutationError, clear_hub_proxy_cache, fetch_from_configured_hub, post_to_configured_hub
        with connect(settings.database_path) as connection:
            state = read_setup_state(connection)
        if state.get("role") != "spoke" or not state["spokeConfig"].get("enabled"):
            return None
        try:
            result = (fetch_from_configured_hub(settings, path, ttl_seconds=0) if body is None
                      else post_to_configured_hub(settings, path, body, strict=True))
            if result is None:
                return JSONResponse({"error": "Sign in to your hub for All computers."}, status_code=401)
            if path in {"/api/auth/logout", "/api/auth/session/rotate"}:
                with connect(settings.database_path) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = read_setup_state(connection)
                    if (current["spokeConfig"].get("hubUrl") != state["spokeConfig"].get("hubUrl")
                            or current["spokeConfig"].get("ownerSession") != state["spokeConfig"].get("ownerSession")):
                        return JSONResponse({"error": "Owner access changed. Reload this computer's session state."}, status_code=409)
                    current["spokeConfig"]["ownerSession"] = result.get("sessionToken", "")
                    current["spokeConfig"]["ownerSessionExpiresAt"] = result.get("expiresAt")
                    write_setup_state(connection, current)
                clear_hub_proxy_cache()
                # Only the spoke's server holds the hub bearer.
                result = {"ok": True}
            return JSONResponse(result)
        except HubMutationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status_code)

    @router.get("/api/auth/context")
    def context():
        from spend_app.setup_wizard import _read_accounts, read_setup_state
        with connect(settings.database_path) as connection:
            state = read_setup_state(connection)
            has_owner = bool(_read_accounts(connection)["users"])
        return {"spoke": state.get("role") == "spoke" and bool(state["spokeConfig"].get("enabled")), "hasOwner": has_owner}

    @router.get("/auth")
    def page():
        from spend_app.auth_page import AUTH_HTML
        state = context()
        html = AUTH_HTML.replace('data-spoke="false"', 'data-spoke="' + str(state["spoke"]).lower() + '"')
        html = html.replace('data-owner="false"', 'data-owner="' + str(state["hasOwner"]).lower() + '"')
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @router.post("/api/auth/bootstrap")
    def bootstrap(request: Request):
        actor = request.state.principal
        if actor.via != "break-glass":
            return JSONResponse({"error": "Use the configured break-glass bearer."}, status_code=403)
        with connect(settings.database_path) as connection:
            data = create_session(connection, actor, "Recovery session", subject_id=owner_id(connection))
            audit(connection, actor, "login", "bootstrap")
        return set_session_cookie(JSONResponse({"ok": True, **data}), request, data)

    @router.get("/api/auth/sessions")
    def sessions(request: Request):
        forwarded = relay("/api/auth/sessions")
        if forwarded is not None:
            return forwarded
        if not request.state.principal.permits("credentials.manage"):
            return JSONResponse({"error": "Owner session required."}, status_code=403)
        with connect(settings.database_path) as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT id,role,created_at,last_used_at,expires_at,revoked_at,device_label FROM owner_sessions ORDER BY created_at DESC"
            )]
        return {"sessions": rows, "currentSessionId": request.state.principal.session_id}

    @router.post("/api/auth/sessions/revoke")
    async def revoke(request: Request):
        from spend_app.setup_wizard import _bounded_json
        body = await _bounded_json(request)
        forwarded = relay("/api/auth/sessions/revoke", body)
        if forwarded is not None:
            return forwarded
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
        forwarded = relay("/api/auth/logout", {})
        if forwarded is not None:
            return forwarded
        actor = request.state.principal
        with connect(settings.database_path) as connection:
            if actor.session_id:
                request.state.audit_recorded = revoke_session(connection, actor, actor.session_id)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/api")
        return response

    @router.post("/api/auth/session/rotate")
    def rotate_session(request: Request):
        forwarded = relay("/api/auth/session/rotate", {})
        if forwarded is not None:
            return forwarded
        actor = request.state.principal
        if not actor.session_id:
            return JSONResponse({"error": "Current owner session required."}, status_code=401)
        with connect(settings.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT device_label FROM owner_sessions WHERE id=? AND revoked_at IS NULL", (actor.session_id,)).fetchone()
            if not row or not revoke_session(connection, actor, actor.session_id):
                return JSONResponse({"error": "Session already rotated."}, status_code=401)
            data = create_session(connection, actor, row[0])
        return set_session_cookie(JSONResponse({"ok": True, **data}), request, data)

    @router.post("/api/auth/rotate")
    def rotate_credentials(request: Request):
        forwarded = relay("/api/auth/rotate", {})
        if forwarded is not None:
            return forwarded
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
            return JSONResponse({"error": "Current machine token required."}, status_code=401)
        if body.get("id", actor.id) != actor.id:
            return JSONResponse({"error": "Cannot rotate another machine."}, status_code=403)
        request.state.audit_target = actor.id
        result = rotate_machine_token(settings.database_path, request.headers["authorization"][7:].strip(), actor=actor)
        if not result:
            return JSONResponse({"error": "Invalid, expired or revoked machine token."}, status_code=401)
        request.state.audit_recorded = True
        result.pop("machines", None)
        return result

    return router
