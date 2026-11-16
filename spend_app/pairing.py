"""Host-bound, expiring, atomic, single-use invitation exchange."""
import hmac
import ipaddress
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse

from spend_app.auth_model import PAIRING_MINUTES, Principal, audit, digest, moment, parse, stamp
from spend_app.db import connect


def hub_origin(url):
    parsed = urlsplit(str(url or "").strip())
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("Use a hub HTTP(S) origin without a path, credentials, query or fragment.")
    host = parsed.hostname.lower()
    is_loopback = host == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if parsed.scheme != "https" and not is_loopback:
        raise ValueError("Use an HTTPS origin for non-loopback hubs.")
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


def issue(path, actor, *, hub_url, name, os_name="", machine_id=None, now=None):
    from spend_app.setup_wizard import encode_pairing_code
    from spend_app.sync import _ensure_unique_active_machine_name, _read_state
    origin = hub_origin(hub_url)
    clean_name = (name or "").strip()
    if not clean_name or len(clean_name) > 64:
        raise ValueError("Machine name must be between 1 and 64 characters.")
    grant_id, secret = secrets.token_hex(16), "br_p_" + secrets.token_urlsafe(32)
    current = moment(now)
    expiry = stamp(current + timedelta(minutes=PAIRING_MINUTES))
    by_name = 0 if machine_id else 1
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        state = _read_state(connection)
        if machine_id:
            if machine_id not in state["machines"]:
                raise ValueError("Unknown machine.")
            _ensure_unique_active_machine_name(state, clean_name, exclude_machine_id=machine_id)
        else:
            # Name-only binding requires an unambiguous non-revoked match; revoked
            # or colliding names must be paired explicitly by machine ID.
            matches = [
                mid for mid, rec in state["machines"].items()
                if isinstance(rec, dict) and str(rec.get("name", "")).strip().lower() == clean_name.lower()
            ]
            active = [mid for mid in matches if not state["machines"][mid].get("revoked")]
            if matches:
                if len(active) != 1:
                    raise ValueError(
                        "Multiple or revoked machines match that name; specify the machine ID."
                    )
                machine_id = active[0]
            else:
                # Reserve a stable machine ID for new name-only invites so revoking
                # once invalidates any spare invitations for the same computer name.
                existing_rows = connection.execute(
                    "SELECT machine_id, expires_at FROM pairing_grants "
                    "WHERE used_at IS NULL AND by_name=1 AND machine_id IS NOT NULL AND LOWER(TRIM(machine_name))=LOWER(?) "
                    "ORDER BY created_at DESC",
                    (clean_name,),
                ).fetchall()
                for row in existing_rows:
                    exp = parse(row["expires_at"])
                    cand_id = row["machine_id"]
                    if exp and exp > current and cand_id not in state["machines"]:
                        machine_id = cand_id
                        break
                if not machine_id:
                    machine_id = f"machine-{secrets.token_hex(8)}"
                    while (
                        machine_id in state["machines"]
                        or machine_id in {"local", "all"}
                        or connection.execute("SELECT 1 FROM pairing_grants WHERE machine_id=?", (machine_id,)).fetchone()
                    ):
                        machine_id = f"machine-{secrets.token_hex(8)}"
        connection.execute(
            "INSERT INTO pairing_grants"
            "(id, secret_hash, hub_origin, machine_id, machine_name, os_name, created_at, expires_at, used_at, by_name) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (grant_id, digest(secret), origin, machine_id, clean_name, os_name[:32], stamp(now), expiry, None, by_name),
        )
        audit(connection, actor, "pairing.create", grant_id, now=now)
    code = encode_pairing_code(hub_url=origin, token=secret, machine_id=grant_id,
                               machine_name=clean_name, hub_name="Hub")
    return {"ok": True, "pairingCode": code, "hubUrl": origin, "expiresAt": expiry}


def redeem(path, actor, code, request_origin, *, now=None):
    from spend_app.setup_wizard import decode_pairing_code
    from spend_app.sync import _ensure_unique_active_machine_name, _read_state, _register_in_connection
    decoded = decode_pairing_code(code)
    grant_id = str(decoded.get("machineId") or "")
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        rec = connection.execute("SELECT * FROM pairing_grants WHERE id=?", (grant_id,)).fetchone()
        if (not rec or not hmac.compare_digest(digest(str(decoded.get("token") or "")), rec["secret_hash"])
                or rec["used_at"] or not rec["machine_id"]
                or not parse(rec["expires_at"]) or parse(rec["expires_at"]) <= moment(now)
                or request_origin != rec["hub_origin"] or hub_origin(str(decoded["hubUrl"])) != rec["hub_origin"]):
            audit(connection, actor, "pairing.redeem", "pairing", "failure", now=now)
            return JSONResponse({"error": "Pairing code invalid, expired, used or bound to another Host."}, status_code=403)
        state = _read_state(connection)
        target_id = rec["machine_id"]
        grant_name = str(rec["machine_name"] or "").strip()
        is_name_bound = bool(rec["by_name"]) if "by_name" in rec.keys() else False
        try:
            if is_name_bound and target_id in state["machines"]:
                existing = state["machines"][target_id]
                if not isinstance(existing, dict) or existing.get("revoked"):
                    raise ValueError("Revoked machine cannot be revived by a name-only invitation.")
                if str(existing.get("name", "")).strip().lower() != grant_name.lower():
                    raise ValueError("Pairing name no longer matches that machine.")
            if is_name_bound:
                for mid, mrec in state["machines"].items():
                    if mid != target_id and isinstance(mrec, dict) and str(mrec.get("name", "")).strip().lower() == grant_name.lower():
                        raise ValueError("Pairing name now matches a different machine.")
            _ensure_unique_active_machine_name(state, grant_name, exclude_machine_id=target_id)
            result = _register_in_connection(
                connection,
                name=grant_name,
                os_name=rec["os_name"],
                machine_id=target_id,
                allow_new_id=bool(target_id),
                now=now,
            )
        except ValueError:
            audit(connection, actor, "pairing.redeem", "pairing", "failure", now=now)
            return JSONResponse(
                {"error": "Pairing code no longer matches a unique active machine; create a fresh code with the machine ID."},
                status_code=403,
            )
        actor = Principal("pairing", grant_id)
        connection.execute("UPDATE pairing_grants SET used_at=? WHERE id=?", (stamp(now), grant_id))
        audit(connection, actor, "pairing.redeem", grant_id, now=now)
        audit(connection, actor, "machine.register", result["machine"]["id"], now=now)
    # A redemption grant never grants owner or registry access.
    result.pop("machines", None)
    return JSONResponse({"ok": True, **result}, headers={"Cache-Control": "no-store"})


def exchange_remote(hub_url, code):
    from spend_app.sync import _HUB_TEST_CLIENTS
    hub_url = hub_origin(hub_url)
    headers = {"X-BURNRATE-Request": "1", "Content-Type": "application/json"}
    test_client = _HUB_TEST_CLIENTS.get(hub_url)
    if test_client is not None:
        response = test_client.post("/api/pairing/redeem", json={"pairingCode": code}, headers=headers)
    else:
        import httpx
        with httpx.Client(timeout=8, trust_env=False) as client:
            response = client.post(hub_url + "/api/pairing/redeem", json={"pairingCode": code}, headers=headers)
    if response.status_code != 200:
        raise ValueError("Pairing refused. Create a fresh code on the hub and try again.")
    result = response.json()
    if not isinstance(result, dict) or not result.get("token"):
        raise ValueError("Invalid pairing response.")
    return result
