"""Dated subscription history exchanged during owner sign-in."""

import json
import re
import uuid

from spend_app.plan_service import (
    FIELDS,
    PlanConflict,
    PlanError,
    _validate_overlap,
    iso_date,
    list_plans,
    validate_term,
)

_STORE_KEY = "subscription_store.v1"
_TERM_FIELDS = ("name", "tool_key", "amount_usd", "cadence", "start_date", "end_date")
_REQUEST_LIMIT = 65536


def _ensure_store(connection):
    # This is additive and lazy, so opening an old database leaves its plans
    # intact and it can still be opened by a peer running the old application.
    connection.execute("""
        CREATE TABLE IF NOT EXISTS subscription_transfer_links (
            origin TEXT NOT NULL,
            source_id INTEGER NOT NULL,
            plan_id INTEGER NOT NULL UNIQUE REFERENCES subscription_plans(id) ON DELETE CASCADE,
            hub_store TEXT,
            hub_revision INTEGER,
            local_revision INTEGER,
            PRIMARY KEY (origin, source_id)
        )
    """)
    row = connection.execute(
        "SELECT value FROM app_meta WHERE key=?", (_STORE_KEY,)
    ).fetchone()
    if row is None:
        store = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO app_meta (key,value) VALUES (?,?)", (_STORE_KEY, store)
        )
        return store
    return row["value"]


def export_history(connection, today, *, native_only=False):
    store = _ensure_store(connection)
    links = {
        row["plan_id"]: row
        for row in connection.execute("SELECT * FROM subscription_transfer_links")
    }
    plans = []
    for plan in list_plans(connection, today)["plans"]:
        plan_id = plan["id"]
        link = links.get(plan_id)
        if native_only and link is not None:
            continue
        plans.append(
            {
                "origin": link["origin"] if link else store,
                "sourceId": link["source_id"] if link else plan_id,
                "version": plan["version"],
                "end_date": plan["end_date"],
                "terms": [
                    {field: term[field] for field in _TERM_FIELDS}
                    for term in plan["terms"]
                ],
            }
        )
    return {"store": store, "plans": plans}


def attach_sign_in_history(payload, history):
    """Keep the sign-in request within its existing body limit."""
    payload["subscriptionHistory"] = history
    if len(json.dumps(payload).encode()) <= _REQUEST_LIMIT:
        return False
    payload["subscriptionHistory"] = {"store": history["store"], "plans": []}
    return True


def deferred_uploads(history):
    """Yield bounded batches without splitting an individual plan's terms."""
    plans = []
    size = len(
        json.dumps(
            {
                "action": "import-history",
                "expectedStore": history["store"],
                "subscriptionHistory": {
                    "store": history["store"],
                    "plans": [],
                },
            }
        ).encode()
    )
    base_size = size
    for plan in history["plans"]:
        plan_size = len(json.dumps(plan).encode()) + 2
        if base_size + plan_size > _REQUEST_LIMIT:
            raise PlanError("A plan history exceeds the account transfer size limit.")
        if size + plan_size > _REQUEST_LIMIT:
            yield {"store": history["store"], "plans": plans}
            plans = []
            size = base_size
        plans.append(plan)
        size += plan_size
    if plans:
        yield {"store": history["store"], "plans": plans}


def _is_store(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value) is not None


def _is_id(value):
    return type(value) is int and 0 < value < 2**63


def _read_history(history, *, from_hub):
    if not isinstance(history, dict) or not _is_store(history.get("store")):
        raise PlanError("Invalid subscription history source.")
    sender = history["store"]
    plans = history.get("plans")
    if not isinstance(plans, list):
        raise PlanError("Invalid subscription history.")
    seen = set()
    decoded = []
    for plan in plans:
        if not isinstance(plan, dict):
            raise PlanError("Invalid subscription history plan.")
        origin = plan.get("origin")
        source_id, version = plan.get("sourceId"), plan.get("version")
        if not _is_store(origin) or not _is_id(source_id) or not _is_id(version):
            raise PlanError("Invalid subscription history identity.")
        identity = (origin, source_id)
        if identity in seen or (not from_hub and origin != sender):
            raise PlanError("Invalid subscription history origin.")
        seen.add(identity)
        end_date = plan.get("end_date")
        iso_date(end_date, optional=True)
        terms = plan.get("terms")
        if not isinstance(terms, list) or not terms:
            raise PlanError("Subscription history must include its effective terms.")
        validated = []
        for values in terms:
            if (
                not isinstance(values, dict)
                or set(values) != FIELDS
                or not isinstance(values["tool_key"], str)
                or not isinstance(values["cadence"], str)
            ):
                raise PlanError("Invalid subscription history term.")
            term = validate_term(values)
            # These stored associations retain their historical meaning when
            # imported, even though a newly entered plan uses the current key.
            if values["tool_key"] in ("cursor", "zcode"):
                term["tool_key"] = values["tool_key"]
            validated.append(term)
        _validate_overlap(validated)
        decoded.append((identity, version, end_date, validated))
    return sender, decoded


def _replace_terms(connection, plan_id, terms):
    # Existing term IDs carry audit and daily cost references. Update a term
    # in place when its effective start matches, and add only new terms.
    previous = {
        row["start_date"]: row["id"]
        for row in connection.execute(
            "SELECT id,start_date FROM subscriptions WHERE plan_id=?", (plan_id,)
        )
    }
    for term in terms:
        term_id = previous.pop(term["start_date"], None)
        values = tuple(term[field] for field in _TERM_FIELDS)
        if term_id is None:
            connection.execute(
                "INSERT INTO subscriptions(name,tool_key,amount_usd,cadence,start_date,end_date,plan_id) VALUES(?,?,?,?,?,?,?)",
                (*values, plan_id),
            )
        else:
            connection.execute(
                "UPDATE subscriptions SET name=?,tool_key=?,amount_usd=?,cadence=?,start_date=?,end_date=? WHERE id=?",
                (*values, term_id),
            )
    for term_id in previous.values():
        connection.execute(
            "DELETE FROM subscription_daily_costs WHERE subscription_id=?", (term_id,)
        )
        connection.execute("DELETE FROM subscriptions WHERE id=?", (term_id,))


def import_history(connection, history, *, from_hub=False, expected_store=None):
    """Install complete dated plans. The hub owns a plan once it adopts it."""
    sender, plans = _read_history(history, from_hub=from_hub)
    local_store = _ensure_store(connection)
    if expected_store is not None and expected_store != local_store:
        raise PlanConflict(
            "The subscription history source changed. Reconnect to the hub."
        )
    changed = 0
    for (origin, source_id), revision, end_date, terms in plans:
        link = connection.execute(
            """
            SELECT l.*,p.version FROM subscription_transfer_links l
            JOIN subscription_plans p ON p.id=l.plan_id
            WHERE l.origin=? AND l.source_id=?
            """,
            (origin, source_id),
        ).fetchone()
        if link is not None:
            if (
                from_hub
                and sender == link["hub_store"]
                and revision > link["hub_revision"]
                and link["version"] == link["local_revision"]
            ):
                plan_id = link["plan_id"]
                local_revision = link["version"] + 1
                _replace_terms(connection, plan_id, terms)
                connection.execute(
                    "UPDATE subscription_plans SET version=?,end_date=? WHERE id=?",
                    (local_revision, end_date, plan_id),
                )
                connection.execute(
                    "UPDATE subscription_transfer_links SET hub_revision=?,local_revision=? WHERE plan_id=?",
                    (revision, local_revision, plan_id),
                )
                changed += 1
            continue
        if origin == local_store:
            if not from_hub:
                continue
            local = connection.execute(
                "SELECT version,end_date FROM subscription_plans WHERE id=?",
                (source_id,),
            ).fetchone()
            local_terms = [
                dict(row)
                for row in connection.execute(
                    "SELECT name,tool_key,amount_usd,cadence,start_date,end_date FROM subscriptions "
                    "WHERE plan_id=? ORDER BY start_date",
                    (source_id,),
                )
            ]
            if local is None or local["end_date"] != end_date or local_terms != terms:
                continue
            plan_id, local_revision = source_id, local["version"]
        else:
            plan_id = connection.execute(
                "INSERT INTO subscription_plans(end_date) VALUES(?)", (end_date,)
            ).lastrowid
            local_revision = 1
            _replace_terms(connection, plan_id, terms)
            changed += 1
        connection.execute(
            """INSERT INTO subscription_transfer_links
               (origin,source_id,plan_id,hub_store,hub_revision,local_revision) VALUES(?,?,?,?,?,?)""",
            (
                origin,
                source_id,
                plan_id,
                sender if from_hub else None,
                revision if from_hub else None,
                local_revision,
            ),
        )
    return changed
