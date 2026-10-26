from __future__ import annotations

import hashlib
import os
import time
from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime
from importlib.resources import as_file, files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.middleware.gzip import GZipMiddleware

from spend_app import __version__
from spend_app.build import build_id


ASSET_IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
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
    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
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
    """Reload the HUD module so a design switch shows up on the next request."""
    import importlib

    import spend_app.hud as hud

    importlib.reload(hud)
    return hud.render_hud(dashboard=dashboard_enabled())


# With the dashboard off, "/" belongs to the HUD: inside the desktop app it
# hides the main window (also on desktop builds that still open "/?app=1" at
# launch); in a browser it opens the HUD.
HUD_LAUNCHER_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>BURNRATE</title>
<script>
(async () => {
  const tauri = window.__TAURI_INTERNALS__;
  if (tauri && typeof tauri.invoke === "function") {
    try { await tauri.invoke("hide_main"); return; } catch {}
  }
  location.replace("/hud");
})();
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
    pricing = PricingEngine.load(settings.pricing_path)
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
    from spend_app.db import connect

    def source_guidance():
        presence = {}
        with connect(settings.database_path) as connection:
            return source_reports(connection, current_now(), presence, settings=settings)
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
            if scheduler:
                scheduler.shutdown(wait=False)
            resource_stack.close()

    app = FastAPI(title="BURNRATE", version=__version__, lifespan=lifespan)
    app.state.history_demand = history_demand
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

    # Explicit application boundary (Release A A13): when BURNRATE_ACCESS_TOKEN
    # is configured, every /api read and write requires it as a bearer token.
    # The default remains loopback-only with no token; a request claiming a
    # trusted Host is never authentication by itself.
    _access_token = os.getenv("BURNRATE_ACCESS_TOKEN", "").strip()

    @app.middleware("http")
    async def access_boundary(request, call_next):
        from spend_app.connection_api import access_allowed
        if not access_allowed(request, _access_token):
            return JSONResponse({"error": "Authentication required for the API"}, status_code=401)
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer br_m_"):
            from spend_app.sync import authenticate_machine_token

            authenticate_machine_token(settings.database_path, auth[7:].strip())
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def value_error_handler(_request, exc: ValueError):
        return JSONResponse(status_code=400, content={"error": str(exc)})

    def _window_or_400(window: str) -> str:
        if window not in SPEND_WINDOWS:
            raise HTTPException(status_code=400, detail="unsupported spend window")
        return canonicalize_window(window)

    @app.get("/api/spend/summary")
    def spend_summary(
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
            return {**spend_hud_usage(machine), **spend_hud_live(machine)}
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
        return aggregate_summary_cached(
            database_path=settings.database_path,
            pricing=pricing,
            window_key=resolved,
            tool=tool,
            timezone=settings.timezone,
            cache_threshold=settings.cache_hit_threshold,
            cadence_seconds=settings.local_ingest_interval_seconds,
            now=slot_now(),
            machine=machine,
        )

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
        return aggregate_entity(
            database_path=settings.database_path,
            pricing=pricing,
            kind=kind,
            key=key,
            window_key=resolved,
            timezone=settings.timezone,
            cache_threshold=settings.cache_hit_threshold,
            now=slot_now(),
            machine=machine,
        )

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

    @app.get("/api/spend/hud")
    def spend_hud_live(machine: str = Query(default="all")) -> dict:
        from spend_app.hud_data import live_snapshot
        from spend_app.sync import fetch_from_configured_hub

        # The HUD polls every few seconds: serve the last Hub copy at once
        # and renew it in the background rather than wait on the network.
        hub = fetch_from_configured_hub(settings, "/api/spend/hud", {"machine": machine},
                                        stale_seconds=HUD_HUB_STALE_SECONDS)
        if hub is not None:
            return _stamp_hub_age(hub, now=current_now())
        return live_snapshot(settings, now=current_now(), machine=machine)

    @app.get("/api/spend/hud/usage")
    def spend_hud_usage(machine: str = Query(default="all")) -> dict:
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
        return recorded_usage(settings, pricing, now=current_now(),
                              machine=local_usage_machine(settings, machine))

    @app.get("/api/spend/share/week")
    def spend_share_week(machine: str = Query(default="all")) -> dict:
        from spend_app.hud_data import local_usage_machine, week_card
        from spend_app.sync import fetch_from_configured_hub

        hub = fetch_from_configured_hub(settings, "/api/spend/share/week", {"machine": machine})
        if hub is not None and "usd" in hub:
            return hub
        return week_card(settings, pricing, now=current_now(), machine=local_usage_machine(settings, machine))

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

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

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
        # A demo in a browser must not move or resize the real desktop HUD.
        if demo_mode():
            return {"ok": False, "reason": "demo"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        action = str((body or {}).get("action") or "open").strip().lower()
        repo_root = Path(__file__).resolve().parents[1]
        data_root = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "BURNRATE"
        candidates = [
            repo_root / "src-tauri" / "target" / "release" / "burnrate-desktop.exe",
            data_root / "burnrate-desktop.exe",
            repo_root / "src-tauri" / "target" / "debug" / "burnrate-desktop.exe",
        ]
        exe_path = next((p for p in candidates if p.is_file()), None)
        if sys.platform == "win32" and action in {"pin", "unpin", "open", "show", "hide", "resize", "drag"}:
            pinned = action != "unpin"
            try:
                import ctypes
                from ctypes import wintypes

                user32 = ctypes.windll.user32
                dwmapi = ctypes.windll.dwmapi
                user32.GetWindowTextLengthW.argtypes = [ctypes.c_ssize_t]
                user32.GetWindowTextLengthW.restype = ctypes.c_int
                user32.GetWindowTextW.argtypes = [ctypes.c_ssize_t, wintypes.LPWSTR, ctypes.c_int]
                user32.GetWindowTextW.restype = ctypes.c_int
                user32.GetClassNameW.argtypes = [ctypes.c_ssize_t, wintypes.LPWSTR, ctypes.c_int]
                user32.GetClassNameW.restype = ctypes.c_int
                user32.GetWindowLongW.argtypes = [ctypes.c_ssize_t, ctypes.c_int]
                user32.GetWindowLongW.restype = ctypes.c_long
                user32.SetWindowLongW.argtypes = [ctypes.c_ssize_t, ctypes.c_int, ctypes.c_long]
                user32.SetWindowLongW.restype = ctypes.c_long
                user32.ShowWindow.argtypes = [ctypes.c_ssize_t, ctypes.c_int]
                user32.ShowWindow.restype = wintypes.BOOL
                user32.PostMessageW.argtypes = [ctypes.c_ssize_t, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]
                user32.SetWindowPos.argtypes = [
                    ctypes.c_ssize_t,
                    ctypes.c_ssize_t,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    wintypes.UINT,
                ]
                user32.SetWindowPos.restype = wintypes.BOOL
                user32.GetDpiForWindow.argtypes = [ctypes.c_ssize_t]
                user32.GetDpiForWindow.restype = wintypes.UINT
                WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_ssize_t, ctypes.c_ssize_t)
                found_hwnds = []

                def _cb(hwnd, _lparam):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        title = buf.value or ""
                        if "Sidecar HUD" in title:
                            cls_buf = ctypes.create_unicode_buffer(128)
                            user32.GetClassNameW(hwnd, cls_buf, 128)
                            if (cls_buf.value or "") == "Tauri Window":
                                found_hwnds.append(hwnd)
                    return True

                user32.EnumWindows(WNDENUMPROC(_cb), 0)
                if found_hwnds:
                    for hwnd in found_hwnds:
                        user32.SetWindowLongW(hwnd, -16, ctypes.c_long(0x96000000 - 0x100000000))
                        pref = ctypes.c_int(2)
                        dwmapi.DwmSetWindowAttribute(ctypes.c_ssize_t(hwnd), 33, ctypes.byref(pref), 4)
                        if action == "hide":
                            user32.ShowWindow(hwnd, 0)
                        elif action == "drag":
                            import threading, time
                            def _drag_loop(target_hwnd):
                                pt_start = wintypes.POINT()
                                rc_start = wintypes.RECT()
                                if not user32.GetCursorPos(ctypes.byref(pt_start)) or not user32.GetWindowRect(target_hwnd, ctypes.byref(rc_start)):
                                    return
                                lx, ly = rc_start.left, rc_start.top
                                while (user32.GetAsyncKeyState(0x01) & 0x8000) != 0:
                                    pt_cur = wintypes.POINT()
                                    if user32.GetCursorPos(ctypes.byref(pt_cur)):
                                        nx = rc_start.left + (pt_cur.x - pt_start.x)
                                        ny = rc_start.top + (pt_cur.y - pt_start.y)
                                        if nx != lx or ny != ly:
                                            lx, ly = nx, ny
                                            user32.SetWindowPos(target_hwnd, 0, nx, ny, 0, 0, 0x0001 | 0x0004 | 0x0010)
                                    time.sleep(0.004)
                            threading.Thread(target=_drag_loop, args=(hwnd,), daemon=True).start()
                        elif action == "resize":
                            w = int(float((body or {}).get("width") or 440))
                            h = int(float((body or {}).get("height") or 580))
                            dpi = user32.GetDpiForWindow(hwnd) or 96
                            pw = max(240, int(round(w * dpi / 96.0)))
                            ph = max(140, int(round(h * dpi / 96.0)))
                            user32.SetWindowPos(hwnd, 0, 0, 0, pw, ph, 0x0002 | 0x0004 | 0x0010 | 0x0020)
                        else:
                            if action in {"open", "show"}:
                                user32.ShowWindow(hwnd, 5)
                            ins = -1 if pinned else -2
                            user32.SetWindowPos(hwnd, ins, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0020 | 0x0040)
                    return {"ok": True, "pinned": pinned, "nativeFound": True, "count": len(found_hwnds)}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        if exe_path is not None and action in {"open", "show", "pin"}:
            try:
                chk = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq burnrate-desktop.exe", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    creationflags=0x08000000 if sys.platform == "win32" else 0,
                )
                if "burnrate-desktop.exe" in (chk.stdout or "").lower():
                    return {"ok": True, "pinned": True, "alreadyRunning": True}
                si = None
                if sys.platform == "win32":
                    si = subprocess.STARTUPINFO()
                    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    si.wShowWindow = 5
                subprocess.Popen(
                    [str(exe_path)],
                    cwd=str(repo_root),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    startupinfo=si,
                )
                return {"ok": True, "pinned": True, "launchedNative": True, "exe": str(exe_path)}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        return {"ok": False, "reason": "native_not_available"}

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
        ico_path = Path(__file__).resolve().parents[1] / "src-tauri" / "icons" / "icon.ico"
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


app = create_app()
