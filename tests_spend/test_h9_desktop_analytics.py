"""Comprehensive backend, bridge, and headless-Chrome UI tests for H9 desktop analytics."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from spend_app.native_desktop import DemoClient, DesktopError, demo_entity, demo_summary
from spend_app.wealthfront_window import (
    ANALYTICS_WINDOWS,
    DesktopBridge,
    normalize_analytics_window,
    period_for_analytics_window,
)
from tests_spend.test_wealthfront_ui import (
    FAST,
    browser,
    build_page,
    needs_chrome,
    probe,
)


# ---------------------------------------------------------------------------
# Backend & Bridge Python unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("window", ANALYTICS_WINDOWS)
def test_demo_client_summary_all_12_windows(window):
    client = DemoClient()
    summary = client.request(f"/api/spend/summary?window={window}&tool=all&machine=all")

    assert summary["window"]["key"] == window
    assert summary["window"]["label"]
    assert summary["status"] == "ok"
    assert len(summary["tools"]) == 5
    assert len(summary["models"]) == 6
    assert len(summary["series"]) >= 14
    assert len(summary["heatmap"]) == 7 * 24

    expected_fallback = window in {"15m", "30m", "1h", "3h", "6h", "12h", "1d"}
    assert summary["heatmapFallback"] is expected_fallback

    totals = summary["totals"]
    assert totals["trackedValue"] > 0
    assert totals["tokens"] > 0
    assert totals["records"] > 0
    assert totals["sessions"] >= 1
    assert totals["effectiveCostPerMillionTokens"] > 0
    assert totals["cacheReusePct"] > 0
    assert totals["unpricedModels"] == []

    coverage = summary["coverage"]
    assert len(coverage["exactProviders"]) == 2
    assert len(coverage["derivedProviders"]) == 3
    assert summary["totals"]["trackedValueMarker"] == "≈"

    assert len(summary["mix"]) == 4
    mix_share_sum = sum(item["share"] for item in summary["mix"])
    assert 99.0 <= mix_share_sum <= 101.0

    assert summary["waste"]["perDay"] > 0
    assert summary["waste"]["perMonth"] == pytest.approx(
        round(summary["waste"]["perDay"] * 30, 2)
    )
    assert len(summary["waste"]["items"]) == 2
    assert summary["cacheSavings"] > 0
    assert summary["projected"]["multipleBasis"] == "ratio"
    assert summary["projected"]["multiple"] > 0


def test_demo_client_summary_unknown_window_and_tool_filter():
    client = DemoClient()
    fallback = client.request("/api/spend/summary?window=unknown-span&tool=all")
    assert fallback["window"]["key"] == "1d"

    all_tools = client.request("/api/spend/summary?window=1d&tool=all")
    claude_only = client.request("/api/spend/summary?window=1d&tool=claude-code")

    assert [t["key"] for t in claude_only["tools"]] == ["claude-code"]
    assert claude_only["models"]
    assert all(m["toolKey"] == "claude-code" for m in claude_only["models"])
    assert claude_only["coverage"]["exactProviders"] == ["Claude Code"]
    assert claude_only["coverage"]["derivedProviders"] == []
    assert claude_only["totals"]["trackedValueMarker"] == ""
    assert claude_only["totals"]["tokens"] < all_tools["totals"]["tokens"]
    assert all(
        item["toolKey"] == "claude-code" for item in claude_only["waste"]["items"]
    )
    assert all(
        list(bucket["byTool"].keys()) == ["claude-code"]
        for bucket in claude_only["series"]
    )

    # Mutating returned dict must not affect subsequent calls.
    claude_only["tools"].clear()
    fresh = client.request("/api/spend/summary?window=1d&tool=claude-code")
    assert len(fresh["tools"]) == 1


@pytest.mark.parametrize(
    ("kind", "key", "expected_exact", "expected_marker"),
    [
        ("model", "claude-opus-4-6", True, ""),
        ("model", "glm-4.7", False, "≈"),
        ("tool", "claude-code", True, ""),
        ("tool", "opencode", False, "≈"),
    ],
)
def test_demo_client_entity_model_and_tool(
    kind, key, expected_exact, expected_marker
):
    client = DemoClient()
    entity_1d = client.request(
        f"/api/spend/entity?kind={kind}&key={key}&window=1d&machine=all"
    )
    entity_1w = client.request(
        f"/api/spend/entity?kind={kind}&key={key}&window=1w&machine=all"
    )

    assert entity_1d["kind"] == kind
    assert entity_1d["key"] == key
    assert entity_1d["window"]["key"] == "1d"
    assert entity_1d["isExact"] is expected_exact
    assert entity_1d["valueMarker"] == expected_marker
    assert entity_1d["tokens"] > 0
    assert entity_1d["value"] > 0
    assert len(entity_1d["series"]) == 24
    assert len(entity_1d["mix"]) == 4
    assert entity_1d["opportunity"]["kind"] in {"cache_gap", "healthy"}
    assert len(entity_1d["sessions"]["rows"]) == 3
    assert entity_1d["sessions"]["shownTotal"] > 0

    assert entity_1w["window"]["key"] == "1w"
    assert entity_1w["tokens"] > entity_1d["tokens"]
    assert entity_1w["value"] > entity_1d["value"]


def test_demo_client_health_and_write_guard():
    client = DemoClient()
    health = client.request("/api/spend/health")
    assert health == {"status": "ok", "pricingGaps": []}
    with pytest.raises(DesktopError, match="disabled"):
        client.request("/api/spend/summary", {"window": "1d"})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        *((w, w) for w in ANALYTICS_WINDOWS),
        ("today", "1d"),
        ("week", "1w"),
        ("month", "mtd"),
        ("7d", "1w"),
        ("30d", "1mo"),
        ("MTD", "mtd"),
        ("YTD", "ytd"),
        ("All", "all"),
        (" 6H ", "6h"),
        (" TODAY ", "1d"),
        ("unknown", "1d"),
        ("", "1d"),
        (None, "1d"),
    ],
)
def test_normalize_analytics_window(raw, expected):
    assert normalize_analytics_window(raw) == expected


@pytest.mark.parametrize(
    ("win", "expected_period"),
    [
        ("15m", "today"),
        ("30m", "today"),
        ("1h", "today"),
        ("3h", "today"),
        ("6h", "today"),
        ("12h", "today"),
        ("1d", "today"),
        ("1w", "week"),
        ("1mo", "month"),
        ("mtd", "month"),
        ("ytd", "all"),
        ("all", "all"),
        ("7d", "week"),
        ("30d", "month"),
        ("month", "month"),
        ("bogus", "today"),
    ],
)
def test_period_for_analytics_window(win, expected_period):
    assert period_for_analytics_window(win) == expected_period


def test_bridge_save_analytics_window_persists_and_reloads(tmp_path):
    prefs = tmp_path / "native-window.json"
    prefs.write_text(json.dumps({"period": "week", "customKey": 42}), encoding="utf-8")

    bridge = DesktopBridge(DemoClient(), preferences=prefs, demo=True)
    initial = bridge.get_initial_state()
    assert initial["period"] == "week"
    assert initial["analyticsWindow"] == "1w"

    res = bridge.save_analytics_window("6h")
    assert res == {"ok": True, "analyticsWindow": "6h", "period": "today"}
    assert bridge.get_initial_state()["analyticsWindow"] == "6h"
    assert bridge.get_initial_state()["period"] == "today"

    saved = json.loads(prefs.read_text(encoding="utf-8"))
    assert saved["analyticsWindow"] == "6h"
    assert saved["period"] == "today"
    assert saved["customKey"] == 42

    reloaded = DesktopBridge(DemoClient(), preferences=prefs, demo=True)
    assert reloaded.get_initial_state()["analyticsWindow"] == "6h"
    assert reloaded.get_initial_state()["period"] == "today"

    alias_res = reloaded.save_analytics_window("30d")
    assert alias_res == {"ok": True, "analyticsWindow": "1mo", "period": "month"}

    unknown_res = reloaded.save_analytics_window("not-a-window")
    assert unknown_res == {"ok": True, "analyticsWindow": "1d", "period": "today"}


class RecordingClient:
    def __init__(self, *, summary_exc=None, entity_exc=None, health_exc=None):
        self.calls: list[str] = []
        self.ensured = 0
        self.summary_exc = summary_exc
        self.entity_exc = entity_exc
        self.health_exc = health_exc

    def ensure_service(self):
        self.ensured += 1

    def request(self, path: str, body=None):
        self.calls.append(path)
        if path.startswith("/api/spend/summary"):
            if self.summary_exc is not None:
                raise self.summary_exc
            qs = parse_qs(urlsplit(path).query)
            return {"window": {"key": qs["window"][0]}, "query": qs}
        if path.startswith("/api/spend/health"):
            if self.health_exc is not None:
                raise self.health_exc
            return {"status": "ok", "pricingGaps": [{"model": "x"}]}
        if path.startswith("/api/spend/entity"):
            if self.entity_exc is not None:
                raise self.entity_exc
            qs = parse_qs(urlsplit(path).query)
            return {"kind": qs["kind"][0], "key": qs["key"][0], "query": qs}
        raise AssertionError(f"Unexpected path: {path}")


def test_bridge_fetch_analytics_summary_forwarding_and_errors(tmp_path):
    prefs = tmp_path / "prefs.json"

    client = RecordingClient()
    bridge = DesktopBridge(client, preferences=prefs, demo=False)
    res = bridge.fetch_analytics_summary(window="7d", tool=" claude-code ", machine="pc-1")
    assert res["ok"] is True
    assert client.ensured == 1
    assert res["summary"]["query"] == {
        "window": ["1w"],
        "tool": ["claude-code"],
        "machine": ["pc-1"],
    }
    assert res["health"] == {"status": "ok", "pricingGaps": [{"model": "x"}]}

    # Health failure should degrade gracefully without failing summary.
    health_err_client = RecordingClient(health_exc=DesktopError("health down"))
    bridge_h = DesktopBridge(health_err_client, preferences=prefs, demo=False)
    res_h = bridge_h.fetch_analytics_summary(window="6h", tool="", machine="")
    assert res_h["ok"] is True
    assert res_h["health"] is None
    assert res_h["summary"]["query"] == {
        "window": ["6h"],
        "tool": ["all"],
        "machine": ["all"],
    }

    # DesktopError propagates message cleanly.
    err_client = RecordingClient(summary_exc=DesktopError("service unreachable"))
    bridge_err = DesktopBridge(err_client, preferences=prefs, demo=False)
    assert bridge_err.fetch_analytics_summary() == {
        "ok": False,
        "error": "service unreachable",
    }

    # Unexpected transport/parse exceptions map to generic message.
    os_client = RecordingClient(summary_exc=OSError("socket reset"))
    bridge_os = DesktopBridge(os_client, preferences=prefs, demo=False)
    assert bridge_os.fetch_analytics_summary() == {
        "ok": False,
        "error": "Could not load analytics summary.",
    }


def test_bridge_fetch_entity_detail_validation_forwarding_and_errors(tmp_path):
    prefs = tmp_path / "prefs.json"

    client = RecordingClient()
    bridge = DesktopBridge(client, preferences=prefs, demo=False)

    # Validation failures do not touch the client.
    for bad_kind, bad_key in [
        ("provider", "claude-code"),
        ("", "claude-code"),
        (None, "claude-code"),
        ("model", ""),
        ("tool", "   "),
        ("model", None),
    ]:
        assert bridge.fetch_entity_detail(bad_kind, bad_key) == {
            "ok": False,
            "error": "Invalid analytics entity.",
        }
    assert client.ensured == 0
    assert client.calls == []

    res = bridge.fetch_entity_detail(" Model ", " claude-opus-4-6 ", window="30d", machine="m2")
    assert res["ok"] is True
    assert client.ensured == 1
    assert res["entity"]["query"] == {
        "kind": ["model"],
        "key": ["claude-opus-4-6"],
        "window": ["1mo"],
        "machine": ["m2"],
    }

    err_bridge = DesktopBridge(
        RecordingClient(entity_exc=DesktopError("not found")),
        preferences=prefs,
        demo=False,
    )
    assert err_bridge.fetch_entity_detail("tool", "claude-code") == {
        "ok": False,
        "error": "not found",
    }

    val_bridge = DesktopBridge(
        RecordingClient(entity_exc=ValueError("bad json")),
        preferences=prefs,
        demo=False,
    )
    assert val_bridge.fetch_entity_detail("tool", "claude-code") == {
        "ok": False,
        "error": "Could not load detail view.",
    }


# ---------------------------------------------------------------------------
# Browser UI fixtures & helpers
# ---------------------------------------------------------------------------


def test_bridge_model_detail_requires_echoed_profile(tmp_path):
    class ScopedClient(RecordingClient):
        echo_scope = True

        def request(self, path, body=None):
            response = super().request(path, body)
            if self.echo_scope:
                response["selectedTool"] = response["query"].get("tool", [None])[0]
            return response

    client = ScopedClient()
    bridge = DesktopBridge(client, preferences=tmp_path / "prefs.json")
    scoped = bridge.fetch_entity_detail("model", "gpt-5", "1d", "m2", "codex-work")
    assert scoped["ok"]
    assert scoped["entity"]["selectedTool"] == "codex-work"
    assert scoped["entity"]["query"]["tool"] == ["codex-work"]

    client.echo_scope = False
    unsupported = bridge.fetch_entity_detail("model", "gpt-5", "1d", "m2", "codex-work")
    assert unsupported == {"ok": False, "error": "Detail for this tool profile is unavailable."}
    global_model = bridge.fetch_entity_detail("model", "gpt-5", "1d", "m2")
    assert global_model["ok"]
    assert "tool" not in global_model["entity"]["query"]


def _build_h9_fixtures() -> dict:
    client = DemoClient()
    summaries_all = {
        win: client.request(f"/api/spend/summary?window={win}&tool=all&machine=all")
        for win in ANALYTICS_WINDOWS
    }
    summaries_claude = {
        win: client.request(
            f"/api/spend/summary?window={win}&tool=claude-code&machine=all"
        )
        for win in ("1d", "6h", "1w")
    }
    entities = {}
    for win in ("1d", "6h", "1w"):
        for kind, key in (
            ("model", "claude-opus-4-6"),
            ("model", "glm-4.7"),
            ("tool", "claude-code"),
            ("tool", "codex"),
        ):
            entity = demo_entity(kind, key, win, "all")
            entities[f"{kind}:{key}:{win}"] = entity
            if kind == "model":
                tool = entity["providerKey"]
                entities[f"{kind}:{key}:{win}:{tool}"] = demo_entity(kind, key, win, "all", tool)

    return {
        "summariesAll": summaries_all,
        "summariesClaude": summaries_claude,
        "entities": entities,
        "health": client.request("/api/spend/health"),
    }


def _h9_api_js(fixtures: dict | None = None, *, health: dict | None = None) -> str:
    payload = fixtures if fixtures is not None else _build_h9_fixtures()
    health_obj = health if health is not None else payload.get("health")
    return f"""
    var __H9 = {json.dumps(payload)};
    var __H9_HEALTH = {json.dumps(health_obj)};
    api.save_analytics_window = async function (w) {{
      window.__calls.push(["save_analytics_window", w]);
      return {{ ok: true, analyticsWindow: w }};
    }};
    api.fetch_analytics_summary = async function (win, tool, machine) {{
      window.__calls.push(["fetch_analytics_summary", win, tool, machine]);
      var source = (tool && tool !== "all" && __H9.summariesClaude && __H9.summariesClaude[win])
        ? __H9.summariesClaude[win]
        : (__H9.summariesAll[win] || __H9.summariesAll["1d"]);
      return {{ ok: true, summary: JSON.parse(JSON.stringify(source)), health: __H9_HEALTH }};
    }};
    api.fetch_entity_detail = async function (kind, key, win, machine, tool) {{
      window.__calls.push(["fetch_entity_detail", kind, key, win, machine, tool]);
      var ent = (__H9.entities && (__H9.entities[kind + ":" + key + ":" + win + ":" + tool]
        || __H9.entities[kind + ":" + key + ":" + win] || __H9.entities[kind + ":" + key + ":1d"]))
        || Object.values(__H9.entities || {{}})[0];
      return {{ ok: true, entity: JSON.parse(JSON.stringify(ent)) }};
    }};
    """


@browser
@needs_chrome
def test_model_click_keyboard_and_refresh_keep_profile(tmp_path):
    fixtures = _build_h9_fixtures()
    model = next(m for m in fixtures["summariesAll"]["1d"]["models"] if m["key"] == "claude-opus-4-6")
    fixtures["summariesAll"]["1d"]["models"].append({**model, "toolKey": "claude-code-work", "tokens": 9000})
    fixtures["entities"]["model:claude-opus-4-6:1d:claude-code-work"] = {
        **fixtures["entities"]["model:claude-opus-4-6:1d"],
        "tokens": 9000,
        "providerName": "Claude Work",
        "selectedTool": "claude-code-work",
    }
    html = build_page(
        init={"analyticsWindow": "1d"},
        api_js=_h9_api_js(fixtures),
        extra_css=FAST,
        probe="""
const selector = '[data-h9-entity-key="claude-opus-4-6"][data-h9-entity-tool="claude-code-work"]';
await until(() => document.querySelector('.h9-model-row' + selector));
document.querySelector('.h9-model-row' + selector).dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
await until(() => state.h9EntityData?.selectedTool === 'claude-code-work');
const keyboard = state.h9EntityData.tokens;
const before = window.__calls.length;
await loadUsage();
await until(() => window.__calls.slice(before).some(c => c[0] === 'fetch_entity_detail'));
const refresh = window.__calls.slice(before).find(c => c[0] === 'fetch_entity_detail');
document.getElementById('h9DetailBack').click();
await until(() => document.querySelector('.h9-model-card' + selector));
document.querySelector('.h9-model-card' + selector).click();
await until(() => state.h9EntityData?.selectedTool === 'claude-code-work');
report({keyboard, refreshTool: refresh[5], click: state.h9EntityData.tokens});
""",
    )
    assert probe(html, 960, tmp_path, height=700) == {
        "keyboard": 9000, "refreshTool": "claude-code-work", "click": 9000,
    }


def _partial_coverage_fixture() -> dict:
    base = demo_summary("1d", "all", "all")
    base["coverage"] = {
        "exactProviders": ["Claude Code"],
        "derivedProviders": ["OpenCode"],
        "note": None,
    }
    base["totals"]["trackedValue"] = None
    base["totals"]["trackedValueMarker"] = None
    base["totals"]["knownValue"] = 42.50
    base["totals"]["priced"] = 30.00
    base["totals"]["publishedRate"] = 12.50
    base["totals"]["unpricedModels"] = [
        {"modelKey": "mystery-model", "name": "Mystery Model", "records": 7}
    ]
    base["totals"]["effectiveCostPerMillionTokens"] = None
    base["totals"]["knownEffectiveCostPerMillionTokens"] = 6.25
    base["totals"]["effectiveCostPricedTokens"] = 6_800_000
    base["totals"]["effectiveCostCoveragePct"] = 75.0
    base["totals"]["effectiveCostComplete"] = False
    base["projected"] = {
        "value": 85.00,
        "planCost": 40.00,
        "fullMonthPlanCost": 100.00,
        "multiple": 2.13,
        "multipleBasis": "lower_bound",
        "method": "Month-to-date reference usage versus configured accrual.",
    }
    return {
        "summariesAll": {"1d": base},
        "summariesClaude": {},
        "entities": {"tool:claude-code:1d": demo_entity("tool", "claude-code", "1d", "all")},
        "health": {"status": "ok", "pricingGaps": [{"model": "mystery-model"}]},
    }


def _exact_coverage_fixture() -> dict:
    base = demo_summary("1d", "claude-code", "all")
    base["coverage"] = {
        "exactProviders": ["Claude Code"],
        "derivedProviders": [],
        "note": None,
    }
    base["totals"]["publishedRate"] = 0.0
    base["totals"]["unpricedModels"] = []
    return {
        "summariesAll": {"1d": base},
        "summariesClaude": {"1d": base},
        "entities": {"tool:claude-code:1d": demo_entity("tool", "claude-code", "1d", "all")},
        "health": {"status": "ok", "pricingGaps": []},
    }


# ---------------------------------------------------------------------------
# Browser UI tests
# ---------------------------------------------------------------------------


RANGE_AND_TOOL_PROBE = """
await until(() => !document.getElementById('h9AnalyticsSection').hidden && !document.getElementById('h9MoreRangeBtn').hidden);
const out = {};
const periodBar = document.getElementById('periodBar');
const moreBtn = document.getElementById('h9MoreRangeBtn');
const moreLabel = document.getElementById('h9MoreRangeLabel');
const pop = document.getElementById('h9RangePopover');

out.removedLegacy = {
  h9RangeBar: document.getElementById('h9RangeBar'),
  h9DetailRanges: document.getElementById('h9DetailRanges'),
};
out.initial = {
  mainPills: [...periodBar.querySelectorAll('.period-pill')].map(b => b.dataset.period),
  activePills: [...periodBar.querySelectorAll('.period-pill.active')].map(b => b.dataset.period),
  isMoreActiveClass: periodBar.classList.contains('is-more-active'),
  moreHidden: moreBtn.hidden,
  moreActive: moreBtn.classList.contains('active'),
  moreLabel: moreLabel.textContent,
};

// Open #h9RangePopover via #h9MoreRangeBtn and inspect the 10 extra windows
moreBtn.click();
await until(() => pop.classList.contains('open'));
out.morePopover = {
  open: pop.classList.contains('open'),
  options: [...pop.querySelectorAll('.scope-opt[data-h9-window]')].map(b => b.dataset.h9Window),
};

// Select 6h from popover -> active More state, no active period-pill, saves 6h, refreshes summary
pop.querySelector('[data-h9-window="6h"]').click();
await until(() => state.h9Summary?.window?.key === '6h');
out.after6h = {
  popOpen: pop.classList.contains('open'),
  moreActive: moreBtn.classList.contains('active'),
  moreLabel: moreLabel.textContent,
  isMoreActiveClass: periodBar.classList.contains('is-more-active'),
  activePills: [...periodBar.querySelectorAll('.period-pill.active')].map(b => b.dataset.period),
  summaryWin: state.h9Summary?.window?.key,
  saveCalls: window.__calls.filter(c => c[0] === 'save_analytics_window'),
};

// Click main period pill 'week' (7 days) -> clears More active, restores 'More' label, loads 1w
periodBar.querySelector('[data-period="week"]').click();
await until(() => state.h9Summary?.window?.key === '1w');
out.afterWeekPill = {
  moreActive: moreBtn.classList.contains('active'),
  moreLabel: moreLabel.textContent,
  isMoreActiveClass: periodBar.classList.contains('is-more-active'),
  activePills: [...periodBar.querySelectorAll('.period-pill.active')].map(b => b.dataset.period),
  summaryWin: state.h9Summary?.window?.key,
};

// Wait for hero number tween to settle before capturing baseline hero value
await until(() => document.getElementById('heroValue').textContent === '$127.34');

// Filter to claude-code
const heroAllVal = document.getElementById('heroValue').textContent;
const heroAllCtx = document.getElementById('heroTokensContext').textContent;
document.querySelector('#h9ToolBar [data-h9-tool="claude-code"]').click();
await until(() => state.h9Summary?.tools?.length === 1 && state.h9Summary.tools[0].key === 'claude-code');
const providerStatesFiltered = [...document.querySelectorAll('#providerList .provider-item')].map(el => [el.dataset.key, el.hidden]);
out.filtered = {
  activeTool: document.querySelector('#h9ToolBar .h9-tool-pill.active')?.dataset.h9Tool,
  drillVisible: !!document.querySelector('#h9ToolBar [data-h9-drill-tool="claude-code"]'),
  drillText: document.querySelector('#h9ToolBar [data-h9-drill-tool]')?.textContent.trim(),
  providers: providerStatesFiltered,
  heroValChanged: document.getElementById('heroValue').textContent !== heroAllVal,
  heroCtx: document.getElementById('heroTokensContext').textContent,
  heroAllCtx,
};

// Restore All tools
document.querySelector('#h9ToolBar [data-h9-tool="all"]').click();
await until(() => (state.h9Summary?.tools?.length || 0) > 1);
out.restored = {
  activeTool: document.querySelector('#h9ToolBar .h9-tool-pill.active')?.dataset.h9Tool,
  drillVisible: !!document.querySelector('#h9ToolBar [data-h9-drill-tool]'),
  hiddenProviders: [...document.querySelectorAll('#providerList .provider-item')].filter(el => el.hidden).length,
  heroCtxRestored: document.getElementById('heroTokensContext').textContent === heroAllCtx,
};
report(out);
"""


@needs_chrome
@browser
def test_h9_ui_range_and_tool_filter(tmp_path):
    html = build_page(
        pref="light",
        init={"analyticsWindow": "1d"},
        api_js=_h9_api_js(),
        probe=RANGE_AND_TOOL_PROBE,
        extra_css=FAST,
    )
    r = probe(html, 960, tmp_path, height=900)
    assert r["removedLegacy"] == {"h9RangeBar": None, "h9DetailRanges": None}
    assert r["initial"] == {
        "mainPills": ["today", "week", "month", "all"],
        "activePills": ["today"],
        "isMoreActiveClass": False,
        "moreHidden": False,
        "moreActive": False,
        "moreLabel": "More",
    }
    assert r["morePopover"]["open"] is True
    assert r["morePopover"]["options"] == [
        "15m",
        "30m",
        "1h",
        "3h",
        "6h",
        "12h",
        "1w",
        "1mo",
        "mtd",
        "ytd",
    ]
    assert r["after6h"] == {
        "popOpen": False,
        "moreActive": True,
        "moreLabel": "6h",
        "isMoreActiveClass": True,
        "activePills": [],
        "summaryWin": "6h",
        "saveCalls": [["save_analytics_window", "6h"]],
    }
    assert r["afterWeekPill"] == {
        "moreActive": False,
        "moreLabel": "More",
        "isMoreActiveClass": False,
        "activePills": ["week"],
        "summaryWin": "1w",
    }

    assert r["filtered"]["activeTool"] == "claude-code"
    assert r["filtered"]["drillVisible"] is True
    assert "Detail" in r["filtered"]["drillText"]
    visible_keys = [k for k, hidden in r["filtered"]["providers"] if not hidden]
    hidden_keys = [k for k, hidden in r["filtered"]["providers"] if hidden]
    assert visible_keys and all(k.startswith("claude") for k in visible_keys)
    assert len(hidden_keys) >= 3
    assert r["filtered"]["heroValChanged"] is True
    assert r["filtered"]["heroCtx"] == "filtered to Claude Code"

    assert r["restored"]["activeTool"] == "all"
    assert r["restored"]["drillVisible"] is False
    assert r["restored"]["hiddenProviders"] == 0
    assert r["restored"]["heroCtxRestored"] is True


COVERAGE_AND_KPI_PROBE = """
await until(() => !document.getElementById('h9AnalyticsSection').hidden && document.getElementById('h9KpiTrackedVal').textContent !== '—');
report({
  bannerState: document.getElementById('h9CoverageBanner').dataset.state,
  bannerTitle: document.getElementById('h9CoverageTitle').textContent,
  bannerExact: document.getElementById('h9CoverageExact').textContent,
  unpricedHidden: document.getElementById('h9CoverageUnpriced').hidden,
  unpricedText: document.getElementById('h9CoverageUnpriced').textContent,
  trackedMarker: document.getElementById('h9KpiTrackedMarker').textContent,
  trackedVal: document.getElementById('h9KpiTrackedVal').textContent,
  trackedChip: document.getElementById('h9KpiTrackedChip').textContent,
  trackedNote: document.getElementById('h9KpiTrackedNote').textContent,
  tokensVal: document.getElementById('h9KpiTokensVal').textContent,
  rateMarker: document.getElementById('h9KpiRateMarker').textContent,
  rateVal: document.getElementById('h9KpiRateVal').textContent,
  rateChip: document.getElementById('h9KpiRateChip').textContent,
  cacheVal: document.getElementById('h9KpiCacheVal').textContent,
  sessionsVal: document.getElementById('h9KpiSessionsVal').textContent,
  forecastMarker: document.getElementById('h9ForecastMarker').textContent,
  forecastVal: document.getElementById('h9ForecastVal').textContent,
  forecastDelta: document.getElementById('h9ForecastDelta').textContent,
  paceLabel: document.getElementById('h9PaceLabel').textContent,
  forecastNote: document.getElementById('h9ForecastNote').textContent,
});
"""


@needs_chrome
@browser
def test_h9_ui_coverage_exact_mixed_and_partial_lower_bound(tmp_path):
    # 1. Exact fixture
    exact_html = build_page(
        pref="light",
        api_js=_h9_api_js(_exact_coverage_fixture()),
        probe=COVERAGE_AND_KPI_PROBE,
        extra_css=FAST,
    )
    exact = probe(exact_html, 960, tmp_path / "exact", height=900)
    assert exact["bannerState"] == "exact"
    assert exact["bannerTitle"] == "Exact pricing coverage"
    assert exact["unpricedHidden"] is True
    assert exact["trackedChip"] == "exact"
    assert exact["trackedMarker"] == ""
    assert exact["tokensVal"] != "—"
    assert exact["rateVal"] != "—"
    assert exact["cacheVal"].endswith("%")
    assert exact["sessionsVal"] != "—"

    # 2. Mixed demo fixture
    mixed_html = build_page(
        pref="light",
        api_js=_h9_api_js(),
        probe=COVERAGE_AND_KPI_PROBE,
        extra_css=FAST,
    )
    mixed = probe(mixed_html, 960, tmp_path / "mixed", height=900)
    assert mixed["bannerState"] == "partial"
    assert mixed["bannerTitle"] == "Partial pricing coverage"
    assert mixed["trackedChip"] == "mixed"
    assert mixed["trackedMarker"] == "≈"
    assert mixed["rateChip"] == "API rates"

    # 3. Partial + unpricedModels + lower_bound forecast fixture
    partial_html = build_page(
        pref="light",
        api_js=_h9_api_js(_partial_coverage_fixture()),
        probe=COVERAGE_AND_KPI_PROBE,
        extra_css=FAST,
    )
    partial = probe(partial_html, 960, tmp_path / "partial", height=900)
    assert partial["bannerState"] == "partial"
    assert partial["unpricedHidden"] is False
    assert "Unpriced models in window: Mystery Model" in partial["unpricedText"]
    assert partial["trackedChip"] == "partial"
    assert partial["trackedMarker"] == "≈"
    assert partial["trackedVal"] == "$42.50"
    assert "7 rows unpriced: mystery-model" in partial["trackedNote"]
    assert partial["rateChip"] == "partial"
    assert partial["rateMarker"] == "≈"
    assert partial["rateVal"] == "$6.25"
    assert partial["forecastMarker"] == "≥"
    assert "2.13× accrued" in partial["forecastDelta"]
    assert "lower bound" in partial["forecastDelta"]
    assert "usage ≥" in partial["paceLabel"]
    assert "Mystery Model unpriced, so this is a lower bound." in partial["forecastNote"]


CHARTS_INSIGHTS_MIX_MODELS_PROBE = """
await until(() => !document.getElementById('h9AnalyticsSection').hidden && document.querySelectorAll('#h9ChartHits [data-h9-hit]').length > 0);
const out = {};

// 1. Stacked vs Cumulative chart toggle
out.initialStacked = {
  stackedHidden: document.getElementById('h9StackedPlot').hidden,
  cumHidden: document.getElementById('h9CumulativePlot').hidden,
  cols: document.querySelectorAll('#h9StackedPlot .h9-col').length,
  tableRows: document.querySelectorAll('#h9ChartTableBody tr').length,
};
document.querySelector('#h9ChartMode [data-mode="cumulative"]').click();
await wait(20);
out.afterCum = {
  stackedHidden: document.getElementById('h9StackedPlot').hidden,
  cumHidden: document.getElementById('h9CumulativePlot').hidden,
  cumActive: document.querySelector('#h9ChartMode [data-mode="cumulative"]').classList.contains('active'),
  lineD: document.getElementById('h9CumLine').getAttribute('d') || '',
  areaD: document.getElementById('h9CumArea').getAttribute('d') || '',
};
document.querySelector('#h9ChartMode [data-mode="stacked"]').click();
await wait(20);

// 2. Pin tooltip via click on hit target, then unpin via Escape
const hit = document.querySelectorAll('#h9ChartHits [data-h9-hit]')[2];
hit.click();
await wait(20);
const tip = document.getElementById('h9ChartTooltip');
out.pinned = {
  tipHidden: tip.hidden,
  tipHead: tip.querySelector('.h9-tip-head')?.textContent.trim(),
  tipRows: tip.querySelectorAll('.h9-tip-row').length,
  tipHint: tip.querySelector('.h9-tip-hint')?.textContent.trim(),
  hitActive: hit.classList.contains('active'),
};
document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
await wait(20);
out.afterEsc = {
  tipHidden: tip.hidden,
  hoverLabel: document.getElementById('h9HoverLabel').textContent,
};

// 3. Insights (waste & forecast)
out.waste = {
  day: document.getElementById('h9WasteDay').textContent,
  month: document.getElementById('h9WasteMonthNote').textContent,
  fixChips: document.querySelectorAll('#h9WasteItems .h9-fix-chip').length,
  cacheSavings: document.getElementById('h9CacheSavings').textContent,
};
out.forecast = {
  delta: document.getElementById('h9ForecastDelta').textContent,
  paceWidth: document.getElementById('h9PaceBar').style.width,
};

// 4. Mix toggle collapse/expand
const mixToggle = document.getElementById('h9MixToggle');
const mixDetails = document.getElementById('h9MixDetails');
out.mixInitial = { expanded: mixToggle.getAttribute('aria-expanded'), hidden: mixDetails.hidden };
mixToggle.click();
await wait(20);
out.mixCollapsed = { expanded: mixToggle.getAttribute('aria-expanded'), hidden: mixDetails.hidden, caret: document.getElementById('h9MixCaret').textContent };
mixToggle.click();
await wait(20);
out.mixReopened = { expanded: mixToggle.getAttribute('aria-expanded'), hidden: mixDetails.hidden, caret: document.getElementById('h9MixCaret').textContent };

// 5. Price board & model table sorting (at 960px width)
out.priceBoard = {
  kicker: document.querySelector('#h9PriceBoard .h9-price-kicker')?.textContent,
  rungs: document.querySelectorAll('#h9PriceBoard .h9-price-rung').length,
};
const modelKeys = () => [...document.querySelectorAll('#h9ModelBody .h9-model-row')].map(r => r.dataset.h9EntityKey);
out.modelsInitial = modelKeys();
document.querySelector('#h9ModelHead [data-h9-sort="name"]').click();
await wait(20);
out.modelsNameDesc = modelKeys();
document.querySelector('#h9ModelHead [data-h9-sort="name"]').click();
await wait(20);
out.modelsNameAsc = modelKeys();
document.querySelector('#h9ModelHead [data-h9-sort="tokens"]').click();
await wait(20);
out.modelsTokDesc = modelKeys();
document.querySelector('#h9ModelHead [data-h9-sort="tokens"]').click();
await wait(20);
out.modelsTokAsc = modelKeys();

report(out);
"""


@needs_chrome
@browser
def test_h9_ui_charts_insights_mix_prices_and_models(tmp_path):
    html = build_page(
        pref="light",
        api_js=_h9_api_js(),
        probe=CHARTS_INSIGHTS_MIX_MODELS_PROBE,
        extra_css=FAST,
    )
    r = probe(html, 960, tmp_path, height=900)

    assert r["initialStacked"]["stackedHidden"] is False
    assert r["initialStacked"]["cumHidden"] is True
    assert r["initialStacked"]["cols"] == 24
    assert r["initialStacked"]["tableRows"] == 24

    assert r["afterCum"]["stackedHidden"] is True
    assert r["afterCum"]["cumHidden"] is False
    assert r["afterCum"]["cumActive"] is True
    assert r["afterCum"]["lineD"].startswith("M")
    assert r["afterCum"]["areaD"].endswith("Z")

    assert r["pinned"]["tipHidden"] is False
    assert r["pinned"]["tipRows"] >= 1
    assert "Pinned" in r["pinned"]["tipHint"]
    assert r["pinned"]["hitActive"] is True
    assert r["afterEsc"]["tipHidden"] is True
    assert "total" in r["afterEsc"]["hoverLabel"].lower()

    assert r["waste"]["day"].endswith("/day")
    assert r["waste"]["month"].endswith("/mo")
    assert r["waste"]["fixChips"] >= 2
    assert r["waste"]["cacheSavings"].startswith("≈$")
    assert "× accrued" in r["forecast"]["delta"]
    assert r["forecast"]["paceWidth"] not in ("", "0%")

    assert r["mixInitial"] == {"expanded": "true", "hidden": False}
    assert r["mixCollapsed"] == {"expanded": "false", "hidden": True, "caret": "▼"}
    assert r["mixReopened"] == {"expanded": "true", "hidden": False, "caret": "▲"}

    assert r["priceBoard"]["kicker"] == "Steepest model"
    assert r["priceBoard"]["rungs"] >= 2

    # Value desc: GPT-5.3 Codex ($41.82) is first, Grok 4 Fast ($5.10) last.
    assert r["modelsInitial"][0] == "gpt-5.3-codex"
    assert r["modelsInitial"][-1] == "grok-4-fast"
    assert r["modelsNameAsc"][0] == "claude-opus-4-6"
    assert r["modelsNameDesc"] == list(reversed(r["modelsNameAsc"]))
    assert r["modelsTokDesc"][0] == "gpt-5.3-codex"
    assert r["modelsTokAsc"][0] == "grok-4-fast"


HEATMAP_PROBE = """
await until(() => !document.getElementById('h9AnalyticsSection').hidden && document.querySelectorAll('#h9Heatmap .h9-heat-cell').length === 168);
const out = {};
out.cells = document.querySelectorAll('#h9Heatmap .h9-heat-cell').length;
out.rows = document.querySelectorAll('#h9Heatmap .h9-heat-row').length;
out.tableRows = document.querySelectorAll('#h9HeatTableBody tr').length;
out.shortWarningHidden = document.getElementById('h9HeatWarning').hidden;
out.shortGridDim = document.getElementById('h9Heatmap').classList.contains('dim');
out.share = document.getElementById('h9HeatShare').textContent;
out.span = document.getElementById('h9HeatSpan').textContent;

// Switch to 1w via merged #periodBar -> short warning hides
document.querySelector('#periodBar [data-period="week"]').click();
await until(() => state.h9Summary?.window?.key === '1w');
out.weekWarningHidden = document.getElementById('h9HeatWarning').hidden;
out.weekGridDim = document.getElementById('h9Heatmap').classList.contains('dim');

// Roving tabIndex moves via ArrowRight and ArrowDown
const grid = document.getElementById('h9Heatmap');
const cells = [...grid.querySelectorAll('.h9-heat-cell')];
out.initialZeroTabCount = cells.filter(c => c.tabIndex === 0).length;
const startCell = cells.find(c => c.tabIndex === 0);
const startIdx = Number(startCell.dataset.h9HeatIdx);
startCell.focus();
if (startIdx % 24 === 23) {
  document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
}
if (Math.floor(Number(document.activeElement.dataset.h9HeatIdx) / 24) === 6) {
  document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowUp', bubbles: true }));
}
const baseIdx = Number(document.activeElement.dataset.h9HeatIdx);
document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
const afterRightIdx = Number(document.activeElement.dataset.h9HeatIdx);
const afterRightTab = document.activeElement.tabIndex;
document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
const afterDownIdx = Number(document.activeElement.dataset.h9HeatIdx);
const afterDownTab = document.activeElement.tabIndex;

out.roving = {
  baseIdx,
  afterRightIdx,
  afterRightTab,
  afterDownIdx,
  afterDownTab,
  zeroTabsNow: [...grid.querySelectorAll('.h9-heat-cell')].filter(c => c.tabIndex === 0).length,
  heatLabel: document.getElementById('h9HeatLabel').textContent,
};
report(out);
"""


@needs_chrome
@browser
def test_h9_ui_heatmap_and_roving_keyboard(tmp_path):
    html = build_page(
        pref="light",
        api_js=_h9_api_js(),
        probe=HEATMAP_PROBE,
        extra_css=FAST,
    )
    r = probe(html, 960, tmp_path, height=900)

    assert r["rows"] == 7
    assert r["cells"] == 168
    assert r["tableRows"] == 168
    assert r["shortWarningHidden"] is False
    assert r["shortGridDim"] is True
    assert r["share"].endswith("%")
    assert "Peak hour" in r["span"]
    assert r["weekWarningHidden"] is True
    assert r["weekGridDim"] is False

    rv = r["roving"]
    assert r["initialZeroTabCount"] == 1
    assert rv["afterRightIdx"] == rv["baseIdx"] + 1
    assert rv["afterRightTab"] == 0
    assert rv["afterDownIdx"] == rv["baseIdx"] + 25
    assert rv["afterDownTab"] == 0
    assert rv["zeroTabsNow"] == 1
    assert rv["heatLabel"]


DETAIL_DRILLDOWN_PROBE = """
await until(() => !document.getElementById('h9AnalyticsSection').hidden && document.querySelectorAll('#h9ModelBody .h9-model-row').length > 0);
const out = {};
const scrollBody = document.getElementById('scrollBody');
const hero = document.getElementById('heroSection');
const stack = document.querySelector('.section-stack');
const detailView = document.getElementById('h9DetailView');
const rangePop = document.getElementById('h9RangePopover');

function snapDetail() {
  return {
    detailHidden: detailView.hidden,
    heroHidden: hero.offsetParent === null,
    stackHidden: stack.hidden,
    heroDisplay: hero.offsetParent === null ? 'none' : getComputedStyle(hero).display,
    stackDisplay: getComputedStyle(stack).display,
    detailScrollTop: scrollBody.scrollTop,
    detailRangeLabel: document.getElementById('h9DetailRangeLabel').textContent,
    kind: document.getElementById('h9DetailKind').textContent,
    name: document.getElementById('h9DetailName').textContent,
    shareWidth: document.getElementById('h9DetailShareBar').style.width,
    kpis: document.querySelectorAll('#h9DetailKpis .h9-kpi-card').length,
    bars: document.querySelectorAll('#h9DetailBars .h9-detail-bar-col').length,
    mixRows: document.querySelectorAll('#h9DetailMixCards .h9-mix-row').length,
    oppTitle: document.getElementById('h9OppTitle').textContent,
    oppVal: document.getElementById('h9OppVal').textContent,
    limitRows: document.querySelectorAll('#h9EntityLimits .limit-row').length,
    sessions: document.querySelectorAll('#h9SessionsBody .h9-session-row').length,
    sessionsFoot: document.getElementById('h9SessionsFoot').textContent,
  };
}

// 1. Scroll overview to ~360px, open from model row, test #h9DetailRangeBtn 12-option popover, return via #h9DetailBack
scrollBody.scrollTo({ top: 360, behavior: 'instant' });
out.scrollBeforeModel = scrollBody.scrollTop;
document.querySelector('#h9ModelBody .h9-model-row[data-h9-entity-key="claude-opus-4-6"]').click();
await until(() => !detailView.hidden && state.h9EntityData?.key === 'claude-opus-4-6');
out.fromModel = snapDetail();

// Open detail range popover -> verify all 12 windows, pick 1w, then open again & close via Escape without closing detail
document.getElementById('h9DetailRangeBtn').click();
await until(() => rangePop.classList.contains('open'));
out.detailPopover = {
  open: rangePop.classList.contains('open'),
  options: [...rangePop.querySelectorAll('.scope-opt[data-h9-window]')].map(b => b.dataset.h9Window),
};
rangePop.querySelector('[data-h9-window="1w"]').click();
await until(() => state.h9EntityData?.window?.key === '1w');
out.detailAfter1w = {
  popOpen: rangePop.classList.contains('open'),
  rangeLabel: document.getElementById('h9DetailRangeLabel').textContent,
  entityWin: state.h9EntityData?.window?.key,
};
document.getElementById('h9DetailRangeBtn').click();
await until(() => rangePop.classList.contains('open'));
document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
await until(() => !rangePop.classList.contains('open'));
out.detailAfterPopEsc = {
  popOpen: rangePop.classList.contains('open'),
  detailHidden: detailView.hidden,
};

document.getElementById('h9DetailBack').click();
await until(() => detailView.hidden && !hero.hidden);
await until(() => scrollBody.scrollTop >= 350, 30);
out.afterBackBtn = {
  detailHidden: detailView.hidden,
  heroHidden: hero.offsetParent === null,
  stackHidden: stack.hidden,
  heroDisplay: hero.offsetParent === null ? 'none' : getComputedStyle(hero).display,
  stackDisplay: getComputedStyle(stack).display,
  restoredScrollTop: scrollBody.scrollTop,
};

// 2. Scroll overview to ~360px, open from legend card, close via Escape & verify scroll restoration
scrollBody.scrollTo({ top: 360, behavior: 'instant' });
out.scrollBeforeLegend = scrollBody.scrollTop;
document.querySelector('#h9LegendCards [data-h9-entity-key="codex"]').click();
await until(() => !detailView.hidden && state.h9EntityData?.key === 'codex');
out.fromLegend = snapDetail();
document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
await until(() => detailView.hidden);
await until(() => scrollBody.scrollTop >= 350, 30);
out.afterEsc = {
  detailHidden: detailView.hidden,
  heroHidden: hero.offsetParent === null,
  stackHidden: stack.hidden,
  heroDisplay: hero.offsetParent === null ? 'none' : getComputedStyle(hero).display,
  stackDisplay: getComputedStyle(stack).display,
  restoredScrollTop: scrollBody.scrollTop,
};

// 3. Filter to claude-code and open via "Detail →" button
document.querySelector('#h9ToolBar [data-h9-tool="claude-code"]').click();
await until(() => !!document.querySelector('#h9ToolBar [data-h9-drill-tool="claude-code"]'));
document.querySelector('#h9ToolBar [data-h9-drill-tool="claude-code"]').click();
await until(() => !detailView.hidden && state.h9EntityData?.key === 'claude-code' && state.h9EntityData?.kind === 'tool');
out.fromDrillBtn = snapDetail();

report(out);
"""


@needs_chrome
@browser
def test_h9_ui_entity_detail_drilldown_and_navigation(tmp_path):
    html = build_page(
        pref="light",
        api_js=_h9_api_js(),
        probe=DETAIL_DRILLDOWN_PROBE,
        extra_css=FAST,
    )
    r = probe(html, 960, tmp_path, height=900)

    assert r["scrollBeforeModel"] >= 350
    m = r["fromModel"]
    assert m["detailHidden"] is False
    assert m["heroHidden"] is True
    assert m["stackHidden"] is True
    assert m["heroDisplay"] == "none"
    assert m["stackDisplay"] == "none"
    assert m["detailScrollTop"] == 0
    assert m["detailRangeLabel"] == "1d"
    assert m["kind"] == "Model detail"
    assert m["name"] == "Claude Opus 4.6"
    assert m["shareWidth"] not in ("", "0%")
    assert m["kpis"] == 4
    assert m["bars"] == 24
    assert m["mixRows"] == 4
    assert m["oppTitle"]
    assert m["oppVal"].startswith("$")
    assert m["limitRows"] >= 1
    assert m["sessions"] == 3
    assert "runs" in m["sessionsFoot"]

    assert r["detailPopover"]["open"] is True
    assert r["detailPopover"]["options"] == [
        "1d",
        "15m",
        "30m",
        "1h",
        "3h",
        "6h",
        "12h",
        "1w",
        "1mo",
        "mtd",
        "ytd",
        "all",
    ]
    assert r["detailAfter1w"] == {
        "popOpen": False,
        "rangeLabel": "1w",
        "entityWin": "1w",
    }
    assert r["detailAfterPopEsc"] == {"popOpen": False, "detailHidden": False}

    ab = r["afterBackBtn"]
    assert ab["detailHidden"] is True
    assert ab["heroHidden"] is False
    assert ab["stackHidden"] is False
    assert ab["heroDisplay"] != "none"
    assert ab["stackDisplay"] != "none"
    assert abs(ab["restoredScrollTop"] - r["scrollBeforeModel"]) <= 4

    assert r["scrollBeforeLegend"] >= 350
    lg = r["fromLegend"]
    assert lg["detailHidden"] is False
    assert lg["heroDisplay"] == "none"
    assert lg["stackDisplay"] == "none"
    assert lg["detailScrollTop"] == 0
    assert lg["kind"] == "Tool detail"
    assert lg["name"] == "Codex"
    assert lg["kpis"] == 4
    assert lg["sessions"] == 3

    ae = r["afterEsc"]
    assert ae["detailHidden"] is True
    assert ae["heroHidden"] is False
    assert ae["stackHidden"] is False
    assert ae["heroDisplay"] != "none"
    assert ae["stackDisplay"] != "none"
    assert abs(ae["restoredScrollTop"] - r["scrollBeforeLegend"]) <= 4

    dr = r["fromDrillBtn"]
    assert dr["detailHidden"] is False
    assert dr["heroDisplay"] == "none"
    assert dr["stackDisplay"] == "none"
    assert dr["kind"] == "Tool detail"
    assert dr["name"] == "Claude Code"
    assert dr["limitRows"] >= 1
    assert dr["sessions"] == 3


RESPONSIVE_PROBE = """
await until(() => !document.getElementById('h9AnalyticsSection').hidden && document.querySelectorAll('#h9ModelBody .h9-model-card').length > 0);
const out = {};
out.theme = document.documentElement.dataset.theme;
out.overview = {
  scrollWidth: document.documentElement.scrollWidth,
  innerWidth: window.innerWidth,
  smallText: smallText(),
};

// Open detail view (using model card or row depending on width) and check fit + font floor
document.querySelector('#h9ModelBody [data-h9-entity-key="claude-opus-4-6"]').click();
await until(() => !document.getElementById('h9DetailView').hidden && state.h9EntityData?.key === 'claude-opus-4-6');
out.detail = {
  scrollWidth: document.documentElement.scrollWidth,
  innerWidth: window.innerWidth,
  smallText: smallText(),
};
report(out);
"""


@needs_chrome
@browser
@pytest.mark.parametrize("pref", ["light", "dark"])
@pytest.mark.parametrize("width", [336, 960])
def test_h9_ui_responsive_fit_and_font_floor_in_light_and_dark(
    tmp_path, pref, width
):
    html = build_page(
        pref=pref,
        color_scheme=pref,
        api_js=_h9_api_js(),
        probe=RESPONSIVE_PROBE,
        extra_css=FAST,
    )
    r = probe(html, width, tmp_path, height=900)
    assert r["theme"] == pref
    assert r["overview"]["scrollWidth"] <= r["overview"]["innerWidth"], (
        pref,
        width,
        r["overview"],
    )
    assert r["overview"]["smallText"] == [], (pref, width, r["overview"]["smallText"])
    assert r["detail"]["scrollWidth"] <= r["detail"]["innerWidth"], (
        pref,
        width,
        r["detail"],
    )
    assert r["detail"]["smallText"] == [], (pref, width, r["detail"]["smallText"])


NARROW_OVERFLOW_PROBE = """
await until(() =>
  !document.getElementById('h9AnalyticsSection').hidden &&
  !document.getElementById('h9MoreRangeBtn').hidden &&
  document.querySelectorAll('#h9ModelBody .h9-model-card').length > 0 &&
  document.querySelectorAll('#h9Heatmap .h9-heat-cell').length === 168
);
const scrollBody = document.getElementById('scrollBody');
const snapFit = () => ({
  docScroll: document.documentElement.scrollWidth,
  docClient: document.documentElement.clientWidth,
  bodyScroll: scrollBody.scrollWidth,
  bodyClient: scrollBody.clientWidth,
});

const out = {};
out.overview = snapFit();

// Open #h9RangePopover from #h9MoreRangeBtn and check overflow + popover bounds
const pop = document.getElementById('h9RangePopover');
document.getElementById('h9MoreRangeBtn').click();
await until(() => pop.classList.contains('open'));
const popRect = pop.getBoundingClientRect();
out.overviewPopover = {
  ...snapFit(),
  popOpen: pop.classList.contains('open'),
  popLeft: popRect.left,
  popRight: popRect.right,
  innerWidth: window.innerWidth,
};
document.getElementById('h9RangeBackdrop').click();
await until(() => !pop.classList.contains('open'));

// Open Detail view via mobile model card and check overflow
document.querySelector('#h9ModelBody .h9-model-card[data-h9-entity-key="claude-opus-4-6"]').click();
await until(() =>
  !document.getElementById('h9DetailView').hidden &&
  state.h9EntityData?.key === 'claude-opus-4-6' &&
  document.querySelectorAll('#h9SessionsBody .h9-session-card').length > 0
);
out.detail = snapFit();
report(out);
"""


@needs_chrome
@browser
@pytest.mark.parametrize("width", [304, 336, 380])
def test_h9_ui_no_horizontal_overflow_at_narrow_widths(tmp_path, width):
    html = build_page(
        pref="light",
        api_js=_h9_api_js(),
        probe=NARROW_OVERFLOW_PROBE,
        extra_css=FAST,
    )
    r = probe(html, width, tmp_path, height=900)

    for state_name in ("overview", "overviewPopover", "detail"):
        fit = r[state_name]
        assert fit["docScroll"] <= fit["docClient"], (width, state_name, fit)
        assert fit["bodyScroll"] <= fit["bodyClient"], (width, state_name, fit)

    pop = r["overviewPopover"]
    assert pop["popOpen"] is True
    assert pop["popLeft"] >= 0
    assert pop["popRight"] <= pop["innerWidth"]


@needs_chrome
@browser
@pytest.mark.parametrize("legacy_summary", [False, True])
def test_h9_hero_chart_scrub_respects_tool_filter(tmp_path, legacy_summary):
    fixtures = _build_h9_fixtures()
    if legacy_summary:
        for bucket in fixtures["summariesClaude"]["1d"]["series"]:
            bucket.pop("bucketUsd")
            bucket.pop("bucketUsdComplete")
    result = probe(
        build_page(
            init={"analyticsWindow": "1d"},
            api_js=_h9_api_js(fixtures),
            extra_css=FAST,
            motion="reduce",
            probe="""
await until(() => state.h9Summary?.tools?.length > 1);
const chart = document.getElementById('chartWrap');
const snap = () => ({
  value: document.getElementById('heroValue').textContent,
  tokens: document.getElementById('heroTokens').textContent,
  context: document.getElementById('heroTokensContext').textContent,
  selected: document.querySelector('#h9ToolBar .h9-tool-pill.active')?.dataset.h9Tool,
  point: chart._scrubIdx,
});
const key = name => chart.dispatchEvent(new KeyboardEvent('keydown', {key: name, bubbles: true}));
const out = {all: snap()};
document.querySelector('#h9ToolBar [data-h9-tool="claude-code"]').click();
await until(() => state.h9Summary?.tools?.[0]?.key === 'claude-code' && state.h9Summary.tools.length === 1);
out.filtered = snap();
chart.focus({preventScroll: true});
key('End');
out.inspected = snap();
key('Escape');
out.escaped = snap();
const rect = chart.getBoundingClientRect();
chart.dispatchEvent(new MouseEvent('mousemove', {clientX: rect.right}));
out.pointer = snap();
chart.dispatchEvent(new MouseEvent('mouseleave'));
out.left = snap();
key('Home');
chart.blur();
out.blurred = snap();
key('End');
document.querySelector('#h9ToolBar [data-h9-tool="all"]').click();
await until(() => state.h9Summary?.tools?.length > 1);
out.restored = snap();
report(out);
""",
        ),
        960,
        tmp_path,
        height=900,
    )
    assert result["all"]["value"] == "$127.34"
    assert result["all"]["tokens"] == "8.2M tokens"
    assert result["filtered"] == {
        "value": "$52.10",
        "tokens": "2.9M tokens",
        "context": "filtered to Claude Code",
        "selected": "claude-code",
        "point": None,
    }
    for step in ("inspected", "pointer"):
        assert result[step]["value"] == ("Unpriced" if legacy_summary else "$52.10")
        assert result[step]["tokens"] == "2.9M tokens"
        assert result[step]["context"].startswith("through ")
    assert result["escaped"] == result["left"] == result["blurred"] == result["filtered"]
    assert result["restored"] == result["all"]


@needs_chrome
@browser
def test_h9_detail_poll_preserves_scroll_focus_and_current_entity(tmp_path):
    api_js = _h9_api_js() + """
    const realInterval = window.setInterval;
    window.setInterval = function (fn, delay, ...args) {
      if (delay === 15000) { window.__usagePoll = fn; return 0; }
      return realInterval.call(this, fn, delay, ...args);
    };
    """
    result = probe(
        build_page(
            init={"analyticsWindow": "1d"},
            api_js=api_js,
            extra_css=FAST,
            probe="""
await until(() => state.h9Summary && window.__usagePoll);
const scroll = document.getElementById('scrollBody');
scroll.style.scrollBehavior = 'auto';
document.querySelector('#h9LegendCards [data-h9-entity-key="claude-code"]').click();
await until(() => state.h9EntityData?.key === 'claude-code');
document.querySelector('#h9DetailHits [data-h9-detail-hit="2"]').focus({preventScroll: true});
scroll.scrollTop = 200;
const original = window.pywebview.api.fetch_entity_detail;
let release;
window.pywebview.api.fetch_entity_detail = async (...args) => {
  const result = await original(...args);
  if (args[1] !== 'claude-code') return result;
  return new Promise(resolve => { release = () => resolve(result); });
};
const prior = state.h9EntityData;
const out = {before: scroll.scrollTop};
window.__usagePoll();
await until(() => release);
out.whileFetching = scroll.scrollTop;
release();
release = null;
await until(() => state.h9EntityData !== prior);
out.after = {
  scroll: scroll.scrollTop,
  focus: document.activeElement?.dataset.h9DetailHit,
};
scroll.scrollTop = 180;
window.__usagePoll();
await until(() => release);
document.getElementById('h9DetailBack').click();
document.querySelector('#h9LegendCards [data-h9-entity-key="codex"]').click();
await until(() => state.h9EntityData?.key === 'codex');
out.newView = scroll.scrollTop;
release();
await wait(40);
out.afterLateResponse = {
  key: state.h9EntityData?.key,
  scroll: scroll.scrollTop,
};
report(out);
""",
        ),
        960,
        tmp_path,
        height=420,
    )
    assert result == {
        "before": 200,
        "whileFetching": 200,
        "after": {"scroll": 200, "focus": "2"},
        "newView": 0,
        "afterLateResponse": {"key": "codex", "scroll": 0},
    }


H9_H10_DIAGNOSTICS_ESCAPE_PROBE = """
await until(() => !document.getElementById('h9AnalyticsSection').hidden && document.querySelectorAll('#h9ModelBody .h9-model-row').length > 0);
const scrollBody = document.getElementById('scrollBody');
const detail = document.getElementById('h9DetailView');
const diagView = document.getElementById('diagnosticsView');
const pop = document.getElementById('h9RangePopover');
const out = {};

scrollBody.style.scrollBehavior = 'auto';
scrollBody.scrollTop = 340;
await wait(20);
out.overviewScrollBefore = scrollBody.scrollTop;

document.querySelector('#h9ModelBody .h9-model-row[data-h9-entity-key="claude-opus-4-6"]').click();
await until(() => !detail.hidden && state.h9EntityData?.key === 'claude-opus-4-6');
scrollBody.scrollTop = 180;
await wait(20);
out.detailScrollBefore = scrollBody.scrollTop;

// Open detail range popover, then open Diagnostics directly -> popover closes, only Diagnostics shows
document.getElementById('h9DetailRangeBtn').click();
await until(() => pop.classList.contains('open'));
document.getElementById('diagBtn').click();
await until(() => state.diagnosticsOpen && !diagView.hidden);
out.diagOpened = {
  isDiagClass: document.documentElement.classList.contains('is-diagnostics'),
  popOpen: pop.classList.contains('open'),
  diagHidden: diagView.hidden,
  scrollHidden: scrollBody.hidden,
  scrollDisplay: getComputedStyle(scrollBody).display,
  opsDisplay: getComputedStyle(document.getElementById('opsBar')).display,
};

// First Escape closes Diagnostics only and restores Detail scroll (~180px)
document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
await until(() => !state.diagnosticsOpen && diagView.hidden);
await wait(50);
out.afterFirstEsc = {
  isDiagClass: document.documentElement.classList.contains('is-diagnostics'),
  diagHidden: diagView.hidden,
  detailHidden: detail.hidden,
  scrollHidden: scrollBody.hidden,
  detailScrollRestored: scrollBody.scrollTop,
};

// Second Escape closes Detail and restores Overview scroll (~340px)
document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
await until(() => detail.hidden);
await wait(50);
out.afterSecondEsc = {
  detailHidden: detail.hidden,
  heroHidden: document.getElementById('heroSection').offsetParent === null,
  stackHidden: document.querySelector('.section-stack').hidden,
  overviewScrollRestored: scrollBody.scrollTop,
};
report(out);
"""


@needs_chrome
@browser
def test_h9_ui_diagnostics_detail_popover_escape_and_scroll_restore(tmp_path):
    api_js = _h9_api_js() + """
    api.get_diagnostics_data = async function () {
      return {
        ok: true,
        freshness: { status: "ok", ingest: { lastPassSeconds: 1.1, lastPassAgeSeconds: 10 }, machines: [], issues: [] },
        health: { ingest: [], quotas: [], sources: [], pricingGaps: [], providerVsComputedVariancePct: 1.2 }
      };
    };
    """
    html = build_page(
        pref="light",
        api_js=api_js,
        probe=H9_H10_DIAGNOSTICS_ESCAPE_PROBE,
        extra_css=FAST,
    )
    r = probe(html, 960, tmp_path, height=420)
    assert r["overviewScrollBefore"] >= 300
    assert r["detailScrollBefore"] >= 150

    d = r["diagOpened"]
    assert d["isDiagClass"] is True
    assert d["popOpen"] is False
    assert d["diagHidden"] is False
    assert d["scrollHidden"] is True
    assert d["scrollDisplay"] == "none"
    assert d["opsDisplay"] == "none"

    e1 = r["afterFirstEsc"]
    assert e1["isDiagClass"] is False
    assert e1["diagHidden"] is True
    assert e1["detailHidden"] is False
    assert e1["scrollHidden"] is False
    assert abs(e1["detailScrollRestored"] - r["detailScrollBefore"]) <= 4

    e2 = r["afterSecondEsc"]
    assert e2["detailHidden"] is True
    assert e2["heroHidden"] is False
    assert e2["stackHidden"] is False
    assert abs(e2["overviewScrollRestored"] - r["overviewScrollBefore"]) <= 4


NARROW_OPS_AND_ANALYTICS_PROBE = """
await until(() =>
  !document.getElementById('h9AnalyticsSection').hidden &&
  !document.getElementById('hudQuickPlan').hidden &&
  !document.getElementById('runningNowCard').hidden &&
  document.querySelectorAll('#h9ModelBody .h9-model-card').length > 0
);
const scrollBody = document.getElementById('scrollBody');
report({
  docScroll: document.documentElement.scrollWidth,
  docClient: document.documentElement.clientWidth,
  bodyScroll: scrollBody.scrollWidth,
  bodyClient: scrollBody.clientWidth,
  opsHeight: document.getElementById('opsBar').getBoundingClientRect().height,
  quickPlanVisible: !document.getElementById('hudQuickPlan').hidden,
  runningVisible: !document.getElementById('runningNowCard').hidden,
  analyticsVisible: !document.getElementById('h9AnalyticsSection').hidden,
});
"""


@needs_chrome
@browser
@pytest.mark.parametrize("width", [304, 336, 380])
def test_h9_ui_narrow_fit_with_ops_bar_and_hud(tmp_path, width):
    from tests_spend.test_wealthfront_ui import demo_report as _demo_report

    rep = _demo_report()
    rep["activity"] = [
        {
            "name": "Fix analytics merge in narrow desktop viewport",
            "modelKey": "claude-opus-4-6",
            "toolKey": "claude-code",
            "machineName": "Desktop",
            "startedAt": "2026-11-16T22:00:00Z",
            "state": "live",
        }
    ]
    api_js = _h9_api_js() + """
    api.get_setup_check = async function () {
      return {
        ok: true,
        check: {
          plans: {
            suggestions: [
              { lane: "claude-code", name: "Claude Max", amount_usd: 100, cadence: "monthly", basis: "Detected local Claude Code activity" }
            ]
          }
        }
      };
    };
    """
    html = build_page(
        pref="light",
        report=rep,
        api_js=api_js,
        probe=NARROW_OPS_AND_ANALYTICS_PROBE,
        extra_css=FAST,
    )
    r = probe(html, width, tmp_path, height=900)
    assert r["docScroll"] <= r["docClient"], (width, r)
    assert r["bodyScroll"] <= r["bodyClient"], (width, r)
    assert r["opsHeight"] > 0
    assert r["quickPlanVisible"] is True
    assert r["runningVisible"] is True
    assert r["analyticsVisible"] is True
