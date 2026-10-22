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
    {"key": "opencode_go", "tool_key": "opencode-go", "name": "OpenCode Go", "amount_usd": 10.0, "cadence": "monthly", "badge": "OpenCode"},
    {"key": "opencode_go_plus", "tool_key": "opencode-go", "name": "OpenCode Go Plus", "amount_usd": 40.0, "cadence": "monthly", "badge": "OpenCode"},
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

      @media (max-width: 760px) {
        .wizard-shell {
          grid-template-columns: minmax(0, 1fr);
          grid-template-areas: "top" "rail" "card";
          row-gap: 20px;
          padding: 16px 16px 28px;
        }
        .wizard-rail { position: static; }
        .wizard-stepper { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); column-gap: 14px; }
        .wizard-stepper li { grid-template-columns: auto minmax(0, 1fr); padding-top: 14px; padding-bottom: 0; }
        .wizard-stepper small { display: none; }
        .wizard-card { padding: 22px 18px 20px; gap: 22px; }
        .auth-tab { min-height: 44px; padding: 6px 8px; font-size: 13px; line-height: 1.2; }
        .scan-row > .btn { width: 100%; }
        .step-head h1 { font-size: 24px; }
        .form-grid, .harness-grid, .presets-grid { grid-template-columns: minmax(0, 1fr); }
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
          <a href="/" class="btn" id="wizard-skip-link"><span><span class="skip-lead">Skip to </span>__SKIP_TARGET__ →</span></a>
        </div>
      </header>

      <aside class="wizard-rail" aria-label="Progress">
        <p class="eyebrow">Progress</p>
        <ol class="wizard-stepper" id="wizard-stepper" aria-label="Setup steps">
          <li data-step="1" class="active"><span class="num">01</span><span class="step-text"><strong>Account login</strong><small>Central account on tk-server</small></span></li>
          <li data-step="2"><span class="num">02</span><span class="step-text"><strong>Installed providers</strong><small>AI tools on this computer</small></span></li>
        </ol>
      </aside>

      <section class="wizard-card">
        <!-- STEP 1: ACCOUNT LOGIN / CREATE ACCOUNT -->
        <div id="wiz-step-1" class="wiz-pane">
          <div class="step-head">
            <p class="eyebrow"><span class="n">01</span>Central account · tk-server</p>
            <h1 id="wiz-auth-heading">Sign in to your BURNRATE account</h1>
            <p class="lede" id="wiz-auth-sub">Log in once on any computer. Your central server on <code>tk-server</code> automatically aggregates usage, live agents, and subscriptions across every computer on your account.</p>
          </div>

          <div class="auth-tabs" role="tablist" aria-label="Account mode">
            <button type="button" class="auth-tab active" data-auth-mode="login" id="wiz-tab-login">Sign In</button>
            <button type="button" class="auth-tab" data-auth-mode="register" id="wiz-tab-register">Create Account &amp; Subscriptions</button>
          </div>

          <div class="form-grid">
            <div class="field-group">
              <label for="wiz-username">Account username or email</label>
              <input id="wiz-username" class="text-input" autocomplete="username" placeholder="e.g. kamol">
            </div>
            <div class="field-group">
              <label for="wiz-password">Password</label>
              <input id="wiz-password" type="password" class="text-input" autocomplete="current-password" placeholder="••••••••">
            </div>
          </div>

          <div class="field-group">
            <label for="wiz-computer-name">This computer's name</label>
            <input id="wiz-computer-name" class="text-input" maxlength="64" placeholder="e.g. MyDesktop, MacBook Pro">
            <span class="hint">Shown in dashboard filters and live agents.</span>
          </div>

          <!-- SUBSCRIPTIONS PICKER (SHOWN WHEN CREATING AN ACCOUNT) -->
          <div id="wiz-register-subs" class="sub-box" hidden>
            <div>
              <h3>Set up your active AI subscriptions</h3>
              <p class="hint">Saved to your account on <code>tk-server</code> so plan ROI is tracked across all your computers.</p>
            </div>
            <div class="presets-grid" id="wiz-presets-grid"></div>
            <div class="inline-row">
              <select id="wiz-custom-tool" class="text-input select-sm">
                <option value="claude-code">Claude Code</option>
                <option value="codex">Codex / OpenAI</option>
                <option value="grok">SuperGrok (xAI)</option>
                <option value="opencode">Z.AI / OpenCode</option>
                <option value="opencode-go">OpenCode Go</option>
                <option value="openrouter">OpenRouter</option>
                <option value="custom">Custom</option>
              </select>
              <input id="wiz-custom-name" class="text-input" placeholder="Custom plan name (optional)">
              <input id="wiz-custom-amount" type="number" min="0" step="0.01" class="text-input amount-input" placeholder="USD/mo">
              <button type="button" class="btn btn-sm" id="wiz-add-custom-plan-btn">Add Plan</button>
            </div>
            <div id="wiz-existing-plans" class="plans-line"></div>
          </div>

          <div id="wiz-auth-status" class="status-banner" hidden></div>

          <details class="advanced-server">
            <summary>Account server: <code id="wiz-server-label">tk-server (100.71.45.59)</code></summary>
            <div class="field-group">
              <label for="wiz-server-url">Central account server URL</label>
              <input id="wiz-server-url" class="text-input mono" placeholder="https://ai-harness-vps.tail982d0e.ts.net:8449">
            </div>
          </details>
        </div>

        <!-- STEP 2: CONNECT INSTALLED PROVIDERS -->
        <div id="wiz-step-2" class="wiz-pane" hidden>
          <div class="step-head">
            <p class="eyebrow"><span class="n">02</span>Installed providers · this computer</p>
            <h1>Connect installed AI providers on this computer</h1>
            <p class="lede">BURNRATE automatically detects the AI coding tools installed on this computer and streams their usage and live quotas to your account.</p>
          </div>

          <div class="scan-row">
            <span id="wiz-scan-summary" class="status-banner">Checking installed AI providers…</span>
            <button type="button" class="btn btn-primary" id="wiz-scan-btn">Reconnect &amp; Sync Installed Providers</button>
          </div>

          <div class="harness-grid" id="wiz-harness-grid"></div>

          <div class="sub-box">
            <label class="check-row">
              <input type="checkbox" id="wiz-hud-auto" checked>
              <span><strong>Launch Sidecar HUD automatically</strong><small>Keep a compact live burn-rate &amp; quota meter pinned on your screen</small></span>
            </label>
            <input type="checkbox" id="wiz-hud-top" checked hidden>
          </div>

          <div id="wiz-finish-banner" class="status-banner good">
            Account connected. Installed providers on this computer will sync automatically in the background.
          </div>
        </div>

        <footer class="wizard-footer">
          <button type="button" class="btn" id="wiz-back-btn" disabled>← Back</button>
          <button type="button" class="btn btn-primary" id="wiz-next-btn">Sign In &amp; Continue →</button>
        </footer>
      </section>
    </main>

    <script>
      (() => {
        const $ = id => document.getElementById(id);
        // The desktop app starts with this window hidden; show it for setup.
        try { window.__TAURI_INTERNALS__?.invoke?.("show_dashboard")?.catch?.(() => {}); } catch {}
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

        // Provider marks come from the shared sprite served at /logos.svg.
        const TOOL_LOGOS = {
          codex: "codex", "claude-code": "claude", opencode: "opencode", zcode: "zai",
          grok: "grok", antigravity: "antigravity", muse: "meta", openrouter: "openrouter", traycer: "traycer", "opencode-go": "opencode"
        };
        const logoFor = (key, name) => /z[.]?ai|glm/i.test(String(name || "")) ? "zai" : (TOOL_LOGOS[key] || "custom");
        function logoTile(id) {
          const tile = document.createElement("span");
          tile.className = "logo-tile";
          tile.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><use href="/logos.svg#' + id + '"></use></svg>';
          return tile;
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
            card.append(logoTile(logoFor(h.tool, h.name)), left, chip);
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
            btn.append(logoTile(logoFor(p.tool_key, p.name)), left, price);
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

        $("wiz-theme-toggle").addEventListener("click", () => {
          const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
          document.documentElement.dataset.theme = next;
          try { localStorage.setItem("burnrate:theme", next); } catch {}
          document.querySelector('meta[name="theme-color"]').content = next === "dark" ? "#08080a" : "#ebe9e4";
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
    from spend_app.config import dashboard_enabled

    return SETUP_WIZARD_HTML.replace("__SKIP_TARGET__", "Dashboard" if dashboard_enabled() else "HUD", 1)
