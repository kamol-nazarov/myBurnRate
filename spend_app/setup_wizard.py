"""Interactive Application Setup Wizard & Multi-Computer Desktop Orchestration."""

from __future__ import annotations

import base64
import json
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
from spend_app.plan_service import effective_terms
from spend_app.pricing import PricingEngine
from spend_app.subscriptions import add_subscription, materialize_subscription_days
from spend_app.sync import (
    STREAM_BROKER,
    list_machines,
    register_machine,
    rename_machine,
    sync_once,
)


SETUP_META_KEY = "app_setup.v1"
PAIRING_PREFIX = "brpair_"

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
}

SUBSCRIPTION_PRESETS = (
    {"key": "claude_max", "tool_key": "claude-code", "name": "Claude Max", "amount_usd": 100.0, "cadence": "monthly", "badge": "Anthropic"},
    {"key": "claude_pro", "tool_key": "claude-code", "name": "Claude Pro", "amount_usd": 20.0, "cadence": "monthly", "badge": "Anthropic"},
    {"key": "codex_pro", "tool_key": "codex", "name": "ChatGPT Pro", "amount_usd": 200.0, "cadence": "monthly", "badge": "OpenAI"},
    {"key": "codex_plus", "tool_key": "codex", "name": "ChatGPT Plus", "amount_usd": 20.0, "cadence": "monthly", "badge": "OpenAI"},
    {"key": "cursor_pro", "tool_key": "cursor", "name": "Cursor Pro", "amount_usd": 20.0, "cadence": "monthly", "badge": "Cursor"},
    {"key": "cursor_ultra", "tool_key": "cursor", "name": "Cursor Ultra", "amount_usd": 200.0, "cadence": "monthly", "badge": "Cursor"},
    {"key": "grok_super", "tool_key": "grok", "name": "SuperGrok", "amount_usd": 30.0, "cadence": "monthly", "badge": "xAI"},
    {"key": "zai_coding", "tool_key": "opencode", "name": "Z.AI Coding Plan", "amount_usd": 10.0, "cadence": "monthly", "badge": "Z.AI"},
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
    hub_url = str(spoke.get("hubUrl") or "").strip()
    token = str(spoke.get("token") or "").strip()
    if not hub_url or not token:
        return None
    try:
        result = sync_once(
            settings,
            pricing,
            hub_url=hub_url,
            token=token,
            machine_name=state.get("computerName") or _default_computer_name(),
            collect_local=False,
            client=client,
        )
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

    @router.post("/api/setup/scan")
    async def scan_and_import():
        from starlette.concurrency import run_in_threadpool

        def _do_scan() -> dict:
            initialize(settings.database_path)
            from spend_app.providers import REGISTRY
            from spend_app.quotas import poll_activity
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
            role = str(body.get("role") or "hub").strip().lower()
            if role not in {"hub", "spoke", "standalone"}:
                raise ValueError("Role must be 'hub', 'spoke', or 'standalone'.")

            preset_keys = [str(k) for k in (body.get("presetKeys") or []) if k]
            custom_plans = body.get("customPlans") if isinstance(body.get("customPlans"), list) else []
            hud_cfg = body.get("hudConfig") if isinstance(body.get("hudConfig"), dict) else {}

            now_local = datetime.now(UTC).astimezone(ZoneInfo(settings.timezone)).date()
            month_first = date(now_local.year, now_local.month, 1).isoformat()

            added_plans = 0
            with connect(settings.database_path) as connection:
                existing = effective_terms(connection)
                existing_pairs = {(row["tool_key"], row["name"].lower()) for row in existing}
                preset_map = {p["key"]: p for p in SUBSCRIPTION_PRESETS}
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

                state = read_setup_state(connection)
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

            rename_machine(settings.database_path, machine_id="local", name=computer_name)
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
        max-width: 880px;
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
        grid-template-columns: repeat(5, minmax(0, 1fr));
        gap: 8px;
        list-style: none;
      }
      .wizard-stepper li {
        padding: 9px 11px;
        border-radius: 10px;
        background: var(--surface);
        border: 1px solid var(--border);
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 12px;
        color: var(--muted);
        cursor: pointer;
        transition: all 0.15s;
      }
      .wizard-stepper li span.num {
        width: 20px;
        height: 20px;
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
      .role-grid {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 12px;
      }
      .role-option {
        background: var(--raised);
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 15px;
        text-align: left;
        cursor: pointer;
        display: flex;
        flex-direction: column;
        gap: 6px;
        color: var(--text);
        transition: all 0.15s;
      }
      .role-option:hover {
        background: var(--card-hover);
        border-color: var(--border-strong);
      }
      .role-option.selected {
        border-color: var(--accent);
        background: var(--accent-soft);
      }
      .role-option strong {
        font-size: 14px;
        display: flex;
        align-items: center;
        justify-content: space-between;
      }
      .role-option small {
        font-size: 12px;
        color: var(--secondary);
        line-height: 1.45;
      }
      .role-tag {
        font-family: var(--font-mono);
        font-size: 10px;
        padding: 2px 6px;
        border-radius: 999px;
        background: rgba(255, 255, 255, 0.07);
        color: var(--secondary);
      }
      .role-option.selected .role-tag {
        background: var(--accent);
        color: #090b0e;
        font-weight: 700;
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
        flex: 1 1 200px;
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
      .pairing-output {
        background: #0b0d11;
        border: 1px solid rgba(99, 198, 137, 0.35);
        border-radius: 10px;
        padding: 12px;
        font-family: var(--font-mono);
        font-size: 11.5px;
        word-break: break-all;
        color: var(--green);
        display: flex;
        flex-direction: column;
        gap: 8px;
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
        background: var(--raised);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 12px 14px;
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
        font-size: 13.5px;
      }
      .preset-card small {
        font-size: 11px;
        color: var(--secondary);
      }
      .preset-price {
        font-family: var(--font-mono);
        font-size: 13px;
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
      @media (max-width: 720px) {
        .wizard-stepper, .role-grid, .harness-grid, .presets-grid {
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
        <li data-step="1" class="active"><span class="num">1</span><span>Computer Identity</span></li>
        <li data-step="2"><span class="num">2</span><span>Multi-Computer Sync</span></li>
        <li data-step="3"><span class="num">3</span><span>AI Harnesses</span></li>
        <li data-step="4"><span class="num">4</span><span>Subscriptions</span></li>
        <li data-step="5"><span class="num">5</span><span>Sidecar HUD & Launch</span></li>
      </ol>

      <section class="wizard-card">
        <!-- STEP 1: IDENTITY -->
        <div id="wiz-step-1" class="wiz-pane">
          <div class="step-head">
            <p class="step-eyebrow">Step 1 of 5 · Welcome</p>
            <h1>Name this computer</h1>
            <p>BURNRATE tracks real-time AI coding token spend, cache efficiency, and live agents across all your computers with zero cloud lock-in.</p>
          </div>
          <div class="field-group" style="margin-top: 14px;">
            <label for="wiz-computer-name">Computer display name (shown in live agent activity & computer filters)</label>
            <input id="wiz-computer-name" class="text-input" maxlength="64" placeholder="e.g. Kamol Workstation, MacBook Pro, Studio Desktop">
          </div>
          <div class="sub-box" style="margin-top: 14px;">
            <h3>Detected Environment</h3>
            <p style="font-size: 12.5px; color: var(--secondary);">
              Platform: <strong class="mono-input" id="wiz-os-label">—</strong> ·
              Timezone: <strong class="mono-input" id="wiz-tz-label">—</strong> ·
              Storage: <strong class="mono-input">Local SQLite WAL</strong>
            </p>
          </div>
        </div>

        <!-- STEP 2: MULTI-COMPUTER ROLE -->
        <div id="wiz-step-2" class="wiz-pane" hidden>
          <div class="step-head">
            <p class="step-eyebrow">Step 2 of 5 · Multi-Computer Architecture</p>
            <h1>How should this computer connect?</h1>
            <p>Run one computer as your self-hosted Hub and pair your other laptops or workstations in seconds using a Pairing Code.</p>
          </div>

          <div class="role-grid" style="margin-top: 14px;" role="radiogroup" aria-label="Computer role">
            <button type="button" class="role-option selected" data-role="hub">
              <strong><span>Primary Hub</span><span class="role-tag">Recommended</span></strong>
              <small>This computer hosts your primary dashboard and receives live usage from your other computers over Tailscale or LAN.</small>
            </button>
            <button type="button" class="role-option" data-role="spoke">
              <strong><span>Connected Computer</span><span class="role-tag">Spoke</span></strong>
              <small>Streams this computer's local AI usage and running agents in real time to your Primary Hub.</small>
            </button>
            <button type="button" class="role-option" data-role="standalone">
              <strong><span>Standalone</span><span class="role-tag">Local only</span></strong>
              <small>Tracks AI coding usage on this computer only with no multi-computer pairing.</small>
            </button>
          </div>

          <!-- HUB PAIRING GENERATOR -->
          <div id="wiz-hub-box" class="sub-box" style="margin-top: 14px;">
            <h3>Pair another computer with this Hub</h3>
            <p style="font-size: 12.5px; color: var(--secondary);">Generate a 1-click Pairing Code to paste into the BURNRATE Setup Wizard on your other laptop or desktop.</p>
            <div class="inline-row">
              <select id="wiz-hub-address" class="text-input mono-input" aria-label="Hub network address"></select>
              <input id="wiz-remote-name" class="text-input" placeholder="Other computer name (e.g. MacBook Pro)">
              <button type="button" class="btn btn-primary" id="wiz-generate-pair-btn">Generate Pairing Code</button>
            </div>
            <div id="wiz-pair-result" class="pairing-output" hidden>
              <div><strong>1-Click Pairing Code (paste in Step 2 on the other computer):</strong></div>
              <div id="wiz-pair-code" style="user-select: all; color: var(--text);"></div>
              <div style="color: var(--secondary);">Or run via CLI: <span id="wiz-pair-cli" style="user-select: all; color: var(--cyan);"></span></div>
              <div><button type="button" class="btn" id="wiz-copy-pair-btn">Copy Pairing Code</button></div>
            </div>
            <div id="wiz-paired-machines-list" style="font-size: 12px; color: var(--secondary);"></div>
          </div>

          <!-- SPOKE CONNECTION TESTER -->
          <div id="wiz-spoke-box" class="sub-box" style="margin-top: 14px;" hidden>
            <h3>Connect this computer to your Primary Hub</h3>
            <div class="field-group">
              <label for="wiz-spoke-code">Paste 1-Click Pairing Code (starts with <code>brpair_</code>)</label>
              <input id="wiz-spoke-code" class="text-input mono-input" placeholder="brpair_eyJodWJVcmwiOi...">
            </div>
            <p style="font-size: 11.5px; color: var(--muted);">— OR enter Hub URL and Machine Token manually —</p>
            <div class="inline-row">
              <input id="wiz-spoke-url" class="text-input mono-input" placeholder="http://100.x.y.z:17331">
              <input id="wiz-spoke-token" class="text-input mono-input" placeholder="br_m_...">
            </div>
            <div class="inline-row">
              <button type="button" class="btn btn-primary" id="wiz-connect-spoke-btn">Test & Pair Connection</button>
              <span id="wiz-spoke-status" style="font-size: 12.5px; color: var(--secondary);"></span>
            </div>
          </div>
        </div>

        <!-- STEP 3: AI HARNESSES -->
        <div id="wiz-step-3" class="wiz-pane" hidden>
          <div class="step-head">
            <p class="step-eyebrow">Step 3 of 5 · Local AI Harnesses</p>
            <h1>Scan & import your AI coding tools</h1>
            <p>BURNRATE automatically detects local session logs from Cursor, Claude Code, Codex, OpenCode, ZCode, Grok, Antigravity, and Traycer.</p>
          </div>
          <div class="inline-row" style="margin-top: 12px; justify-content: space-between;">
            <span id="wiz-scan-summary" class="status-banner">Checking local AI coding harnesses…</span>
            <button type="button" class="btn btn-primary" id="wiz-scan-btn">Scan & Import Usage Now</button>
          </div>
          <div class="harness-grid" id="wiz-harness-grid" style="margin-top: 12px;"></div>
        </div>

        <!-- STEP 4: SUBSCRIPTIONS -->
        <div id="wiz-step-4" class="wiz-pane" hidden>
          <div class="step-head">
            <p class="step-eyebrow">Step 4 of 5 · Subscriptions & Plans</p>
            <h1>Select your active AI subscriptions</h1>
            <p>BURNRATE compares your real API-equivalent usage against your monthly subscription costs to show true plan ROI.</p>
          </div>
          <div class="presets-grid" id="wiz-presets-grid" style="margin-top: 12px;"></div>
          <div class="sub-box" style="margin-top: 12px;">
            <h3>Add a custom subscription (optional)</h3>
            <div class="inline-row">
              <select id="wiz-custom-tool" class="text-input">
                <option value="claude-code">Claude Code</option>
                <option value="codex">Codex / OpenAI</option>
                <option value="cursor">Cursor</option>
                <option value="grok">SuperGrok / xAI</option>
                <option value="opencode">Z.AI / OpenCode</option>
                <option value="openrouter">OpenRouter</option>
                <option value="custom">Custom</option>
              </select>
              <input id="wiz-custom-name" class="text-input" placeholder="Plan name (e.g. Team Seat)">
              <input id="wiz-custom-amount" type="number" min="0" step="0.01" class="text-input mono-input" placeholder="USD / month" style="max-width: 140px;">
              <button type="button" class="btn" id="wiz-add-custom-plan-btn">Add Plan</button>
            </div>
            <div id="wiz-existing-plans" style="font-size: 12px; color: var(--secondary);"></div>
          </div>
        </div>

        <!-- STEP 5: SIDECAR HUD & FINISH -->
        <div id="wiz-step-5" class="wiz-pane" hidden>
          <div class="step-head">
            <p class="step-eyebrow">Step 5 of 5 · Native Desktop & Sidecar HUD</p>
            <h1>Configure your Always-on-Top Sidecar HUD</h1>
            <p>Keep a compact live meter docked on the side of your screen showing real-time burn rate, active agents across all computers, and quota limits.</p>
          </div>
          <div class="sub-box" style="margin-top: 14px;">
            <label style="display: flex; align-items: center; gap: 10px; cursor: pointer;">
              <input type="checkbox" id="wiz-hud-auto" checked style="width: 17px; height: 17px;">
              <span><strong>Launch Sidecar HUD automatically</strong> — Keep live multi-computer activity visible on the side</span>
            </label>
            <label style="display: flex; align-items: center; gap: 10px; cursor: pointer;">
              <input type="checkbox" id="wiz-hud-top" checked style="width: 17px; height: 17px;">
              <span><strong>Pin Always-on-Top</strong> — Float above your IDE and terminal windows</span>
            </label>
            <div class="inline-row" style="margin-top: 6px;">
              <button type="button" class="btn" id="wiz-preview-hud-btn">Preview Sidecar HUD Window ↗</button>
            </div>
          </div>
          <div id="wiz-finish-banner" class="status-banner good" style="margin-top: 14px;">
            Ready to launch BURNRATE with real-time multi-computer telemetry and Sidecar HUD.
          </div>
        </div>

        <footer class="wizard-footer">
          <button type="button" class="btn" id="wiz-back-btn" disabled>← Back</button>
          <div class="inline-row">
            <button type="button" class="btn btn-primary" id="wiz-next-btn">Continue →</button>
          </div>
        </footer>
      </section>
    </main>

    <script>
      (() => {
        const $ = id => document.getElementById(id);
        let step = 1;
        let stateData = null;
        let role = "hub";
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

        function setStep(next) {
          step = Math.max(1, Math.min(5, next));
          for (let i = 1; i <= 5; i++) {
            $("wiz-step-" + i).hidden = i !== step;
          }
          document.querySelectorAll("#wizard-stepper li").forEach(li => {
            const s = Number(li.dataset.step);
            li.classList.toggle("active", s === step);
            li.classList.toggle("done", s < step);
          });
          $("wiz-back-btn").disabled = step === 1;
          $("wiz-next-btn").textContent = step === 5 ? "Finish Setup & Launch BURNRATE" : "Continue →";
        }

        function setRole(nextRole) {
          role = nextRole;
          document.querySelectorAll("[data-role]").forEach(btn => {
            btn.classList.toggle("selected", btn.dataset.role === role);
          });
          $("wiz-hub-box").hidden = role !== "hub";
          $("wiz-spoke-box").hidden = role !== "spoke";
        }

        function renderHarnesses(harnesses, stats) {
          const grid = $("wiz-harness-grid");
          const detectedCount = (harnesses || []).filter(h => h.detected).length;
          $("wiz-scan-summary").textContent =
            `${detectedCount} of ${(harnesses || []).length} AI harnesses detected · ${stats?.eventsCount ?? 0} usage records · ${stats?.liveAgentsCount ?? 0} live agents`;
          $("wiz-scan-summary").className = "status-banner" + (detectedCount > 0 ? " good" : "");

          grid.replaceChildren(...(harnesses || []).map(h => {
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
            chip.textContent = h.state === "healthy" ? "Healthy" : h.detected ? "Detected" : "Not found";
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
            sub.textContent = already ? `${p.badge} · Already active` : `${p.badge} · Click to include`;
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
            ? "Configured plans: " + allPlans.map(p => `${p.name} ($${p.amount_usd}/mo)`).join(" · ")
            : "No existing plans configured yet.";
        }

        function renderMachinesList(machines) {
          const remote = (machines || []).filter(m => !m.isLocal && m.id !== "local");
          $("wiz-paired-machines-list").textContent = remote.length
            ? `Paired computers (${remote.length}): ` + remote.map(m => m.name).join(", ")
            : "No remote computers paired yet.";
        }

        async function loadInitialState() {
          try {
            stateData = await api("/api/setup/state");
            const s = stateData.setup || {};
            $("wiz-computer-name").value = s.computerName || "";
            $("wiz-os-label").textContent = stateData.os || "local";
            $("wiz-tz-label").textContent = stateData.timezone || "UTC";
            setRole(s.role || "hub");
            if (s.spokeConfig?.hubUrl) $("wiz-spoke-url").value = s.spokeConfig.hubUrl;
            $("wiz-hud-auto").checked = s.hudConfig?.autoOpen !== false;
            $("wiz-hud-top").checked = s.hudConfig?.alwaysOnTop !== false;

            const addrSelect = $("wiz-hub-address");
            addrSelect.replaceChildren(...(stateData.networkAddresses || []).map(a => {
              const opt = document.createElement("option");
              opt.value = a.url;
              opt.textContent = `${a.label} — ${a.url}`;
              return opt;
            }));

            renderMachinesList(stateData.machines);
            renderHarnesses(stateData.harnesses, stateData.stats);
            renderPresets(stateData.presets, stateData.subscriptions);
          } catch (err) {
            $("wiz-scan-summary").textContent = "Could not load setup metadata: " + err.message;
          }
        }

        document.querySelectorAll("#wizard-stepper li").forEach(li => {
          li.addEventListener("click", () => setStep(Number(li.dataset.step)));
        });
        document.querySelectorAll("[data-role]").forEach(btn => {
          btn.addEventListener("click", () => setRole(btn.dataset.role));
        });

        $("wiz-generate-pair-btn").addEventListener("click", async () => {
          const name = $("wiz-remote-name").value.trim() || "Remote Computer";
          const hubUrl = $("wiz-hub-address").value;
          try {
            const res = await api("/api/setup/pair-hub", "POST", {name, hubUrl});
            $("wiz-pair-result").hidden = false;
            $("wiz-pair-code").textContent = res.pairingCode;
            $("wiz-pair-cli").textContent = res.syncCommand;
            renderMachinesList(res.machines);
          } catch (err) {
            alert(err.message);
          }
        });

        $("wiz-copy-pair-btn").addEventListener("click", async () => {
          const code = $("wiz-pair-code").textContent;
          try {
            await navigator.clipboard.writeText(code);
            $("wiz-copy-pair-btn").textContent = "Copied Pairing Code!";
          } catch {}
        });

        $("wiz-connect-spoke-btn").addEventListener("click", async () => {
          const status = $("wiz-spoke-status");
          status.textContent = "Testing connection & syncing…";
          status.style.color = "var(--secondary)";
          try {
            const res = await api("/api/setup/connect-spoke", "POST", {
              pairingCode: $("wiz-spoke-code").value.trim(),
              hubUrl: $("wiz-spoke-url").value.trim(),
              token: $("wiz-spoke-token").value.trim(),
              computerName: $("wiz-computer-name").value.trim(),
              testSync: true
            });
            status.textContent = `Connected! Pushed ${res.syncResult?.eventsWritten ?? 0} usage events to Hub.`;
            status.style.color = "var(--green)";
          } catch (err) {
            status.textContent = "Connection failed: " + err.message;
            status.style.color = "var(--danger)";
          }
        });

        $("wiz-scan-btn").addEventListener("click", async () => {
          const btn = $("wiz-scan-btn");
          btn.disabled = true;
          btn.textContent = "Scanning & Importing…";
          try {
            const res = await api("/api/setup/scan", "POST", {});
            renderHarnesses(res.harnesses, {eventsCount: res.eventsCount, liveAgentsCount: res.liveAgentsCount});
          } catch (err) {
            $("wiz-scan-summary").textContent = "Scan error: " + err.message;
          } finally {
            btn.disabled = false;
            btn.textContent = "Scan & Import Usage Now";
          }
        });

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

        $("wiz-preview-hud-btn").addEventListener("click", () => {
          const internals = window.__TAURI_INTERNALS__;
          if (internals && typeof internals.invoke === "function") {
            internals.invoke("toggle_hud");
            return;
          }
          window.open("/hud", "burnrate-hud", "popup=yes,width=360,height=580,left=24,top=72");
        });

        $("wiz-back-btn").addEventListener("click", () => setStep(step - 1));
        $("wiz-next-btn").addEventListener("click", async () => {
          if (step < 5) {
            setStep(step + 1);
            return;
          }
          const btn = $("wiz-next-btn");
          btn.disabled = true;
          btn.textContent = "Saving & Launching…";
          try {
            const autoHud = $("wiz-hud-auto").checked;
            const topHud = $("wiz-hud-top").checked;
            await api("/api/setup/complete", "POST", {
              computerName: $("wiz-computer-name").value.trim(),
              role,
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
              try { window.open("/hud", "burnrate-hud", "popup=yes,width=360,height=580,left=24,top=72"); } catch {}
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
