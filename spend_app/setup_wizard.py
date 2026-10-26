"""Interactive Application Setup Wizard & Central Account Multi-Computer Orchestration."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from spend_app import aggregate
from spend_app.config import dashboard_enabled
from spend_app.db import connect, initialize, utc_now
from spend_app.diagnostics import detect_local_sources
from spend_app.plan_service import PlanConflict, PlanError, apply_mutation, effective_terms, list_plans
from spend_app.pricing import PricingEngine
from spend_app.setup_checks import collection_progress, consent_state, lane_rows, plan_issues, plan_suggestions, write_consent
from spend_app.subscriptions import add_subscription, materialize_subscription_days
from spend_app.sync import (
    STREAM_BROKER,
    _HUB_TEST_CLIENTS,
    authenticate_machine_token,
    clear_hub_proxy_cache,
    expire_hub_proxy_cache,
    list_machines,
    post_to_configured_hub,
    register_machine,
    register_or_reconnect_machine,
    rename_machine,
    sync_once,
)


SETUP_META_KEY = "app_setup.v1"
ACCOUNTS_META_KEY = "accounts.v1"
PAIRING_PREFIX = "brpair_"

# No built-in hub: a computer joins the hub its owner names (pairing code,
# saved account, or BURNRATE_CENTRAL_SERVER_URL).
DEFAULT_CENTRAL_SERVER_URLS: tuple[str, ...] = ()
# A hub that already has an account accepts no new ones (one owner, many
# computers); computers join with a pairing code. Opt out deliberately.
ALLOW_REGISTRATION_ENV = "BURNRATE_ALLOW_REGISTRATION"


def default_central_server_url() -> str:
    return os.getenv("BURNRATE_CENTRAL_SERVER_URL", "").strip().rstrip("/")


HARNESS_CATALOG = {
    "codex_local": {"name": "OpenAI Codex CLI", "tool": "codex", "desc": "Local ~/.codex/sessions transcripts"},
    "claude_local": {"name": "Claude Code", "tool": "claude-code", "desc": "Local ~/.claude/projects JSONL logs"},
    "opencode_local": {"name": "OpenCode", "tool": "opencode", "desc": "Local OpenCode SQLite session database"},
    "zcode_local": {"name": "ZCode", "tool": "zcode", "desc": "Local ZCode agent database"},
    "grok_local": {"name": "Grok CLI", "tool": "grok", "desc": "Local ~/.grok session logs"},
    "antigravity_local": {"name": "Antigravity", "tool": "antigravity", "desc": "Local Antigravity app state"},
    "traycer_local": {"name": "Traycer", "tool": "traycer", "desc": "Local Traycer host database"},
    "muse_local": {"name": "Muse Code", "tool": "muse", "desc": "Local Muse Code session logs"},
}

SUBSCRIPTION_PRESETS = (
    {"key": "claude_max_20x", "tool_key": "claude-code", "name": "Claude Max 20x", "amount_usd": 200.0, "cadence": "monthly", "badge": "Anthropic"},
    {"key": "claude_max", "tool_key": "claude-code", "name": "Claude Max 5x", "amount_usd": 100.0, "cadence": "monthly", "badge": "Anthropic"},
    {"key": "claude_pro", "tool_key": "claude-code", "name": "Claude Pro", "amount_usd": 20.0, "cadence": "monthly", "badge": "Anthropic"},
    {"key": "codex_pro", "tool_key": "codex", "name": "ChatGPT Pro", "amount_usd": 200.0, "cadence": "monthly", "badge": "OpenAI"},
    {"key": "codex_plus", "tool_key": "codex", "name": "ChatGPT Plus", "amount_usd": 20.0, "cadence": "monthly", "badge": "OpenAI"},
    {"key": "grok_heavy", "tool_key": "grok", "name": "SuperGrok Heavy", "amount_usd": 300.0, "cadence": "monthly", "badge": "xAI"},
    {"key": "grok_super", "tool_key": "grok", "name": "SuperGrok", "amount_usd": 30.0, "cadence": "monthly", "badge": "xAI"},
    {"key": "zai_coding", "tool_key": "opencode", "name": "Z.AI Coding Plan", "amount_usd": 10.0, "cadence": "monthly", "badge": "Z.AI"},
    {"key": "opencode_go", "tool_key": "opencode-go", "name": "OpenCode Go", "amount_usd": 10.0, "cadence": "monthly", "badge": "OpenCode"},
    {"key": "opencode_go_plus", "tool_key": "opencode-go", "name": "OpenCode Go Plus", "amount_usd": 40.0, "cadence": "monthly", "badge": "OpenCode"},
    {"key": "google_ai_ultra", "tool_key": "antigravity", "name": "Google AI Ultra", "amount_usd": 249.99, "cadence": "monthly", "badge": "Google"},
    {"key": "google_ai_pro", "tool_key": "antigravity", "name": "Google AI Pro", "amount_usd": 19.99, "cadence": "monthly", "badge": "Google"},
    {"key": "muse_power", "tool_key": "muse", "name": "Muse Code Power Usage", "amount_usd": 0.0, "cadence": "monthly", "badge": "Meta"},
)
MIN_PASSWORD_LENGTH = 8


def _default_computer_name() -> str:
    """This computer's host name, short enough to be a machine name.

    macOS host names often carry a ``.local`` suffix and can run past the
    64-character limit, which made finishing setup fail on those Macs.
    """
    try:
        host = socket.gethostname().strip().split(".", 1)[0].strip()[:64].strip()
        if host:
            return host
    except Exception:
        pass
    return "Primary Computer"


def default_setup_state() -> dict:
    return {
        "version": 1,
        "completed": False,
        "completedAt": None,
        # "Set up later": the desktop app stops opening the wizard on launch.
        "deferredAt": None,
        "role": "hub",
        "computerName": _default_computer_name(),
        "account": {
            "username": "",
            "serverUrl": default_central_server_url(),
            "machineId": "",
            "connectedAt": None,
        },
        "spokeConfig": {
            "hubUrl": "",
            "token": "",
            "enabled": False,
            "lastSyncAt": None,
            "lastSyncError": None,
            "eventsPushed": 0,
        },
        "hudConfig": {
            "autoOpen": True,
            "alwaysOnTop": True,
            "dock": "top-left",
        },
    }


def read_setup_state(connection) -> dict:
    base = default_setup_state()
    row = connection.execute(
        "SELECT value FROM app_meta WHERE key=?", (SETUP_META_KEY,)
    ).fetchone()
    if not row or not row[0]:
        return base
    try:
        parsed = json.loads(row[0])
        if not isinstance(parsed, dict):
            return base
        merged = {**base, **parsed}
        merged["account"] = {
            **base["account"],
            **(parsed.get("account") if isinstance(parsed.get("account"), dict) else {}),
        }
        merged["spokeConfig"] = {
            **base["spokeConfig"],
            **(parsed.get("spokeConfig") if isinstance(parsed.get("spokeConfig"), dict) else {}),
        }
        merged["hudConfig"] = {
            **base["hudConfig"],
            **(parsed.get("hudConfig") if isinstance(parsed.get("hudConfig"), dict) else {}),
        }
        return merged
    except ValueError:
        return base


def write_setup_state(connection, state: dict) -> dict:
    clean = {
        **default_setup_state(),
        **state,
    }
    connection.execute(
        "INSERT INTO app_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (SETUP_META_KEY, json.dumps(clean, separators=(",", ":"), sort_keys=True)),
    )
    return clean


def public_setup_state(state: dict) -> dict:
    spoke = dict(state.get("spokeConfig") or {})
    raw_token = str(spoke.get("token") or "")
    spoke["hasToken"] = bool(raw_token)
    spoke["tokenPreview"] = (
        f"{raw_token[:12]}…{raw_token[-4:]}" if len(raw_token) > 18 else ("configured" if raw_token else "")
    )
    spoke.pop("token", None)
    return {**state, "spokeConfig": spoke}


def _read_accounts(connection) -> dict:
    row = connection.execute(
        "SELECT value FROM app_meta WHERE key=?", (ACCOUNTS_META_KEY,)
    ).fetchone()
    if not row or not row[0]:
        return {"version": 1, "users": {}}
    try:
        data = json.loads(row[0])
        if isinstance(data, dict) and isinstance(data.get("users"), dict):
            return data
    except ValueError:
        pass
    return {"version": 1, "users": {}}


def _write_accounts(connection, data: dict) -> None:
    connection.execute(
        "INSERT INTO app_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (ACCOUNTS_META_KEY, json.dumps(data, separators=(",", ":"), sort_keys=True)),
    )


def _hash_password(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return salt.hex(), digest.hex()


def _verify_password(password: str, salt_hex: str, expected_hash_hex: str) -> bool:
    if not salt_hex or not expected_hash_hex:
        return False
    _, actual_hex = _hash_password(password, salt_hex)
    return hmac.compare_digest(actual_hex, expected_hash_hex)


def _apply_subscription_presets_and_custom(
    connection,
    timezone_name: str,
    preset_keys: list[str],
    custom_plans: list[dict],
) -> int:
    now_local = datetime.now(UTC).astimezone(ZoneInfo(timezone_name)).date()
    month_first = date(now_local.year, now_local.month, 1).isoformat()
    existing = effective_terms(connection)
    existing_pairs = {(row["tool_key"], row["name"].lower()) for row in existing}
    preset_map = {p["key"]: p for p in SUBSCRIPTION_PRESETS}
    added_plans = 0

    for key in preset_keys:
        preset = preset_map.get(key) or preset_map.get(key.replace("-", "_"))
        if not preset:
            continue
        pair = (preset["tool_key"], preset["name"].lower())
        if pair in existing_pairs:
            continue
        add_subscription(
            connection,
            tool_key=preset["tool_key"],
            name=preset["name"],
            amount_usd=float(preset["amount_usd"]),
            cadence=preset["cadence"],
            start_date=month_first,
            end_date=None,
        )
        existing_pairs.add(pair)
        added_plans += 1

    for item in custom_plans:
        if not isinstance(item, dict):
            continue
        t_key = str(item.get("tool_key") or "custom").strip()
        p_name = str(item.get("name") or "").strip()
        amt = float(item.get("amount_usd") or 0)
        cad = str(item.get("cadence") or "monthly").strip()
        if not p_name or amt <= 0:
            continue
        pair = (t_key, p_name.lower())
        if pair in existing_pairs:
            continue
        add_subscription(
            connection,
            tool_key=t_key,
            name=p_name,
            amount_usd=amt,
            cadence=cad,
            start_date=month_first,
            end_date=None,
        )
        existing_pairs.add(pair)
        added_plans += 1

    if added_plans > 0:
        materialize_subscription_days(
            connection,
            start=date.fromisoformat(month_first),
            end=now_local + timedelta(days=40),
        )
    return added_plans


def _sync_plans_into_local_db(connection, timezone_name: str, remote_plans: list[dict]) -> int:
    if not isinstance(remote_plans, list) or not remote_plans:
        return 0
    now_local = datetime.now(UTC).astimezone(ZoneInfo(timezone_name)).date()
    month_first = date(now_local.year, now_local.month, 1).isoformat()
    existing = effective_terms(connection)
    existing_pairs = {(row["tool_key"], row["name"].lower()) for row in existing}
    added = 0
    for item in remote_plans:
        if not isinstance(item, dict):
            continue
        t_key = str(item.get("tool_key") or "custom").strip()
        p_name = str(item.get("name") or "").strip()
        amt = float(item.get("amount_usd") or 0)
        cad = str(item.get("cadence") or "monthly").strip()
        start_d = str(item.get("start_date") or month_first).strip()
        if not p_name or amt <= 0:
            continue
        pair = (t_key, p_name.lower())
        if pair in existing_pairs:
            continue
        add_subscription(
            connection,
            tool_key=t_key,
            name=p_name,
            amount_usd=amt,
            cadence=cad,
            start_date=start_d,
            end_date=item.get("end_date"),
        )
        existing_pairs.add(pair)
        added += 1
    if added > 0:
        materialize_subscription_days(
            connection,
            start=date.fromisoformat(month_first),
            end=now_local + timedelta(days=40),
        )
    return added


def encode_pairing_code(
    *,
    hub_url: str,
    token: str,
    machine_id: str,
    machine_name: str,
    hub_name: str,
) -> str:
    payload = {
        "v": 1,
        "hubUrl": hub_url.strip().rstrip("/"),
        "token": token.strip(),
        "machineId": machine_id,
        "machineName": machine_name,
        "hubName": hub_name,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{PAIRING_PREFIX}{encoded}"


def decode_pairing_code(code: str) -> dict:
    clean = (code or "").strip()
    if not clean.startswith(PAIRING_PREFIX):
        raise ValueError("Pairing code must start with 'brpair_'.")
    b64 = clean[len(PAIRING_PREFIX) :]
    padding = "=" * ((4 - len(b64) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode((b64 + padding).encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("Invalid pairing code format.") from exc
    if not isinstance(data, dict) or not data.get("hubUrl") or not data.get("token"):
        raise ValueError("Pairing code is missing hubUrl or token.")
    return data


def ensure_tailscale_hub_proxy(port: int = 17331) -> bool:
    """Expose 127.0.0.1:<port> on the local Tailscale interface only (never public)."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    ts_bin = shutil.which("tailscale")
    if not ts_bin:
        return False
    try:
        proc = subprocess.run(
            [ts_bin, "serve", "--bg", "--tcp", str(port), f"tcp://127.0.0.1:{port}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


def detect_network_addresses(port: int = 17331) -> list[dict]:
    from spend_app.config import demo_mode

    if demo_mode():
        # A demo, often screenshotted, never shows this computer's real addresses.
        return [{"label": "Localhost (this computer)", "url": f"http://127.0.0.1:{port}", "kind": "local"}]
    urls: list[dict] = []
    seen: set[str] = set()
    ts_bin = shutil.which("tailscale")
    if ts_bin:
        try:
            proc = subprocess.run(
                [ts_bin, "serve", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                serve_data = json.loads(proc.stdout)
                web = serve_data.get("Web") or {}
                target_suffix = f":{port}"
                for host_port, cfg in web.items():
                    handlers = (cfg or {}).get("Handlers") or {}
                    for handler in handlers.values():
                        proxy = str((handler or {}).get("Proxy") or "")
                        if proxy.endswith(target_suffix) or f":{port}/" in proxy:
                            https_url = f"https://{host_port[:-4]}" if host_port.endswith(":443") else f"https://{host_port}"
                            if https_url not in seen:
                                seen.add(https_url)
                                urls.append(
                                    {
                                        "label": f"Tailscale Serve HTTPS ({https_url})",
                                        "url": https_url,
                                        "kind": "tailscale_https",
                                    }
                                )
        except Exception:
            pass
    try:
        host_ips = socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET)
        for item in host_ips:
            ip = str(item[4][0])
            if ip.startswith("127.") or ip in seen:
                continue
            seen.add(ip)
            kind = "Tailscale" if ip.startswith("100.") else "LAN"
            urls.append({"label": f"{kind} ({ip})", "url": f"http://{ip}:{port}", "kind": kind.lower()})
    except Exception:
        pass
    rank = {"tailscale_https": 0, "tailscale": 1, "lan": 2}
    urls.sort(key=lambda entry: (rank.get(entry["kind"], 3), entry["url"]))
    urls.append({"label": "Localhost (this computer)", "url": f"http://127.0.0.1:{port}", "kind": "local"})
    return urls


def maybe_run_configured_spoke_sync(settings, pricing: PricingEngine, *, client=None) -> dict | None:
    """Called by background scheduler or on-demand to push spoke telemetry when configured."""
    try:
        with connect(settings.database_path) as connection:
            state = read_setup_state(connection)
    except Exception:
        return None
    spoke = state.get("spokeConfig") or {}
    if state.get("role") != "spoke" or not spoke.get("enabled"):
        return None
    hub_url = str(spoke.get("hubUrl") or "").strip().rstrip("/")
    token = str(spoke.get("token") or "").strip()
    if not hub_url or not token:
        return None
    resolved_client = client or _HUB_TEST_CLIENTS.get(hub_url)
    try:
        result = sync_once(
            settings,
            pricing,
            hub_url=hub_url,
            token=token,
            machine_name=state.get("computerName") or _default_computer_name(),
            collect_local=False,
            client=resolved_client,
        )
        # A push does not make the last Hub copy wrong. Dropping it forces the
        # next HUD poll to wait on the Hub and then fall back to this computer.
        expire_hub_proxy_cache()
        with connect(settings.database_path) as connection:
            latest = read_setup_state(connection)
            latest["spokeConfig"]["lastSyncAt"] = utc_now()
            latest["spokeConfig"]["lastSyncError"] = None
            latest["spokeConfig"]["eventsPushed"] = int(
                latest["spokeConfig"].get("eventsPushed") or 0
            ) + int(result.get("eventsWritten") or 0)
            write_setup_state(connection, latest)
        return result
    except Exception as exc:
        with connect(settings.database_path) as connection:
            latest = read_setup_state(connection)
            latest["spokeConfig"]["lastSyncError"] = str(exc)[:240]
            write_setup_state(connection, latest)
        return {"ok": False, "error": str(exc)}


# Setup shows a tool as not read recently only after this long without a
# successful read; the first pass after a restart can take several minutes.
SETUP_STALE_AFTER_SECONDS = 15 * 60


def _build_harness_list(settings) -> list[dict]:
    """Each known tool: is it installed, and how did its latest collection go.

    Built from the presence check and the ingest log only, so it answers in
    milliseconds even while a collection pass is running (the dashboard's full
    source diagnostics take seconds).
    """
    from spend_app.diagnostics import source_path_hint
    from spend_app.setup_checks import _parse

    try:
        presence = detect_local_sources(Path.home())
    except Exception:
        presence = {}
    now = datetime.now(UTC)
    with connect(settings.database_path) as connection:
        latest = {
            row["source"]: row["status"]
            for row in connection.execute(
                "SELECT r.source, r.status FROM ingest_runs r "
                "JOIN (SELECT source, MAX(id) AS max_id FROM ingest_runs GROUP BY source) last ON r.id = last.max_id"
            )
        }
        successes = dict(connection.execute(
            "SELECT source, MAX(finished_at) FROM ingest_runs WHERE status IN ('success','partial') GROUP BY source"
        ).fetchall())
    result: list[dict] = []
    for source, meta in HARNESS_CATALOG.items():
        installed = bool(presence.get(source))
        last_ok = successes.get(source)
        read_at = _parse(last_ok)
        if latest.get(source) == "failed":
            state = "failed"
        elif latest.get(source) == "running":
            state = "reading"
        elif read_at is None:
            state = "detected_without_history" if installed else "not_detected"
        elif (now - read_at).total_seconds() > SETUP_STALE_AFTER_SECONDS:
            state = "stale"
        else:
            state = "healthy"
        result.append(
            {
                "source": source,
                "name": meta["name"],
                "tool": meta["tool"],
                "description": meta["desc"],
                "state": state,
                "detected": installed or read_at is not None,
                "path": source_path_hint(source) or "",
                "lastSuccess": last_ok,
            }
        )
    return result


def _call_central_server_account(
    server_url: str,
    endpoint_path: str,
    payload: dict,
) -> tuple[str, dict]:
    """Call `/api/account/register` or `/api/account/login` on the central server with fallback."""
    clean_primary = (server_url or "").strip().rstrip("/") or default_central_server_url()
    if not clean_primary:
        raise ValueError("Enter your hub's address, or join with a pairing code instead.")
    candidates = [clean_primary]
    for fallback in DEFAULT_CENTRAL_SERVER_URLS:
        if clean_primary in DEFAULT_CENTRAL_SERVER_URLS and fallback not in candidates:
            candidates.append(fallback)

    last_error = "Could not reach central account server."
    for base_url in candidates:
        test_client = _HUB_TEST_CLIENTS.get(base_url)
        try:
            if test_client is not None:
                res = test_client.post(endpoint_path, json=payload)
            else:
                import httpx

                with httpx.Client(timeout=8.0) as http:
                    res = http.post(f"{base_url}{endpoint_path}", json=payload)
            data = res.json() if res.content else {}
            if res.status_code >= 400:
                err_msg = data.get("error") if isinstance(data, dict) else None
                raise ValueError(err_msg or f"Server returned HTTP {res.status_code}")
            if not isinstance(data, dict) or not data.get("token"):
                raise ValueError("Invalid response from central account server.")
            return base_url, data
        except ValueError:
            raise
        except Exception as exc:
            last_error = f"Could not connect to central server ({base_url}): {exc}"
    raise ValueError(last_error)


def _setup_request_refused(request: Request) -> JSONResponse | None:
    """Setup changes come only from BURNRATE's own pages.

    The custom header cannot be sent by another site without a CORS preflight,
    which BURNRATE never grants, so a web page cannot repoint this computer at
    another hub or change its settings.
    """
    if request.headers.get("x-burnrate-request") == "1":
        return None
    return JSONResponse({"error": "Setup changes must come from the BURNRATE setup page."}, status_code=403)


def _api_connections(settings) -> dict:
    """Provider API connections (balances and admin usage) without any secret."""
    try:
        from spend_app.connections import Service

        listing = Service(settings).list()
    except Exception:
        return {"connections": [], "vaultAvailable": False}
    rows = [
        {key: row.get(key) for key in ("source", "name", "state", "detail", "enabled", "revision", "capabilities",
                                        "credentialHelp", "externallyManaged", "credentialConfigured", "lastImport")}
        for row in listing["connections"] if row.get("kind") == "api"
    ]
    return {"connections": rows, "vaultAvailable": bool(listing.get("vaultAvailable"))}


def setup_router(settings, pricing: PricingEngine, *, collect_now=None) -> APIRouter:
    router = APIRouter()

    @router.get("/setup")
    def setup_page():
        return HTMLResponse(render_setup_wizard(), headers={"Cache-Control": "no-cache"})

    @router.get("/connections")
    def connections_page():
        # Provider keys live in the wizard's "Plans & balances" step, so they
        # can be managed from the HUD even when the dashboard is off.
        return RedirectResponse("/setup?step=3", status_code=307)

    @router.get("/api/setup/state")
    def get_setup_state(request: Request):
        initialize(settings.database_path)
        port = request.url.port or 17331
        with connect(settings.database_path) as connection:
            state = read_setup_state(connection)
            machines = list_machines(connection)
            active_plans = effective_terms(connection)
            events_count = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
            agents_count = connection.execute(
                "SELECT COUNT(*) FROM agent_runs WHERE LOWER(state) IN ('live', 'running')"
            ).fetchone()[0]
        return JSONResponse(
            {
                "setup": public_setup_state(state),
                "centralServerUrl": default_central_server_url(),
                "os": sys.platform,
                "timezone": settings.timezone,
                "networkAddresses": detect_network_addresses(port),
                "machines": machines,
                "subscriptions": active_plans,
                "presets": list(SUBSCRIPTION_PRESETS),
                "stats": {
                    "eventsCount": int(events_count),
                    "liveAgentsCount": int(agents_count),
                },
                "consent": consent_state(),
                "apiConnections": _api_connections(settings),
                "dashboard": dashboard_enabled(),
                "minPasswordLength": MIN_PASSWORD_LENGTH,
            },
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/api/setup/check")
    def setup_check():
        """One row per plan card: is usage read, does the meter have a current reading, and the fix."""
        initialize(settings.database_path)
        found = lane_rows(settings, _build_harness_list(settings))
        with connect(settings.database_path) as connection:
            plans = plan_suggestions(connection, found["lanes"], settings.timezone)
        return JSONResponse(
            {**found, "consent": consent_state(), "plans": plans, "checkedAt": utc_now()},
            headers={"Cache-Control": "no-store"},
        )

    def plans_payload() -> tuple[dict | None, str]:
        """The plans setup should show: the Hub's for a spoke (plans live there), else this computer's."""
        from spend_app.sync import fetch_from_configured_hub

        hub = fetch_from_configured_hub(settings, "/api/subscriptions", ttl_seconds=20)
        if isinstance(hub, dict) and isinstance(hub.get("plans"), list):
            return hub, "hub"
        now_local = datetime.now(UTC).astimezone(ZoneInfo(settings.timezone)).date()
        with connect(settings.database_path) as connection:
            return list_plans(connection, now_local), "local"

    @router.get("/api/setup/plan-issues")
    def setup_plan_issues():
        initialize(settings.database_path)
        payload, where = plans_payload()
        return JSONResponse({"issues": plan_issues(payload), "where": where}, headers={"Cache-Control": "no-store"})

    @router.post("/api/setup/plans/fix")
    async def setup_fix_plan(request: Request):
        refused = _setup_request_refused(request)
        if refused:
            return refused
        import uuid as _uuid

        try:
            body = await request.json()
            plan_id, version, fix = body.get("planId"), body.get("version"), body.get("fix")
            if type(plan_id) is not int or type(version) is not int or fix not in {"end", "link"}:
                raise ValueError("Choose a plan and a fix.")
            payload, where = plans_payload()
            issue = next((item for item in plan_issues(payload) if item["planId"] == plan_id and item["fix"] == fix), None)
            if issue is None:
                raise ValueError("That plan no longer needs this fix. Reload setup.")
            today = datetime.now(UTC).astimezone(ZoneInfo(settings.timezone)).date().isoformat()
            request_id = lambda: "setup-" + _uuid.uuid4().hex  # noqa: E731
            if fix == "end":
                steps = [{"operation": "end", "request_id": request_id(), "plan_id": plan_id,
                          "expected_version": version, "end_date": today}]
            else:
                plan = next(item for item in payload["plans"] if item["id"] == plan_id)
                term = next(item for item in plan["terms"] if item.get("id") == issue["termId"])
                values = {key: term[key] for key in ("name", "amount_usd", "cadence", "start_date", "end_date")}
                values["tool_key"] = issue["toolKeyFix"]
                steps = [{"operation": "correct", "request_id": request_id(), "plan_id": plan_id, "expected_version": version,
                          "term_id": issue["termId"], "values": values, "preview": True}]

            def apply(step: dict) -> dict:
                if where == "hub":
                    from spend_app.sync import post_to_configured_hub

                    result = post_to_configured_hub(settings, "/api/account/subscriptions", {"action": "plan", **step})
                    if not isinstance(result, dict):
                        raise ValueError("Your hub did not accept the change. It may need the latest BURNRATE.")
                    return result
                with connect(settings.database_path) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    return apply_mutation(connection, step, datetime.now(UTC).astimezone(ZoneInfo(settings.timezone)).date())

            result = apply(steps[0])
            if fix == "link":
                # The button states the change; preview, then confirm it.
                token = result.get("preview_token")
                if not token:
                    raise ValueError("The plan could not be previewed for correction.")
                confirm = {**steps[0], "request_id": request_id(), "preview": False, "confirmed": True, "preview_token": token}
                result = apply(confirm)
            aggregate.reset_memo()
            return JSONResponse({"ok": True, "result": result}, headers={"Cache-Control": "no-store"})
        except (PlanConflict, PlanError, ValueError, KeyError, StopIteration) as exc:
            return JSONResponse({"error": str(exc) or "The plan could not be changed."}, status_code=422)

    @router.get("/api/setup/progress")
    def setup_progress():
        initialize(settings.database_path)
        return JSONResponse(collection_progress(settings), headers={"Cache-Control": "no-store"})

    @router.post("/api/setup/consent")
    async def setup_consent(request: Request):
        refused = _setup_request_refused(request)
        if refused:
            return refused
        try:
            body = await request.json()
            switches = body.get("switches") if isinstance(body, dict) else None
            if not isinstance(switches, dict) or not switches:
                raise ValueError("Choose at least one setting to change.")
            consent, warning = write_consent({str(key): bool(value) for key, value in switches.items()})
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        # Readers check these switches on every poll; make the next one due now.
        from spend_app.quotas import nudge_quota_lanes

        nudge_quota_lanes(settings.database_path)
        return JSONResponse({"ok": True, "consent": consent, "warning": warning}, headers={"Cache-Control": "no-store"})

    @router.get("/api/setup/notifications")
    def setup_notifications():
        from spend_app.notify import public_settings

        return JSONResponse(public_settings(settings.database_path), headers={"Cache-Control": "no-store"})

    @router.post("/api/setup/notifications")
    async def setup_save_notifications(request: Request):
        refused = _setup_request_refused(request)
        if refused:
            return refused
        from spend_app.notify import save_kinds

        try:
            body = await request.json()
            kinds = body.get("kinds") if isinstance(body, dict) else None
            if not isinstance(kinds, dict) or not kinds:
                raise ValueError("Choose a notification to change.")
            saved = save_kinds(settings.database_path, {str(key): bool(value) for key, value in kinds.items()})
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        return JSONResponse(saved, headers={"Cache-Control": "no-store"})

    @router.post("/api/setup/notifications/test")
    async def setup_test_notification(request: Request):
        refused = _setup_request_refused(request)
        if refused:
            return refused
        from spend_app.notify import show_toast
        from starlette.concurrency import run_in_threadpool

        try:
            await run_in_threadpool(show_toast, "BURNRATE notifications are on",
                                    "You'll hear from BURNRATE when a plan nears its limit or a meter stops updating.",
                                    tag="burnrate-test")
        except (OSError, ValueError) as exc:
            return JSONResponse({"error": f"Windows did not show it: {exc}"}, status_code=502)
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    @router.post("/api/setup/diagnostics")
    async def setup_diagnostics(request: Request):
        refused = _setup_request_refused(request)
        if refused:
            return refused
        from spend_app.support_report import build_report
        from starlette.concurrency import run_in_threadpool

        text = await run_in_threadpool(build_report, settings)
        return JSONResponse({"text": text}, headers={"Cache-Control": "no-store"})

    @router.post("/api/setup/defer")
    async def defer_setup(request: Request):
        refused = _setup_request_refused(request)
        if refused:
            return refused
        initialize(settings.database_path)
        with connect(settings.database_path) as connection:
            state = read_setup_state(connection)
            state["deferredAt"] = utc_now()
            saved = write_setup_state(connection, state)
        return JSONResponse({"ok": True, "setup": public_setup_state(saved)}, headers={"Cache-Control": "no-store"})

    @router.post("/api/account/register")
    async def central_account_register(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            username = str(body.get("username") or "").strip()
            password = str(body.get("password") or "")
            computer_name = str(body.get("computerName") or "").strip() or "Connected Computer"
            os_name = str(body.get("os") or "").strip()
            preset_keys = [str(k) for k in (body.get("presetKeys") or []) if k]
            custom_plans = body.get("customPlans") if isinstance(body.get("customPlans"), list) else []

            if len(username) < 2 or len(username) > 64:
                raise ValueError("Username must be between 2 and 64 characters.")
            if len(password) < MIN_PASSWORD_LENGTH:
                raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")

            user_key = username.lower()
            initialize(settings.database_path)
            with connect(settings.database_path) as connection:
                accounts = _read_accounts(connection)
                existing_user = accounts["users"].get(user_key)
                if isinstance(existing_user, dict):
                    if not _verify_password(
                        password,
                        str(existing_user.get("salt") or ""),
                        str(existing_user.get("passwordHash") or ""),
                    ):
                        return JSONResponse(
                            {"error": "Account already exists with a different password. Sign in instead."},
                            status_code=401,
                        )
                else:
                    allow = os.getenv(ALLOW_REGISTRATION_ENV, "").strip().lower() in {"1", "true", "yes"}
                    if accounts["users"] and not allow:
                        return JSONResponse(
                            {"error": "This hub already has an account, so it accepts no new ones. Sign in with that "
                                      "account, or join with a pairing code created on the hub."},
                            status_code=403,
                        )
                    salt_hex, hash_hex = _hash_password(password)
                    accounts["users"][user_key] = {
                        "username": username,
                        "salt": salt_hex,
                        "passwordHash": hash_hex,
                        "createdAt": utc_now(),
                    }
                    _write_accounts(connection, accounts)

                added_plans = _apply_subscription_presets_and_custom(
                    connection,
                    settings.timezone,
                    preset_keys,
                    custom_plans,
                )
                plans_out = effective_terms(connection)

            reg = register_or_reconnect_machine(
                settings.database_path,
                name=computer_name,
                os_name=os_name,
            )
            aggregate.reset_memo()
            return JSONResponse(
                {
                    "ok": True,
                    "account": {"username": username},
                    "addedPlans": added_plans,
                    "machine": reg["machine"],
                    "token": reg["token"],
                    "machines": reg["machines"],
                    "subscriptions": plans_out,
                },
                headers={"Cache-Control": "no-store"},
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    @router.post("/api/account/login")
    async def central_account_login(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            username = str(body.get("username") or "").strip()
            password = str(body.get("password") or "")
            computer_name = str(body.get("computerName") or "").strip() or "Connected Computer"
            os_name = str(body.get("os") or "").strip()
            preset_keys = [str(k) for k in (body.get("presetKeys") or []) if k]
            custom_plans = body.get("customPlans") if isinstance(body.get("customPlans"), list) else []

            if not username or not password:
                raise ValueError("Enter your account username and password.")

            user_key = username.lower()
            initialize(settings.database_path)
            with connect(settings.database_path) as connection:
                accounts = _read_accounts(connection)
                user_rec = accounts["users"].get(user_key)
                if not isinstance(user_rec, dict):
                    # A typo must not quietly create a second, empty account.
                    return JSONResponse(
                        {"error": f"There is no account named {username} on this server. Check the spelling, or choose Create account."},
                        status_code=401,
                    )
                else:
                    if not _verify_password(
                        password,
                        str(user_rec.get("salt") or ""),
                        str(user_rec.get("passwordHash") or ""),
                    ):
                        return JSONResponse(
                            {"error": "Invalid username or password."},
                            status_code=401,
                        )
                    display_username = str(user_rec.get("username") or username)

                if preset_keys or custom_plans:
                    _apply_subscription_presets_and_custom(
                        connection,
                        settings.timezone,
                        preset_keys,
                        custom_plans,
                    )
                plans_out = effective_terms(connection)

            reg = register_or_reconnect_machine(
                settings.database_path,
                name=computer_name,
                os_name=os_name,
            )
            aggregate.reset_memo()
            return JSONResponse(
                {
                    "ok": True,
                    "account": {"username": display_username},
                    "machine": reg["machine"],
                    "token": reg["token"],
                    "machines": reg["machines"],
                    "subscriptions": plans_out,
                },
                headers={"Cache-Control": "no-store"},
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    @router.post("/api/account/subscriptions")
    async def central_account_subscriptions(request: Request):
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        machine_id = authenticate_machine_token(settings.database_path, token)
        if not machine_id:
            return JSONResponse({"error": "Invalid or revoked machine token."}, status_code=401)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            now_local = datetime.now(UTC).astimezone(ZoneInfo(settings.timezone)).date()
            initialize(settings.database_path)
            with connect(settings.database_path) as connection:
                if "action" in body or "operation" in body:
                    connection.execute("BEGIN IMMEDIATE")
                    result = apply_mutation(connection, {key: value for key, value in body.items() if key != "action"}, now_local)
                    aggregate.reset_memo()
                    return JSONResponse(result, headers={"Cache-Control": "no-store"})
                preset_keys = [str(k) for k in (body.get("presetKeys") or []) if k]
                custom_plans = body.get("customPlans") if isinstance(body.get("customPlans"), list) else []
                _apply_subscription_presets_and_custom(
                    connection,
                    settings.timezone,
                    preset_keys,
                    custom_plans,
                )
                result = {**list_plans(connection, now_local), "timezone": settings.timezone}
            aggregate.reset_memo()
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except PlanConflict as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except (PlanError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    @router.post("/api/setup/account")
    async def setup_connect_account(request: Request):
        refused = _setup_request_refused(request)
        if refused:
            return refused
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            mode = str(body.get("mode") or "login").strip().lower()
            if mode not in {"login", "register"}:
                raise ValueError("Mode must be 'login' or 'register'.")
            username = str(body.get("username") or "").strip()
            password = str(body.get("password") or "")
            if not username or not password:
                raise ValueError("Enter your account username and password.")
            if mode == "register" and len(password) < MIN_PASSWORD_LENGTH:
                raise ValueError(f"Choose a password of at least {MIN_PASSWORD_LENGTH} characters.")
            computer_name = str(body.get("computerName") or "").strip() or _default_computer_name()
            server_url = str(body.get("serverUrl") or "").strip() or default_central_server_url()
            preset_keys = [str(k) for k in (body.get("presetKeys") or []) if k]
            custom_plans = list(body.get("customPlans")) if isinstance(body.get("customPlans"), list) else []
            test_sync = bool(body.get("testSync", True))

            initialize(settings.database_path)
            with connect(settings.database_path) as connection:
                for local_plan in effective_terms(connection):
                    custom_plans.append(
                        {
                            "tool_key": local_plan["tool_key"],
                            "name": local_plan["name"],
                            "amount_usd": float(local_plan["amount_usd"]),
                            "cadence": local_plan["cadence"],
                        }
                    )

            endpoint = "/api/account/register" if mode == "register" else "/api/account/login"
            remote_payload = {
                "username": username,
                "password": password,
                "computerName": computer_name,
                "os": sys.platform,
                "presetKeys": preset_keys,
                "customPlans": custom_plans,
            }
            working_url, remote_data = _call_central_server_account(server_url, endpoint, remote_payload)
            token = str(remote_data["token"]).strip()
            machine = remote_data.get("machine") or {}
            remote_subs = remote_data.get("subscriptions") if isinstance(remote_data.get("subscriptions"), list) else []
            account_info = remote_data.get("account") if isinstance(remote_data.get("account"), dict) else {"username": username}

            sync_result = None
            if test_sync:
                sync_client = _HUB_TEST_CLIENTS.get(working_url)
                sync_result = sync_once(
                    settings,
                    pricing,
                    hub_url=working_url,
                    token=token,
                    machine_name=computer_name,
                    collect_local=False,
                    client=sync_client,
                )

            initialize(settings.database_path)
            with connect(settings.database_path) as connection:
                _sync_plans_into_local_db(connection, settings.timezone, remote_subs)
                state = read_setup_state(connection)
                state["role"] = "spoke"
                state["computerName"] = computer_name
                state["account"] = {
                    "username": str(account_info.get("username") or username),
                    "serverUrl": working_url,
                    "machineId": str(machine.get("id") or ""),
                    "connectedAt": utc_now(),
                }
                state["spokeConfig"] = {
                    "hubUrl": working_url,
                    "token": token,
                    "enabled": True,
                    "lastSyncAt": utc_now() if sync_result else state["spokeConfig"].get("lastSyncAt"),
                    "lastSyncError": None,
                    "eventsPushed": int((sync_result or {}).get("eventsWritten") or 0),
                }
                saved = write_setup_state(connection, state)
                local_subs = effective_terms(connection)

            rename_machine(settings.database_path, machine_id="local", name=computer_name)
            clear_hub_proxy_cache()
            aggregate.reset_memo()
            return JSONResponse(
                {
                    "ok": True,
                    "account": state["account"],
                    "setup": public_setup_state(saved),
                    "subscriptions": remote_subs or local_subs,
                    "machines": remote_data.get("machines") or [],
                    "syncResult": sync_result,
                },
                headers={"Cache-Control": "no-store"},
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    @router.post("/api/setup/scan")
    async def scan_and_import(request: Request):
        refused = _setup_request_refused(request)
        if refused:
            return refused
        from starlette.concurrency import run_in_threadpool

        def _do_scan() -> dict:
            initialize(settings.database_path)
            from spend_app.quotas import nudge_quota_lanes

            started = utc_now()
            if collect_now is not None:
                collect_now()
            nudge_quota_lanes(settings.database_path)
            # Results come from /api/setup/check and /api/setup/progress; this
            # only starts the work, so it stays fast during a busy first pass.
            return {"ok": True, "started": started, "harnesses": _build_harness_list(settings)}

        res = await run_in_threadpool(_do_scan)
        return JSONResponse(res, headers={"Cache-Control": "no-store"})

    @router.post("/api/setup/pair-hub")
    async def create_hub_pairing(request: Request):
        refused = _setup_request_refused(request)
        if refused:
            return refused
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            remote_name = str(body.get("name") or body.get("remoteName") or "Remote Computer").strip()
            hub_url = str(
                body.get("hubUrl")
                or body.get("hubHost")
                or f"{request.url.scheme}://{request.headers.get('host', '127.0.0.1:17331')}"
            ).strip().rstrip("/")
            ensure_tailscale_hub_proxy(int(getattr(settings, "port", 17331)))
            reg = register_machine(
                settings.database_path,
                name=remote_name,
                os_name=str(body.get("os") or ""),
            )
            with connect(settings.database_path) as connection:
                state = read_setup_state(connection)
            pairing_code = encode_pairing_code(
                hub_url=hub_url,
                token=reg["token"],
                machine_id=reg["machine"]["id"],
                machine_name=reg["machine"]["name"],
                hub_name=state.get("computerName") or _default_computer_name(),
            )
            reg["ok"] = True
            reg["hubUrl"] = hub_url
            reg["pairingCode"] = pairing_code
            reg["syncCommand"] = f"python -m spend_app sync --hub {hub_url} --token {reg['token']}"
            return JSONResponse(reg, headers={"Cache-Control": "no-store"})
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    @router.post("/api/setup/connect-spoke")
    async def connect_spoke(request: Request):
        refused = _setup_request_refused(request)
        if refused:
            return refused
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            pairing_code = str(body.get("pairingCode") or "").strip()
            if pairing_code:
                decoded = decode_pairing_code(pairing_code)
                hub_url = str(decoded["hubUrl"]).strip().rstrip("/")
                token = str(decoded["token"]).strip()
                default_name = str(decoded.get("machineName") or "")
            else:
                hub_url = str(body.get("hubUrl") or "").strip().rstrip("/")
                token = str(body.get("token") or "").strip()
                default_name = ""
            if not hub_url or not token:
                raise ValueError("Provide a Pairing Code (brpair_...) or both Hub URL and Token.")
            computer_name = (
                str(body.get("computerName") or "").strip()
                or default_name
                or _default_computer_name()
            )
            test_sync = bool(body.get("testSync", True))
            sync_result = None
            if test_sync:
                sync_result = sync_once(
                    settings,
                    pricing,
                    hub_url=hub_url,
                    token=token,
                    machine_name=computer_name,
                    collect_local=False,
                    client=_HUB_TEST_CLIENTS.get(hub_url),
                )
            initialize(settings.database_path)
            with connect(settings.database_path) as connection:
                state = read_setup_state(connection)
                state["role"] = "spoke"
                state["computerName"] = computer_name
                state["spokeConfig"] = {
                    "hubUrl": hub_url,
                    "token": token,
                    "enabled": True,
                    "lastSyncAt": utc_now() if sync_result else state["spokeConfig"].get("lastSyncAt"),
                    "lastSyncError": None,
                    "eventsPushed": int((sync_result or {}).get("eventsWritten") or 0),
                }
                saved = write_setup_state(connection, state)
            rename_machine(settings.database_path, machine_id="local", name=computer_name)
            clear_hub_proxy_cache()
            return JSONResponse(
                {
                    "ok": True,
                    "setup": public_setup_state(saved),
                    "syncResult": sync_result,
                },
                headers={"Cache-Control": "no-store"},
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    @router.post("/api/setup/complete")
    async def complete_setup(request: Request):
        refused = _setup_request_refused(request)
        if refused:
            return refused
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            initialize(settings.database_path)
            computer_name = str(body.get("computerName") or "").strip() or _default_computer_name()
            requested_role = body.get("role")
            preset_keys = [str(k) for k in (body.get("presetKeys") or []) if k]
            custom_plans = body.get("customPlans") if isinstance(body.get("customPlans"), list) else []
            hud_cfg = body.get("hudConfig") if isinstance(body.get("hudConfig"), dict) else {}

            with connect(settings.database_path) as connection:
                state = read_setup_state(connection)
                if requested_role is not None:
                    role = str(requested_role).strip().lower()
                elif state.get("spokeConfig", {}).get("enabled"):
                    role = "spoke"
                else:
                    role = str(state.get("role") or "hub").strip().lower()
                if role not in {"hub", "spoke", "standalone"}:
                    raise ValueError("Role must be 'hub', 'spoke', or 'standalone'.")

                added_plans = _apply_subscription_presets_and_custom(
                    connection,
                    settings.timezone,
                    preset_keys,
                    custom_plans,
                )

                state["completed"] = True
                state["completedAt"] = utc_now()
                state["deferredAt"] = None
                state["computerName"] = computer_name
                state["role"] = role
                if role != "spoke":
                    state["spokeConfig"]["enabled"] = False
                state["hudConfig"] = {
                    **state["hudConfig"],
                    "autoOpen": bool(hud_cfg.get("autoOpen", state["hudConfig"].get("autoOpen", True))),
                    "alwaysOnTop": bool(hud_cfg.get("alwaysOnTop", state["hudConfig"].get("alwaysOnTop", True))),
                    "dock": str(hud_cfg.get("dock") or state["hudConfig"].get("dock") or "top-left"),
                }
                saved = write_setup_state(connection, state)
                plans_out = effective_terms(connection)

            if role == "spoke" and (preset_keys or custom_plans):
                post_to_configured_hub(
                    settings,
                    "/api/account/subscriptions",
                    {"presetKeys": preset_keys, "customPlans": custom_plans},
                )
            if role == "spoke":
                maybe_run_configured_spoke_sync(settings, pricing)

            rename_machine(settings.database_path, machine_id="local", name=computer_name)
            clear_hub_proxy_cache()
            aggregate.reset_memo()
            STREAM_BROKER.publish({"type": "setup_complete", "role": role})
            return JSONResponse(
                {
                    "ok": True,
                    "addedPlans": added_plans,
                    "setup": public_setup_state(saved),
                    "subscriptions": plans_out,
                },
                headers={"Cache-Control": "no-store"},
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    return router


SETUP_WIZARD_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="color-scheme" content="dark light">
    <meta name="theme-color" content="#ebe9e4">
    <title>BURNRATE · Application Setup Wizard</title>
    <link rel="icon" type="image/svg+xml" href="/favicon.svg">
    <script>
      (() => {
        let theme = null;
        try { theme = localStorage.getItem("burnrate:theme"); } catch {}
        if (theme !== "dark" && theme !== "light") theme = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
        document.documentElement.dataset.theme = theme;
        document.querySelector('meta[name="theme-color"]').content = theme === "dark" ? "#08080a" : "#ebe9e4";
      })();
    </script>
    <style>
      /* "Instrument" tokens, copied from frontend_src/spend.css. Light is the default; dark overrides below. */
      :root {
        color-scheme: light;
        --bg: #ebe9e4;
        --surface: #f8f7f4;
        --raised: #ffffff;
        --soft: #f1efeb;
        --sunken: #e5e2dc;
        --text: #141311;
        --secondary: #4a4741;
        --muted: #5f5b55;
        --dim: #74706a;
        --line: rgba(20, 18, 15, 0.1);
        --border: rgba(20, 18, 15, 0.12);
        --border-strong: rgba(20, 18, 15, 0.22);
        --accent: #cf4509;
        --accent-soft: rgba(207, 69, 9, 0.09);
        --on-accent: #ffffff;
        --green: #13804c;
        --danger: #c42b24;
        --good-bg: rgba(19, 128, 76, 0.07);
        --good-border: rgba(19, 128, 76, 0.28);
        --danger-bg: rgba(196, 43, 36, 0.07);
        --danger-border: rgba(196, 43, 36, 0.3);
        --meter: #141311;
        --track: rgba(20, 18, 15, 0.13);
        --lift: 0 1px 2px rgba(20, 18, 15, 0.05);
        --fd: Bahnschrift, "DIN Alternate", "DIN 2014", Barlow, "Segoe UI", system-ui, sans-serif;
        --font-sans: "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, sans-serif;
        --font-mono: "Cascadia Mono", ui-monospace, Consolas, monospace;
      }
      [data-theme=dark] {
        color-scheme: dark;
        --bg: #08080a;
        --surface: #111114;
        --raised: #19191d;
        --soft: #151518;
        --sunken: #0c0c0e;
        --text: #f2f0eb;
        --secondary: #b8b4ac;
        --muted: #9a968f;
        --dim: #807d76;
        --line: rgba(255, 255, 255, 0.075);
        --border: rgba(255, 255, 255, 0.1);
        --border-strong: rgba(255, 255, 255, 0.2);
        --accent: #ff6b22;
        --accent-soft: rgba(255, 107, 34, 0.12);
        --on-accent: #0a0a0b;
        --green: #3dd68c;
        --danger: #ff5247;
        --good-bg: rgba(61, 214, 140, 0.07);
        --good-border: rgba(61, 214, 140, 0.26);
        --danger-bg: rgba(255, 82, 71, 0.08);
        --danger-border: rgba(255, 82, 71, 0.32);
        --meter: #f2f0eb;
        --track: rgba(255, 255, 255, 0.1);
        --lift: none;
      }
      * { box-sizing: border-box; margin: 0; padding: 0; }
      [hidden] { display: none !important; }
      body {
        min-height: 100vh;
        background: var(--bg);
        color: var(--text);
        font: 14px/1.5 var(--font-sans);
        font-variant-numeric: tabular-nums;
        -webkit-font-smoothing: antialiased;
      }
      button, input, select { font: inherit; color: inherit; }
      code { font-family: var(--font-mono); font-size: 0.9em; }
      :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
      ::selection { background: var(--accent); color: var(--on-accent); }

      .eyebrow {
        font: 600 10px/1.2 var(--fd);
        letter-spacing: 0.12em;
        text-transform: uppercase;
        font-stretch: 87.5%;
        color: var(--muted);
      }
      .eyebrow .n { color: var(--accent); letter-spacing: 0.04em; margin-right: 8px; }

      .wizard-shell {
        min-height: 100vh;
        max-width: 1120px;
        margin: 0 auto;
        padding: 22px 32px 40px;
        display: grid;
        grid-template-columns: 232px minmax(0, 1fr);
        grid-template-areas: "top top" "rail card";
        column-gap: 48px;
        row-gap: 28px;
        align-content: start;
      }
      .wizard-topbar {
        grid-area: top;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        min-height: 44px;
        padding-bottom: 18px;
        border-bottom: 1px solid var(--line);
      }
      .wizard-brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
      .wizard-brand img { flex: 0 0 32px; width: 32px; height: 32px; border-radius: 8px; display: block; }
      .wizard-wordmark {
        font: 700 18px/0.88 var(--fd);
        letter-spacing: 0.035em;
        white-space: nowrap;
      }
      .wizard-wordmark strong { color: var(--accent); font: inherit; }
      .wizard-sub {
        padding-left: 12px;
        border-left: 1px solid var(--line);
        white-space: nowrap;
      }
      .wizard-actions { display: flex; align-items: center; gap: 8px; }

      .btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: 8px;
        min-height: 44px;
        padding: 0 18px;
        border: 1px solid var(--border-strong);
        border-radius: 8px;
        background: transparent;
        color: var(--text);
        font: 600 14px/1 var(--font-sans);
        text-decoration: none;
        white-space: nowrap;
        cursor: pointer;
        transition: border-color 0.15s, background-color 0.15s, opacity 0.15s;
      }
      .btn:hover:not(:disabled) { border-color: var(--text); }
      .btn:disabled { opacity: 0.45; cursor: not-allowed; }
      .btn-sm { min-height: 36px; padding: 0 14px; font-size: 13px; }
      .btn-primary { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
      .btn-primary:hover:not(:disabled) { background: var(--accent); border-color: var(--accent); opacity: 0.9; }
      .icon-btn {
        width: 44px;
        height: 44px;
        display: inline-grid;
        place-items: center;
        border: 1px solid var(--border-strong);
        border-radius: 8px;
        background: transparent;
        color: var(--text);
        cursor: pointer;
      }
      .icon-btn:hover { border-color: var(--text); }
      .icon-btn svg { width: 16px; height: 16px; display: block; }

      .wizard-rail {
        grid-area: rail;
        align-self: start;
        position: sticky;
        top: 22px;
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .wizard-stepper { list-style: none; display: flex; flex-direction: column; }
      .wizard-stepper li {
        position: relative;
        display: grid;
        grid-template-columns: 30px minmax(0, 1fr);
        column-gap: 10px;
        align-items: start;
        padding: 18px 0 20px;
        cursor: pointer;
      }
      .wizard-stepper li::before {
        content: "";
        position: absolute;
        top: 0;
        left: 0;
        right: 0;
        height: 2px;
        background: var(--track);
      }
      .wizard-stepper li.active::before,
      .wizard-stepper li.done::before { background: var(--meter); }
      .wizard-stepper .num {
        font: 600 13px/1 var(--fd);
        letter-spacing: 0.04em;
        padding-top: 3px;
        color: var(--muted);
      }
      .wizard-stepper .step-text { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
      .wizard-stepper strong {
        font: 600 16px/1.25 var(--fd);
        letter-spacing: 0.01em;
        color: var(--secondary);
      }
      .wizard-stepper small { font-size: 12px; line-height: 1.4; color: var(--muted); }
      .wizard-stepper li.active .num { color: var(--accent); }
      .wizard-stepper li.active strong,
      .wizard-stepper li.done strong { color: var(--text); }
      .wizard-stepper li.done .num { color: var(--green); }

      .wizard-card {
        grid-area: card;
        min-width: 0;
        display: flex;
        flex-direction: column;
        gap: 26px;
        padding: 32px 36px 28px;
        background: var(--surface);
        border: 1px solid var(--line);
        border-radius: 12px;
        box-shadow: var(--lift);
      }
      .wiz-pane { display: flex; flex-direction: column; gap: 20px; min-width: 0; }
      .step-head { display: flex; flex-direction: column; gap: 10px; }
      .step-head h1 {
        font: 600 30px/1.12 var(--fd);
        letter-spacing: -0.005em;
        color: var(--text);
      }
      .lede { color: var(--secondary); font-size: 14px; line-height: 1.55; max-width: 62ch; }

      .auth-tabs {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 4px;
        padding: 4px;
        background: var(--sunken);
        border: 1px solid var(--line);
        border-radius: 10px;
      }
      .auth-tab {
        appearance: none;
        min-height: 40px;
        padding: 0 14px;
        border: 0;
        border-radius: 7px;
        background: transparent;
        color: var(--muted);
        font: 600 14px/1 var(--fd);
        letter-spacing: 0.02em;
        cursor: pointer;
        transition: background-color 0.15s, color 0.15s;
      }
      .auth-tab:hover:not(.active) { color: var(--text); }
      .auth-tab.active { background: var(--text); color: var(--bg); }

      .form-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 16px;
      }
      .field-group { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
      .field-group > label {
        font: 600 10px/1.2 var(--fd);
        letter-spacing: 0.12em;
        text-transform: uppercase;
        color: var(--muted);
      }
      .hint { font-size: 12px; line-height: 1.4; color: var(--muted); }
      .hint code { color: var(--secondary); }
      .text-input {
        width: 100%;
        min-height: 42px;
        padding: 0 12px;
        background: var(--sunken);
        border: 1px solid var(--border-strong);
        border-radius: 8px;
        color: var(--text);
        font-size: 14px;
        outline: none;
        transition: border-color 0.15s;
      }
      .text-input::placeholder { color: var(--muted); }
      .text-input:focus { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
      .text-input.mono { font-family: var(--font-mono); font-size: 13px; }

      .sub-box {
        display: flex;
        flex-direction: column;
        gap: 14px;
        min-width: 0;
        padding: 18px;
        background: var(--soft);
        border: 1px solid var(--line);
        border-radius: 12px;
      }
      .sub-box h3 { font: 600 17px/1.25 var(--fd); color: var(--text); }
      .plans-line { font-size: 12.5px; line-height: 1.5; color: var(--secondary); }
      .inline-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
      .inline-row > .text-input { flex: 1 1 160px; }
      .inline-row > .select-sm { flex: 0 1 170px; }
      .inline-row > .amount-input { flex: 0 1 120px; }

      .presets-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 10px;
      }
      .preset-card {
        appearance: none;
        display: flex;
        align-items: center;
        gap: 12px;
        width: 100%;
        min-height: 56px;
        padding: 12px 14px;
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 10px;
        color: var(--text);
        text-align: left;
        cursor: pointer;
        transition: border-color 0.15s;
      }
      .preset-card:hover { border-color: var(--border-strong); }
      .preset-card.selected { border-color: var(--text); }
      .preset-card::before {
        content: "\\2713";
        display: grid;
        place-items: center;
        flex: 0 0 18px;
        width: 18px;
        height: 18px;
        border: 1px solid var(--border-strong);
        border-radius: 5px;
        color: transparent;
        font: 700 11px/1 var(--font-sans);
      }
      .preset-card.selected::before { background: var(--text); border-color: var(--text); color: var(--bg); }
      .preset-card > div { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
      .preset-card strong { font: 600 15px/1.25 var(--fd); color: var(--text); }
      .preset-card small { font-size: 12px; color: var(--muted); }
      .preset-price { font: 500 16px/1 var(--fd); color: var(--text); white-space: nowrap; }

      .harness-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 10px;
      }
      .harness-item {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        min-width: 0;
        padding: 14px 16px;
        background: var(--soft);
        border: 1px solid var(--line);
        border-radius: 10px;
      }
      .harness-item.detected { border-color: var(--good-border); }
      .harness-item > div { flex: 1; min-width: 0; }
      .logo-tile {
        flex: none;
        display: grid;
        place-items: center;
        width: 34px;
        height: 34px;
        padding: 8px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: var(--surface);
        color: var(--text);
      }
      .logo-tile svg { display: block; width: 100%; height: 100%; }
      .harness-item strong { display: block; font: 600 15px/1.3 var(--fd); color: var(--text); overflow-wrap: anywhere; }
      .harness-item small {
        display: block;
        margin-top: 2px;
        font: 12px/1.4 var(--font-mono);
        color: var(--muted);
        overflow-wrap: anywhere;
      }
      .status-chip {
        flex: 0 0 auto;
        display: inline-flex;
        align-items: center;
        min-height: 24px;
        padding: 0 9px;
        border: 1px solid var(--line);
        border-radius: 6px;
        font: 600 10px/1 var(--fd);
        letter-spacing: 0.12em;
        text-transform: uppercase;
        color: var(--muted);
        white-space: nowrap;
      }
      .status-chip.good { color: var(--green); border-color: var(--good-border); background: var(--good-bg); }

      .status-banner {
        padding: 12px 14px;
        background: var(--soft);
        border: 1px solid var(--line);
        border-radius: 8px;
        font-size: 13px;
        line-height: 1.45;
        color: var(--secondary);
      }
      .status-banner.good { color: var(--green); border-color: var(--good-border); background: var(--good-bg); }
      .status-banner.err { color: var(--danger); border-color: var(--danger-border); background: var(--danger-bg); }
      .scan-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
      .scan-row > .status-banner { flex: 1 1 260px; }

      .check-row { display: flex; align-items: flex-start; gap: 12px; cursor: pointer; }
      .check-row input[type=checkbox] {
        flex: 0 0 18px;
        width: 18px;
        height: 18px;
        margin-top: 2px;
        accent-color: var(--text);
        cursor: pointer;
      }
      .check-row strong { display: block; font: 600 15px/1.3 var(--fd); color: var(--text); }
      .check-row small { display: block; margin-top: 2px; font-size: 12.5px; color: var(--muted); }

      details.advanced-server { color: var(--muted); }
      details.advanced-server summary {
        cursor: pointer;
        user-select: none;
        font: 600 10px/1.2 var(--fd);
        letter-spacing: 0.12em;
        text-transform: uppercase;
      }
      details.advanced-server summary code { text-transform: none; letter-spacing: 0; font-size: 12px; }
      details.advanced-server .field-group { margin-top: 12px; }

      .wizard-footer {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding-top: 22px;
        border-top: 1px solid var(--line);
      }
      .wizard-footer .btn-primary { min-width: 220px; }

      .role-grid {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 10px;
      }
      .role-card {
        appearance: none;
        display: flex;
        flex-direction: column;
        gap: 6px;
        min-height: 118px;
        padding: 16px;
        background: var(--soft);
        border: 1px solid var(--border);
        border-radius: 10px;
        color: var(--text);
        text-align: left;
        cursor: pointer;
        transition: border-color 0.15s;
      }
      .role-card:hover { border-color: var(--border-strong); }
      .role-card[aria-checked=true] { border-color: var(--text); box-shadow: inset 0 0 0 1px var(--text); }
      .role-card strong { font: 600 16px/1.25 var(--fd); }
      .role-card small { font-size: 12.5px; line-height: 1.45; color: var(--muted); }
      .current-box { flex-direction: row; align-items: center; justify-content: space-between; gap: 14px; flex-wrap: wrap; }
      .current-box p { flex: 1 1 280px; }
      .code-box {
        padding: 12px;
        background: var(--sunken);
        border: 1px solid var(--border-strong);
        border-radius: 8px;
        font: 12px/1.5 var(--font-mono);
        overflow-wrap: anywhere;
        user-select: all;
      }
      textarea.text-input { min-height: 72px; padding: 10px 12px; resize: vertical; font: 12px/1.5 var(--font-mono); }
      .link-btn {
        appearance: none;
        align-self: flex-start;
        padding: 0;
        border: 0;
        background: none;
        color: var(--accent);
        font: 600 13px/1.4 var(--font-sans);
        cursor: pointer;
      }
      .link-btn:hover { text-decoration: underline; }
      .scan-row .btn { flex: 0 0 auto; }
      .lane-list { display: flex; flex-direction: column; gap: 10px; }
      .lane-row {
        display: grid;
        grid-template-columns: 34px minmax(0, 1fr) auto;
        column-gap: 14px;
        row-gap: 8px;
        align-items: center;
        padding: 14px 16px;
        background: var(--soft);
        border: 1px solid var(--line);
        border-radius: 10px;
      }
      .lane-row.attention { border-color: var(--danger-border); }
      .lane-main { min-width: 0; display: flex; flex-direction: column; gap: 1px; }
      .lane-main strong { font: 600 15px/1.3 var(--fd); color: var(--text); }
      .lane-main small { font-size: 12px; color: var(--muted); }
      .lane-facts { grid-column: 2 / -1; display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: var(--secondary); min-width: 0; }
      .fact { display: flex; gap: 10px; align-items: baseline; min-width: 0; }
      .fact-key { flex: 0 0 52px; font: 600 10px/1.7 var(--fd); letter-spacing: 0.12em; text-transform: uppercase; color: var(--muted); }
      .fact span:last-child { min-width: 0; overflow-wrap: anywhere; }
      .lane-detail { font-size: 12.5px; color: var(--muted); }
      .lane-fix { font-size: 13px; color: var(--text); }
      .lane-form { grid-column: 2 / -1; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
      .lane-form .text-input { flex: 1 1 220px; }
      .status-chip.warn { color: var(--accent); border-color: var(--accent); background: var(--accent-soft); }
      .status-chip.bad { color: var(--danger); border-color: var(--danger-border); background: var(--danger-bg); }
      .switch-row {
        grid-column: 2 / -1;
        display: flex;
        align-items: flex-start;
        gap: 10px;
        margin-top: 2px;
        padding-top: 10px;
        border-top: 1px solid var(--line);
        cursor: pointer;
      }
      .switch-row input { flex: 0 0 18px; width: 18px; height: 18px; margin-top: 2px; accent-color: var(--text); cursor: pointer; }
      .switch-row strong { display: block; font: 600 14px/1.3 var(--fd); color: var(--text); }
      .switch-row small { display: block; margin-top: 2px; font-size: 12.5px; line-height: 1.45; color: var(--muted); }
      .section-head { display: flex; flex-direction: column; gap: 4px; }
      .section-head h3 { font: 600 17px/1.25 var(--fd); color: var(--text); }
      .plan-list { display: flex; flex-direction: column; gap: 8px; }
      .plan-row {
        display: grid;
        grid-template-columns: 18px minmax(0, 1fr) 130px;
        gap: 12px;
        align-items: center;
        padding: 10px 14px;
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 10px;
      }
      .plan-row.tracked { grid-template-columns: minmax(0, 1fr) auto; }
      .plan-row input[type=checkbox] { width: 18px; height: 18px; accent-color: var(--text); cursor: pointer; }
      .plan-row strong { display: block; font: 600 14px/1.3 var(--fd); }
      .plan-row small { display: block; font-size: 12px; color: var(--muted); }
      .plan-row .amount-input { min-height: 36px; }
      .plan-row .price { font: 500 15px/1 var(--fd); white-space: nowrap; }
      details.more { color: var(--secondary); }
      details.more > summary { cursor: pointer; font: 600 13px/1.4 var(--font-sans); color: var(--secondary); }
      details.more > *:not(summary) { margin-top: 10px; }
      .preview-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
      .preview-card {
        display: flex;
        flex-direction: column;
        gap: 8px;
        padding: 12px 14px;
        background: var(--soft);
        border: 1px solid var(--line);
        border-radius: 10px;
      }
      .preview-head { display: flex; align-items: center; gap: 10px; }
      .preview-head .logo-tile { width: 28px; height: 28px; padding: 6px; }
      .preview-head strong { font: 600 15px/1.2 var(--fd); }
      .preview-row { display: grid; grid-template-columns: 74px minmax(0, 1fr) 58px; gap: 10px; align-items: center; font-size: 12px; color: var(--muted); }
      .preview-row b { font: 500 14px/1 var(--fd); color: var(--text); text-align: right; }
      .bar { height: 6px; border-radius: 3px; background: var(--track); overflow: hidden; }
      .bar i { display: block; height: 100%; width: 0; background: var(--meter); }
      .bar.payg { background: repeating-linear-gradient(135deg, var(--track) 0 2px, transparent 2px 6px); }
      .issue-list { list-style: none; display: flex; flex-direction: column; gap: 8px; }
      .issue-list li { padding: 10px 12px; background: var(--soft); border: 1px solid var(--line); border-radius: 8px; font-size: 13px; color: var(--secondary); }
      .issue-list li strong { display: block; font: 600 14px/1.3 var(--fd); color: var(--text); }
      .muted-line { font-size: 12.5px; color: var(--muted); }

      @media (max-width: 760px) {
        .wizard-shell {
          grid-template-columns: minmax(0, 1fr);
          grid-template-areas: "top" "rail" "card";
          row-gap: 20px;
          padding: 16px 16px 28px;
        }
        .wizard-rail { position: static; }
        .wizard-stepper { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); column-gap: 14px; }
        .wizard-stepper li { grid-template-columns: auto minmax(0, 1fr); padding-top: 14px; padding-bottom: 0; }
        .wizard-stepper small { display: none; }
        .wizard-card { padding: 22px 18px 20px; gap: 22px; }
        .auth-tab { min-height: 44px; padding: 6px 8px; font-size: 13px; line-height: 1.2; }
        .scan-row > .btn { width: 100%; }
        .step-head h1 { font-size: 24px; }
        .form-grid, .harness-grid, .presets-grid, .role-grid, .preview-grid { grid-template-columns: minmax(0, 1fr); }
        .plan-row { grid-template-columns: 18px minmax(0, 1fr); }
        .plan-row .amount-input { grid-column: 2; }
        .wizard-footer { flex-direction: column-reverse; align-items: stretch; }
        .wizard-footer .btn-primary { min-width: 0; }
      }
      @media (max-width: 520px) {
        .wizard-sub { display: none; }
        .skip-lead { display: none; }
      }
    </style>
  </head>
  <body>
    <main class="wizard-shell" id="wizard-shell">
      <header class="wizard-topbar" data-tauri-drag-region>
        <div class="wizard-brand" data-tauri-drag-region>
          <img src="/favicon.svg" alt="" width="32" height="32">
          <span class="wizard-wordmark" data-tauri-drag-region>BURN<strong>RATE</strong></span>
          <span class="wizard-sub eyebrow" data-tauri-drag-region>Setup</span>
        </div>
        <div class="wizard-actions">
          <button type="button" class="icon-btn" id="wiz-theme-toggle" aria-label="Switch color theme" title="Switch color theme">
            <svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.25" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 1.75a6.25 6.25 0 0 1 0 12.5z" fill="currentColor"/></svg>
          </button>
          <button type="button" class="btn" id="wizard-later" title="Go to the __SKIP_TARGET__ now. Open setup again any time from the HUD or the tray.">Set up later</button>
        </div>
      </header>

      <aside class="wizard-rail" aria-label="Progress">
        <p class="eyebrow">Progress</p>
        <ol class="wizard-stepper" id="wizard-stepper" aria-label="Setup steps">
          <li data-step="1" class="active"><span class="num">01</span><span class="step-text"><strong>This computer</strong><small>On its own, or with your hub</small></span></li>
          <li data-step="2"><span class="num">02</span><span class="step-text"><strong>What we found</strong><small>Tools, usage and plan meters</small></span></li>
          <li data-step="3"><span class="num">03</span><span class="step-text"><strong>Plans &amp; balances</strong><small>What you pay and what is left</small></span></li>
          <li data-step="4"><span class="num">04</span><span class="step-text"><strong>Check</strong><small>Your HUD, with live data</small></span></li>
        </ol>
      </aside>

      <section class="wizard-card">
        <!-- STEP 1: THIS COMPUTER -->
        <div id="wiz-step-1" class="wiz-pane">
          <div class="step-head">
            <p class="eyebrow"><span class="n">01</span>This computer</p>
            <h1>How should this computer work?</h1>
            <p class="lede">BURNRATE reads the AI tools on this computer. It can keep that to itself, or join your other computers through a hub so every HUD shows the same totals.</p>
          </div>

          <div class="field-group">
            <label for="wiz-computer-name">This computer's name</label>
            <input id="wiz-computer-name" class="text-input" maxlength="64" placeholder="e.g. MyDesktop, MacBook Pro">
            <span class="hint">Shown in computer filters and next to live agents.</span>
          </div>

          <div id="wiz-current" class="sub-box current-box" hidden>
            <p id="wiz-current-text" class="lede"></p>
            <button type="button" class="btn btn-sm" id="wiz-change-role">Change</button>
          </div>

          <div class="role-grid" id="wiz-roles" role="radiogroup" aria-label="How this computer works">
            <button type="button" class="role-card" role="radio" data-role="standalone" aria-checked="true">
              <strong>Just this computer</strong>
              <small>Usage stays here. No account, nothing leaves this computer.</small>
            </button>
            <button type="button" class="role-card" role="radio" data-role="spoke" aria-checked="false">
              <strong>Join my hub</strong>
              <small>Send this computer's usage to the hub, and see every computer's totals here.</small>
            </button>
            <button type="button" class="role-card" role="radio" data-role="hub" aria-checked="false">
              <strong>Make this the hub</strong>
              <small>Your other computers send their usage here. You give each one a pairing code.</small>
            </button>
          </div>

          <div id="wiz-join" class="sub-box" hidden>
            <div class="auth-tabs" role="tablist" aria-label="How to join">
              <button type="button" class="auth-tab active" id="wiz-join-code-tab" role="tab">Pairing code</button>
              <button type="button" class="auth-tab" id="wiz-join-account-tab" role="tab">Account</button>
            </div>
            <div id="wiz-join-code" class="field-group">
              <label for="wiz-pair-input">Pairing code from your hub</label>
              <textarea id="wiz-pair-input" class="text-input" spellcheck="false" autocomplete="off" placeholder="brpair_…"></textarea>
              <span class="hint">On the hub, open setup, choose Make this the hub, then Create pairing code.</span>
            </div>
            <div id="wiz-join-account" class="field-group" hidden>
              <div class="form-grid">
                <div class="field-group">
                  <label for="wiz-username">Username or email</label>
                  <input id="wiz-username" class="text-input" autocomplete="username">
                </div>
                <div class="field-group">
                  <label for="wiz-password">Password</label>
                  <input id="wiz-password" type="password" class="text-input" autocomplete="current-password">
                </div>
              </div>
              <div class="field-group" id="wiz-confirm-group" hidden>
                <label for="wiz-password-confirm">Repeat the password</label>
                <input id="wiz-password-confirm" type="password" class="text-input" autocomplete="new-password">
                <span class="hint" id="wiz-password-hint"></span>
              </div>
              <button type="button" class="link-btn" id="wiz-auth-switch">Create an account instead</button>
              <details class="advanced-server">
                <summary>Hub address: <code id="wiz-server-label"></code></summary>
                <div class="field-group">
                  <label for="wiz-server-url">Hub address</label>
                  <input id="wiz-server-url" class="text-input mono" spellcheck="false">
                </div>
              </details>
            </div>
          </div>

          <div id="wiz-hub" class="sub-box" hidden>
            <div class="section-head">
              <h3>Add another computer</h3>
              <p class="hint">Create a code here, then paste it on the other computer under Join my hub. Each code works for one computer.</p>
            </div>
            <div class="field-group">
              <label>Address the other computer uses to reach this one</label>
              <div id="wiz-hub-addresses" class="plan-list"></div>
            </div>
            <div class="inline-row">
              <input id="wiz-remote-name" class="text-input" maxlength="64" placeholder="Name of the other computer">
              <button type="button" class="btn btn-sm" id="wiz-pair-create">Create pairing code</button>
            </div>
            <div id="wiz-pair-result" class="field-group" hidden>
              <label for="wiz-pair-code">Pairing code</label>
              <div id="wiz-pair-code" class="code-box"></div>
              <div class="inline-row">
                <button type="button" class="btn btn-sm" id="wiz-pair-copy">Copy code</button>
                <span class="hint">The code contains a key for this hub. Share it only with your own computers.</span>
              </div>
            </div>
          </div>

          <div id="wiz-role-warning" class="status-banner err" hidden></div>
          <div id="wiz-step1-status" class="status-banner" hidden></div>
        </div>

        <!-- STEP 2: WHAT WE FOUND -->
        <div id="wiz-step-2" class="wiz-pane" hidden>
          <div class="step-head">
            <p class="eyebrow"><span class="n">02</span>What we found</p>
            <h1>What BURNRATE found on this computer</h1>
            <p class="lede">Each row is a card on your HUD. It shows whether usage is being read, whether the plan meter has a current reading, and what to do when it does not.</p>
          </div>
          <div class="scan-row">
            <span id="wiz-progress" class="status-banner"><span id="wiz-progress-text">Reading your tools…</span></span>
            <button type="button" class="btn btn-sm" id="wiz-scan-btn">Check again</button>
          </div>
          <div class="lane-list" id="wiz-lanes"></div>
          <div id="wiz-usage-only-wrap" class="field-group" hidden>
            <p class="eyebrow">Also reading usage from</p>
            <div class="lane-list" id="wiz-usage-only"></div>
          </div>
          <p id="wiz-not-found" class="muted-line" hidden></p>
        </div>

        <!-- STEP 3: PLANS & BALANCES -->
        <div id="wiz-step-3" class="wiz-pane" hidden>
          <div class="step-head">
            <p class="eyebrow"><span class="n">03</span>Plans &amp; balances</p>
            <h1>What you pay, and what is left</h1>
            <p class="lede">Plans let BURNRATE compare what you pay with what you use. Balances show the money left on pay-as-you-go accounts.</p>
          </div>

          <div class="sub-box">
            <div class="section-head">
              <h3>Plans</h3>
              <p class="hint">Suggestions come from what the meters on this computer report. Check each price before you add it.</p>
            </div>
            <div id="wiz-plan-issues-wrap" class="field-group" hidden>
              <p class="eyebrow">Plans that need a look</p>
              <div class="plan-list" id="wiz-plan-issues"></div>
            </div>
            <div class="plan-list" id="wiz-plans-suggested"></div>
            <div class="plan-list" id="wiz-plans-added"></div>
            <details class="more" id="wiz-plans-add-wrap">
              <summary>Add another plan</summary>
              <div class="inline-row">
                <select id="wiz-plan-preset" class="text-input select-sm" aria-label="Plan"></select>
                <input id="wiz-plan-name" class="text-input" placeholder="Plan name" aria-label="Plan name">
                <input id="wiz-plan-amount" type="number" min="0" step="0.01" class="text-input amount-input" placeholder="USD / month" aria-label="Monthly price in USD">
                <button type="button" class="btn btn-sm" id="wiz-plan-add">Add</button>
              </div>
            </details>
            <details class="more" id="wiz-plans-tracked-wrap">
              <summary id="wiz-plans-tracked-label">Plans already tracked</summary>
              <div class="plan-list" id="wiz-plans-tracked"></div>
            </details>
          </div>

          <div class="sub-box">
            <div class="section-head">
              <h3>Pay-as-you-go balances</h3>
              <p class="hint">Keys are tested, then stored in this computer's credential store (Windows Credential Manager, the macOS Keychain or the desktop keyring). They are only used to read the balance.</p>
            </div>
            <div class="lane-list" id="wiz-balances"></div>
            <p id="wiz-vault-note" class="status-banner err" hidden>This computer's credential store is not available, so keys cannot be saved here.</p>
            <details class="more" id="wiz-admin-wrap" hidden>
              <summary>Organization usage APIs</summary>
              <div class="lane-list" id="wiz-admin-apis"></div>
            </details>
          </div>
          <div id="wiz-step3-status" class="status-banner" hidden></div>
        </div>

        <!-- STEP 4: CHECK -->
        <div id="wiz-step-4" class="wiz-pane" hidden>
          <div class="step-head">
            <p class="eyebrow"><span class="n">04</span>Check</p>
            <h1>Check your HUD</h1>
            <p class="lede">This is what your HUD shows right now. Anything that needs you is listed below the cards.</p>
          </div>
          <div class="preview-grid" id="wiz-preview"></div>
          <div class="field-group">
            <p class="eyebrow">Needs attention</p>
            <ul class="issue-list" id="wiz-attention"></ul>
          </div>
          <p class="muted-line" id="wiz-progress-4"></p>
          <div class="sub-box" id="wiz-notify-box">
            <div class="section-head">
              <h3>Desktop notifications</h3>
              <p class="hint">Windows notifications from BURNRATE, sent once each and held back in quiet hours.</p>
            </div>
            <div class="lane-list" id="wiz-notify-kinds"></div>
            <div class="inline-row">
              <button type="button" class="btn btn-sm" id="wiz-notify-test">Send a test</button>
              <span class="muted-line" id="wiz-notify-status"></span>
            </div>
          </div>
          <div class="sub-box">
            <div class="section-head">
              <h3>Diagnostics</h3>
              <p class="hint">Versions, freshness, meters and recent log lines, with keys, e-mail addresses and your home folder removed. Nothing is sent; paste it where you choose.</p>
            </div>
            <div class="inline-row">
              <button type="button" class="btn btn-sm" id="wiz-diag-copy">Copy diagnostics</button>
              <span class="muted-line" id="wiz-diag-status"></span>
            </div>
            <textarea id="wiz-diag-text" class="text-input mono" rows="8" readonly hidden aria-label="Diagnostics"></textarea>
          </div>
          <div class="sub-box">
            <label class="check-row">
              <input type="checkbox" id="wiz-hud-auto" checked>
              <span><strong>Open the HUD when BURNRATE starts</strong><small>A compact live meter pinned on your screen</small></span>
            </label>
            <input type="checkbox" id="wiz-hud-top" checked hidden>
          </div>
          <div id="wiz-finish-banner" class="status-banner" hidden></div>
        </div>

        <footer class="wizard-footer">
          <button type="button" class="btn" id="wiz-back-btn" disabled>← Back</button>
          <button type="button" class="btn btn-primary" id="wiz-next-btn">Continue →</button>
        </footer>
      </section>
    </main>

    <script>
      (() => {
        const $ = id => document.getElementById(id);
        // The desktop app starts with this window hidden; show it for setup.
        try { window.__TAURI_INTERNALS__?.invoke?.("show_dashboard")?.catch?.(() => {}); } catch {}
        const LAST_STEP = 4;
        let step = 1;
        let role = "standalone";
        let joinMode = "code";
        let authMode = "login";
        let connected = false;
        let changingRole = false;
        let stateData = null;
        let checkData = null;
        let scanSince = null;
        let progressTimer = null;
        let progressStarted = 0;
        const suggestionChoices = new Map();
        const addedPlans = [];

        async function api(url, method = "GET", body = null) {
          const opts = {method, cache: "no-store", headers: {}};
          if (method !== "GET") opts.headers["X-BURNRATE-Request"] = "1";
          if (body != null) {
            opts.headers["Content-Type"] = "application/json";
            opts.body = JSON.stringify(body);
          }
          const res = await fetch(url, opts);
          const data = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error(data.error || data.detail || ("HTTP " + res.status));
          return data;
        }
        const el = (tag, cls, text) => {
          const node = document.createElement(tag);
          if (cls) node.className = cls;
          if (text != null) node.textContent = text;
          return node;
        };
        function banner(id, kind, text) {
          const node = $(id);
          node.hidden = !text;
          node.className = "status-banner" + (kind ? " " + kind : "");
          node.textContent = text || "";
        }
        function uuid() {
          if (window.crypto?.randomUUID) return crypto.randomUUID();
          const b = crypto.getRandomValues(new Uint8Array(16));
          b[6] = (b[6] & 15) | 64;
          b[8] = (b[8] & 63) | 128;
          const h = [...b].map(x => x.toString(16).padStart(2, "0")).join("");
          return [h.slice(0, 8), h.slice(8, 12), h.slice(12, 16), h.slice(16, 20), h.slice(20)].join("-");
        }
        function ago(iso) {
          const ms = Date.now() - Date.parse(iso || "");
          if (!Number.isFinite(ms)) return "";
          const min = Math.floor(ms / 60000);
          if (min < 1) return "just now";
          if (min < 60) return min + " min ago";
          const hours = Math.floor(min / 60);
          return hours < 48 ? hours + " h ago" : Math.floor(hours / 24) + " days ago";
        }
        const money = value => "$" + Number(value).toFixed(2);

        // Provider marks come from the shared sprite served at /logos.svg.
        const TOOL_LOGOS = {
          codex: "codex", "claude-code": "claude", opencode: "zai", zcode: "zai", "opencode-go": "opencode",
          grok: "grok", antigravity: "antigravity", muse: "meta", openrouter: "openrouter", traycer: "traycer"
        };
        const logoFor = key => TOOL_LOGOS[key] || (String(key).startsWith("claude") ? "claude" : String(key).startsWith("opencode") ? "opencode" : "custom");
        function logoTile(key) {
          const tile = el("span", "logo-tile");
          tile.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><use href="/logos.svg#' + logoFor(key) + '"></use></svg>';
          return tile;
        }

        /* ---------- Step 1: this computer ---------- */
        function renderStep1() {
          const s = stateData?.setup || {};
          const spoke = s.spokeConfig || {};
          connected = Boolean(spoke.enabled && spoke.hasToken && s.role === "spoke");
          $("wiz-current").hidden = !connected;
          if (connected) {
            let text = "This computer sends its usage to your hub at " + spoke.hubUrl;
            if (s.account?.username) text += " as " + s.account.username;
            if (spoke.lastSyncError) text += ". The last sync failed: " + spoke.lastSyncError;
            else if (spoke.lastSyncAt) text += " · last sync " + ago(spoke.lastSyncAt);
            $("wiz-current-text").textContent = text + ".";
          }
          const choosing = !connected || changingRole;
          $("wiz-roles").hidden = !choosing;
          document.querySelectorAll(".role-card").forEach(card => card.setAttribute("aria-checked", String(card.dataset.role === role)));
          $("wiz-join").hidden = !choosing || role !== "spoke";
          $("wiz-hub").hidden = role !== "hub";
          $("wiz-join-code-tab").classList.toggle("active", joinMode === "code");
          $("wiz-join-account-tab").classList.toggle("active", joinMode === "account");
          $("wiz-join-code").hidden = joinMode !== "code";
          $("wiz-join-account").hidden = joinMode !== "account";
          $("wiz-confirm-group").hidden = authMode !== "register";
          $("wiz-password").autocomplete = authMode === "register" ? "new-password" : "current-password";
          $("wiz-auth-switch").textContent = authMode === "register" ? "I already have an account" : "Create an account instead";
          $("wiz-password-hint").textContent = "At least " + (stateData?.minPasswordLength || 8) + " characters.";
          $("wiz-server-label").textContent = $("wiz-server-url").value || "";
          const leaving = connected && changingRole && role !== "spoke";
          banner("wiz-role-warning", "err", leaving ? "When you finish, this computer stops sending its usage to " + spoke.hubUrl + "." : "");
        }

        function renderHubAddresses() {
          const list = $("wiz-hub-addresses");
          list.replaceChildren(...(stateData?.networkAddresses || []).map((address, index) => {
            const label = el("label", "check-row");
            const input = el("input");
            input.type = "radio";
            input.name = "hub-address";
            input.value = address.url;
            input.checked = index === 0;
            const copy = el("span");
            copy.append(el("strong", null, address.url), el("small", null, address.label));
            label.append(input, copy);
            return label;
          }));
        }

        async function createPairingCode() {
          const remoteName = $("wiz-remote-name").value.trim() || "Another computer";
          const hubUrl = document.querySelector('input[name="hub-address"]:checked')?.value;
          try {
            const res = await api("/api/setup/pair-hub", "POST", {remoteName, hubUrl});
            $("wiz-pair-code").textContent = res.pairingCode;
            $("wiz-pair-result").hidden = false;
            banner("wiz-step1-status", "", "");
          } catch (err) {
            banner("wiz-step1-status", "err", err.message);
          }
        }

        async function leaveStep1() {
          const computerName = $("wiz-computer-name").value.trim();
          if (role !== "spoke" || (connected && !changingRole)) return true;
          if (joinMode === "code") {
            const pairingCode = $("wiz-pair-input").value.trim();
            if (!pairingCode) {
              banner("wiz-step1-status", "err", "Paste the pairing code from your hub.");
              return false;
            }
            banner("wiz-step1-status", "", "Joining your hub…");
            const res = await api("/api/setup/connect-spoke", "POST", {pairingCode, computerName, testSync: false});
            stateData.setup = res.setup;
          } else {
            const username = $("wiz-username").value.trim();
            const password = $("wiz-password").value;
            const minimum = stateData?.minPasswordLength || 8;
            if (!username || !password) {
              banner("wiz-step1-status", "err", "Enter your username and password.");
              return false;
            }
            if (authMode === "register") {
              if (password.length < minimum) {
                banner("wiz-step1-status", "err", "Choose a password of at least " + minimum + " characters.");
                return false;
              }
              if (password !== $("wiz-password-confirm").value) {
                banner("wiz-step1-status", "err", "The two passwords do not match.");
                return false;
              }
            }
            banner("wiz-step1-status", "", authMode === "register" ? "Creating your account…" : "Signing in…");
            const res = await api("/api/setup/account", "POST", {
              mode: authMode, username, password, computerName,
              serverUrl: $("wiz-server-url").value.trim(), testSync: false
            });
            stateData.setup = res.setup;
          }
          $("wiz-password").value = "";
          $("wiz-password-confirm").value = "";
          changingRole = false;
          renderStep1();
          banner("wiz-step1-status", "good", "Joined your hub. This computer's usage will sync in the background.");
          return true;
        }

        /* ---------- Step 2: what we found ---------- */
        const METER_CHIPS = {ok: ["good", "Meter OK"], held: ["warn", "Old reading"], unavailable: ["bad", "No meter"], idle: ["", "Idle"]};
        const USAGE_CHIPS = {ok: "good", warn: "warn", bad: "bad"};
        function chip(kind, text) { return el("span", "status-chip" + (kind ? " " + kind : ""), text); }
        function fact(key, text) {
          const row = el("p", "fact");
          row.append(el("span", "fact-key", key), el("span", null, text));
          return row;
        }
        function consentFor(key) {
          return (checkData?.consent || stateData?.consent || []).find(item => item.key === key);
        }
        function consentSwitch(key) {
          const info = consentFor(key) || {};
          const row = el("label", "switch-row");
          const input = el("input");
          input.type = "checkbox";
          input.checked = Boolean(info.enabled);
          input.dataset.consent = key;
          input.addEventListener("change", () => setConsent(key, input.checked));
          const copy = el("span");
          copy.append(el("strong", null, info.label || key), el("small", null, info.detail || ""));
          row.append(input, copy);
          return row;
        }
        function laneRow(row) {
          const card = el("article", "lane-row" + (row.meter.state === "ok" || row.meter.state === "idle" ? "" : " attention"));
          const main = el("div", "lane-main");
          main.append(el("strong", null, row.name), el("small", null, row.plan || ""));
          const [kind, label] = METER_CHIPS[row.meter.state] || ["", row.meter.state];
          card.append(logoTile(row.tool), main, chip(kind, label));
          const facts = el("div", "lane-facts");
          facts.append(fact("Usage", row.usage.text), fact("Meter", row.meter.text));
          if (row.meter.detail) facts.append(el("p", "lane-detail", row.meter.detail));
          if (row.meter.fix) facts.append(el("p", "lane-fix", "→ " + row.meter.fix));
          card.append(facts);
          if (row.consent) card.append(consentSwitch(row.consent));
          return card;
        }
        const USAGE_LABELS = {ok: "Reading", warn: "Not recent", bad: "Failed", waiting: "Waiting", missing: "Not installed"};
        function usageRow(item) {
          const card = el("article", "lane-row");
          const main = el("div", "lane-main");
          main.append(el("strong", null, item.name), el("small", null, [item.usage.text, item.usage.path].filter(Boolean).join(" · ")));
          card.append(logoTile(item.tool), main, chip(USAGE_CHIPS[item.usage.state] || "", USAGE_LABELS[item.usage.state] || item.usage.state));
          return card;
        }
        function renderCheck() {
          if (!checkData) return;
          $("wiz-lanes").replaceChildren(...checkData.lanes.map(laneRow));
          $("wiz-usage-only").replaceChildren(...checkData.usageOnly.map(usageRow));
          $("wiz-usage-only-wrap").hidden = !checkData.usageOnly.length;
          $("wiz-not-found").hidden = !checkData.notFound.length;
          $("wiz-not-found").textContent = "Not installed on this computer: " + checkData.notFound.join(", ") + ".";
        }
        async function loadCheck() {
          if (!checkData) $("wiz-lanes").replaceChildren(el("p", "muted-line", "Checking each tool…"));
          try {
            checkData = await api("/api/setup/check");
            renderCheck();
            if (step === 3) renderPlans();
          } catch (err) {
            $("wiz-lanes").replaceChildren(el("p", "status-banner err", "Could not check this computer: " + err.message));
          }
        }
        async function setConsent(key, on) {
          const inputs = () => document.querySelectorAll('input[data-consent="' + key + '"]');
          try {
            const res = await api("/api/setup/consent", "POST", {switches: {[key]: on}});
            if (checkData) checkData.consent = res.consent;
            if (stateData) stateData.consent = res.consent;
            inputs().forEach(input => { input.checked = on; });
            if (res.warning) $("wiz-progress-text").textContent = res.warning;
            setTimeout(loadCheck, 5000);
          } catch (err) {
            inputs().forEach(input => { input.checked = !on; });
            $("wiz-progress-text").textContent = "Could not change that setting: " + err.message;
          }
        }
        async function startScan() {
          const btn = $("wiz-scan-btn");
          btn.disabled = true;
          // Show what is known now while the new pass runs.
          const shown = loadCheck();
          try {
            const res = await api("/api/setup/scan", "POST", {});
            scanSince = res.started;
            startProgress();
          } catch (err) {
            $("wiz-progress-text").textContent = "Could not start a check: " + err.message;
          } finally {
            btn.disabled = false;
          }
          await shown;
        }
        function startProgress() {
          clearInterval(progressTimer);
          progressStarted = Date.now();
          progressTimer = setInterval(pollProgress, 2500);
          pollProgress();
        }
        async function pollProgress() {
          let data;
          try { data = await api("/api/setup/progress"); } catch { return; }
          const since = Date.parse(scanSince || "") || 0;
          const sources = data.sources || [];
          const beat = data.heartbeat || {};
          const done = sources.filter(item => item.status !== "running" && Date.parse(item.finishedAt || "") >= since);
          const failed = sources.filter(item => item.status === "failed").map(item => item.name);
          const passDone = Date.parse(beat.lastPassAt || "") >= since;
          const gaveUp = Date.now() - progressStarted > 10 * 60 * 1000;
          let text = passDone
            ? "Checked " + sources.length + " tools " + ago(beat.lastPassAt) + "."
            : "Reading your tools… " + done.length + " of " + sources.length + " done.";
          if (failed.length) text += " Could not read " + failed.join(", ") + ".";
          if (gaveUp && !passDone) text = "Still reading your tools in the background. The rows below show the latest results.";
          $("wiz-progress-text").textContent = text;
          $("wiz-progress-4").textContent = text;
          if (passDone || gaveUp) {
            clearInterval(progressTimer);
            progressTimer = null;
            await loadCheck();
            if (step === 4) loadPreview();
          }
        }

        /* ---------- Step 3: plans and balances ---------- */
        function planRow(item, {checkbox = null, priceInput = null, price = null, note = ""} = {}) {
          const row = el("div", "plan-row" + (checkbox ? "" : " tracked"));
          if (checkbox) row.append(checkbox);
          const copy = el("div");
          copy.append(el("strong", null, item.name), el("small", null, note));
          row.append(copy);
          if (priceInput) row.append(priceInput);
          else if (price != null) row.append(el("span", "price", money(price) + " / mo"));
          return row;
        }
        function renderPlans() {
          const plans = checkData?.plans || {active: [], suggestions: []};
          const suggested = plans.suggestions.map(item => {
            if (!suggestionChoices.has(item.lane)) suggestionChoices.set(item.lane, {checked: true, amount: item.amount_usd});
            const choice = suggestionChoices.get(item.lane);
            const box = el("input");
            box.type = "checkbox";
            box.checked = choice.checked;
            box.setAttribute("aria-label", "Add " + item.name);
            box.addEventListener("change", () => { choice.checked = box.checked; });
            const price = el("input", "text-input amount-input");
            price.type = "number";
            price.min = "0";
            price.step = "0.01";
            price.placeholder = "USD / month";
            price.value = choice.amount ?? "";
            price.setAttribute("aria-label", "Monthly price for " + item.name);
            price.addEventListener("input", () => { choice.amount = price.value === "" ? null : Number(price.value); });
            return planRow(item, {checkbox: box, priceInput: price, note: "Suggested · " + item.basis});
          });
          $("wiz-plans-suggested").replaceChildren(...suggested);
          if (!suggested.length) {
            $("wiz-plans-suggested").replaceChildren(el("p", "muted-line", plans.active.length
              ? "Every measured plan on this computer is already tracked."
              : "No plan meters have a reading yet. You can add plans yourself below."));
          }
          $("wiz-plans-added").replaceChildren(...addedPlans.map((item, index) => {
            const row = planRow(item, {price: item.amount_usd, note: "Will be added"});
            const remove = el("button", "link-btn", "Remove");
            remove.type = "button";
            remove.addEventListener("click", () => { addedPlans.splice(index, 1); renderPlans(); });
            row.append(remove);
            return row;
          }));
          $("wiz-plans-tracked").replaceChildren(...plans.active.map(item => planRow(item, {price: item.amount_usd, note: "Tracked"})));
          $("wiz-plans-tracked-label").textContent = "Plans already tracked (" + plans.active.length + ")";
          $("wiz-plans-tracked-wrap").hidden = !plans.active.length;
        }
        let planIssues = [];
        async function loadPlanIssues() {
          try { planIssues = (await api("/api/setup/plan-issues")).issues || []; } catch { planIssues = []; }
          renderPlanIssues();
        }
        function renderPlanIssues() {
          $("wiz-plan-issues").replaceChildren(...planIssues.map(issue => {
            const row = el("div", "plan-row tracked");
            const copy = el("div");
            copy.append(el("strong", null, issue.name + " · " + money(issue.amountUsd) + " / mo"), el("small", null, issue.issue));
            const fix = el("button", "btn btn-sm", issue.fixLabel);
            fix.type = "button";
            fix.addEventListener("click", async () => {
              fix.disabled = true;
              fix.textContent = "Saving…";
              try {
                await api("/api/setup/plans/fix", "POST", {planId: issue.planId, version: issue.version, fix: issue.fix});
                banner("wiz-step3-status", "good", issue.fix === "end" ? issue.name + " ended today." : issue.name + " is now " + issue.fixLabel.replace("Link to ", "linked to ") + ".");
                await loadPlanIssues();
                await loadCheck();
              } catch (err) {
                fix.disabled = false;
                fix.textContent = issue.fixLabel;
                banner("wiz-step3-status", "err", err.message);
              }
            });
            row.append(copy, fix);
            return row;
          }));
          $("wiz-plan-issues-wrap").hidden = !planIssues.length;
        }
        function renderPresetPicker() {
          const select = $("wiz-plan-preset");
          const presets = stateData?.presets || [];
          select.replaceChildren(
            ...presets.map((preset, index) => {
              const option = el("option", null, preset.name);
              option.value = String(index);
              return option;
            }),
            Object.assign(el("option", null, "Other plan…"), {value: "custom"})
          );
          const fill = () => {
            const preset = presets[Number(select.value)];
            $("wiz-plan-name").value = preset ? preset.name : "";
            $("wiz-plan-amount").value = preset && preset.amount_usd > 0 ? preset.amount_usd : "";
          };
          select.addEventListener("change", fill);
          fill();
        }
        function addPlan() {
          const presets = stateData?.presets || [];
          const preset = presets[Number($("wiz-plan-preset").value)];
          const name = $("wiz-plan-name").value.trim();
          const amount = Number($("wiz-plan-amount").value);
          if (!name || !Number.isFinite(amount) || amount <= 0) {
            banner("wiz-step3-status", "err", "Give the plan a name and a monthly price.");
            return;
          }
          addedPlans.push({tool_key: preset && preset.name === name ? preset.tool_key : (preset?.tool_key || "custom"), name, amount_usd: amount, cadence: "monthly"});
          banner("wiz-step3-status", "", "");
          renderPlans();
        }

        const CONNECTION_STATES = {
          balance_available: ["good", "Connected"], awaiting_ingest: ["good", "Connected · first reading soon"],
          receiving_usage: ["good", "Connected"], waiting_activity: ["good", "Connected"],
          needs_attention: ["bad", "Needs attention"], disabled: ["", "Disconnected"]
        };
        function connectionRow(conn, vaultAvailable) {
          const card = el("article", "lane-row");
          const main = el("div", "lane-main");
          const caps = conn.capabilities || [];
          main.append(el("strong", null, conn.name), el("small", null, caps.includes("balance") ? "" : caps.join(", ")));
          const live = conn.externallyManaged || (conn.credentialConfigured && conn.enabled !== false);
          const [kind, label] = conn.externallyManaged
            ? ["good", "Set outside BURNRATE"]
            : live ? (CONNECTION_STATES[conn.state] || ["good", "Connected"]) : ["", "Not connected"];
          card.append(logoTile(conn.source), main, chip(kind, label));
          const facts = el("div", "lane-facts");
          facts.append(el("p", "lane-detail", conn.credentialHelp || ""));
          if (live && conn.detail) facts.append(el("p", "lane-fix", conn.detail));
          card.append(facts);
          if (conn.externallyManaged) return card;
          const form = el("form", "lane-form");
          const key = el("input", "text-input mono");
          key.type = "password";
          key.autocomplete = "off";
          key.spellcheck = false;
          key.placeholder = live ? "Paste a new key to replace it" : "Paste the key";
          key.setAttribute("aria-label", conn.name + " key");
          const save = el("button", "btn btn-sm btn-primary", live ? "Replace key" : "Test and connect");
          save.type = "submit";
          save.disabled = !vaultAvailable;
          const result = el("p", "lane-detail");
          form.append(key, save);
          if (live) {
            const off = el("button", "btn btn-sm", "Disconnect");
            off.type = "button";
            off.addEventListener("click", () => changeConnection(conn, "disable", null, result));
            form.append(off);
          }
          form.addEventListener("submit", event => {
            event.preventDefault();
            const secret = key.value.trim();
            key.value = "";
            if (!secret) { result.textContent = "Paste the key first."; return; }
            changeConnection(conn, "connect", secret, result);
          });
          card.append(form, result);
          result.style.gridColumn = "2 / -1";
          return card;
        }
        async function changeConnection(conn, operation, secret, result) {
          result.textContent = operation === "connect" ? "Testing the key…" : "Disconnecting…";
          try {
            await api("/api/connections", "POST", {
              source: conn.source, operation, location: "", mode: "api",
              revision: conn.revision || 0, requestId: uuid(), ...(secret ? {secret} : {})
            });
            stateData = await api("/api/setup/state");
            renderBalances();
          } catch (err) {
            result.textContent = err.message;
          }
        }
        function renderBalances() {
          const data = stateData?.apiConnections || {connections: [], vaultAvailable: false};
          const isBalance = conn => (conn.capabilities || []).includes("balance");
          $("wiz-balances").replaceChildren(...data.connections.filter(isBalance).map(conn => connectionRow(conn, data.vaultAvailable)));
          const admin = data.connections.filter(conn => !isBalance(conn));
          $("wiz-admin-apis").replaceChildren(...admin.map(conn => connectionRow(conn, data.vaultAvailable)));
          $("wiz-admin-wrap").hidden = !admin.length;
          $("wiz-vault-note").hidden = data.vaultAvailable;
        }

        /* ---------- Step 4: check ---------- */
        function windowName(row) {
          const text = String(row.label || row.limitKey || "").toLowerCase();
          let scope = "";
          if (text.includes("gemini")) scope = "Gemini ";
          else if (text.includes("claude/gpt")) scope = "Other ";
          let win = row.limitKey || "Plan";
          if (/5-hour|five-hour|5h/.test(text)) win = "5h";
          else if (text.includes("month")) win = "Month";
          else if (text.includes("week")) win = "Week";
          else if (text.includes("fund") || text.includes("balance")) win = "Balance";
          return scope + win;
        }
        function previewCard(card) {
          const box = el("article", "preview-card");
          const head = el("div", "preview-head");
          head.append(logoTile(card.toolKey || card.providerKey), el("strong", null, card.shortName || card.providerName || card.providerKey));
          box.append(head);
          for (const row of card.rows || []) {
            const line = el("div", "preview-row");
            const payg = Boolean(row.isPayg || card.isPayg);
            const bar = el("div", "bar" + (payg ? " payg" : ""));
            const fill = el("i");
            if (row.pct != null && !payg) fill.style.width = Math.max(0, Math.min(100, Number(row.pct))) + "%";
            bar.append(fill);
            const value = row.pct != null && !payg ? Math.round(Number(row.pct)) + "%"
              : payg && row.unit === "usd" && row.used != null ? money(row.used) : "—";
            line.append(el("span", null, windowName(row)), bar, el("b", null, value));
            box.append(line);
            if (payg && row.runway && row.runway.line) box.append(el("p", "muted-line", row.runway.line));
          }
          return box;
        }
        async function loadNotifications() {
          let data;
          try { data = await api("/api/setup/notifications"); } catch { $("wiz-notify-box").hidden = true; return; }
          $("wiz-notify-test").disabled = !data.available;
          if (!data.available) $("wiz-notify-status").textContent = "Notifications come from the desktop service on Windows.";
          $("wiz-notify-kinds").replaceChildren(...data.kinds.map(kind => {
            const row = el("label", "check-row");
            const input = el("input");
            input.type = "checkbox";
            input.checked = kind.on;
            input.addEventListener("change", async () => {
              try { await api("/api/setup/notifications", "POST", {kinds: {[kind.key]: input.checked}}); }
              catch (err) { input.checked = !input.checked; $("wiz-notify-status").textContent = "Could not save: " + err.message; }
            });
            const text = el("span");
            text.append(el("strong", null, kind.label));
            row.append(input, text);
            return row;
          }));
        }
        $("wiz-notify-test").addEventListener("click", async () => {
          const status = $("wiz-notify-status");
          status.textContent = "Sending…";
          try { await api("/api/setup/notifications/test", "POST", {}); status.textContent = "Sent. If nothing appeared, check Windows notification settings for BURNRATE."; }
          catch (err) { status.textContent = err.message; }
        });
        $("wiz-diag-copy").addEventListener("click", async () => {
          const status = $("wiz-diag-status");
          const box = $("wiz-diag-text");
          status.textContent = "Collecting…";
          try {
            const {text} = await api("/api/setup/diagnostics", "POST", {});
            box.value = text;
            try { await navigator.clipboard.writeText(text); status.textContent = "Copied to the clipboard."; box.hidden = true; }
            catch { box.hidden = false; box.select(); status.textContent = "Select the text below and copy it."; }
          } catch (err) {
            status.textContent = "Could not collect diagnostics: " + err.message;
          }
        });
        async function loadPreview() {
          loadNotifications();
          if (!planIssues.length) await loadPlanIssues();
          const [hud, freshness] = await Promise.allSettled([api("/api/spend/hud"), api("/api/spend/freshness")]);
          const cards = hud.status === "fulfilled" ? (hud.value.capacity || []) : [];
          $("wiz-preview").replaceChildren(...cards.map(previewCard));
          if (!cards.length) $("wiz-preview").replaceChildren(el("p", "muted-line", "No plan cards yet. They appear after the first readings."));
          const issues = [];
          for (const row of checkData?.lanes || []) {
            if (row.meter.state !== "ok" && row.meter.state !== "idle") issues.push([row.name, row.meter.text + (row.meter.fix ? ". " + row.meter.fix : "")]);
          }
          for (const issue of planIssues) issues.push([issue.name, issue.issue + " " + issue.fixLabel + " in Plans & balances."]);
          if (freshness.status === "fulfilled") {
            for (const item of freshness.value.issues || []) {
              if (item.kind === "version" || (item.kind !== "lane" && item.severity !== "info")) issues.push([item.name, item.detail]);
            }
          }
          const list = $("wiz-attention");
          list.replaceChildren(...issues.map(([name, detail]) => {
            const item = el("li");
            item.append(el("strong", null, name), document.createTextNode(detail || ""));
            return item;
          }));
          if (!issues.length) list.replaceChildren(el("li", null, "Nothing. Every card has a current reading."));
        }

        async function finish() {
          const plans = [...(checkData?.plans?.suggestions || [])
            .filter(item => suggestionChoices.get(item.lane)?.checked && Number(suggestionChoices.get(item.lane)?.amount) > 0)
            .map(item => ({tool_key: item.tool_key, name: item.name, amount_usd: Number(suggestionChoices.get(item.lane).amount), cadence: "monthly"})),
            ...addedPlans];
          const autoHud = $("wiz-hud-auto").checked;
          const topHud = $("wiz-hud-top").checked;
          await api("/api/setup/complete", "POST", {
            computerName: $("wiz-computer-name").value.trim(),
            role: connected && !changingRole ? "spoke" : role,
            presetKeys: [],
            customPlans: plans,
            hudConfig: {autoOpen: autoHud, alwaysOnTop: topHud, dock: "top-left"}
          });
          const internals = window.__TAURI_INTERNALS__;
          if (internals && typeof internals.invoke === "function") {
            try { await internals.invoke(autoHud ? "show_hud" : "hide_hud"); } catch {}
          } else if (autoHud) {
            try {
              const nat = await fetch("/api/hud/native", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({action: topHud ? "pin" : "open"})
              }).then(r => r.json()).catch(() => null);
              if (!nat || !nat.ok) window.open("/hud", "burnrate-hud", "popup=yes,width=440,height=580,left=24,top=72");
            } catch {}
          }
          window.location.href = "/";
        }

        /* ---------- navigation ---------- */
        function showStep(next) {
          step = Math.max(1, Math.min(LAST_STEP, next));
          for (let index = 1; index <= LAST_STEP; index++) $("wiz-step-" + index).hidden = index !== step;
          document.querySelectorAll("#wizard-stepper li").forEach(li => {
            const n = Number(li.dataset.step);
            li.classList.toggle("active", n === step);
            li.classList.toggle("done", n < step);
          });
          $("wiz-back-btn").disabled = step === 1;
          $("wiz-next-btn").textContent = step === LAST_STEP ? "Finish setup" : "Continue →";
          if (step === 2 && !scanSince) startScan();
          if (step === 3) { renderPlans(); renderBalances(); loadPlanIssues(); if (!checkData) loadCheck(); }
          if (step === 4) { loadPreview(); if (!scanSince) startScan(); }
        }
        async function goTo(target) {
          if (target === step || !stateData) return;
          if (step === 1 && target > 1) {
            const btn = $("wiz-next-btn");
            btn.disabled = true;
            try {
              if (!(await leaveStep1())) return;
            } catch (err) {
              banner("wiz-step1-status", "err", err.message);
              return;
            } finally {
              btn.disabled = false;
            }
          }
          showStep(target);
        }

        document.querySelectorAll(".role-card").forEach(card => card.addEventListener("click", () => {
          role = card.dataset.role;
          banner("wiz-step1-status", "", "");
          renderStep1();
        }));
        $("wiz-change-role").addEventListener("click", () => { changingRole = true; renderStep1(); });
        $("wiz-join-code-tab").addEventListener("click", () => { joinMode = "code"; renderStep1(); });
        $("wiz-join-account-tab").addEventListener("click", () => { joinMode = "account"; renderStep1(); });
        $("wiz-auth-switch").addEventListener("click", () => { authMode = authMode === "register" ? "login" : "register"; renderStep1(); });
        $("wiz-server-url").addEventListener("input", () => { $("wiz-server-label").textContent = $("wiz-server-url").value; });
        $("wiz-pair-create").addEventListener("click", createPairingCode);
        $("wiz-pair-copy").addEventListener("click", async () => {
          try { await navigator.clipboard.writeText($("wiz-pair-code").textContent); $("wiz-pair-copy").textContent = "Copied"; } catch {}
        });
        $("wiz-scan-btn").addEventListener("click", startScan);
        $("wiz-plan-add").addEventListener("click", addPlan);
        document.querySelectorAll("#wizard-stepper li").forEach(li => li.addEventListener("click", () => goTo(Number(li.dataset.step))));
        $("wiz-back-btn").addEventListener("click", () => showStep(step - 1));
        $("wiz-next-btn").addEventListener("click", async () => {
          if (step < LAST_STEP) return goTo(step + 1);
          const btn = $("wiz-next-btn");
          btn.disabled = true;
          btn.textContent = "Finishing…";
          try {
            await finish();
          } catch (err) {
            banner("wiz-finish-banner", "err", "Could not finish setup: " + err.message);
            btn.disabled = false;
            btn.textContent = "Finish setup";
          }
        });
        $("wizard-later").addEventListener("click", async () => {
          try { await api("/api/setup/defer", "POST", {}); } catch {}
          window.location.href = "/";
        });
        $("wiz-theme-toggle").addEventListener("click", () => {
          const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
          document.documentElement.dataset.theme = next;
          try { localStorage.setItem("burnrate:theme", next); } catch {}
          document.querySelector('meta[name="theme-color"]').content = next === "dark" ? "#08080a" : "#ebe9e4";
        });

        async function init() {
          const next = $("wiz-next-btn");
          next.disabled = true;
          document.querySelectorAll(".role-card").forEach(card => { card.disabled = true; });
          banner("wiz-step1-status", "", "Loading this computer's settings…");
          try {
            stateData = await api("/api/setup/state");
          } catch (err) {
            banner("wiz-step1-status", "err", "Could not load setup: " + err.message + " Reload the page to try again.");
            return;
          }
          next.disabled = false;
          document.querySelectorAll(".role-card").forEach(card => { card.disabled = false; });
          banner("wiz-step1-status", "", "");
          const s = stateData.setup || {};
          $("wiz-computer-name").value = s.computerName || "";
          $("wiz-username").value = s.account?.username || "";
          $("wiz-server-url").value = s.account?.serverUrl || stateData.centralServerUrl || "";
          $("wiz-hud-auto").checked = s.hudConfig?.autoOpen !== false;
          $("wiz-hud-top").checked = s.hudConfig?.alwaysOnTop !== false;
          role = s.role === "spoke" ? "spoke" : (s.completed && s.role === "hub" ? "hub" : "standalone");
          renderStep1();
          renderHubAddresses();
          renderPresetPicker();
          const wanted = Number(new URLSearchParams(location.search).get("step"));
          if (wanted >= 2 && wanted <= LAST_STEP) showStep(wanted);
        }
        init();
      })();
    </script>
  </body>
</html>
"""


def render_setup_wizard() -> str:
    from spend_app.config import dashboard_enabled

    return SETUP_WIZARD_HTML.replace("__SKIP_TARGET__", "Dashboard" if dashboard_enabled() else "HUD", 1)
