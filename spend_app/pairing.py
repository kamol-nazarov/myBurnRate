"""Host-bound, expiring, atomic, single-use invitation exchange."""
import hmac
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse

from spend_app.auth_model import PAIRING_MINUTES, Principal, audit, digest, moment, parse, stamp
from spend_app.db import connect


def hub_origin(url):
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("Use a hub HTTP(S) origin without a path, credentials, query or fragment.")
    return f"{parsed.scheme}://{parsed.netloc.lower()}"


def issue(path, actor, *, hub_url, name, os_name="", machine_id=None, now=None):
    from spend_app.setup_wizard import encode_pairing_code
    from spend_app.sync import _read_state
    origin = hub_origin(hub_url)
    if not name or len(name) > 64:
        raise ValueError("Machine name must be between 1 and 64 characters.")
    grant_id, secret = secrets.token_hex(16), "br_p_" + secrets.token_urlsafe(32)
    expiry = stamp(moment(now) + timedelta(minutes=PAIRING_MINUTES))
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        state = _read_state(connection)
        if machine_id and machine_id not in state["machines"]:
            raise ValueError("Unknown machine.")
        # An owner may re-pair a revoked or expired computer without SQL edits.
        if not machine_id:
            machine_id = next((mid for mid, rec in state["machines"].items()
                               if str(rec.get("name", "")).lower() == name.lower()), None)
        connection.execute("INSERT INTO pairing_grants VALUES(?,?,?,?,?,?,?,?,?)",
                           (grant_id, digest(secret), origin, machine_id, name, os_name[:32], stamp(now), expiry, None))
        audit(connection, actor, "pairing.create", grant_id, now=now)
    code = encode_pairing_code(hub_url=origin, token=secret, machine_id=grant_id,
                               machine_name=name, hub_name="Hub")
    return {"ok": True, "pairingCode": code, "hubUrl": origin, "expiresAt": expiry}


def redeem(path, actor, code, request_origin, *, now=None):
    from spend_app.setup_wizard import decode_pairing_code
    from spend_app.sync import _register_in_connection
    decoded = decode_pairing_code(code)
    grant_id = str(decoded.get("machineId") or "")
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        rec = connection.execute("SELECT * FROM pairing_grants WHERE id=?", (grant_id,)).fetchone()
        if (not rec or not hmac.compare_digest(digest(str(decoded.get("token") or "")), rec["secret_hash"])
                or rec["used_at"] or not parse(rec["expires_at"]) or parse(rec["expires_at"]) <= moment(now)
                or request_origin != rec["hub_origin"] or hub_origin(str(decoded["hubUrl"])) != rec["hub_origin"]):
            audit(connection, actor, "pairing.redeem", "pairing", "failure", now=now)
            return JSONResponse({"error": "Pairing code invalid, expired, used or bound to another Host."}, status_code=403)
        result = _register_in_connection(connection, name=rec["machine_name"], os_name=rec["os_name"],
                                         machine_id=rec["machine_id"], now=now)
        actor = Principal("pairing", grant_id)
        connection.execute("UPDATE pairing_grants SET used_at=? WHERE id=?", (stamp(now), grant_id))
        audit(connection, actor, "pairing.redeem", grant_id, now=now)
        audit(connection, actor, "machine.register", result["machine"]["id"], now=now)
    # A redemption grant never grants owner or registry access.
    result.pop("machines", None)
    return JSONResponse({"ok": True, **result}, headers={"Cache-Control": "no-store"})


def exchange_remote(hub_url, code):
    from spend_app.sync import _HUB_TEST_CLIENTS
    test_client = _HUB_TEST_CLIENTS.get(hub_url)
    if test_client is not None:
        response = test_client.post("/api/pairing/redeem", json={"pairingCode": code})
    else:
        import httpx
        with httpx.Client(timeout=8, trust_env=False) as client:
            response = client.post(hub_url + "/api/pairing/redeem", json={"pairingCode": code})
    if response.status_code != 200:
        raise ValueError("Pairing refused. Create a fresh code on the hub and try again.")
    result = response.json()
    if not isinstance(result, dict) or not result.get("token"):
        raise ValueError("Invalid pairing response.")
    return result
