"""Settings-only protected discovery, verification and connection mutations."""
import hmac
import json
import os
import sqlite3
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from spend_app.connections import Conflict, Service
from spend_app.connection_paths import LocationError


_MACHINE_READ_PATHS = {
    "/api/spend/usage",
    "/api/spend/summary",
    "/api/spend/entity",
}

_PUBLIC_ACCOUNT_PATHS = {
    "/api/account/register",
    "/api/account/login",
    "/api/pairing/redeem",
}

_TEST_HOSTS = {"testserver", "test"}


def central_server() -> bool:
    """True on a self-hosted hub (the deployment that needs authentication)."""
    return os.getenv("BURNRATE_CENTRAL_SERVER", "").strip().lower() in {"1", "true", "yes"}


def test_hosts_allowed() -> bool:
    """Test hostnames are accepted only while a test suite is running."""
    if os.getenv("PYTEST_CURRENT_TEST"):
        return True
    return os.getenv("BURNRATE_TEST_HOSTS", "").strip().lower() in {"1", "true", "yes"}


def allowed_hosts() -> set[str]:
    hosts = {"127.0.0.1", "localhost", "::1"}
    hosts.update(
        s.strip().lower()
        for s in os.getenv("BURNRATE_ALLOWED_HOSTS", "").split(",")
        if s.strip()
    )
    if test_hosts_allowed():
        hosts.update(_TEST_HOSTS)
    return hosts


def host_allowed(host: str) -> bool:
    """Exact Host allowlist, shared by reads, writes and the cookie path."""
    try:
        parsed = urlsplit("//" + (host or ""))
    except ValueError:
        return False
    return bool(
        parsed.hostname in allowed_hosts()
        and not parsed.username
        and not parsed.password
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    )


def _bearer_matches(request, token: str) -> bool:
    auth = request.headers.get("authorization", "")
    if not token or not auth.startswith("Bearer "):
        return False
    return hmac.compare_digest(auth[7:].strip(), token)


def access_allowed(
    request,
    token,
    *,
    central: bool | None = None,
    machine_id: str | None = None,
    principal=None,
):
    """Permission boundary after credential validation. Unknown roles deny."""
    from spend_app.auth_model import Principal
    if principal is None:
        principal = getattr(getattr(request, "state", None), "principal", None)
    if principal is None:
        # Standalone routers can supply their explicit legacy bearer. The app
        # always supplies the validated principal, including unauthenticated.
        principal = (Principal("machine", machine_id) if machine_id else
                     Principal("owner", "break-glass", "owner", via="break-glass")
                     if isinstance(token, str) and _bearer_matches(request, token) else Principal())
    if request.url.path in _PUBLIC_ACCOUNT_PATHS:
        return True
    if not request.url.path.startswith("/api/"):
        return True
    if central is None:
        central = central_server()
    method = getattr(request, "method", "GET")
    if principal.kind == "machine":
        if request.url.path in {"/api/sync/push", "/api/auth/machine/rotate"}:
            return method == "POST"
        if request.url.path == "/api/machines":
            # The route allows a machine to rename or revoke only itself.
            return method in {"GET", "HEAD"} or method == "POST"
        if method in {"GET", "HEAD"} and request.url.path in _MACHINE_READ_PATHS:
            return request.query_params.getlist("machine") == [principal.id]
        return False
    if principal.kind == "owner":
        permission = "hub.read" if method in {"GET", "HEAD"} else "admin.write"
        return principal.permits(permission)
    if not token:
        return not central
    return False


def csrf_mutation_allowed(request, *, bearer_ok: bool = False) -> bool:
    """Reject cross-origin CSRF and DNS-rebinding mutations across all /api/* routes."""
    if getattr(request, "method", "GET") in {"GET", "HEAD", "OPTIONS"} or not request.url.path.startswith("/api/"):
        return True
    if request.url.path in _PUBLIC_ACCOUNT_PATHS:
        return True
    if bearer_ok:
        return True
    host = request.headers.get("host", "")
    if not host_allowed(host):
        return False
    allowed_origins = {s.strip() for s in os.getenv("BURNRATE_ALLOWED_ORIGINS", "").split(",") if s.strip()}
    try:
        fetch_site = request.headers.get("sec-fetch-site", "").strip().lower()
        if fetch_site in {"cross-site", "same-site"}:
            return False
        origin_text = request.headers.get("origin", "").strip()
        if not origin_text:
            return True
        if origin_text.lower() == "null":
            return False
        origin = urlsplit(origin_text)
        return (
            origin.scheme in {"http", "https"}
            and (origin.netloc == host or origin_text in allowed_origins)
            and not any((origin.username, origin.password, origin.path, origin.query, origin.fragment))
        )
    except ValueError:
        return False


def trusted(request, mutation=True):
    host = request.headers.get("host", "")
    if not host_allowed(host):
        return False
    origins = {s.strip() for s in os.getenv("BURNRATE_ALLOWED_ORIGINS", "").split(",") if s.strip()}
    try:
        origin_text = request.headers.get("origin", "")
        if not origin_text and not mutation:
            return request.headers.get("sec-fetch-site", "same-origin") in {"same-origin", "none"}
        origin = urlsplit(origin_text)
        return (origin.scheme in {"http", "https"} and origin.netloc == host and
                (origin.scheme == request.url.scheme or origin_text in origins) and
                not any((origin.username, origin.password, origin.path, origin.query, origin.fragment)))
    except ValueError:
        return False


async def payload(request):
    if not trusted(request) or request.headers.get("x-burnrate-request") != "1":
        raise PermissionError("Request rejected: use the configured BURNRATE origin.")
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise LocationError("Use application/json.")
    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > 16384:
            raise LocationError("Request too large.")
        chunks.extend(chunk)
    body = json.loads(chunks, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if not isinstance(body, dict):
        raise LocationError("Expected a JSON object.")
    return body


def connection_router(settings):
    router = APIRouter()
    service = Service(settings)

    @router.get("/api/connections")
    def status(request: Request):
        if not trusted(request, False):
            return JSONResponse({"error": "Use the configured BURNRATE origin."}, status_code=403)
        return JSONResponse(service.list(), headers={"Cache-Control": "no-store"})

    async def action(request, method):
        try:
            body = await payload(request)
            # Run bounded filesystem/vault calls outside the event loop using
            # FastAPI's existing worker pool, not a connection-specific worker.
            from starlette.concurrency import run_in_threadpool
            result = await run_in_threadpool(method, body)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=403)
        except Conflict as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except LocationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        except (ValueError, TypeError, KeyError):
            return JSONResponse({"error": "Invalid connection request."}, status_code=422)
        except (sqlite3.Error, OSError):
            return JSONResponse({"error": "Could not save. Retry with the same request identifier."}, status_code=503)

    @router.post("/api/connections/discover")
    async def discover(request: Request):
        return await action(request, lambda body: service.discover())

    @router.post("/api/connections/verify")
    async def verify(request: Request):
        return await action(request, service.verify)

    @router.post("/api/connections")
    async def mutate(request: Request):
        return await action(request, service.mutate)

    return router
