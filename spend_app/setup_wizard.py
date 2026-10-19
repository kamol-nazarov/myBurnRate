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
from fastapi.responses import HTMLResponse, JSONResponse

from spend_app import aggregate
from spend_app.db import connect, initialize, utc_now
from spend_app.diagnostics import detect_local_sources, source_reports
from spend_app.plan_service import PlanConflict, PlanError, apply_mutation, effective_terms, list_plans
from spend_app.pricing import PricingEngine
from spend_app.subscriptions import add_subscription, materialize_subscription_days
from spend_app.sync import (
    STREAM_BROKER,
    _HUB_TEST_CLIENTS,
    authenticate_machine_token,
    clear_hub_proxy_cache,
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

DEFAULT_CENTRAL_SERVER_URLS = (
    "https://ai-harness-vps.tail982d0e.ts.net:8449",
    "http://100.71.45.59:17331",
)


def default_central_server_url() -> str:
    override = os.getenv("BURNRATE_CENTRAL_SERVER_URL", "").strip().rstrip("/")
    return override or DEFAULT_CENTRAL_SERVER_URLS[0]


HARNESS_CATALOG = {
    "codex_local": {"name": "OpenAI Codex CLI", "tool": "codex", "desc": "Local ~/.codex/sessions transcripts"},
    "claude_local": {"name": "Claude Code", "tool": "claude-code", "desc": "Local ~/.claude/projects JSONL logs"},
    "cursor_local": {"name": "Cursor IDE", "tool": "cursor", "desc": "Local Cursor project & agent transcripts"},
    "cursor_usage_service": {"name": "Cursor Usage Service", "tool": "cursor", "desc": "Local Cursor SQLite state.vscdb token meter"},
    "opencode_local": {"name": "OpenCode", "tool": "opencode", "desc": "Local OpenCode SQLite session database"},
    "zcode_local": {"name": "ZCode", "tool": "zcode", "desc": "Local ZCode agent database"},
    "grok_local": {"name": "Grok CLI", "tool": "grok", "desc": "Local ~/.grok session logs"},
    "antigravity_local": {"name": "Antigravity", "tool": "antigravity", "desc": "Local Antigravity app state"},
    "traycer_local": {"name": "Traycer", "tool": "traycer", "desc": "Local Traycer host database"},
    "muse_local": {"name": "Muse Code", "tool": "muse", "desc": "Local Muse Code session logs"},
}

SUBSCRIPTION_PRESETS = (
    {"key": "claude_max", "tool_key": "claude-code", "name": "Claude Max", "amount_usd": 100.0, "cadence": "monthly", "badge": "Anthropic"},
    {"key": "claude_pro", "tool_key": "claude-code", "name": "Claude Pro", "amount_usd": 20.0, "cadence": "monthly", "badge": "Anthropic"},
    {"key": "codex_pro", "tool_key": "codex", "name": "ChatGPT Pro", "amount_usd": 200.0, "cadence": "monthly", "badge": "OpenAI"},
    {"key": "codex_plus", "tool_key": "codex", "name": "ChatGPT Plus", "amount_usd": 20.0, "cadence": "monthly", "badge": "OpenAI"},
    {"key": "grok_super", "tool_key": "grok", "name": "SuperGrok", "amount_usd": 30.0, "cadence": "monthly", "badge": "xAI"},
    {"key": "zai_coding", "tool_key": "opencode", "name": "Z.AI Coding Plan", "amount_usd": 10.0, "cadence": "monthly", "badge": "Z.AI"},
    {"key": "muse_power", "tool_key": "muse", "name": "Muse Code Power Usage", "amount_usd": 0.0, "cadence": "monthly", "badge": "Meta"},
)


def _default_computer_name() -> str:
    try:
        host = socket.gethostname().strip()
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
        clear_hub_proxy_cache()
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


def _build_harness_list(settings) -> list[dict]:
    try:
        presence = detect_local_sources(Path.home())
    except Exception:
        presence = {}
    with connect(settings.database_path) as connection:
        reports = source_reports(connection, datetime.now(UTC), presence, settings=settings)
    result: list[dict] = []
    for row in reports:
        source = row["source"]
        if source not in HARNESS_CATALOG:
            continue
        meta = HARNESS_CATALOG[source]
        detected = bool(presence.get(source)) or row["state"] in {"healthy", "partial", "detected_without_history", "stale"}
        result.append(
            {
                "source": source,
                "name": meta["name"],
                "tool": meta["tool"],
                "description": meta["desc"],
                "state": row["state"],
                "detected": detected,
                "path": row.get("path") or "",
                "lastSuccess": row.get("lastSuccess"),
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


def setup_router(settings, pricing: PricingEngine) -> APIRouter:
    router = APIRouter()

    @router.get("/setup")
    def setup_page():
        return HTMLResponse(render_setup_wizard(), headers={"Cache-Control": "no-cache"})

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
                "harnesses": _build_harness_list(settings),
                "machines": machines,
                "subscriptions": active_plans,
                "presets": list(SUBSCRIPTION_PRESETS),
                "stats": {
                    "eventsCount": int(events_count),
                    "liveAgentsCount": int(agents_count),
                },
            },
            headers={"Cache-Control": "no-store"},
        )

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
            if len(password) < 4:
                raise ValueError("Password must be at least 4 characters.")

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
                    salt_hex, hash_hex = _hash_password(password)
                    accounts["users"][user_key] = {
                        "username": username,
                        "salt": salt_hex,
                        "passwordHash": hash_hex,
                        "createdAt": utc_now(),
                    }
                    _write_accounts(connection, accounts)
                    display_username = username
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
                if "action" in body:
                    connection.execute("BEGIN IMMEDIATE")
                    result = apply_mutation(connection, body, now_local)
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
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            mode = str(body.get("mode") or "login").strip().lower()
            if mode not in {"login", "register"}:
                raise ValueError("Mode must be 'login' or 'register'.")
            username = str(body.get("username") or "").strip()
            password = str(body.get("password") or "")
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
    async def scan_and_import():
        from starlette.concurrency import run_in_threadpool

        def _do_scan() -> dict:
            initialize(settings.database_path)
            from spend_app.providers import REGISTRY
            from spend_app.quotas import poll_activity, poll_quotas
            from spend_app.scheduler import _run_ingest_specs

            try:
                _run_ingest_specs(
                    [(spec, {}) for spec in REGISTRY if spec.kind in {"local", "manual"}],
                    settings,
                    pricing,
                )
            except Exception:
                pass
            try:
                poll_activity(database_path=settings.database_path)
            except Exception:
                pass
            try:
                poll_quotas(database_path=settings.database_path, settings=settings)
            except Exception:
                pass
            sync_res = maybe_run_configured_spoke_sync(settings, pricing)
            aggregate.reset_memo()
            STREAM_BROKER.publish({"type": "ingest", "source": "setup_scan"})
            with connect(settings.database_path) as connection:
                events_count = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
                tokens_total = connection.execute(
                    "SELECT COALESCE(SUM(input_tokens + cache_write_tokens + output_tokens), 0) FROM usage_events"
                ).fetchone()[0]
                agents_count = connection.execute(
                    "SELECT COUNT(*) FROM agent_runs WHERE LOWER(state) IN ('live', 'running')"
                ).fetchone()[0]
            return {
                "ok": True,
                "harnesses": _build_harness_list(settings),
                "eventsCount": int(events_count),
                "tokensTotal": int(tokens_total),
                "liveAgentsCount": int(agents_count),
                "syncResult": sync_res,
            }

        res = await run_in_threadpool(_do_scan)
        return JSONResponse(res, headers={"Cache-Control": "no-store"})

    @router.post("/api/setup/pair-hub")
    async def create_hub_pairing(request: Request):
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
    <meta name="theme-color" content="#0b0d10">
    <title>BURNRATE · Application Setup Wizard</title>
    <link rel="icon" type="image/svg+xml" href="/favicon.svg">
    <style>
      :root {
        color-scheme: dark;
        --bg: #090b0e;
        --surface: #11141a;
        --raised: #171b23;
        --card-hover: #1c212b;
        --border: rgba(255, 255, 255, 0.08);
        --border-strong: rgba(255, 255, 255, 0.16);
        --text: #eef1f6;
        --secondary: #a4adbb;
        --muted: #6d7685;
        --accent: #d9a441;
        --accent-soft: rgba(217, 164, 65, 0.14);
        --cyan: #5cd6e8;
        --green: #63c689;
        --green-soft: rgba(99, 198, 137, 0.12);
        --danger: #ef6461;
        --font-sans: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        --font-mono: "JetBrains Mono", "Cascadia Mono", Consolas, monospace;
      }
      * { box-sizing: border-box; margin: 0; padding: 0; }
      [hidden] { display: none !important; }
      html, body {
        min-height: 100vh;
        background: radial-gradient(circle at 18% 0%, rgba(217, 164, 65, 0.08), transparent 45%),
                    radial-gradient(circle at 85% 10%, rgba(92, 214, 232, 0.06), transparent 45%),
                    var(--bg);
        color: var(--text);
        font-family: var(--font-sans);
        font-size: 14px;
        line-height: 1.5;
      }
      .wizard-shell {
        max-width: 780px;
        margin: 0 auto;
        padding: 20px 24px 36px;
        display: flex;
        flex-direction: column;
        gap: 20px;
        min-height: 100vh;
      }
      .wizard-topbar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding-bottom: 14px;
        border-bottom: 1px solid var(--border);
        cursor: grab;
      }
      .wizard-brand {
        display: flex;
        align-items: center;
        gap: 10px;
      }
      .wizard-brand img {
        width: 30px;
        height: 30px;
        border-radius: 8px;
      }
      .wizard-wordmark {
        font-size: 15px;
        font-weight: 700;
        letter-spacing: 0.08em;
      }
      .wizard-wordmark strong { color: var(--accent); }
      .wizard-sub {
        font-size: 12px;
        color: var(--secondary);
        margin-left: 6px;
        padding-left: 10px;
        border-left: 1px solid var(--border);
      }
      .wizard-skip {
        appearance: none;
        background: transparent;
        border: 1px solid var(--border);
        color: var(--secondary);
        border-radius: 8px;
        padding: 6px 12px;
        font-size: 12px;
        cursor: pointer;
        text-decoration: none;
      }
      .wizard-skip:hover {
        color: var(--text);
        border-color: var(--border-strong);
        background: var(--surface);
      }
      .wizard-stepper {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 10px;
        list-style: none;
      }
      .wizard-stepper li {
        padding: 10px 14px;
        border-radius: 10px;
        background: var(--surface);
        border: 1px solid var(--border);
        display: flex;
        align-items: center;
        gap: 10px;
        font-size: 13px;
        color: var(--muted);
        cursor: pointer;
        transition: all 0.15s;
      }
      .wizard-stepper li span.num {
        width: 22px;
        height: 22px;
        border-radius: 50%;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        font-family: var(--font-mono);
        font-size: 11px;
        background: var(--raised);
        color: var(--secondary);
        flex-shrink: 0;
      }
      .wizard-stepper li.active {
        border-color: var(--accent);
        background: var(--accent-soft);
        color: var(--text);
        font-weight: 600;
      }
      .wizard-stepper li.active span.num {
        background: var(--accent);
        color: #090b0e;
        font-weight: 700;
      }
      .wizard-stepper li.done {
        border-color: rgba(99, 198, 137, 0.35);
        color: var(--secondary);
      }
      .wizard-stepper li.done span.num {
        background: var(--green-soft);
        color: var(--green);
      }
      .wizard-card {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 16px;
        padding: 26px 28px;
        display: flex;
        flex-direction: column;
        gap: 20px;
        box-shadow: 0 18px 42px rgba(0, 0, 0, 0.38);
      }
      .step-eyebrow {
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.09em;
        color: var(--accent);
        font-weight: 600;
      }
      .step-head h1 {
        font-size: 22px;
        font-weight: 600;
        margin-top: 3px;
      }
      .step-head p {
        color: var(--secondary);
        font-size: 13.5px;
        margin-top: 5px;
      }
      .auth-tabs {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 8px;
        background: var(--raised);
        padding: 5px;
        border-radius: 11px;
        border: 1px solid var(--border);
      }
      .auth-tab {
        appearance: none;
        background: transparent;
        border: 1px solid transparent;
        border-radius: 8px;
        padding: 9px 14px;
        color: var(--secondary);
        font-size: 13px;
        font-weight: 600;
        cursor: pointer;
        transition: all 0.15s;
      }
      .auth-tab.active {
        background: var(--accent-soft);
        border-color: var(--accent);
        color: var(--text);
      }
      .form-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 12px;
      }
      .field-group {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .field-group label {
        font-size: 12px;
        font-weight: 600;
        color: var(--secondary);
      }
      .text-input {
        background: var(--raised);
        border: 1px solid var(--border-strong);
        color: var(--text);
        border-radius: 9px;
        padding: 10px 12px;
        font-size: 14px;
        font-family: var(--font-sans);
        outline: none;
      }
      .text-input:focus {
        border-color: var(--accent);
      }
      .mono-input {
        font-family: var(--font-mono);
        font-size: 12.5px;
      }
      .sub-box {
        background: var(--raised);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 16px;
        display: flex;
        flex-direction: column;
        gap: 12px;
      }
      .sub-box h3 {
        font-size: 13px;
        font-weight: 600;
        color: var(--text);
      }
      .inline-row {
        display: flex;
        gap: 8px;
        align-items: center;
        flex-wrap: wrap;
      }
      .inline-row .text-input {
        flex: 1 1 180px;
      }
      .btn {
        appearance: none;
        border: 1px solid var(--border-strong);
        background: var(--raised);
        color: var(--text);
        border-radius: 9px;
        padding: 9px 15px;
        font-size: 13px;
        font-weight: 600;
        cursor: pointer;
        transition: all 0.15s;
        display: inline-flex;
        align-items: center;
        gap: 6px;
      }
      .btn:hover:not(:disabled) {
        background: var(--card-hover);
        border-color: var(--text);
      }
      .btn:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }
      .btn-primary {
        background: var(--accent);
        border-color: var(--accent);
        color: #090b0e;
      }
      .btn-primary:hover:not(:disabled) {
        background: #e5b252;
        border-color: #e5b252;
      }
      .harness-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 10px;
      }
      .harness-item {
        background: var(--raised);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 11px 13px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
      }
      .harness-item.detected {
        border-color: rgba(99, 198, 137, 0.38);
      }
      .harness-item strong {
        display: block;
        font-size: 13px;
      }
      .harness-item small {
        display: block;
        font-size: 11px;
        color: var(--secondary);
        font-family: var(--font-mono);
      }
      .status-chip {
        font-family: var(--font-mono);
        font-size: 10.5px;
        padding: 3px 8px;
        border-radius: 999px;
        background: rgba(255, 255, 255, 0.06);
        color: var(--muted);
        white-space: nowrap;
      }
      .status-chip.good {
        background: var(--green-soft);
        color: var(--green);
      }
      .presets-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 10px;
      }
      .preset-card {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 11px 13px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        cursor: pointer;
        color: var(--text);
        text-align: left;
      }
      .preset-card.selected {
        border-color: var(--accent);
        background: var(--accent-soft);
      }
      .preset-card strong {
        display: block;
        font-size: 13px;
      }
      .preset-card small {
        font-size: 11px;
        color: var(--secondary);
      }
      .preset-price {
        font-family: var(--font-mono);
        font-size: 12.5px;
        font-weight: 600;
        color: var(--accent);
      }
      .wizard-footer {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding-top: 10px;
        border-top: 1px solid var(--border);
      }
      .status-banner {
        padding: 10px 12px;
        border-radius: 9px;
        font-size: 12.5px;
        background: var(--raised);
        border: 1px solid var(--border);
        color: var(--secondary);
      }
      .status-banner.good {
        border-color: rgba(99, 198, 137, 0.4);
        color: var(--green);
        background: var(--green-soft);
      }
      .status-banner.err {
        border-color: rgba(239, 100, 97, 0.4);
        color: var(--danger);
      }
      details.advanced-server {
        font-size: 12px;
        color: var(--muted);
      }
      details.advanced-server summary {
        cursor: pointer;
        user-select: none;
      }
      @media (max-width: 720px) {
        .wizard-stepper, .form-grid, .harness-grid, .presets-grid {
          grid-template-columns: 1fr;
        }
      }
    </style>
  </head>
  <body>
    <main class="wizard-shell" id="wizard-shell">
      <header class="wizard-topbar" data-tauri-drag-region>
        <div class="wizard-brand" data-tauri-drag-region>
          <img src="/favicon.svg" alt="" width="30" height="30">
          <span class="wizard-wordmark" data-tauri-drag-region>BURN<strong>RATE</strong></span>
          <span class="wizard-sub">Application Setup Wizard</span>
        </div>
        <a href="/" class="wizard-skip" id="wizard-skip-link">Skip to Dashboard →</a>
      </header>

      <ol class="wizard-stepper" id="wizard-stepper" aria-label="Setup steps">
        <li data-step="1" class="active"><span class="num">1</span><span>Account Login</span></li>
        <li data-step="2"><span class="num">2</span><span>Installed Providers</span></li>
      </ol>

      <section class="wizard-card">
        <!-- STEP 1: ACCOUNT LOGIN / CREATE ACCOUNT -->
        <div id="wiz-step-1" class="wiz-pane">
          <div class="step-head">
            <p class="step-eyebrow">Step 1 of 2 · Central Account (tk-server)</p>
            <h1 id="wiz-auth-heading">Sign in to your BURNRATE account</h1>
            <p id="wiz-auth-sub">Log in once on any computer. Your central server on <code>tk-server</code> automatically aggregates usage, live agents, and subscriptions across every computer on your account.</p>
          </div>

          <div class="auth-tabs" role="tablist" aria-label="Account mode" style="margin-top: 14px;">
            <button type="button" class="auth-tab active" data-auth-mode="login" id="wiz-tab-login">Sign In</button>
            <button type="button" class="auth-tab" data-auth-mode="register" id="wiz-tab-register">Create Account &amp; Subscriptions</button>
          </div>

          <div class="form-grid" style="margin-top: 14px;">
            <div class="field-group">
              <label for="wiz-username">Account username or email</label>
              <input id="wiz-username" class="text-input" autocomplete="username" placeholder="e.g. kamol">
            </div>
            <div class="field-group">
              <label for="wiz-password">Password</label>
              <input id="wiz-password" type="password" class="text-input" autocomplete="current-password" placeholder="••••••••">
            </div>
          </div>

          <div class="field-group" style="margin-top: 12px;">
            <label for="wiz-computer-name">This computer's name (shown in dashboard filters &amp; live agents)</label>
            <input id="wiz-computer-name" class="text-input" maxlength="64" placeholder="e.g. MyDesktop, MacBook Pro">
          </div>

          <!-- SUBSCRIPTIONS PICKER (SHOWN WHEN CREATING AN ACCOUNT) -->
          <div id="wiz-register-subs" class="sub-box" style="margin-top: 14px;" hidden>
            <div>
              <h3>Set up your active AI subscriptions</h3>
              <p style="font-size: 12px; color: var(--secondary); margin-top: 2px;">Saved to your account on <code>tk-server</code> so plan ROI is tracked across all your computers.</p>
            </div>
            <div class="presets-grid" id="wiz-presets-grid"></div>
            <div class="inline-row" style="margin-top: 4px;">
              <select id="wiz-custom-tool" class="text-input" style="max-width: 170px;">
                <option value="claude-code">Claude Code</option>
                <option value="codex">Codex / OpenAI</option>
                <option value="grok">SuperGrok / Cursor (xAI)</option>
                <option value="opencode">Z.AI / OpenCode</option>
                <option value="openrouter">OpenRouter</option>
                <option value="custom">Custom</option>
              </select>
              <input id="wiz-custom-name" class="text-input" placeholder="Custom plan name (optional)">
              <input id="wiz-custom-amount" type="number" min="0" step="0.01" class="text-input mono-input" placeholder="USD/mo" style="max-width: 120px;">
              <button type="button" class="btn" id="wiz-add-custom-plan-btn">Add Plan</button>
            </div>
            <div id="wiz-existing-plans" style="font-size: 12px; color: var(--secondary);"></div>
          </div>

          <div id="wiz-auth-status" class="status-banner" style="margin-top: 14px;" hidden></div>

          <details class="advanced-server" style="margin-top: 10px;">
            <summary>Account server: <code id="wiz-server-label">tk-server (100.71.45.59)</code></summary>
            <div class="field-group" style="margin-top: 8px;">
              <label for="wiz-server-url">Central Account Server URL</label>
              <input id="wiz-server-url" class="text-input mono-input" placeholder="https://ai-harness-vps.tail982d0e.ts.net:8449">
            </div>
          </details>
        </div>

        <!-- STEP 2: CONNECT INSTALLED PROVIDERS -->
        <div id="wiz-step-2" class="wiz-pane" hidden>
          <div class="step-head">
            <p class="step-eyebrow">Step 2 of 2 · Installed Providers</p>
            <h1>Connect installed AI providers on this computer</h1>
            <p>BURNRATE automatically detects the AI coding tools installed on this computer and streams their usage and live quotas to your account.</p>
          </div>

          <div class="inline-row" style="margin-top: 12px; justify-content: space-between;">
            <span id="wiz-scan-summary" class="status-banner">Checking installed AI providers…</span>
            <button type="button" class="btn btn-primary" id="wiz-scan-btn">Reconnect &amp; Sync Installed Providers</button>
          </div>

          <div class="harness-grid" id="wiz-harness-grid" style="margin-top: 12px;"></div>

          <div class="sub-box" style="margin-top: 14px;">
            <label style="display: flex; align-items: center; gap: 10px; cursor: pointer;">
              <input type="checkbox" id="wiz-hud-auto" checked style="width: 17px; height: 17px;">
              <span><strong>Launch Sidecar HUD automatically</strong> — Keep a compact live burn-rate &amp; quota meter pinned on your screen</span>
            </label>
            <input type="checkbox" id="wiz-hud-top" checked hidden>
          </div>

          <div id="wiz-finish-banner" class="status-banner good" style="margin-top: 12px;">
            Account connected. Installed providers on this computer will sync automatically in the background.
          </div>
        </div>

        <footer class="wizard-footer">
          <button type="button" class="btn" id="wiz-back-btn" disabled>← Back</button>
          <div class="inline-row">
            <button type="button" class="btn btn-primary" id="wiz-next-btn">Sign In &amp; Continue →</button>
          </div>
        </footer>
      </section>
    </main>

    <script>
      (() => {
        const $ = id => document.getElementById(id);
        let step = 1;
        let authMode = "login";
        let stateData = null;
        let accountConnected = false;
        const selectedPresets = new Set();
        const customPlans = [];

        async function api(url, method = "GET", body = null) {
          const opts = {method, cache: "no-store", headers: {}};
          if (body != null) {
            opts.headers["Content-Type"] = "application/json";
            opts.body = JSON.stringify(body);
          }
          const res = await fetch(url, opts);
          const data = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
          return data;
        }

        function setAuthMode(nextMode) {
          authMode = nextMode === "register" ? "register" : "login";
          $("wiz-tab-login").classList.toggle("active", authMode === "login");
          $("wiz-tab-register").classList.toggle("active", authMode === "register");
          $("wiz-register-subs").hidden = authMode !== "register";
          $("wiz-auth-heading").textContent = authMode === "register"
            ? "Create your account & subscriptions"
            : "Sign in to your BURNRATE account";
          if (step === 1) {
            $("wiz-next-btn").textContent = authMode === "register"
              ? "Create Account & Continue →"
              : "Sign In & Continue →";
          }
        }

        function setStep(next) {
          step = Math.max(1, Math.min(2, next));
          $("wiz-step-1").hidden = step !== 1;
          $("wiz-step-2").hidden = step !== 2;
          document.querySelectorAll("#wizard-stepper li").forEach(li => {
            const s = Number(li.dataset.step);
            li.classList.toggle("active", s === step);
            li.classList.toggle("done", s < step);
          });
          $("wiz-back-btn").disabled = step === 1;
          if (step === 1) {
            $("wiz-next-btn").textContent = authMode === "register"
              ? "Create Account & Continue →"
              : "Sign In & Continue →";
          } else {
            $("wiz-next-btn").textContent = "Finish Setup & Launch BURNRATE";
          }
        }

        function renderHarnesses(harnesses, stats) {
          const grid = $("wiz-harness-grid");
          const list = harnesses || [];
          const detectedCount = list.filter(h => h.detected).length;
          $("wiz-scan-summary").textContent =
            `${detectedCount} installed AI provider${detectedCount === 1 ? "" : "s"} connected · ${stats?.eventsCount ?? 0} usage records · ${stats?.liveAgentsCount ?? 0} live agents`;
          $("wiz-scan-summary").className = "status-banner" + (detectedCount > 0 ? " good" : "");

          grid.replaceChildren(...list.map(h => {
            const card = document.createElement("div");
            card.className = "harness-item" + (h.detected ? " detected" : "");
            const left = document.createElement("div");
            const title = document.createElement("strong");
            title.textContent = h.name;
            const sub = document.createElement("small");
            sub.textContent = h.path || h.description;
            left.append(title, sub);
            const chip = document.createElement("span");
            chip.className = "status-chip" + (h.detected ? " good" : "");
            chip.textContent = h.detected ? "Connected" : "Not installed";
            card.append(left, chip);
            return card;
          }));
        }

        function renderPresets(presets, existing) {
          const grid = $("wiz-presets-grid");
          const existingNames = new Set((existing || []).map(p => (p.tool_key + ":" + String(p.name).toLowerCase())));
          grid.replaceChildren(...(presets || []).map(p => {
            const already = existingNames.has(p.tool_key + ":" + p.name.toLowerCase());
            const on = already || selectedPresets.has(p.key);
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "preset-card" + (on ? " selected" : "");
            const left = document.createElement("div");
            const title = document.createElement("strong");
            title.textContent = p.name;
            const sub = document.createElement("small");
            sub.textContent = already ? `${p.badge} · Active on account` : `${p.badge} · Click to include`;
            left.append(title, sub);
            const price = document.createElement("span");
            price.className = "preset-price";
            price.textContent = "$" + p.amount_usd + "/mo";
            btn.append(left, price);
            btn.addEventListener("click", () => {
              if (already) return;
              if (selectedPresets.has(p.key)) selectedPresets.delete(p.key);
              else selectedPresets.add(p.key);
              renderPresets(presets, existing);
            });
            return btn;
          }));
          const allPlans = [...(existing || []), ...customPlans];
          $("wiz-existing-plans").textContent = allPlans.length
            ? "Account plans: " + allPlans.map(p => `${p.name} ($${p.amount_usd}/mo)`).join(" · ")
            : "Select your monthly plans above.";
        }

        async function runProviderScan() {
          const btn = $("wiz-scan-btn");
          btn.disabled = true;
          btn.textContent = "Scanning & Syncing…";
          try {
            const res = await api("/api/setup/scan", "POST", {});
            renderHarnesses(res.harnesses, {eventsCount: res.eventsCount, liveAgentsCount: res.liveAgentsCount});
          } catch (err) {
            $("wiz-scan-summary").textContent = "Scan error: " + err.message;
          } finally {
            btn.disabled = false;
            btn.textContent = "Reconnect & Sync Installed Providers";
          }
        }

        async function loadInitialState() {
          try {
            stateData = await api("/api/setup/state");
            const s = stateData.setup || {};
            $("wiz-computer-name").value = s.computerName || "";
            if (s.account?.username) {
              $("wiz-username").value = s.account.username;
            }
            const serverUrl = s.account?.serverUrl || stateData.centralServerUrl || "";
            $("wiz-server-url").value = serverUrl;
            if (s.spokeConfig?.hasToken && s.account?.username) {
              accountConnected = true;
              const status = $("wiz-auth-status");
              status.hidden = false;
              status.className = "status-banner good";
              status.textContent = `Signed in as ${s.account.username} on ${s.computerName || "this computer"}.`;
            }
            $("wiz-hud-auto").checked = s.hudConfig?.autoOpen !== false;
            $("wiz-hud-top").checked = s.hudConfig?.alwaysOnTop !== false;

            renderHarnesses(stateData.harnesses, stateData.stats);
            renderPresets(stateData.presets, stateData.subscriptions);
          } catch (err) {
            $("wiz-scan-summary").textContent = "Could not load setup metadata: " + err.message;
          }
        }

        $("wiz-tab-login").addEventListener("click", () => setAuthMode("login"));
        $("wiz-tab-register").addEventListener("click", () => setAuthMode("register"));

        document.querySelectorAll("#wizard-stepper li").forEach(li => {
          li.addEventListener("click", () => {
            const target = Number(li.dataset.step);
            if (target === 1 || accountConnected) setStep(target);
          });
        });

        $("wiz-scan-btn").addEventListener("click", () => runProviderScan());

        $("wiz-add-custom-plan-btn").addEventListener("click", () => {
          const tool_key = $("wiz-custom-tool").value;
          const name = $("wiz-custom-name").value.trim();
          const amount_usd = Number($("wiz-custom-amount").value);
          if (!name || !Number.isFinite(amount_usd) || amount_usd <= 0) return;
          customPlans.push({tool_key, name, amount_usd, cadence: "monthly"});
          $("wiz-custom-name").value = "";
          $("wiz-custom-amount").value = "";
          renderPresets(stateData?.presets || [], stateData?.subscriptions || []);
        });

        $("wiz-back-btn").addEventListener("click", () => setStep(step - 1));

        $("wiz-next-btn").addEventListener("click", async () => {
          const btn = $("wiz-next-btn");
          if (step === 1) {
            const username = $("wiz-username").value.trim();
            const password = $("wiz-password").value;
            const computerName = $("wiz-computer-name").value.trim();
            const serverUrl = $("wiz-server-url").value.trim();
            const status = $("wiz-auth-status");

            if (!username || !password) {
              status.hidden = false;
              status.className = "status-banner err";
              status.textContent = "Please enter your account username and password.";
              return;
            }

            btn.disabled = true;
            btn.textContent = authMode === "register" ? "Creating Account…" : "Signing In…";
            status.hidden = false;
            status.className = "status-banner";
            status.textContent = "Connecting to central account server (tk-server)…";
            try {
              const res = await api("/api/setup/account", "POST", {
                mode: authMode,
                username,
                password,
                computerName,
                serverUrl,
                presetKeys: [...selectedPresets],
                customPlans,
                testSync: false
              });
              accountConnected = true;
              status.className = "status-banner good";
              status.textContent = `Connected as ${res.account?.username || username}!`;
              setStep(2);
              runProviderScan();
            } catch (err) {
              status.className = "status-banner err";
              status.textContent = err.message;
              btn.disabled = false;
              setStep(1);
            } finally {
              btn.disabled = false;
            }
            return;
          }

          btn.disabled = true;
          btn.textContent = "Finishing & Launching…";
          try {
            const autoHud = $("wiz-hud-auto").checked;
            const topHud = $("wiz-hud-top").checked;
            await api("/api/setup/complete", "POST", {
              computerName: $("wiz-computer-name").value.trim(),
              role: accountConnected ? "spoke" : "hub",
              presetKeys: [...selectedPresets],
              customPlans,
              hudConfig: {autoOpen: autoHud, alwaysOnTop: topHud, dock: "top-left"}
            });
            const internals = window.__TAURI_INTERNALS__;
            if (internals && typeof internals.invoke === "function") {
              if (autoHud) {
                try { await internals.invoke("show_hud"); } catch {}
              } else {
                try { await internals.invoke("hide_hud"); } catch {}
              }
            } else if (autoHud) {
              try {
                const nat = await fetch("/api/hud/native", {
                  method: "POST",
                  headers: {"Content-Type": "application/json"},
                  body: JSON.stringify({action: topHud ? "pin" : "open"})
                }).then(r => r.json()).catch(() => null);
                if (!nat || !nat.ok) {
                  window.open("/hud", "burnrate-hud", "popup=yes,width=440,height=580,left=24,top=72");
                }
              } catch {}
            }
            window.location.href = "/";
          } catch (err) {
            $("wiz-finish-banner").className = "status-banner err";
            $("wiz-finish-banner").textContent = "Could not finish setup: " + err.message;
            btn.disabled = false;
            btn.textContent = "Finish Setup & Launch BURNRATE";
          }
        });

        loadInitialState();
      })();
    </script>
  </body>
</html>
"""


def render_setup_wizard() -> str:
    return SETUP_WIZARD_HTML
