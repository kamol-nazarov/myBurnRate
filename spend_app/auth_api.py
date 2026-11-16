"""Owner credential management. The middleware supplies the principal."""
import secrets
import hmac
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from spend_app.auth_model import (
    COOKIE, LOCAL_COOKIE, Principal, audit, authenticate_local_session, clear_local_cookie,
    create_session, digest, owner_id, revoke_local_session, revoke_session, set_session_cookie,
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
            cookie_out = {} if path == "/api/auth/session/rotate" else None
            result = (fetch_from_configured_hub(settings, path, ttl_seconds=0) if body is None
                      else post_to_configured_hub(settings, path, body, strict=True, cookie_out=cookie_out))
            if result is None:
                return JSONResponse({"error": "Sign in to your hub for All computers."}, status_code=401)
            if path in {"/api/auth/logout", "/api/auth/session/rotate"}:
                new_session = cookie_out.get("session", "") if cookie_out is not None else ""
                if path == "/api/auth/session/rotate" and not new_session:
                    return JSONResponse({"error": "The hub returned an invalid session response."}, status_code=502)
                with connect(settings.database_path) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = read_setup_state(connection)
                    if (current["spokeConfig"].get("hubUrl") != state["spokeConfig"].get("hubUrl")
                            or current["spokeConfig"].get("ownerSession") != state["spokeConfig"].get("ownerSession")):
                        return JSONResponse({"error": "Owner access changed. Reload this computer's session state."}, status_code=409)
                    current["spokeConfig"]["ownerSession"] = new_session
                    current["spokeConfig"]["ownerSessionExpiresAt"] = result.get("expiresAt")
                    write_setup_state(connection, current)
                clear_hub_proxy_cache()
                # Only the spoke's server holds the hub bearer.
                result = {"ok": True}
            return JSONResponse(result)
        except HubMutationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=exc.status_code)

    def spoke_enabled() -> bool:
        from spend_app.setup_wizard import read_setup_state
        with connect(settings.database_path) as connection:
            state = read_setup_state(connection)
        return state.get("role") == "spoke" and bool(state["spokeConfig"].get("enabled"))

    def local_sid(request: Request) -> str | None:
        return authenticate_local_session(
            settings.database_path, request.cookies.get(LOCAL_COOKIE, "")
        )

    def local_guard(request: Request, *, mutation: bool = False):
        """On a spoke, hub credential management needs a local sign-in.

        The spoke's own API is loopback-anonymous by design; without this gate
        an unauthenticated local caller could relay through the stored hub
        owner session. Hubs keep using the normal principal checks.
        """
        if not spoke_enabled():
            return None
        if not local_sid(request):
            return JSONResponse(
                {"error": "Sign in on this computer before managing hub sessions."},
                status_code=401,
            )
        if mutation:
            from spend_app.connection_api import trusted
            if not trusted(request):
                return JSONResponse(
                    {"error": "Request rejected: use this computer's BURNRATE origin."},
                    status_code=403,
                )
        return None

    @router.get("/api/auth/context")
    def context(request: Request):
        from spend_app.setup_wizard import _read_accounts, read_setup_state
        with connect(settings.database_path) as connection:
            state = read_setup_state(connection)
            has_owner = bool(_read_accounts(connection)["users"])
        spoke = state.get("role") == "spoke" and bool(state["spokeConfig"].get("enabled"))
        return {"spoke": spoke, "hasOwner": has_owner,
                "localSession": bool(spoke and local_sid(request))}

    @router.get("/auth")
    def page(request: Request):
        from spend_app.auth_page import AUTH_HTML
        state = context(request)
        html = AUTH_HTML.replace('data-spoke="false"', 'data-spoke="' + str(state["spoke"]).lower() + '"')
        html = html.replace('data-owner="false"', 'data-owner="' + str(state["hasOwner"]).lower() + '"')
        html = html.replace('data-local="false"', 'data-local="' + str(state["localSession"]).lower() + '"')
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

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
        guard = local_guard(request)
        if guard:
            return guard
        forwarded = relay("/api/auth/sessions")
        if forwarded is not None:
            return forwarded
        if spoke_enabled():
            return JSONResponse({"error": "Sign in to your hub for owner access."}, status_code=401)
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
        guard = local_guard(request, mutation=True)
        if guard:
            return guard
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
        guard = local_guard(request, mutation=True)
        if guard:
            return guard
        forwarded = relay("/api/auth/logout", {})
        if forwarded is not None:
            sid = local_sid(request)
            if sid:
                with connect(settings.database_path) as connection:
                    revoke_local_session(connection, sid)
            clear_local_cookie(forwarded)
            return forwarded
        actor = request.state.principal
        with connect(settings.database_path) as connection:
            if actor.session_id:
                request.state.audit_recorded = revoke_session(connection, actor, actor.session_id)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @router.post("/api/auth/session/rotate")
    def rotate_session(request: Request):
        guard = local_guard(request, mutation=True)
        if guard:
            return guard
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
        return set_session_cookie(JSONResponse(
            {"ok": True, "sessionId": data["sessionId"], "expiresAt": data["expiresAt"]},
            headers={"Cache-Control": "no-store"},
        ), request, data)

    @router.post("/api/auth/rotate")
    def rotate_credentials(request: Request):
        # The break-glass credential is hub authority; a paired computer must
        # never relay it. Rotate it on the hub's own /auth page.
        if spoke_enabled():
            return JSONResponse(
                {"error": "Rotate the break-glass credential on the hub, not from a paired computer."},
                status_code=403,
            )
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
        guard = local_guard(request)
        if guard:
            return guard
        forwarded = relay("/api/auth/audit")
        if forwarded is not None:
            return forwarded
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
