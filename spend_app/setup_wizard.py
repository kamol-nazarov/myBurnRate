"""Interactive Application Setup Wizard & Central Account Multi-Computer Orchestration."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from spend_app import aggregate, auth_throttle
from spend_app.config import dashboard_enabled
from spend_app.db import connect, initialize, utc_now
from spend_app.error_codes import (
    HUB_PROTOCOL_VERSION,
    CodedError,
    check_protocol_version,
    error_message,
    error_response,
    format_error,
    map_http_error,
    map_transport_error,
)
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
    rename_machine,
    sync_once,
)


SETUP_META_KEY = "app_setup.v1"
ACCOUNTS_META_KEY = "accounts.v1"
SETUP_TOKEN_ENV = "BURNRATE_SETUP_TOKEN"
PBKDF2_ITERATIONS = 600_000
LEGACY_PBKDF2_ITERATIONS = 120_000

# A hub that already has an account accepts no new ones (one owner, many
# computers); computers join by signing in. Opt out deliberately.
ALLOW_REGISTRATION_ENV = "BURNRATE_ALLOW_REGISTRATION"


def _safe_resolve_hub_url() -> str:
    from spend_app.config import resolve_hub_url

    try:
        return resolve_hub_url()
    except ValueError:
        return ""


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
MIN_PASSWORD_LENGTH = 12


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
            "machineId": "",
            "connectedAt": None,
            "needsEmail": False,
            "emailPromptDismissed": False,
        },
        "spokeConfig": {
            "hubUrl": "",
            "token": "",
            "enabled": False,
            "lastSyncAt": None,
            "lastSyncError": None,
            "usageChangesPending": False,
            "eventsPushed": 0,
            "tokenExpiresAt": None,
            "rotationRequired": False,
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
        merged["account"].pop("serverUrl", None)
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
    if isinstance(clean.get("account"), dict):
        clean["account"] = {k: v for k, v in clean["account"].items() if k != "serverUrl"}
    connection.execute(
        "INSERT INTO app_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (SETUP_META_KEY, json.dumps(clean, separators=(",", ":"), sort_keys=True)),
    )
    return clean


SPOKE_SYNC_ERROR_CODES = frozenset({"hub_unreachable", "hub_rejected", "sync_failed"})


def normalize_spoke_sync_error(value: object) -> str | None:
    """Map any non-empty legacy/unknown spoke sync error to a fixed code."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text in SPOKE_SYNC_ERROR_CODES:
        return text
    return "sync_failed"


def public_setup_state(state: dict) -> dict:
    spoke = dict(state.get("spokeConfig") or {})
    raw_hub_url = str(spoke.pop("hubUrl", "") or "").strip()
    spoke["hubConfigured"] = bool(raw_hub_url)
    raw_token = str(spoke.get("token") or "")
    spoke["hasToken"] = bool(raw_token)
    spoke.pop("token", None)
    spoke["hasOwnerSession"] = bool(spoke.pop("ownerSession", None))
    spoke["tokenPreview"] = "configured" if raw_token else ""
    spoke["lastSyncError"] = normalize_spoke_sync_error(spoke.get("lastSyncError"))
    account = dict(state.get("account") or {})
    account.pop("serverUrl", None)
    return {**state, "account": account, "spokeConfig": spoke}


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


def _hash_password(
    password: str,
    salt_hex: str | None = None,
    iterations: int = PBKDF2_ITERATIONS,
) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    return salt.hex(), digest.hex()


def _verify_password(
    password: str,
    salt_hex: str,
    expected_hash_hex: str,
    iterations: int = LEGACY_PBKDF2_ITERATIONS,
) -> bool:
    if not salt_hex or not expected_hash_hex:
        return False
    _, actual_hex = _hash_password(password, salt_hex, iterations)
    return hmac.compare_digest(actual_hex, expected_hash_hex)


def _account_iterations(record: dict) -> int:
    try:
        value = int(record.get("iterations") or LEGACY_PBKDF2_ITERATIONS)
    except (TypeError, ValueError):
        return LEGACY_PBKDF2_ITERATIONS
    return max(LEGACY_PBKDF2_ITERATIONS, value)


def _client_key(request: Request, username: str, database_path: Path | None = None) -> str:
    from spend_app.exposure import resolve_client_ip

    host = resolve_client_ip(request)
    scope = str(database_path) if database_path is not None else ""
    return f"{scope}|{username.strip().lower()}|{host}"


def _register_ip_key(request: Request, database_path: Path | None = None) -> str:
    from spend_app.exposure import resolve_client_ip

    host = resolve_client_ip(request)
    scope = str(database_path) if database_path is not None else ""
    return f"{scope}|register_ip|{host}"


_GLOBAL_THROTTLE = auth_throttle.Throttle(window=5 * 60, max_failures=30, lockout=5 * 60)


def _global_key(database_path: Path | None) -> str:
    return f"{database_path or ''}|global"


def _throttled(retry_after: int) -> JSONResponse:
    return error_response("AUTH-102", status_code=429, retry_after=retry_after)


class RequestTooLargeError(ValueError):
    """Raised when an incoming JSON request body exceeds the allowed byte limit."""


async def _bounded_json(request: Request, limit: int = 65536) -> dict:
    """Read a small JSON object without letting an anonymous body exhaust memory."""
    if request.headers.get("x-burnrate-request") != "1":
        raise HTTPException(
            status_code=403,
            detail="Request rejected: use the configured BURNRATE origin.",
        )
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="Use application/json.")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise RequestTooLargeError("Request too large.")
    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > limit:
            raise RequestTooLargeError("Request too large.")
        chunks.extend(chunk)
    value = json.loads(
        chunks, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
    )
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object.")
    return value


_EMAIL_LOCAL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$")
_EMAIL_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def normalize_email(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if len(text) < 3 or len(text) > 254 or any(ch.isspace() for ch in text):
        return None
    if text.count("@") != 1:
        return None
    local, _, domain = text.partition("@")
    if not local or len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    if not _EMAIL_LOCAL_RE.fullmatch(local):
        return None
    if not domain or len(domain) > 253 or "." not in domain or ".." in domain:
        return None
    labels = domain.split(".")
    if any(not _EMAIL_LABEL_RE.fullmatch(label) for label in labels):
        return None
    if len(labels[-1]) < 2 or labels[-1].isdigit():
        return None
    return f"{local.lower()}@{domain.lower()}"


def is_valid_email(raw: object) -> bool:
    return normalize_email(raw) is not None


def account_needs_email(record: dict | None) -> bool:
    if not isinstance(record, dict):
        return False
    candidate = record.get("email") or record.get("username") or ""
    return not is_valid_email(candidate)


def central_server_for_accounts() -> bool:
    return os.getenv("BURNRATE_CENTRAL_SERVER", "").strip().lower() in {"1", "true", "yes"}


def deprecated_account_env_warnings() -> list[str]:
    warnings: list[str] = []
    if os.getenv(SETUP_TOKEN_ENV, "").strip():
        warnings.append(
            f"{SETUP_TOKEN_ENV} is deprecated and ignored: first-account registration "
            "requires no setup token and registration closes once an account exists. "
            "Remove it from the environment."
        )
    if os.getenv(ALLOW_REGISTRATION_ENV, "").strip():
        warnings.append(
            f"{ALLOW_REGISTRATION_ENV} is deprecated and ignored: hub registration closes "
            "permanently after the first account is created. Remove it from the environment."
        )
    return warnings


def setup_token_warning(connection) -> str | None:
    """Warn when the one-time setup token outlives the account it protects."""
    if not os.getenv(SETUP_TOKEN_ENV, "").strip():
        return None
    if not central_server_for_accounts():
        return None
    if not _read_accounts(connection).get("users"):
        return None
    return (
        f"{SETUP_TOKEN_ENV} is still set, but this hub already has an owner account, "
        "so the token is ignored. Remove it from the environment."
    )


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



def _should_auto_rotate_token(
    expires_at_raw: object, rotation_required: bool, *, now: datetime | None = None
) -> bool:
    if rotation_required:
        return True
    from spend_app.auth_model import ROTATION_GRACE_DAYS, moment, parse

    expires = parse(expires_at_raw)
    if expires is None:
        return False
    return expires <= moment(now) + timedelta(days=ROTATION_GRACE_DAYS)


def rotate_spoke_machine_token(settings, *, client=None) -> dict | None:
    """Exchange the configured spoke's machine bearer for a fresh one on the hub."""
    from spend_app.config import resolve_hub_url
    from spend_app.hub_origin import hub_origin
    from spend_app.sync import (
        _cursor_key,
        _vault_get_secret,
        _vault_put_secret,
        read_sync_cursor,
        rotate_spoke_sync_credential,
    )

    with connect(settings.database_path) as connection:
        state = read_setup_state(connection)
    spoke = state.get("spokeConfig") or {}
    if state.get("role") != "spoke" or not spoke.get("enabled"):
        return None
    db_has_token = bool(str(spoke.get("token") or "").strip())
    token = str(spoke.get("token") or "").strip() or (
        _vault_get_secret(settings.database_path, "burnrate-machine-token") or ""
    )
    if not token:
        return None
    raw_hub_url = resolve_hub_url() or str(spoke.get("hubUrl") or "").strip()
    if not raw_hub_url:
        return None
    hub_url = hub_origin(raw_hub_url)
    prior_generation = read_sync_cursor(settings.database_path, hub_url).get("generation")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-BURNRATE-Request": "1",
        "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
        "Content-Type": "application/json",
    }
    resolved_client = client or _HUB_TEST_CLIENTS.get(hub_url)
    if resolved_client is not None:
        response = resolved_client.post("/api/auth/machine/rotate", json={}, headers=headers)
    else:
        import httpx
        with httpx.Client(timeout=8, trust_env=False) as http:
            response = http.post(f"{hub_url}/api/auth/machine/rotate", json={}, headers=headers)
    if response.status_code != 200:
        return {"ok": False, "status": response.status_code}
    replacement = response.json()
    if not isinstance(replacement, dict) or not str(replacement.get("token") or "").strip():
        return {"ok": False, "status": 502}
    with connect(settings.database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = read_setup_state(connection)
        row = connection.execute("SELECT value FROM app_meta WHERE key=?", (_cursor_key(hub_url),)).fetchone()
        generation = json.loads(row[0]).get("generation") if row else None
        current_tok = str(current["spokeConfig"].get("token") or "").strip() or (
            _vault_get_secret(settings.database_path, "burnrate-machine-token") or ""
        )
        if current_tok != token or generation != prior_generation:
            return {"ok": False, "status": 409}
        vault_saved = _vault_put_secret(
            settings.database_path, "burnrate-machine-token", replacement["token"]
        )
        if not vault_saved:
            from spend_app.connection_secrets import get_vault

            active_vault = None
            try:
                active_vault = get_vault(settings.database_path)
            except Exception:
                active_vault = None
            vault_available = active_vault is not None and bool(
                getattr(active_vault, "available", lambda: True)()
            )
            if not db_has_token or vault_available:
                return {"ok": False, "code": "APP-903", "message": error_message("APP-903")}
            current["spokeConfig"]["token"] = replacement["token"]
        else:
            current["spokeConfig"].pop("token", None)
        current["spokeConfig"]["tokenExpiresAt"] = replacement.get("expiresAt")
        current["spokeConfig"]["rotationRequired"] = False
        rotate_spoke_sync_credential(connection, hub_url, replacement["token"])
        write_setup_state(connection, current)
    return {"ok": True, "expiresAt": replacement.get("expiresAt")}


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
    from spend_app.config import resolve_hub_url
    from spend_app.hub_origin import hub_origin
    from spend_app.sync import _vault_get_secret, migrate_legacy_spoke_secrets

    migrated = migrate_legacy_spoke_secrets(settings.database_path)
    if migrated.get("token") or migrated.get("ownerSession"):
        try:
            with connect(settings.database_path) as connection:
                state = read_setup_state(connection)
            spoke = state.get("spokeConfig") or {}
        except Exception:
            pass

    token = str(spoke.get("token") or "").strip() or (
        _vault_get_secret(settings.database_path, "burnrate-machine-token") or ""
    )
    if not token:
        return None
    try:
        configured_hub_url = resolve_hub_url() or str(spoke.get("hubUrl") or "").strip().rstrip("/")
        if not configured_hub_url:
            return None
        hub_url = hub_origin(configured_hub_url)
        resolved_client = client or _HUB_TEST_CLIENTS.get(hub_url)
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
        should_rotate = False
        with connect(settings.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = read_setup_state(connection)
            latest_hub = _safe_resolve_hub_url() or str(latest["spokeConfig"].get("hubUrl") or "").strip().rstrip("/")
            latest_tok = str(latest["spokeConfig"].get("token") or "").strip() or (
                _vault_get_secret(settings.database_path, "burnrate-machine-token") or ""
            )
            if latest_tok != token or latest_hub != configured_hub_url:
                return {"ok": False, "error": "Hub connection changed; retry sync with the current configuration."}
            latest["spokeConfig"]["lastSyncAt"] = utc_now()
            latest["spokeConfig"]["lastSyncError"] = None
            if isinstance(result.get("machineId"), str) and result["machineId"]:
                latest["account"] = {**latest.get("account", {}), "machineId": result["machineId"]}
            if result.get("tokenExpiresAt"):
                latest["spokeConfig"]["tokenExpiresAt"] = result["tokenExpiresAt"]
            if "rotationRequired" in result:
                latest["spokeConfig"]["rotationRequired"] = bool(result["rotationRequired"])
            latest["spokeConfig"]["usageChangesPending"] = bool(result.get("usageChangesPending"))
            latest["spokeConfig"]["eventsPushed"] = int(
                latest["spokeConfig"].get("eventsPushed") or 0
            ) + int(result.get("eventsWritten") or 0)
            write_setup_state(connection, latest)
            should_rotate = _should_auto_rotate_token(
                latest["spokeConfig"].get("tokenExpiresAt"),
                bool(latest["spokeConfig"].get("rotationRequired")),
            )
        if should_rotate:
            try:
                rotated = rotate_spoke_machine_token(settings, client=resolved_client)
                if isinstance(rotated, dict) and rotated.get("ok"):
                    result["tokenRotated"] = True
                    result["tokenExpiresAt"] = rotated.get("expiresAt")
                    result["rotationRequired"] = False
            except Exception:
                pass
        return result
    except Exception as exc:
        import httpx

        msg = str(exc)
        if isinstance(exc, (httpx.RequestError, OSError)):
            code = "hub_unreachable"
        elif "Sync push failed (401)" in msg or "Sync push failed (403)" in msg:
            code = "hub_rejected"
        else:
            code = "sync_failed"
        with connect(settings.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = read_setup_state(connection)
            latest_hub = _safe_resolve_hub_url() or str(latest["spokeConfig"].get("hubUrl") or "").strip().rstrip("/")
            latest_tok = str(latest["spokeConfig"].get("token") or "").strip() or (
                _vault_get_secret(settings.database_path, "burnrate-machine-token") or ""
            )
            if latest_tok != token or latest_hub != configured_hub_url:
                return {"ok": False, "error": "Hub connection changed; retry sync with the current configuration."}
            latest["spokeConfig"]["lastSyncError"] = code
            write_setup_state(connection, latest)
        return {"ok": False, "error": code}


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


def _hub_protocol_handshake(base_url: str, test_client) -> None:
    """Verify hub protocol compatibility via unauthenticated /api/hub/info (B3)."""
    if test_client is not None and not hasattr(test_client, "get"):
        return
    headers = {"X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION)}
    try:
        if test_client is not None:
            info_res = test_client.get("/api/hub/info", headers=headers)
        else:
            import httpx

            with httpx.Client(timeout=5.0, trust_env=False) as http:
                info_res = http.get(f"{base_url}/api/hub/info", headers=headers)
    except Exception as exc:
        code, status = map_transport_error(exc)
        raise CodedError(code, status_code=status) from exc

    if info_res.status_code == 200:
        try:
            info_data = info_res.json() if getattr(info_res, "content", b"") else {}
        except ValueError:
            info_data = {}
        if not isinstance(info_data, dict):
            raise CodedError("HUB-501", status_code=502)
        raw_proto = info_data.get("protocolVersion")
        if raw_proto is None:
            raw_proto = getattr(info_res, "headers", {}).get("x-burnrate-protocol")
        if raw_proto is None:
            raise CodedError("HUB-502", status_code=426)
        try:
            hub_proto = int(raw_proto)
        except (TypeError, ValueError):
            raise CodedError("HUB-501", status_code=502) from None
        if hub_proto < HUB_PROTOCOL_VERSION:
            raise CodedError("HUB-502", status_code=426)
        if hub_proto > HUB_PROTOCOL_VERSION:
            raise CodedError("APP-902", status_code=426)
        return

    if info_res.status_code == 403:
        raise CodedError("NET-002", status_code=403)
    if info_res.status_code == 426:
        try:
            err_data = info_res.json() if getattr(info_res, "content", b"") else {}
        except ValueError:
            err_data = None
        raise map_http_error(
            426,
            err_data,
            headers=getattr(info_res, "headers", None),
            endpoint_path="/api/hub/info",
        )
    if info_res.status_code >= 500:
        raise CodedError("HUB-501", status_code=502)

    if info_res.status_code in {401, 404}:
        try:
            if test_client is not None:
                hz_res = test_client.get("/healthz")
            else:
                import httpx

                with httpx.Client(timeout=5.0, trust_env=False) as http:
                    hz_res = http.get(f"{base_url}/healthz")
        except Exception as exc:
            code, status = map_transport_error(exc)
            raise CodedError(code, status_code=status) from exc
        if hz_res.status_code == 200:
            try:
                hz_data = hz_res.json() if getattr(hz_res, "content", b"") else {}
            except ValueError:
                hz_data = {}
            if isinstance(hz_data, dict) and hz_data.get("status") == "ok":
                raw_proto = hz_data.get("protocolVersion")
                if raw_proto is None:
                    raise CodedError("HUB-502", status_code=426)
                try:
                    hub_proto = int(raw_proto)
                except (TypeError, ValueError):
                    raise CodedError("HUB-502", status_code=426) from None
                if hub_proto < HUB_PROTOCOL_VERSION:
                    raise CodedError("HUB-502", status_code=426)
                if hub_proto > HUB_PROTOCOL_VERSION:
                    raise CodedError("APP-902", status_code=426)


def _call_central_server_account(
    server_url: str,
    endpoint_path: str,
    payload: dict,
) -> tuple[str, dict, str]:
    """Call `/api/account/register` or `/api/account/login` on the configured hub."""
    from spend_app.config import resolve_hub_url
    from spend_app.hub_origin import hub_origin

    try:
        clean_primary = (server_url or "").strip().rstrip("/") or resolve_hub_url()
        if not clean_primary:
            raise CodedError("NET-001", status_code=422)
        clean_primary = hub_origin(clean_primary)
    except ValueError as exc:
        if isinstance(exc, CodedError):
            raise
        raise CodedError("NET-003" if "HTTPS" in str(exc) else "NET-001", status_code=422) from exc
    candidates = [clean_primary]

    headers = {
        "X-BURNRATE-Request": "1",
        "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
        "Content-Type": "application/json",
    }
    from spend_app.auth_model import extract_cookie
    last_error: CodedError = CodedError("NET-001")
    for base_url in candidates:
        test_client = _HUB_TEST_CLIENTS.get(base_url)
        try:
            _hub_protocol_handshake(base_url, test_client)
            if test_client is not None:
                cookies = getattr(test_client, "cookies", None)
                saved_jar = list(cookies.jar) if cookies is not None and hasattr(cookies, "jar") else None
                if cookies is not None:
                    cookies.clear()
                try:
                    res = test_client.post(endpoint_path, json=payload, headers=headers)
                finally:
                    if cookies is not None and saved_jar is not None:
                        cookies.clear()
                        for cookie in saved_jar:
                            cookies.jar.set_cookie(cookie)
            else:
                import httpx

                with httpx.Client(timeout=8.0, trust_env=False) as http:
                    res = http.post(f"{base_url}{endpoint_path}", json=payload, headers=headers)
            try:
                data = res.json() if getattr(res, "content", b"") else {}
            except ValueError:
                data = None
            if res.status_code >= 400:
                raise map_http_error(
                    res.status_code,
                    data,
                    headers=getattr(res, "headers", None),
                    endpoint_path=endpoint_path,
                )
            session_token = extract_cookie(res)
            if not isinstance(data, dict) or not session_token:
                raise CodedError("HUB-501", status_code=502)
            return base_url, data, session_token
        except CodedError as exc:
            if exc.code in {"NET-001", "NET-003"} and len(candidates) > 1:
                last_error = exc
                continue
            raise
        except Exception as exc:
            code, status = map_transport_error(exc)
            last_error = CodedError(code, status_code=status)
    raise last_error


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
    rows = []
    for row in listing.get("connections") or []:
        if row.get("kind") != "api":
            continue
        item = {
            key: row.get(key)
            for key in (
                "source",
                "name",
                "state",
                "detail",
                "enabled",
                "revision",
                "capabilities",
                "credentialHelp",
                "externallyManaged",
                "credentialConfigured",
                "lastImport",
            )
        }
        profiles = [p for p in (row.get("profiles") or []) if isinstance(p, dict)]
        if profiles:
            primary = profiles[0]
            if item["revision"] is None:
                item["revision"] = primary.get("revision", 0)
            if item["state"] is None:
                item["state"] = primary.get("state")
            if item["enabled"] is None:
                item["enabled"] = primary.get("state") != "disabled"
        rows.append(item)
    return {"connections": rows, "vaultAvailable": bool(listing.get("vaultAvailable"))}


def setup_router(settings, pricing: PricingEngine, *, collect_now=None) -> APIRouter:
    router = APIRouter()

    @router.get("/api/setup/state")
    def get_setup_state(request: Request):
        initialize(settings.database_path)
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
                "hubAvailable": bool(_safe_resolve_hub_url()),
                "os": sys.platform,
                "timezone": settings.timezone,
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
            body = await _bounded_json(request)
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
            body = await _bounded_json(request)
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
            body = await _bounded_json(request)
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
        from starlette.concurrency import run_in_threadpool

        proto_err = check_protocol_version(request.headers.get("x-burnrate-protocol"))
        if proto_err is not None:
            return error_response(proto_err, status_code=426)
        try:
            try:
                body = await _bounded_json(request)
            except RequestTooLargeError:
                return error_response("REQ-401", status_code=422)
            except HTTPException as exc:
                return error_response(
                    "NET-002" if exc.status_code == 403 else "APP-901",
                    status_code=exc.status_code,
                )
            except ValueError:
                return error_response("APP-901", status_code=422)
            if not isinstance(body, dict):
                return error_response("APP-901", status_code=422)
            username = normalize_email(body.get("username"))
            password = str(body.get("password") or "")
            computer_name = str(body.get("computerName") or "").strip() or "Connected Computer"
            preset_keys = [str(k) for k in (body.get("presetKeys") or []) if k]
            custom_plans = body.get("customPlans") if isinstance(body.get("customPlans"), list) else []

            if username is None:
                return error_response("AUTH-106", status_code=422)
            if len(password) < MIN_PASSWORD_LENGTH:
                return error_response("AUTH-105", status_code=422)

            key = _client_key(request, username, settings.database_path)
            reg_ip_key = _register_ip_key(request, settings.database_path)
            global_key = _global_key(settings.database_path)
            retry = _GLOBAL_THROTTLE.retry_after(global_key)
            if retry:
                return _throttled(retry)
            retry = auth_throttle.retry_after(reg_ip_key)
            if retry:
                return _throttled(retry)
            retry = auth_throttle.retry_after(key)
            if retry:
                return _throttled(retry)

            existing_principal = request.state.principal
            device_label = str(body.get("deviceLabel") or computer_name)

            def _register_tx():
                from spend_app.auth_model import Principal, audit, create_session

                initialize(settings.database_path)
                with connect(settings.database_path) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    local_setup = read_setup_state(connection)
                    if local_setup.get("role") == "spoke" and local_setup["spokeConfig"].get("enabled"):
                        global_retry = _GLOBAL_THROTTLE.fail(global_key)
                        reg_retry = auth_throttle.fail(reg_ip_key)
                        retry_after = auth_throttle.fail(key)
                        if global_retry or reg_retry or retry_after:
                            return None, _throttled(max(global_retry, reg_retry, retry_after))
                        return None, error_response("AUTH-103", status_code=403)
                    accounts = _read_accounts(connection)
                    if accounts["users"]:
                        global_retry = _GLOBAL_THROTTLE.fail(global_key)
                        reg_retry = auth_throttle.fail(reg_ip_key)
                        retry_after = auth_throttle.fail(key)
                        if global_retry or reg_retry or retry_after:
                            return None, _throttled(max(global_retry, reg_retry, retry_after))
                        return None, error_response("AUTH-103", status_code=403)
                    if not central_server_for_accounts():
                        protected = bool(
                            os.getenv("BURNRATE_ACCESS_TOKEN", "").strip()
                            or connection.execute(
                                "SELECT 1 FROM app_meta WHERE key='auth.break_glass.v1'"
                            ).fetchone()
                        )
                        if protected and not existing_principal.permits("credentials.manage"):
                            global_retry = _GLOBAL_THROTTLE.fail(global_key)
                            reg_retry = auth_throttle.fail(reg_ip_key)
                            retry_after = auth_throttle.fail(key)
                            if global_retry or reg_retry or retry_after:
                                return None, _throttled(max(global_retry, reg_retry, retry_after))
                            return None, error_response("AUTH-103", status_code=403)
                    salt_hex, hash_hex = _hash_password(password)
                    account_id = secrets.token_hex(16)
                    accounts["users"][username] = {
                        "username": username,
                        "email": username,
                        "id": account_id,
                        "role": "owner",
                        "salt": salt_hex,
                        "passwordHash": hash_hex,
                        "iterations": PBKDF2_ITERATIONS,
                        "createdAt": utc_now(),
                    }
                    _write_accounts(connection, accounts)
                    auth_throttle.succeed(key)
                    auth_throttle.succeed(reg_ip_key)
                    added_plans = _apply_subscription_presets_and_custom(
                        connection,
                        settings.timezone,
                        preset_keys,
                        custom_plans,
                    )
                    plans_out = effective_terms(connection)
                    actor = Principal("owner", account_id, "owner")
                    session = create_session(connection, actor, device_label)
                    audit(connection, actor, "account.register", account_id)
                    return {
                        "actor": actor,
                        "session": session,
                        "addedPlans": added_plans,
                        "subscriptions": plans_out,
                    }, None

            tx_out, err_resp = await run_in_threadpool(_register_tx)
            if err_resp is not None:
                return err_resp
            request.state.principal = tx_out["actor"]
            request.state.audit_recorded = True
            session = tx_out["session"]
            from spend_app.auth_model import set_session_cookie

            public_session = {"sessionId": session["sessionId"], "expiresAt": session["expiresAt"]}
            if body.get("sessionOnly"):
                return set_session_cookie(
                    JSONResponse({"ok": True, **public_session}, headers={"Cache-Control": "no-store"}),
                    request,
                    session,
                )
            aggregate.reset_memo()
            return set_session_cookie(JSONResponse(
                {
                    "ok": True,
                    **public_session,
                    "account": {"username": username, "email": username, "needsEmail": False},
                    "addedPlans": tx_out["addedPlans"],
                    "subscriptions": tx_out["subscriptions"],
                },
                headers={"Cache-Control": "no-store"},
            ), request, session)
        except ValueError:
            return error_response("HUB-501", status_code=422)

    @router.post("/api/account/login")
    async def central_account_login(request: Request):
        from starlette.concurrency import run_in_threadpool

        proto_err = check_protocol_version(request.headers.get("x-burnrate-protocol"))
        if proto_err is not None:
            return error_response(proto_err, status_code=426)
        try:
            try:
                body = await _bounded_json(request)
            except RequestTooLargeError:
                return error_response("REQ-401", status_code=422)
            except HTTPException as exc:
                return error_response(
                    "NET-002" if exc.status_code == 403 else "APP-901",
                    status_code=exc.status_code,
                )
            except ValueError:
                return error_response("APP-901", status_code=422)
            if not isinstance(body, dict):
                return error_response("APP-901", status_code=422)
            username = str(body.get("username") or "").strip()
            password = str(body.get("password") or "")
            computer_name = str(body.get("computerName") or "").strip() or "Connected Computer"
            preset_keys = [str(k) for k in (body.get("presetKeys") or []) if k]
            custom_plans = body.get("customPlans") if isinstance(body.get("customPlans"), list) else []

            if not username:
                return error_response("AUTH-106", status_code=422)
            if not password:
                return error_response("AUTH-101", status_code=422)

            key = _client_key(request, username, settings.database_path)
            global_key = _global_key(settings.database_path)
            retry = _GLOBAL_THROTTLE.retry_after(global_key)
            if retry:
                return _throttled(retry)
            retry = auth_throttle.retry_after(key)
            if retry:
                return _throttled(retry)

            user_key = username.lower()
            device_label = str(body.get("deviceLabel") or computer_name)

            def _login_tx():
                from spend_app.auth_model import Principal, audit, create_session

                initialize(settings.database_path)
                with connect(settings.database_path) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    accounts = _read_accounts(connection)
                    user_rec = accounts["users"].get(user_key)
                    if not isinstance(user_rec, dict):
                        for k, candidate in accounts["users"].items():
                            if isinstance(candidate, dict) and (
                                str(k).lower() == user_key
                                or str(candidate.get("email") or "").lower() == user_key
                                or str(candidate.get("username") or "").lower() == user_key
                            ):
                                user_rec = candidate
                                break
                    if not isinstance(user_rec, dict):
                        _verify_password(password, "00" * 16, "00" * 32, PBKDF2_ITERATIONS)
                        global_retry = _GLOBAL_THROTTLE.fail(global_key)
                        retry_after = auth_throttle.fail(key)
                        if global_retry or retry_after:
                            return None, _throttled(max(global_retry, retry_after))
                        if not accounts["users"]:
                            return None, error_response("AUTH-104", status_code=401)
                        return None, error_response("AUTH-101", status_code=401)
                    iterations = _account_iterations(user_rec)
                    if not _verify_password(
                        password,
                        str(user_rec.get("salt") or ""),
                        str(user_rec.get("passwordHash") or ""),
                        iterations,
                    ):
                        global_retry = _GLOBAL_THROTTLE.fail(global_key)
                        retry_after = auth_throttle.fail(key)
                        if global_retry or retry_after:
                            return None, _throttled(max(global_retry, retry_after))
                        return None, error_response("AUTH-101", status_code=401)
                    display_username = str(user_rec.get("username") or username)
                    needs_write = False
                    if not user_rec.get("id"):
                        user_rec["id"] = secrets.token_hex(16)
                        needs_write = True
                    if iterations < PBKDF2_ITERATIONS:
                        salt_hex, hash_hex = _hash_password(password, iterations=PBKDF2_ITERATIONS)
                        user_rec.update(
                            salt=salt_hex,
                            passwordHash=hash_hex,
                            iterations=PBKDF2_ITERATIONS,
                        )
                        needs_write = True
                    if needs_write:
                        _write_accounts(connection, accounts)
                    auth_throttle.succeed(key)
                    if preset_keys or custom_plans:
                        _apply_subscription_presets_and_custom(
                            connection,
                            settings.timezone,
                            preset_keys,
                            custom_plans,
                        )
                    plans_out = effective_terms(connection)
                    if user_rec.get("role", "owner") != "owner":
                        return None, error_response("AUTH-103", status_code=403)
                    actor = Principal("owner", user_rec["id"], "owner")
                    session = create_session(connection, actor, device_label)
                    audit(connection, actor, "login", user_rec["id"])
                    acct_out = {
                        "username": display_username,
                        "needsEmail": account_needs_email(user_rec),
                    }
                    if user_rec.get("email"):
                        acct_out["email"] = user_rec["email"]
                    return {
                        "actor": actor,
                        "session": session,
                        "account": acct_out,
                        "subscriptions": plans_out,
                    }, None

            tx_out, err_resp = await run_in_threadpool(_login_tx)
            if err_resp is not None:
                return err_resp
            request.state.principal = tx_out["actor"]
            request.state.audit_recorded = True
            session = tx_out["session"]
            from spend_app.auth_model import set_session_cookie

            public_session = {"sessionId": session["sessionId"], "expiresAt": session["expiresAt"]}
            if body.get("sessionOnly"):
                return set_session_cookie(
                    JSONResponse({"ok": True, **public_session}, headers={"Cache-Control": "no-store"}),
                    request,
                    session,
                )
            aggregate.reset_memo()
            return set_session_cookie(JSONResponse(
                {
                    "ok": True,
                    **public_session,
                    "account": tx_out["account"],
                    "subscriptions": tx_out["subscriptions"],
                },
                headers={"Cache-Control": "no-store"},
            ), request, session)
        except ValueError:
            return error_response("HUB-501", status_code=422)

    @router.post("/api/account/password")
    async def central_account_password(request: Request):
        from starlette.concurrency import run_in_threadpool

        proto_err = check_protocol_version(request.headers.get("x-burnrate-protocol"))
        if proto_err is not None:
            return error_response(proto_err, status_code=426)
        actor = request.state.principal
        if actor.kind != "owner" or not actor.permits("credentials.manage"):
            return error_response("AUTH-107", status_code=401 if actor.kind == "unauthenticated" else 403)
        try:
            try:
                body = await _bounded_json(request)
            except RequestTooLargeError:
                return error_response("REQ-401", status_code=422)
            except HTTPException as exc:
                return error_response(
                    "NET-002" if exc.status_code == 403 else "APP-901",
                    status_code=exc.status_code,
                )
            except ValueError:
                return error_response("APP-901", status_code=422)
            current_password = str(body.get("currentPassword") or "")
            new_password = str(body.get("newPassword") or "")

            def _change_pw_tx():
                from spend_app.auth_model import audit

                initialize(settings.database_path)
                with connect(settings.database_path) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    accounts = _read_accounts(connection)
                    users = accounts["users"]
                    rec = next(
                        (u for u in users.values() if isinstance(u, dict) and u.get("id") == actor.id),
                        None,
                    )
                    if rec is None:
                        rec = next(
                            (u for u in users.values() if isinstance(u, dict) and u.get("role", "owner") == "owner"),
                            None,
                        )
                    if not isinstance(rec, dict):
                        return error_response("AUTH-104", status_code=401), False
                    rec.setdefault("id", secrets.token_hex(16))
                    iterations = _account_iterations(rec)
                    if not current_password or not _verify_password(
                        current_password,
                        str(rec.get("salt") or ""),
                        str(rec.get("passwordHash") or ""),
                        iterations,
                    ):
                        if not current_password:
                            _verify_password("invalid", "00" * 16, "00" * 32, PBKDF2_ITERATIONS)
                        audit(connection, actor, "account.password_change", rec["id"], "failure")
                        return error_response("AUTH-101", status_code=401), True
                    if len(new_password) < MIN_PASSWORD_LENGTH:
                        audit(connection, actor, "account.password_change", rec["id"], "failure")
                        return error_response("AUTH-105", status_code=422), True
                    salt_hex, hash_hex = _hash_password(new_password, iterations=PBKDF2_ITERATIONS)
                    now_stamp = utc_now()
                    rec.update(
                        salt=salt_hex,
                        passwordHash=hash_hex,
                        iterations=PBKDF2_ITERATIONS,
                        passwordChangedAt=now_stamp,
                    )
                    _write_accounts(connection, accounts)
                    if actor.session_id:
                        connection.execute(
                            "UPDATE owner_sessions SET revoked_at=? WHERE id != ? AND revoked_at IS NULL",
                            (now_stamp, actor.session_id),
                        )
                    else:
                        connection.execute(
                            "UPDATE owner_sessions SET revoked_at=? WHERE revoked_at IS NULL",
                            (now_stamp,),
                        )
                    audit(connection, actor, "account.password_change", rec["id"])
                    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"}), True

            resp, recorded = await run_in_threadpool(_change_pw_tx)
            if recorded:
                request.state.audit_recorded = True
            return resp
        except ValueError:
            return error_response("HUB-501", status_code=422)

    @router.post("/api/account/email")
    async def central_account_email(request: Request):
        from starlette.concurrency import run_in_threadpool

        proto_err = check_protocol_version(request.headers.get("x-burnrate-protocol"))
        if proto_err is not None:
            return error_response(proto_err, status_code=426)
        actor = request.state.principal
        if actor.kind != "owner" or not actor.permits("credentials.manage"):
            return error_response("AUTH-107", status_code=401 if actor.kind == "unauthenticated" else 403)
        try:
            try:
                body = await _bounded_json(request)
            except RequestTooLargeError:
                return error_response("REQ-401", status_code=422)
            except HTTPException as exc:
                return error_response(
                    "NET-002" if exc.status_code == 403 else "APP-901",
                    status_code=exc.status_code,
                )
            except ValueError:
                return error_response("APP-901", status_code=422)
            email = normalize_email(body.get("email"))
            password = str(body.get("password") or "")
            if email is None:
                return error_response("AUTH-106", status_code=422)

            def _set_email_tx():
                from spend_app.auth_model import audit

                initialize(settings.database_path)
                with connect(settings.database_path) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    accounts = _read_accounts(connection)
                    users = accounts["users"]
                    matched_key = None
                    rec = None
                    for k, u in users.items():
                        if isinstance(u, dict) and u.get("id") == actor.id:
                            matched_key, rec = k, u
                            break
                    if rec is None:
                        for k, u in users.items():
                            if isinstance(u, dict) and u.get("role", "owner") == "owner":
                                matched_key, rec = k, u
                                break
                    if not isinstance(rec, dict) or matched_key is None:
                        return error_response("AUTH-104", status_code=401), False
                    rec.setdefault("id", secrets.token_hex(16))
                    iterations = _account_iterations(rec)
                    if not password or not _verify_password(
                        password,
                        str(rec.get("salt") or ""),
                        str(rec.get("passwordHash") or ""),
                        iterations,
                    ):
                        if not password:
                            _verify_password("invalid", "00" * 16, "00" * 32, PBKDF2_ITERATIONS)
                        audit(connection, actor, "account.email_set", rec["id"], "failure")
                        return error_response("AUTH-101", status_code=401), True
                    conflict = users.get(email)
                    if isinstance(conflict, dict) and conflict.get("id") != rec["id"]:
                        audit(connection, actor, "account.email_set", rec["id"], "failure")
                        return error_response("AUTH-103", status_code=403), True
                    if matched_key != email:
                        users.pop(matched_key, None)
                    rec.update(username=email, email=email, emailSetAt=utc_now())
                    users[email] = rec
                    _write_accounts(connection, accounts)
                    audit(connection, actor, "account.email_set", rec["id"])
                    return JSONResponse(
                        {
                            "ok": True,
                            "account": {"username": email, "email": email, "needsEmail": False},
                        },
                        headers={"Cache-Control": "no-store"},
                    ), True

            resp, recorded = await run_in_threadpool(_set_email_tx)
            if recorded:
                request.state.audit_recorded = True
            return resp
        except ValueError:
            return error_response("HUB-501", status_code=422)

    @router.post("/api/account/subscriptions")
    async def central_account_subscriptions(request: Request):
        if not request.state.principal.permits("admin.write"):
            return JSONResponse({"error": "Owner session required to change plans."}, status_code=403)
        try:
            body = await _bounded_json(request)
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


    @router.post("/api/setup/owner-session")
    async def spoke_owner_session(request: Request):
        refused = _setup_request_refused(request)
        if refused:
            return refused
        try:
            try:
                body = await _bounded_json(request)
            except RequestTooLargeError:
                return error_response("REQ-401", status_code=422)
            except HTTPException as exc:
                return error_response(
                    "NET-002" if exc.status_code == 403 else "APP-901",
                    status_code=exc.status_code,
                )
            except ValueError:
                return error_response("APP-901", status_code=422)
            with connect(settings.database_path) as connection:
                state = read_setup_state(connection)
            spoke = state["spokeConfig"]
            if state.get("role") != "spoke" or not spoke.get("enabled"):
                return error_response("AUTH-107", status_code=409)
            from spend_app.hub_origin import hub_origin
            from spend_app.config import resolve_hub_url

            try:
                raw_hub_url = resolve_hub_url() or str(spoke.get("hubUrl") or "").strip()
                validated_hub_url = hub_origin(raw_hub_url)
            except ValueError as exc:
                return error_response(
                    "NET-003" if "HTTPS" in str(exc) else "NET-001",
                    status_code=422,
                )
            _, remote, owner_session = _call_central_server_account(validated_hub_url, "/api/account/login", {
                "username": str(body.get("username") or ""), "password": str(body.get("password") or ""),
                "sessionOnly": True, "deviceLabel": state.get("computerName") or "Paired computer",
            })
            with connect(settings.database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                state = read_setup_state(connection)
                if state["spokeConfig"].get("hubUrl") != spoke.get("hubUrl"):
                    return error_response("AUTH-107", status_code=409)
                state["spokeConfig"]["ownerSession"] = owner_session
                state["spokeConfig"]["ownerSessionExpiresAt"] = remote["expiresAt"]
                write_setup_state(connection, state)
            clear_hub_proxy_cache()
            return JSONResponse(
                {"ok": True, "expiresAt": remote["expiresAt"]},
                headers={"Cache-Control": "no-store"},
            )
        except CodedError as exc:
            return error_response(exc.code, status_code=exc.status_code, retry_after=exc.retry_after)
        except ValueError:
            return error_response("AUTH-101", status_code=401)

    @router.post("/api/setup/rotate-machine")
    async def spoke_rotate_machine(request: Request):
        refused = _setup_request_refused(request)
        if refused:
            return refused
        with connect(settings.database_path) as connection:
            state = read_setup_state(connection)
        spoke = state["spokeConfig"]
        if state.get("role") != "spoke" or not spoke.get("enabled"):
            return error_response("AUTH-107", status_code=409)
        headers = {
            "Authorization": "Bearer " + str(spoke.get("token") or ""),
            "X-BURNRATE-Request": "1",
            "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
            "Content-Type": "application/json",
        }
        from spend_app.hub_origin import hub_origin
        from spend_app.config import resolve_hub_url

        try:
            raw_hub_url = resolve_hub_url() or str(spoke.get("hubUrl") or "").strip()
            hub_url = hub_origin(raw_hub_url)
        except ValueError as exc:
            return error_response(
                "NET-003" if "HTTPS" in str(exc) else "NET-001",
                status_code=422,
            )
        from spend_app.sync import read_sync_cursor
        prior_generation = read_sync_cursor(settings.database_path, hub_url).get("generation")
        client = _HUB_TEST_CLIENTS.get(hub_url)
        try:
            if client is not None:
                response = client.post("/api/auth/machine/rotate", json={}, headers=headers)
            else:
                import httpx
                with httpx.Client(timeout=8, trust_env=False) as http:
                    response = http.post(hub_url + "/api/auth/machine/rotate", json={}, headers=headers)
            if response.status_code != 200:
                try:
                    err_data = response.json() if getattr(response, "content", b"") else {}
                except ValueError:
                    err_data = None
                mapped = map_http_error(
                    response.status_code,
                    err_data,
                    headers=getattr(response, "headers", None),
                    endpoint_path="/api/auth/machine/rotate",
                    has_machine_token=True,
                )
                return error_response(
                    mapped.code,
                    status_code=response.status_code if response.status_code in {401, 403, 426, 429} else mapped.status_code,
                    retry_after=mapped.retry_after,
                )
            replacement = response.json()
            with connect(settings.database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = read_setup_state(connection)
                from spend_app.sync import _cursor_key
                row = connection.execute("SELECT value FROM app_meta WHERE key=?", (_cursor_key(hub_url),)).fetchone()
                generation = json.loads(row[0]).get("generation") if row else None
                if current["spokeConfig"].get("token") != spoke.get("token") or generation != prior_generation:
                    return error_response("DEV-301", status_code=409)
                current["spokeConfig"]["token"] = replacement["token"]
                current["spokeConfig"]["tokenExpiresAt"] = replacement["expiresAt"]
                current["spokeConfig"]["rotationRequired"] = False
                from spend_app.sync import rotate_spoke_sync_credential
                rotate_spoke_sync_credential(connection, hub_url, replacement["token"])
                write_setup_state(connection, current)
            return {"ok": True, "expiresAt": replacement["expiresAt"]}
        except ValueError:
            return error_response("HUB-501", status_code=503)
        except Exception as exc:
            err_code, err_status = map_transport_error(exc)
            return error_response(err_code, status_code=err_status)

    @router.post("/api/setup/account")
    async def setup_connect_account(request: Request):
        refused = _setup_request_refused(request)
        if refused:
            return refused
        try:
            try:
                body = await _bounded_json(request)
            except RequestTooLargeError:
                return error_response("REQ-401", status_code=422)
            except HTTPException as exc:
                return error_response(
                    "NET-002" if exc.status_code == 403 else "APP-901",
                    status_code=exc.status_code,
                )
            except ValueError:
                return error_response("APP-901", status_code=422)
            if not isinstance(body, dict):
                return error_response("APP-901", status_code=422)
            mode = str(body.get("mode") or "login").strip().lower()
            if mode not in {"login", "register"}:
                return error_response("APP-901", status_code=422)
            raw_username = str(body.get("username") or "").strip()
            password = str(body.get("password") or "")
            if mode == "register":
                username = normalize_email(raw_username)
                if username is None:
                    return error_response("AUTH-106", status_code=422)
                if len(password) < MIN_PASSWORD_LENGTH:
                    return error_response("AUTH-105", status_code=422)
            else:
                username = raw_username
                if not username:
                    return error_response("AUTH-106", status_code=422)
                if not password:
                    return error_response("AUTH-101", status_code=422)
            computer_name = str(body.get("computerName") or "").strip() or _default_computer_name()
            from spend_app.config import resolve_hub_url
            from spend_app.hub_origin import hub_origin

            try:
                raw_server_url = resolve_hub_url()
                if not raw_server_url:
                    return error_response("NET-001", status_code=422)
                server_url = hub_origin(raw_server_url)
            except ValueError as exc:
                return error_response(
                    "NET-003" if "HTTPS" in str(exc) else "NET-001",
                    status_code=422,
                )
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
            with connect(settings.database_path) as connection:
                previous = read_setup_state(connection)
            previous_spoke = previous["spokeConfig"]
            reuse_machine = (mode == "login" and previous.get("role") == "spoke"
                            and previous_spoke.get("enabled")
                            and str(previous_spoke.get("hubUrl") or "").rstrip("/").lower() == server_url)
            if reuse_machine:
                remote_payload["sessionOnly"] = True
            working_url, remote_data, owner_session = _call_central_server_account(server_url, endpoint, remote_payload)
            working_url = hub_origin(working_url)
            if reuse_machine:
                token = str(previous_spoke.get("token") or "").strip()
                machine = {"id": previous["account"].get("machineId")}
                machines_list = []
            else:
                reg_headers = {
                    "Authorization": f"Bearer {owner_session}",
                    "X-BURNRATE-Request": "1",
                    "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
                    "Content-Type": "application/json",
                }
                from spend_app.sync import get_or_create_device_id

                reg_payload = {
                    "action": "register",
                    "name": computer_name,
                    "os": sys.platform,
                    "deviceId": get_or_create_device_id(settings.database_path),
                }
                reg_client = _HUB_TEST_CLIENTS.get(working_url)
                try:
                    if reg_client is not None:
                        cookies = getattr(reg_client, "cookies", None)
                        saved_jar = list(cookies.jar) if cookies is not None and hasattr(cookies, "jar") else None
                        if cookies is not None:
                            cookies.clear()
                        try:
                            reg_res = reg_client.post("/api/machines", json=reg_payload, headers=reg_headers)
                        finally:
                            if cookies is not None and saved_jar is not None:
                                cookies.clear()
                                for cookie in saved_jar:
                                    cookies.jar.set_cookie(cookie)
                    else:
                        import httpx
                        with httpx.Client(timeout=8.0, trust_env=False) as http:
                            reg_res = http.post(f"{working_url}/api/machines", json=reg_payload, headers=reg_headers)
                except Exception as exc:
                    err_code, err_status = map_transport_error(exc)
                    raise CodedError(err_code, status_code=err_status) from exc
                try:
                    reg_data = reg_res.json() if getattr(reg_res, "content", b"") else {}
                except ValueError:
                    reg_data = None
                if reg_res.status_code >= 400:
                    raise map_http_error(
                        reg_res.status_code,
                        reg_data,
                        headers=getattr(reg_res, "headers", None),
                        endpoint_path="/api/machines",
                    )
                if not isinstance(reg_data, dict) or not str(reg_data.get("token") or "").strip():
                    raise CodedError("HUB-501", status_code=502)
                token = str(reg_data["token"]).strip()
                machine = reg_data.get("machine") or {}
                machines_list = reg_data.get("machines") or []
            remote_subs = remote_data.get("subscriptions") if isinstance(remote_data.get("subscriptions"), list) else []
            account_info = remote_data.get("account") if isinstance(remote_data.get("account"), dict) else {"username": username}

            account_generation = None
            if not reuse_machine:
                from spend_app.sync import prepare_spoke_sync
                account_generation = prepare_spoke_sync(settings.database_path, working_url, token)
            sync_result = None
            if test_sync:
                sync_client = _HUB_TEST_CLIENTS.get(working_url)
                try:
                    sync_result = sync_once(
                        settings,
                        pricing,
                        hub_url=working_url,
                        token=token,
                        machine_name=computer_name,
                        collect_local=False,
                        client=sync_client,
                    )
                except Exception:
                    # Owner credentials have already been verified. Keep the
                    # session usable independently from a failed first push.
                    sync_result = {
                        "ok": False,
                        "code": "SYNC-201",
                        "error": format_error("SYNC-201"),
                    }

            initialize(settings.database_path)
            with connect(settings.database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                state = read_setup_state(connection)
                if (state["spokeConfig"].get("token") != previous_spoke.get("token")
                        or state["spokeConfig"].get("hubUrl") != previous_spoke.get("hubUrl")):
                    return error_response("AUTH-107", status_code=409)
                if account_generation:
                    from spend_app.sync import _cursor_key
                    row = connection.execute("SELECT value FROM app_meta WHERE key=?", (_cursor_key(working_url),)).fetchone()
                    if not row or json.loads(row[0]).get("generation") != account_generation:
                        return error_response("AUTH-107", status_code=409)
                _sync_plans_into_local_db(connection, settings.timezone, remote_subs)
                state["role"] = "spoke"
                state["computerName"] = computer_name
                account_username = str(account_info.get("username") or username)
                needs_email = bool(account_info.get("needsEmail", not is_valid_email(account_username)))
                prev_dismissed = bool((state.get("account") or {}).get("emailPromptDismissed"))
                state["account"] = {
                    "username": account_username,
                    "machineId": str(machine.get("id") or ""),
                    "connectedAt": utc_now(),
                    "needsEmail": needs_email,
                    "emailPromptDismissed": prev_dismissed if needs_email else False,
                }
                token_expires_at = (
                    (sync_result or {}).get("tokenExpiresAt")
                    or machine.get("expiresAt")
                    or (None if reuse_machine else reg_data.get("expiresAt"))
                    or previous_spoke.get("tokenExpiresAt")
                )
                rotation_required = bool(
                    (sync_result or {}).get("rotationRequired")
                    if isinstance(sync_result, dict) and "rotationRequired" in sync_result
                    else (previous_spoke.get("rotationRequired") if reuse_machine else machine.get("rotationRequired"))
                )
                state["spokeConfig"] = {
                    "hubUrl": working_url,
                    "token": token,
                    "ownerSession": owner_session,
                    "ownerSessionExpiresAt": remote_data.get("expiresAt"),
                    "tokenExpiresAt": token_expires_at,
                    "rotationRequired": rotation_required,
                    "enabled": True,
                    "lastSyncAt": utc_now() if sync_result and sync_result.get("ok") else state["spokeConfig"].get("lastSyncAt"),
                    "lastSyncError": normalize_spoke_sync_error(sync_result.get("error")) if sync_result and not sync_result.get("ok") else None,
                    "usageChangesPending": bool((sync_result or {}).get("usageChangesPending")),
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
                    "machines": machines_list,
                    "syncResult": sync_result,
                },
                headers={"Cache-Control": "no-store"},
            )
        except CodedError as exc:
            return error_response(exc.code, status_code=exc.status_code, retry_after=exc.retry_after)
        except ValueError as exc:
            msg = str(exc)
            if "HTTPS" in msg:
                return error_response("NET-003", status_code=422)
            if "address" in msg.lower():
                return error_response("NET-001", status_code=422)
            return error_response("HUB-501", status_code=422)

    @router.post("/api/setup/account/email")
    async def spoke_set_account_email(request: Request):
        refused = _setup_request_refused(request)
        if refused:
            return refused
        try:
            try:
                body = await _bounded_json(request)
            except RequestTooLargeError:
                return error_response("REQ-401", status_code=422)
            except HTTPException as exc:
                return error_response(
                    "NET-002" if exc.status_code == 403 else "APP-901",
                    status_code=exc.status_code,
                )
            except ValueError:
                return error_response("APP-901", status_code=422)
            email = normalize_email(body.get("email"))
            password = str(body.get("password") or "")
            if email is None:
                return error_response("AUTH-106", status_code=422)
            if not password:
                return error_response("AUTH-101", status_code=401)
            with connect(settings.database_path) as connection:
                state = read_setup_state(connection)
            spoke = state.get("spokeConfig") or {}
            if state.get("role") != "spoke" or not spoke.get("enabled"):
                return error_response("AUTH-107", status_code=409)
            if not str(spoke.get("ownerSession") or "").strip():
                return error_response("AUTH-107", status_code=401)
            from spend_app.sync import HubMutationError
            from starlette.concurrency import run_in_threadpool

            try:
                remote = await run_in_threadpool(
                    post_to_configured_hub,
                    settings,
                    "/api/account/email",
                    {"email": email, "password": password},
                    strict=True,
                )
            except HubMutationError as exc:
                return error_response(
                    exc.code or "HUB-501",
                    status_code=exc.status_code,
                    retry_after=exc.retry_after,
                )
            if not isinstance(remote, dict) or not remote.get("ok"):
                return error_response("HUB-501", status_code=502)
            acct = remote.get("account") if isinstance(remote.get("account"), dict) else {}
            new_username = str(acct.get("username") or email)
            with connect(settings.database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                latest = read_setup_state(connection)
                latest["account"] = {
                    **latest.get("account", {}),
                    "username": new_username,
                    "needsEmail": False,
                    "emailPromptDismissed": False,
                }
                saved = write_setup_state(connection, latest)
            clear_hub_proxy_cache()
            return JSONResponse(
                {"ok": True, "account": saved["account"], "setup": public_setup_state(saved)},
                headers={"Cache-Control": "no-store"},
            )
        except Exception as exc:
            err_code, err_status = map_transport_error(exc)
            return error_response(err_code, status_code=err_status)

    @router.post("/api/setup/account/dismiss-email-prompt")
    async def spoke_dismiss_email_prompt(request: Request):
        refused = _setup_request_refused(request)
        if refused:
            return refused
        initialize(settings.database_path)
        with connect(settings.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = read_setup_state(connection)
            state["account"] = {**state.get("account", {}), "emailPromptDismissed": True}
            saved = write_setup_state(connection, state)
        return JSONResponse(
            {"ok": True, "setup": public_setup_state(saved)},
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/setup/account/password")
    async def spoke_change_account_password(request: Request):
        refused = _setup_request_refused(request)
        if refused:
            return refused
        try:
            try:
                body = await _bounded_json(request)
            except RequestTooLargeError:
                return error_response("REQ-401", status_code=422)
            except HTTPException as exc:
                return error_response(
                    "NET-002" if exc.status_code == 403 else "APP-901",
                    status_code=exc.status_code,
                )
            except ValueError:
                return error_response("APP-901", status_code=422)
            with connect(settings.database_path) as connection:
                state = read_setup_state(connection)
            spoke = state.get("spokeConfig") or {}
            if state.get("role") != "spoke" or not spoke.get("enabled"):
                return error_response("AUTH-107", status_code=409)
            if not str(spoke.get("ownerSession") or "").strip():
                return error_response("AUTH-107", status_code=401)
            from spend_app.sync import HubMutationError
            from starlette.concurrency import run_in_threadpool

            try:
                remote = await run_in_threadpool(
                    post_to_configured_hub,
                    settings,
                    "/api/account/password",
                    {
                        "currentPassword": str(body.get("currentPassword") or ""),
                        "newPassword": str(body.get("newPassword") or ""),
                    },
                    strict=True,
                )
            except HubMutationError as exc:
                return error_response(
                    exc.code or "HUB-501",
                    status_code=exc.status_code,
                    retry_after=exc.retry_after,
                )
            if not isinstance(remote, dict) or not remote.get("ok"):
                return error_response("HUB-501", status_code=502)
            return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
        except Exception as exc:
            err_code, err_status = map_transport_error(exc)
            return error_response(err_code, status_code=err_status)

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


    @router.post("/api/setup/complete")
    async def complete_setup(request: Request):
        refused = _setup_request_refused(request)
        if refused:
            return refused
        try:
            body = await _bounded_json(request)
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

