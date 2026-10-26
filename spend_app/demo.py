"""A demo BURNRATE with made-up usage, for trying it out and for screenshots.

``burnrate demo`` seeds a fresh database in its own folder and serves it
with collection, quota polling and HUD window control off, so nothing is
collected from this computer. Every number is synthetic and the projects are
fictional (Northwind-style names). The same seed draws the same month every
time; times follow the clock, so the demo is always "live".
"""

from __future__ import annotations

import contextlib
import os
import random
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEMO_PORT = 17377
SEED = 20261026
PROJECTS = ("northwind-web", "contoso-api", "fabrikam-ml", "tailspin-mobile", "wingtip-docs")
# tool, source, models newest first, share of sessions. A day uses the newest
# model priced on it and skips a tool with none, so every demo row is priced.
MIX = (
    ("claude-code", "claude_local", ("claude-opus-5-5", "claude-opus-5"), 0.34),
    ("claude-code", "claude_local", ("claude-sonnet-5-5", "claude-sonnet-5"), 0.18),
    ("codex", "codex_local", ("gpt-6.1-sol", "gpt-5.6-sol"), 0.26),
    ("codex", "codex_local", ("gpt-5.6-terra",), 0.08),
    ("opencode-go", "opencode_local", ("opencode-go:glm-5.3", "opencode-go:glm-5.2"), 0.09),
    ("grok", "grok_local", ("xai:grok-4.6",), 0.07),
)
LAPTOP = "Travel Laptop"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _priced_model(pricing, models: tuple[str, ...], when: datetime) -> str | None:
    from spend_app.pricing import UnpricedModelError

    for model in models:
        try:
            pricing.resolve(model, when)
            return model
        except UnpricedModelError:
            continue
    return None


def _rows(now: datetime, rng: random.Random, pricing, *, days: int):
    from spend_app.adapters.common import UsageRow

    weights = [share for *_rest, share in MIX]
    for day in range(days, -1, -1):
        date = (now - timedelta(days=day)).date()
        weekend = date.weekday() >= 5
        for index in range(rng.randint(2, 5) if weekend else rng.randint(8, 13)):
            tool, source, models, _share = rng.choices(MIX, weights=weights)[0]
            project = rng.choices(PROJECTS, weights=(5, 4, 3, 2, 1))[0]
            hour = rng.choice((9, 10, 10, 11, 13, 14, 14, 15, 16, 17, 20, 21))
            start = datetime(date.year, date.month, date.day, hour, rng.randint(0, 59), tzinfo=UTC)
            model = _priced_model(pricing, models, start)
            if model is None:
                continue
            session = f"demo-{date.isoformat()}-{index}"
            for turn in range(rng.randint(12, 36)):
                when = start + timedelta(minutes=turn * rng.randint(1, 4), seconds=rng.randint(0, 59))
                if when > now:
                    break
                context = 40_000 + turn * rng.randint(4_000, 9_000)
                yield UsageRow(
                    source=source, tool_key=tool, model_key=model, occurred_at=when, session_id=session,
                    project=project, input_tokens=context + rng.randint(800, 6_000), cached_input_tokens=context,
                    cache_write_tokens=rng.randint(1_500, 9_000) if tool == "claude-code" else 0, cache_write_1h_tokens=0,
                    output_tokens=rng.randint(900, 6_500), reasoning_tokens=None, cost_usd=None,
                    raw_id=f"{source}:{session}:{turn}",
                )


def seed(database_path: Path, *, now: datetime | None = None, days: int = 30) -> dict:
    """Fill an empty database with a month of synthetic usage, plans and meters."""
    from spend_app.adapters.common import persist_rows
    from spend_app.config import ROOT
    from spend_app.db import connect, initialize, upsert_quota
    from spend_app.pricing import PricingEngine
    from spend_app.setup_wizard import read_setup_state, write_setup_state
    from spend_app.subscriptions import add_subscription
    from spend_app.sync import prefix_machine_id, register_machine, rename_machine

    now = (now or datetime.now(UTC)).astimezone(UTC)
    rng = random.Random(SEED)
    initialize(database_path)
    pricing = PricingEngine.load(ROOT / "pricing")
    rename_machine(database_path, machine_id="local", name="Studio PC")
    laptop = register_machine(database_path, name=LAPTOP, os_name="macOS", now=now)["machine"]["id"]

    by_source: dict[str, list] = {}
    for row in _rows(now, rng, pricing, days=days):
        by_source.setdefault(row.source, []).append(row)
    for row in _rows(now, random.Random(SEED + 1), pricing, days=days):
        if rng.random() < 0.35:  # the laptop is the lighter computer
            moved = replace(row, raw_id=prefix_machine_id(laptop, row.raw_id),
                            session_id=prefix_machine_id(laptop, row.session_id))
            by_source.setdefault(row.source, []).append(moved)
    for source, rows in by_source.items():
        persist_rows(database_path=database_path, pricing=pricing, source=source, usage_rows=rows)

    month_start = now.date().replace(day=1).isoformat()
    with connect(database_path) as connection:
        for tool, name, amount in (("claude-code", "Claude Max 20x", 200.0), ("codex", "ChatGPT Pro", 200.0),
                                   ("opencode-go", "OpenCode Go", 10.0)):
            add_subscription(connection, tool_key=tool, name=name, amount_usd=amount, cadence="monthly",
                             start_date=month_start, end_date=None)
        polled = _iso(now)
        meters = (
            ("claude-code", "5h", "Claude 5-hour window", 46.0, now + timedelta(hours=2, minutes=14)),
            ("claude-code", "weekly", "Claude weekly window", 61.0, now + timedelta(days=3, hours=6)),
            ("codex", "weekly", "Codex weekly window", 38.0, now + timedelta(days=4, hours=2)),
            ("grok", "weekly", "Grok Build weekly", 12.0, now + timedelta(days=5)),
            ("opencode-go", "5h", "OpenCode Go 5-hour window", 31.0, now + timedelta(hours=1, minutes=5)),
            ("opencode-go", "weekly", "OpenCode Go weekly window", 44.0, now + timedelta(days=2, hours=9)),
            ("opencode-go", "monthly", "OpenCode Go monthly window", 54.0, now + timedelta(days=12)),
        )
        for provider, limit, label, pct, resets in meters:
            # Codex reports from the laptop, so no Codex sign-in on this computer is read.
            source = f"sync:{laptop}:demo" if provider == "codex" else "demo"
            upsert_quota(connection, provider_key=provider, limit_key=limit, label=label, unit="pct", pct=pct,
                         resets_at=_iso(resets), source=source, polled_at=polled, is_payg=False)
        upsert_quota(connection, provider_key="openrouter", limit_key="balance", label="OpenRouter funds remaining",
                     unit="usd", used=23.40, source="demo", polled_at=polled, is_payg=True)
        state = read_setup_state(connection)
        state.update(completed=True, completedAt=polled, role="hub", computerName="Studio PC")
        write_setup_state(connection, state)
    refresh_agents(database_path, now=now, laptop=laptop)
    return {"database": str(database_path), "laptop": laptop}


AGENTS = (
    ("claude:demo-live-1", "Claude Code · northwind-web", "claude-opus-5-5"),
    ("codex:demo-live-2", "Codex · contoso-api", "gpt-6.1-sol"),
)


def refresh_agents(database_path: Path, *, now: datetime | None = None, laptop: str | None = None) -> None:
    """Keep two agents running and the laptop online, so the live parts have something to show."""
    from spend_app.db import connect
    from spend_app.sync import _read_state, _write_state, prefix_machine_id

    stamp = _iso(now or datetime.now(UTC))
    with connect(database_path) as connection:
        state = _read_state(connection)
        if laptop in state["machines"]:
            state["machines"][laptop]["last_seen_at"] = stamp
            _write_state(connection, state)
        for index, (identifier, name, model) in enumerate(AGENTS):
            if index == 1 and laptop:
                identifier = prefix_machine_id(laptop, identifier)
            connection.execute(
                "INSERT INTO agent_runs(id, name, model_key, state, started_at, last_seen_at) VALUES(?, ?, ?, 'running', ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET last_seen_at=excluded.last_seen_at, state='running'",
                (identifier, name, model, stamp, stamp),
            )


def run(*, port: int = DEMO_PORT, data_root: Path | None = None, open_browser: bool = True) -> int:
    """Seed a fresh demo and serve it on 127.0.0.1 until interrupted."""
    import tempfile

    root = Path(data_root) if data_root else Path(tempfile.mkdtemp(prefix="burnrate-demo-"))
    root.mkdir(parents=True, exist_ok=True)
    # Point everything at the demo folder before the app is imported.
    os.environ["BURNRATE_DATA_ROOT"] = str(root)
    os.environ["SPEND_DATABASE_PATH"] = str(root / "spend.db")
    os.environ.setdefault("BURNRATE_ENABLE_DASHBOARD", "1")
    os.environ["BURNRATE_DESKTOP_NOTIFICATIONS"] = "0"
    os.environ["BURNRATE_DEMO"] = "1"
    database = root / "spend.db"
    if not database.exists():
        info = seed(database)
    else:
        from spend_app.db import connect
        from spend_app.sync import _read_state

        with connect(database) as connection:
            info = {"laptop": next(iter(_read_state(connection)["machines"]), None)}

    def keep_live() -> None:
        while True:
            time.sleep(20)
            # A busy database skips one refresh; the next one catches up.
            with contextlib.suppress(sqlite3.Error):
                refresh_agents(database, laptop=info.get("laptop"))

    threading.Thread(target=keep_live, daemon=True).start()
    import uvicorn

    from spend_app.api import create_app
    from spend_app.config import load_settings

    app = create_app(load_settings(), enable_scheduler=False)
    url = f"http://127.0.0.1:{port}/hud"
    print(f"BURNRATE demo with made-up data: {url}", flush=True)
    print(f"Dashboard: http://127.0.0.1:{port}/   Demo data: {root}   Stop: Ctrl+C", flush=True)
    if open_browser:
        import webbrowser

        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0
