"""The 440 x 640 full window: limits first, refill timelines, profiles and states, light and dark."""

from __future__ import annotations

import pytest

from spend_app.wealthfront_window import merge_provider_entries
from tests_spend.d2_fixtures import d2_report
from tests_spend.test_h9_desktop_analytics import _h9_api_js
from tests_spend.test_wealthfront_ui import (
    FAST,
    build_page,
    needs_chrome,
    probe,
)

browser = pytest.mark.browser


def test_disabled_profile_hides_local_capacity_and_history_from_live_providers():
    report = {"tools": [{"key": "claude-code", "tokens": 17}, {"key": "claude-code-work", "tokens": 23}]}
    hud = {"capacity": [{"providerKey": "claude-code", "rows": [{"source": "sync:desk:usage"}]}]}
    connections = [{"source": "claude_local", "profiles": [
        {"key": "default", "state": "disabled"}, {"key": "claude-code-work", "state": "receiving"},
    ]}]
    scopes = {"connections": connections, "local_machine_id": "desk"}
    assert [row["key"] for row in merge_provider_entries(report, hud=hud, **scopes)] == ["claude-code-work"]
    assert report["tools"][0] == {"key": "claude-code", "tokens": 17}
    hud["capacity"][0]["rows"][0]["source"] = "sync:laptop:usage"
    assert {row["key"] for row in merge_provider_entries(report, hud=hud, **scopes)} == {"claude-code", "claude-code-work"}
    assert [row["key"] for row in merge_provider_entries(report, hud=hud, machine="desk", **scopes)] == ["claude-code-work"]


def test_removed_profile_does_not_reappear_from_cached_quota_or_old_usage():
    report = {"tools": [{"key": "claude-code-work", "tokens": 23}]}
    hud = {"capacity": [{"providerKey": "claude-code-work", "rows": [{"source": "sync:desk:usage"}]}]}
    connections = [{"source": "claude_local", "profiles": [{"key": "default", "state": "receiving"}],
                    "removedProfileKeys": ["claude-code-work"]}]
    assert merge_provider_entries(report, hud=hud, connections=connections, local_machine_id="desk") == []
    connections[0]["profiles"].append({"key": "claude-code-work", "state": "receiving"})
    assert [p["key"] for p in merge_provider_entries(report, hud=hud, connections=connections,
                                                       local_machine_id="desk")] == ["claude-code-work"]


@needs_chrome
@browser
def test_configured_codex_speed_is_labeled_as_local_preference(tmp_path):
    report = d2_report()
    for provider in report["providers"]:
        provider["configuredSpeed"] = "ultrafast"
    script = """
await until(() => document.querySelector('.provider-speed'));
report([...document.querySelectorAll('.provider-speed')].map(row => [
  row.closest('.provider-item').dataset.key, row.textContent.trim(),
  row.getBoundingClientRect().right <= window.innerWidth,
]));
"""
    html = build_page(report=report, pref="dark", probe=script, extra_css=FAST)
    assert probe(html, 380, tmp_path, height=520) == [["codex", "Ultrafast · configured on this PC", True]]

LIMITS_PROBE = """
await until(() => document.querySelectorAll('#providerList .provider-item').length >= 5);
const groups = [...document.querySelectorAll('#providerList .provider-item')];
const text = (el, sel) => { const n = el.querySelector(sel); return n ? n.textContent.trim().replace(/\\s+/g, ' ') : null; };
const lane = (g, key) => g.querySelector('.lane[data-limit="' + key + '"]');
const claude = groups[0];
report({
  order: groups.map(g => g.dataset.key),
  levels: groups.map(g => g.dataset.level),
  titles: groups.map(g => text(g, '.provider-name')),
  tags: groups.map(g => text(g, '.tag')),
  claude5h: {
    note: text(lane(claude, '5h'), '.lane-note'),
    pct: text(lane(claude, '5h'), '.lane-pct'),
    cls: lane(claude, '5h').className,
    dry: !!lane(claude, '5h').querySelector('.lane-dry'),
    dot: !!lane(claude, '5h').querySelector('.lane-dot'),
    refill: !!lane(claude, '5h').querySelector('.lane-refill'),
  },
  claudeWeekly: { note: text(lane(claude, 'weekly'), '.lane-note'), cls: lane(claude, 'weekly').className },
  go: [...groups[2].querySelectorAll('.lane')].map(l => [text(l, '.lane-name'), text(l, '.lane-pct'), text(l, '.lane-note'), !!l.querySelector('.lane-dry')]),
  grok: [text(groups[3], '.lane-name'), text(groups[3], '.lane-pct'), text(groups[3], '.lane-note')],
  balance: { amount: text(groups[4], '.balrow strong'), line: text(groups[4], '.balrow span'), lanes: groups[4].querySelectorAll('.lane').length },
  plans: [...document.querySelectorAll('#plansList li')].map(li => li.children[0].textContent + ' | ' + li.children[1].textContent),
  ticks: [...document.querySelectorAll('#limitTicks span')].map(s => s.textContent),
  logos: groups.map(g => {
    const chip = g.querySelector('.provider-row .brand-chip');
    return { sym: chip && chip.querySelector('use').getAttribute('href'), letters: chip ? chip.textContent.trim() : null };
  }),
  monograms: document.querySelectorAll('.mono').length,
  scale: [limitX(0, 288), limitX(6, 288), limitX(24, 288), limitX(72, 288), limitX(120, 288), limitX(288, 288), limitX(900, 288)].map(v => Math.round(v * 1000) / 1000),
  hint: document.getElementById('providersHint').textContent,
});
"""


@needs_chrome
@browser
@pytest.mark.parametrize("pref", ["light", "dark"])
def test_limits_follow_the_contract_with_real_logos_and_refill_lanes(tmp_path, pref):
    html = build_page(pref=pref, color_scheme=pref, report=d2_report(), probe=LIMITS_PROBE, extra_css=FAST)
    r = probe(html, 440, tmp_path, height=640)

    # most urgent first: soon, then warn by hours to dry (14h before 32h), ok, then the balance
    assert r["order"] == ["claude-code", "codex", "opencode-go", "grok", "openrouter"]
    assert r["levels"] == ["soon", "warn", "warn", "ok", "balance"]
    assert r["titles"] == ["Claude Code", "Codex", "OpenCode Go", "Grok Build", "OpenRouter"]
    assert r["tags"] == ["About to run out", "Dry before refill", "Dry before refill", None, None]
    assert r["hint"].startswith("most urgent first")

    c = r["claude5h"]
    assert c["note"] == "Dry in ~1h · refills in 2h" and c["pct"] == "54%"
    assert "is-soon" in c["cls"] and c["dry"] and c["dot"] and c["refill"]
    assert r["claudeWeekly"]["note"] == "Dry in ~27h · refills in 3d"
    assert "is-warn" in r["claudeWeekly"]["cls"]
    assert r["go"] == [
        ["5-hour", "69%", "Refills in 1h", False],
        ["Weekly", "56%", "Dry in ~32h · refills in 2d", True],
        ["Monthly", "46%", "Refills in 12d", False],
    ]
    assert r["grok"] == ["Weekly", "88%", "Refills in 5d"]
    assert r["balance"] == {
        "amount": "$23.40",
        "line": "balance left · runway ~42d at recent pace",
        "lanes": 0,
    }
    assert r["plans"] == [
        "Claude Code | Claude Max",
        "Codex | ChatGPT Pro",
        "OpenCode Go | Go",
        "Grok Build | SuperGrok Heavy",
    ]
    assert r["ticks"] == ["now", "6h", "1d", "3d", "12d"]
    # the real provider logos, never letters
    assert [x["sym"] for x in r["logos"]] == ["#claude", "#codex", "#opencode", "#grok", "#openrouter"]
    assert all(x["letters"] == "" for x in r["logos"])
    assert r["monograms"] == 0
    # piecewise time scale: the first day takes 45%, days 1 to 3 take 10%
    assert r["scale"] == [0, 0.113, 0.45, 0.55, 0.65, 1, 1]


OLD_SERVICE_PROBE = """
await until(() => document.querySelectorAll('#providerList .provider-item').length >= 5);
const g = [...document.querySelectorAll('#providerList .provider-item')];
report({
  order: g.map(x => x.dataset.key),
  tags: document.querySelectorAll('#providerList .tag').length,
  dry: document.querySelectorAll('#providerList .lane-dry').length,
  note: g[0].querySelector('.lane .lane-note').textContent,
  titles: g.map(x => x.querySelector('.provider-name').textContent),
  balance: g[4].querySelector('.balrow span').textContent,
  hint: document.getElementById('providersHint').textContent,
});
"""


@needs_chrome
@browser
def test_an_older_service_without_the_contract_fields_still_renders(tmp_path):
    html = build_page(pref="light", report=d2_report("old-service"), probe=OLD_SERVICE_PROBE, extra_css=FAST)
    r = probe(html, 440, tmp_path, height=640)
    # nothing derived: no tags, no dry segments, the existing forecast text shown as is, service order kept
    assert r["order"] == ["claude-code", "codex", "opencode-go", "grok", "openrouter"]
    assert r["tags"] == 0 and r["dry"] == 0
    assert r["note"] == "Refills in 2h · Runs dry in about 1h at this pace"
    assert r["balance"] == "balance left · Runway ~42d at recent pace"
    assert r["hint"].startswith("your order")


PROFILES_PROBE = """
await until(() => document.querySelectorAll('#providerList .provider-item').length >= 6);
const g = [...document.querySelectorAll('#providerList .provider-item')];
report({
  titles: g.map(x => x.querySelector('.provider-name').textContent),
  keys: g.map(x => x.dataset.key),
  badges: g.map(x => (x.querySelector('.acct-tag') || {}).textContent || null),
  hint: document.getElementById('providersHint').textContent,
  logos: g.filter(x => x.dataset.key.startsWith('claude-code')).map(x => x.querySelector('.brand-chip use').getAttribute('href')),
});
"""


@needs_chrome
@browser
def test_two_profiles_of_one_provider_get_one_group_each_ordered_by_display_rank(tmp_path):
    html = build_page(pref="dark", color_scheme="dark", report=d2_report("profiles"), probe=PROFILES_PROBE, extra_css=FAST)
    r = probe(html, 440, tmp_path, height=640)
    assert "Claude Code · Personal" in r["titles"] and "Claude Code · Work" in r["titles"]
    # the service's displayRank decides: the healthy Work profile does not jump ahead of Codex or OpenCode Go
    assert r["keys"] == ["claude-code", "codex", "opencode-go", "claude-code-work", "grok", "openrouter"]
    assert r["hint"].startswith("most urgent first")
    assert r["badges"].count(None) == len(r["badges"])
    assert r["logos"] == ["#claude", "#claude"]


ORDER_PROBE = """
await until(() => document.querySelectorAll('#providerList .provider-item').length >= 3);
const g = [...document.querySelectorAll('#providerList .provider-item')];
report({ keys: g.map(x => x.dataset.key), levels: g.map(x => x.dataset.level), hint: document.getElementById('providersHint').textContent });
"""


@needs_chrome
@browser
def test_a_more_urgent_provider_is_never_behind_a_healthy_profile(tmp_path):
    """Claude Primary out, Claude Work ok, Codex soon must render out, soon, ok."""
    html = build_page(pref="light", report=d2_report("mixed-profiles"), probe=ORDER_PROBE, extra_css=FAST)
    r = probe(html, 440, tmp_path, height=640)
    assert r["keys"] == ["claude-code", "codex", "claude-code-work"]
    assert r["levels"] == ["out", "soon", "ok"]
    assert r["hint"].startswith("most urgent first")


@needs_chrome
@browser
def test_a_saved_custom_order_beats_display_rank(tmp_path):
    report = d2_report("profiles")
    service_order = [p["key"] for p in report["providers"]]
    html = build_page(
        pref="light", report=report, init={"providerOrder": service_order}, probe=ORDER_PROBE.replace(">= 3", ">= 6"), extra_css=FAST
    )
    r = probe(html, 440, tmp_path, height=640)
    assert r["keys"] == service_order and r["keys"] != sorted(service_order, key=lambda k: {p["key"]: p["displayRank"] for p in report["providers"]}[k])
    assert r["hint"].startswith("your order")


@needs_chrome
@browser
def test_without_display_rank_the_service_order_is_kept(tmp_path):
    report = d2_report("mixed-profiles")
    for prov in report["providers"]:
        del prov["displayRank"]
    html = build_page(pref="light", report=report, probe=ORDER_PROBE, extra_css=FAST)
    r = probe(html, 440, tmp_path, height=640)
    assert r["keys"] == ["claude-code", "claude-code-work", "codex"]  # as sent: the page does not sort on its own
    assert r["hint"].startswith("your order")


LANE_STATES_PROBE = """
await until(() => document.querySelectorAll('#providerList .lane').length >= 5);
const lane = (key) => document.querySelector('#providerList .lane[data-limit="' + key + '"]');
const read = (key) => {
  const l = lane(key);
  return {
    pct: l.querySelector('.lane-pct').textContent,
    note: l.querySelector('.lane-note').textContent,
    use: !!l.querySelector('.lane-use'),
    dry: !!l.querySelector('.lane-dry'),
    refill: !!l.querySelector('.lane-refill'),
    soon: l.classList.contains('is-soon'),
    warn: l.classList.contains('is-warn'),
    level: !!l.querySelector('.lane-level'),
    levelWidth: l.querySelector('.lane-level') ? l.querySelector('.lane-level').style.width : null,
    grid: l.querySelectorAll('.lane-grid').length,
    trackLevel: l.querySelector('.lane-track').classList.contains('is-level'),
    label: l.querySelector('.lane-track').getAttribute('aria-label'),
  };
};
const legendLevel = document.getElementById('limitLegendLevel');
report({
  none: read('5h'), stale: read('weekly'), out: read('monthly'), safe: read('credits'), outDry: read('extra'),
  legend: { hidden: legendLevel.hidden, text: legendLevel.textContent, visible: legendLevel.offsetParent !== null },
});
"""


@needs_chrome
@browser
@pytest.mark.parametrize("pref", ["light", "dark"])
def test_lanes_claim_no_usable_time_without_a_forecast_and_none_when_exhausted(tmp_path, pref):
    html = build_page(pref=pref, color_scheme=pref, report=d2_report("lane-states"), probe=LANE_STATES_PROBE, extra_css=FAST)
    r = probe(html, 440, tmp_path, height=640)

    # no forecast, and a stale one: a level bar (the share left, off the time axis), no usable timeline, no refill marker
    for case in ("none", "stale"):
        lane = r[case]
        assert lane["level"] is True and lane["trackLevel"] is True, (case, lane)
        assert lane["use"] is False and lane["dry"] is False and lane["refill"] is False and lane["grid"] == 0, (case, lane)
        assert "pace unknown" not in lane["note"] and lane["note"].startswith("Refills in "), (case, lane)
    assert r["none"]["note"] == "Refills in 3h" and r["stale"]["note"] == "Refills in 3d"
    assert r["none"]["pct"] == "40%" and r["stale"]["pct"] == "35%"
    assert r["none"]["levelWidth"] == "40%" and r["stale"]["levelWidth"] == "35%"
    assert r["none"]["label"] == "5-hour: 40% left, refills in 3h, no pace forecast yet"
    # the legend explains the soft bar, and only because a lane uses it
    assert r["legend"] == {"hidden": False, "text": "left (no pace yet)", "visible": True}

    # exhausted: no usable time before the refill, the whole wait is the dry stretch, and the label agrees
    for case in ("out", "outDry"):
        lane = r[case]
        assert lane["pct"] == "0%" and lane["use"] is False and lane["dry"] is True and lane["refill"] is True, (case, lane)
        assert lane["soon"] is True and lane["note"].startswith("Out · refills in ") and lane["level"] is False, (case, lane)
        assert "0% left" in lane["label"] and "Out" in lane["label"], (case, lane)
    assert r["out"]["note"] == "Out · refills in 24h"

    # a forecast that says it lasts (even with numeric hours) is usable time all the way to the refill
    safe = r["safe"]
    assert safe["use"] is True and safe["dry"] is False and safe["refill"] is True
    assert safe["note"] == "Refills in 10h" and not safe["soon"] and not safe["warn"] and safe["level"] is False
    assert safe["grid"] > 0


STATES_PROBE = """
await until(() => document.querySelectorAll('#providerList .provider-item').length >= 5);
const codex = [...document.querySelectorAll('#providerList .provider-item')].find(g => g.dataset.key === 'codex');
const lane = codex.querySelector('.lane');
report({
  muted: lane.classList.contains('is-muted'),
  name: lane.querySelector('.lane-name').textContent,
  pct: lane.querySelector('.lane-pct').textContent,
  note: lane.querySelector('.lane-note').textContent,
  noRefill: !lane.querySelector('.lane-refill') && !lane.querySelector('.lane-use'),
  tag: codex.querySelector('.tag'),
  level: codex.dataset.level,
});
"""


@needs_chrome
@browser
def test_an_unavailable_provider_is_a_muted_lane_with_its_reason(tmp_path):
    html = build_page(pref="light", report=d2_report("unavailable"), probe=STATES_PROBE, extra_css=FAST)
    r = probe(html, 440, tmp_path, height=640)
    assert r == {
        "muted": True,
        "name": "Weekly",
        "pct": "—",
        "note": "Sign in to Codex to read limits",
        "noRefill": True,
        "tag": None,
        "level": "idle",
    }


EMPTY_PROBE = """
await until(() => document.querySelector('#providerList .limits-empty'));
const card = document.querySelector('#providerList .limits-empty');
const btn = card.querySelector('button');
const rows = [...document.querySelectorAll('#providerList .provider-item')];
const out = {
  title: card.querySelector('b').textContent,
  button: btn.textContent,
  buttonClass: btn.className,
  sub: card.querySelector('span').textContent,
  groups: rows.length,
  mutedLanes: document.querySelectorAll('#providerList .lane.is-muted').length,
  reasons: [...new Set([...document.querySelectorAll('#providerList .lane.is-muted .lane-note')].map(n => n.textContent))],
  headlines: document.querySelectorAll('#providerList .balrow').length,
  ticksHidden: document.getElementById('limitTicks').hidden,
  legendHidden: document.getElementById('limitLegend').hidden,
};
btn.click();
await until(() => state.settingsOpen);
out.settingsOpen = state.settingsOpen;
out.settingsTab = state.settingsTab;
report(out);
"""


@needs_chrome
@browser
def test_connected_but_idle_sources_wait_for_readings_with_add_a_source_as_a_link(tmp_path):
    html = build_page(pref="light", report=d2_report("no-limits"), probe=EMPTY_PROBE, extra_css=FAST)
    r = probe(html, 440, tmp_path, height=640)
    assert r == {
        "title": "Waiting for the first readings",
        "button": "Add a source",
        "buttonClass": "limits-empty-link",
        "sub": "Limits show here as each source reports.",
        "groups": 5,
        "mutedLanes": 5,
        "reasons": ["Waiting for fresh reading"],
        "headlines": 0,
        "ticksHidden": True,
        "legendHidden": True,
        "settingsOpen": True,
        "settingsTab": "sources",
    }


SUMMARY_ONLY_PROBE = """
await until(() => document.querySelectorAll('#providerList .provider-item').length >= 5);
const rows = [...document.querySelectorAll('#providerList .provider-item .balrow')];
report({
  empty: !!document.querySelector('#providerList .limits-empty'),
  rows: rows.length,
  first: rows[0].querySelector('strong').textContent,
  lanes: document.querySelectorAll('#providerList .lane').length,
});
"""


@needs_chrome
@browser
def test_an_older_service_headline_without_windows_is_a_summary_row_not_an_empty_card(tmp_path):
    html = build_page(pref="light", report=d2_report("summary-only"), probe=SUMMARY_ONLY_PROBE, extra_css=FAST)
    r = probe(html, 440, tmp_path, height=640)
    assert r == {"empty": False, "rows": 5, "first": "54% left", "lanes": 0}


PLANS_PROBE = """
await until(() => !document.getElementById('h9AnalyticsSection').hidden);
document.querySelector('#viewNavBar [data-view="plans"]').click();
await wait(40);
const rows = () => [...document.querySelectorAll('#plansList li')].map(li => li.children[0].textContent + ' | ' + li.children[1].textContent);
report({ rows: rows(), sub: document.getElementById('plansSub').textContent, shown: getComputedStyle(document.getElementById('plansCard')).display });
"""


@needs_chrome
@browser
def test_plans_show_their_price_from_the_analytics_summary(tmp_path):
    api_js = _h9_api_js() + """
    var __sum = api.fetch_analytics_summary;
    api.fetch_analytics_summary = async function () {
      var res = await __sum.apply(null, arguments);
      res.summary.subscriptions = [
        { name: "Claude Max 20x", toolKey: "claude-code", amountUsd: 200, cadence: "monthly", planState: "paid" },
        { name: "ChatGPT Pro", toolKey: "codex", amountUsd: 200, cadence: "monthly", planState: "paid" },
        { name: "Cursor annual", toolKey: "cursor", amountUsd: 192.5, cadence: "annual", planState: "paid" },
        { name: "Grok free", toolKey: "grok", amountUsd: null, cadence: "monthly", planState: "no paid plan" },
      ];
      return res;
    };
    """
    html = build_page(pref="light", report=d2_report(), api_js=api_js, probe=PLANS_PROBE, extra_css=FAST)
    r = probe(html, 440, tmp_path, height=640)
    assert r["rows"] == [
        "Claude Max 20x | $200/mo",
        "ChatGPT Pro | $200/mo",
        "Cursor annual | $192.50/yr",
        "Grok free | no paid plan",
    ]
    assert r["sub"].endswith("accrued this period") and r["shown"] != "none"


PLAN_SETTINGS_API = """
const testPlans = { plans: [], authority: "local", today: "2026-09-20",
    tools: ["codex", "claude-code"], cadences: ["monthly", "annual"] };
let rejectFirst = true;
window.planChanges = [];
api.get_plan_settings = async () => ({ok: true, result: structuredClone(testPlans)});
api.change_plan = async (authority, body) => {
  window.planChanges.push({ authority, ...body });
  if (rejectFirst) { rejectFirst = false; return {ok: false, error: "Please retry"}; }
  if (body.operation === "add") {
    testPlans.plans = [{ id: 8, version: 1, terms: [{ id: 17, status: "active", ...body.values }] }];
  } else if (body.preview) {
    return {ok: true, result: { from: "2026-09-01", through: "2026-09-20", before_usd: "10", after_usd: "12", preview_token: "preview" }};
  } else if (body.operation === "end") {
    testPlans.plans[0].terms[0].status = "ended";
  }
  return {ok: true, result: {id: 8, version: 2}};
};
"""


PLAN_SETTINGS_PROBE = """
const byId = id => document.getElementById(id);
const pick = selector => document.querySelector(selector);
const submit = () => byId('planEditor').requestSubmit();
await until(() => !byId('managePlansBtn').hidden);
const out = { inferredPlans: document.querySelectorAll('#plansList li').length };
byId('managePlansBtn').click();
await until(() => byId('addPlanBtn'));
byId('addPlanBtn').click();
pick('#planEditor [name=name]').value = 'Synthetic Plan';
pick('#planEditor [name=amount_usd]').value = '18.25';
pick('#planEditor [name=start_date]').value = '2026-09-01';
submit();
await until(() => pick('#sheetBody [role=alert]')?.textContent === 'Please retry');
out.retainedPrice = pick('#planEditor [name=amount_usd]').value;
submit();
await until(() => pick('[data-plan-action=end]'));
out.savedPlan = byId('sheetBody').textContent.includes('Synthetic Plan');
out.reusedRequest = window.planChanges[0].request_id === window.planChanges[1].request_id;
pick('#sheetBody details').open = true;
pick('[data-plan-action=correct]').click();
pick('#planEditor [name=name]').value = 'Corrected Plan';
submit();
await until(() => pick('#planEditor [type=submit]')?.textContent === 'Confirm correction');
out.preview = byId('planEditor').textContent.includes('Accrued cost $10 → $12');
submit();
await until(() => pick('[data-plan-action=end]'));
pick('[data-plan-action=end]').click();
submit();
await until(() => byId('endedPlans'));
out.historyPreserved = byId('endedPlans').textContent.includes('Synthetic Plan') && !byId('endedPlans').open;
out.requests = window.planChanges;
out.hiddenProviders = getNormalizedProviders({providers: [], providersAuthoritative: true, tools: [{key: 'codex'}]}).length;
report(out);
"""


@needs_chrome
@browser
def test_manage_plans_uses_configured_terms_and_preserves_draft_on_retry(tmp_path):
    html = build_page(pref="dark", report=d2_report(), init={"demo": False},
                      api_js=PLAN_SETTINGS_API, probe=PLAN_SETTINGS_PROBE, extra_css=FAST)
    result = probe(html, 380, tmp_path, height=520)
    assert result["inferredPlans"] == result["hiddenProviders"] == 0
    assert result["retainedPrice"] == "18.25"
    assert all(result[key] for key in ("savedPlan", "reusedRequest", "preview", "historyPreserved"))
    add, retry, preview, correction, end = result["requests"]
    assert add == retry
    assert add["authority"] == "local" and add["values"]["amount_usd"] == "18.25"
    assert preview["preview"] is True and preview["term_id"] == 17
    assert correction["confirmed"] is True and correction["preview_token"] == "preview"
    assert correction["values"]["name"] == "Corrected Plan"
    assert (end["operation"], end["plan_id"], end["expected_version"], end["end_date"]) == ("end", 8, 1, "2026-09-20")


LOADING_PROBE = """
await wait(120);
report({
  skeleton: !!document.querySelector('#providerList .limits-skel'),
  status: (document.querySelector('#providerList .limits-skel .sr-only') || {}).textContent,
  heroLoading: document.getElementById('heroSection').classList.contains('is-loading'),
  heroBusy: document.getElementById('heroSection').getAttribute('aria-busy'),
  ticksHidden: document.getElementById('limitTicks').hidden,
  legendHidden: document.getElementById('limitLegend').hidden,
  fakeValues: document.querySelectorAll('#providerList .lane-pct, #providerList .provider-name').length,
});
"""


@needs_chrome
@browser
def test_loading_shows_skeletons_and_no_invented_values(tmp_path):
    api_js = "api.fetch_usage = function () { return new Promise(function () {}); };"
    html = build_page(pref="light", api_js=api_js, probe=LOADING_PROBE, extra_css=FAST)
    r = probe(html, 440, tmp_path, height=640)
    assert r == {
        "skeleton": True,
        "status": "Loading your AI providers…",
        "heroLoading": True,
        "heroBusy": "true",
        "ticksHidden": True,
        "legendHidden": True,
        "fakeValues": 0,
    }


ERROR_PROBE = """
await until(() => document.querySelectorAll('#providerList .provider-item').length >= 5);
const banner = document.getElementById('errorBanner');
const before = { hidden: banner.hidden, stale: document.getElementById('sectionStack').classList.contains('is-stale') };
window.__fail = { ok: false, error: "Can't reach the hub. Error NET-001" };
await loadUsage(true, false);
await wait(30);
const during = {
  hidden: banner.hidden,
  message: banner.querySelector('span').textContent,
  code: banner.querySelector('code').textContent,
  note: banner.querySelector('small').textContent,
  role: banner.getAttribute('role'),
  stale: document.getElementById('sectionStack').classList.contains('is-stale'),
  groupsKept: document.querySelectorAll('#providerList .provider-item').length,
  pill: document.getElementById('liveText').textContent,
  hubAddress: /https?:|\\d+\\.\\d+\\.\\d+\\.\\d+/.test(banner.textContent),
};
window.__fail = { ok: false, error: 'Usage service stopped', code: 'SVC-210' };
await loadUsage(true, false);
await wait(30);
const coded = { message: banner.querySelector('span').textContent, code: banner.querySelector('code').textContent };
window.__fail = null;
await loadUsage(true, false);
await wait(30);
report({ before, during, coded, after: { hidden: banner.hidden, stale: document.getElementById('sectionStack').classList.contains('is-stale') } });
"""


@needs_chrome
@browser
def test_a_failed_refresh_shows_the_message_and_code_and_marks_values_stale(tmp_path):
    api_js = """
    var __usage = api.fetch_usage;
    api.fetch_usage = async function () { return window.__fail ? window.__fail : __usage.apply(null, arguments); };
    """
    html = build_page(pref="dark", color_scheme="dark", report=d2_report(), api_js=api_js, probe=ERROR_PROBE, extra_css=FAST)
    r = probe(html, 440, tmp_path, height=640)
    assert r["before"] == {"hidden": True, "stale": False}
    d = r["during"]
    assert d["hidden"] is False and d["role"] == "alert"
    assert d["message"] == "Can't reach the hub." and d["code"] == "NET-001"
    assert d["note"] == "Showing the last known values."
    assert d["stale"] is True and d["groupsKept"] == 5
    assert d["pill"] == "Unavailable"
    assert d["hubAddress"] is False
    assert r["coded"] == {"message": "Usage service stopped", "code": "SVC-210"}
    assert r["after"] == {"hidden": True, "stale": False}


WINDOW_BUTTONS_PROBE = """
await until(() => document.querySelector('.provider-row'));
const top = [...document.querySelectorAll('.top-actions button')].map(b => b.id);
const foot = [...document.querySelectorAll('#opsBar button')].map(b => b.id);
document.getElementById('tabBtn').click();
document.getElementById('pinBtn').click();
await wait(40);
report({ top, foot, calls: window.__calls.map(c => c[0]).filter(n => ['show_tab', 'minimize_window', 'set_topmost'].includes(n)) });
"""


@needs_chrome
@browser
def test_title_bar_shrinks_to_the_tab_and_the_pin_lives_in_the_footer(tmp_path):
    api_js = """
    api.show_tab = async function () { window.__calls.push(['show_tab']); return { ok: true }; };
    api.minimize_window = function () { window.__calls.push(['minimize_window']); };
    api.set_topmost = async function (v) { window.__calls.push(['set_topmost', v]); return { ok: true, topmost: v }; };
    """
    html = build_page(pref="light", api_js=api_js, probe=WINDOW_BUTTONS_PROBE, extra_css=FAST)
    r = probe(html, 440, tmp_path, height=640)
    assert r["top"] == ["refreshBtn", "tabBtn", "closeBtn"]
    assert r["foot"] == ["diagBtn", "shareWeekBtn", "pinBtn", "settingsBtn"]
    assert r["calls"] == ["show_tab", "set_topmost"]


FALLBACK_PROBE = """
await until(() => document.querySelector('.provider-row'));
document.getElementById('tabBtn').click();
await wait(30);
report(window.__calls.map(c => c[0]).filter(n => ['show_tab', 'minimize_window'].includes(n)));
"""


@needs_chrome
@browser
def test_shrink_falls_back_to_minimize_on_a_host_without_show_tab(tmp_path):
    api_js = "api.minimize_window = function () { window.__calls.push(['minimize_window']); };"
    html = build_page(pref="light", api_js=api_js, probe=FALLBACK_PROBE, extra_css=FAST)
    assert probe(html, 440, tmp_path, height=640) == ["minimize_window"]


FIT_PROBE = """
await until(() => document.querySelectorAll('#providerList .provider-item').length >= 5);
const out = [];
for (const width of [440, 380, 360]) {
  await setWidth(width);
  const de = document.documentElement;
  const sb = document.getElementById('scrollBody');
  const over = [];
  const inner = window.innerWidth;
  document.querySelectorAll('body *').forEach(el => {
    if (el.closest('svg') || el.closest('[hidden]') || el.closest('.sr-only') || el.classList.contains('sr-only')) return;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.position === 'fixed') return;
    const r = el.getBoundingClientRect();
    if (r.width && (r.right > inner + 0.5 || r.left < -0.5) && !el.closest('.settings-sheet, .scope-popover, .rz, .toast')) over.push((el.id || el.className || el.tagName).toString().slice(0, 40));
  });
  const scope = document.getElementById('scopeBtn').getBoundingClientRect();
  const label = document.getElementById('scopeLabel');
  const lanes = [...document.querySelectorAll('.lane')];
  out.push({
    width, inner,
    docScroll: de.scrollWidth - inner,
    bodyScroll: sb.scrollWidth - sb.clientWidth,
    over: over.slice(0, 6),
    scopeLabelClipped: label.scrollWidth > label.clientWidth + 1,
    scopeInside: scope.right <= inner + 0.5,
    laneOverlap: lanes.some(l => l.querySelector('.lane-name').getBoundingClientRect().right > l.querySelector('.lane-track').getBoundingClientRect().left + 1),
    trackWidths: lanes.slice(0, 1).map(l => Math.round(l.querySelector('.lane-track').getBoundingClientRect().width)),
    tickOverlap: (() => {
      const spans = [...document.querySelectorAll('#limitTicks span')].filter(s => getComputedStyle(s).display !== 'none').map(s => s.getBoundingClientRect());
      return spans.some((a, i) => i && spans[i - 1].right > a.left - 2);
    })(),
  });
}
report(out);
"""


@needs_chrome
@browser
@pytest.mark.parametrize("pref", ["light", "dark"])
def test_no_horizontal_overflow_at_440_380_and_360(tmp_path, pref):
    html = build_page(pref=pref, color_scheme=pref, report=d2_report("profiles"), probe=FIT_PROBE, extra_css=FAST)
    r = probe(html, 440, tmp_path, height=640)
    assert [c["width"] for c in r] == [440, 380, 360]
    for case in r:
        assert case["inner"] == case["width"], case
        assert case["docScroll"] <= 0, case
        assert case["bodyScroll"] <= 1, case
        assert case["over"] == [], case
        assert case["scopeInside"] is True and case["laneOverlap"] is False, case
        assert case["tickOverlap"] is False, case
    assert r[0]["scopeLabelClipped"] is False, r[0]  # the computer selector fits at 440 without an ellipsis
    assert r[0]["trackWidths"][0] > r[2]["trackWidths"][0] > 120


CONTRAST_PROBE = """
await until(() => document.querySelectorAll('#providerList .provider-item').length >= 5);
const pairs = {
  tag_soon: '.tag.soon', tag_warn: '.tag.warn', note_soon: '.lane.is-soon .lane-note', note_warn: '.lane.is-warn .lane-note',
  pct_soon: '.lane.is-soon .lane-pct', pct: '.lane:not(.is-soon) .lane-pct', name: '.lane-name', note: '.lane:not(.is-soon):not(.is-warn) .lane-note',
  balance: '.balrow strong', balance_text: '.balrow span', sub: '.provider-sub', title: '.provider-name', legend: '.leg span',
  tick: '#limitTicks span', hint: '#providersHint',
};
const out = {};
for (const [k, sel] of Object.entries(pairs)) { const el = document.querySelector(sel); out[k] = el ? textContrast(el) : null; }
document.getElementById('errorBanner').hidden = false;
document.getElementById('errorBanner').innerHTML = '<span>Can\\'t reach the hub.</span><code>NET-001</code><small>Showing the last known values.</small>';
out.err = textContrast(document.querySelector('#errorBanner span'));
out.err_small = textContrast(document.querySelector('#errorBanner small'));
report(out);
"""


@needs_chrome
@browser
@pytest.mark.parametrize("pref", ["light", "dark"])
def test_limit_text_meets_aa_contrast_in_light_and_dark(tmp_path, pref):
    html = build_page(pref=pref, color_scheme=pref, report=d2_report(), probe=CONTRAST_PROBE, extra_css=FAST)
    r = probe(html, 440, tmp_path, height=640)
    for key, ratio in r.items():
        assert ratio is not None, key
        # the hint and the axis ticks are tertiary captions; everything else is body text
        floor = 3.0 if key in {"tick", "hint"} else 4.5
        assert ratio >= floor, (pref, key, ratio)


def test_the_page_no_longer_carries_the_mini_bar_or_monograms():
    from tests_spend.test_wealthfront_ui import _source

    text = _source()
    for gone in ("hudMiniBar", "hud-mini", "fullModeBtn", "miniModeBtn", "is-mini", 'class="mono"'):
        assert gone not in text, gone
    assert "set_hud_mini" not in text
    assert "show_tab" in text


REAL_DATA_PROBE = """
await until(() => document.querySelectorAll('#providerList .provider-item').length >= 8);
const g = [...document.querySelectorAll('#providerList .provider-item')];
report({
  keys: g.map(x => x.dataset.key),
  titles: g.map(x => x.querySelector('.provider-name').textContent),
  tags: g.map(x => (x.querySelector('.tag') || {}).textContent || null),
  claudeLane: g[0].querySelector('.lane .lane-note').textContent,
  laneCount: document.querySelectorAll('#providerList .lane').length,
  noteGlitches: [...document.querySelectorAll('#providerList .lane-note')].filter(n => /pace →|→|undefined|NaN/.test(n.textContent)).length,
  balance: [...document.querySelectorAll('#providerList .balrow')].map(b => b.textContent.trim().replace(/\s+/g, ' ')),
});
"""


@needs_chrome
@browser
@pytest.mark.parametrize("pref", ["light", "dark"])
def test_real_demo_data_follows_display_rank_and_renders_cleanly(tmp_path, pref):
    """The demo snapshot as the real ``merge_provider_entries`` sends it, not a hand-made fixture."""
    from tests_spend.test_wealthfront_ui import demo_report

    report = demo_report()
    html = build_page(pref=pref, color_scheme=pref, report=report, probe=REAL_DATA_PROBE, extra_css=FAST)
    r = probe(html, 440, tmp_path, height=640)
    providers = report["providers"]
    ranks = [p.get("displayRank") for p in providers]
    # one source of order for both surfaces: the window shows exactly the service's ranking
    expected = [p["key"] for p in sorted(providers, key=lambda p: p["displayRank"])] if all(rank is not None for rank in ranks) else [p["key"] for p in providers]
    assert r["keys"] == expected
    by_key = dict(zip(r["keys"], zip(r["titles"], r["tags"])))
    assert by_key["claude-code"] == ("Claude Code · Primary", "About to run out")
    assert by_key["claude-code-work"][0] == "Claude Code · Work" and by_key["claude-code-work"][1] is None
    assert by_key["codex"][1] == "Dry before refill" and by_key["opencode-go"][1] == "Dry before refill"
    assert r["keys"][0] == "claude-code"
    assert r["claudeLane"] == "Dry in ~1h · refills in 2h"
    assert r["noteGlitches"] == 0
    assert any(line.startswith("$23.40 balance left · runway ~42d") for line in r["balance"])


LEVEL_CONTRAST_PROBE = """
await until(() => document.querySelector('#providerList .lane-level'));
const cs = getComputedStyle(document.documentElement);
const track = parseColor(cs.getPropertyValue('--track').trim());
const accent = parseColor(cs.getPropertyValue('--accent').trim());
const level = document.querySelector('#providerList .lane-level');
const base = document.querySelector('#providerList .lane-base');
const fill = parseColor(getComputedStyle(level).backgroundColor);
const solid = parseColor(getComputedStyle(document.querySelector('.leg .u')).backgroundColor);
const swatch = document.querySelector('.leg .l');
report({
  outline: contrast(accent.rgb, track.rgb),
  fillAlpha: fill.a,
  solidAlpha: solid.a,
  fillIsSoft: fill.a < 0.6 && fill.a > 0.2,
  outlineShadow: getComputedStyle(level).boxShadow !== 'none',
  swatchShadow: getComputedStyle(swatch).boxShadow !== 'none',
  trackBg: getComputedStyle(base).backgroundColor,
});
"""


@needs_chrome
@browser
@pytest.mark.parametrize("pref", ["light", "dark"])
def test_the_level_bar_is_soft_but_meets_non_text_contrast_against_the_track(tmp_path, pref):
    html = build_page(pref=pref, color_scheme=pref, report=d2_report("lane-states"), probe=LEVEL_CONTRAST_PROBE, extra_css=FAST)
    r = probe(html, 440, tmp_path, height=640)
    assert r["fillIsSoft"] is True and r["solidAlpha"] == 1
    # the outline carries the 3:1 non-text contrast; the soft fill alone could not
    assert r["outline"] >= 3.0, r
    assert r["outlineShadow"] is True and r["swatchShadow"] is True


def test_the_default_canvas_state_has_no_level_lanes_so_the_extra_legend_stays_hidden():
    # structural: every lane in the canvas fixture has a forecast, so none uses the level bar
    from tests_spend.d2_fixtures import d2_report as build

    for prov in build()["providers"]:
        for win in prov["windows"]:
            assert win["unit"] == "usd" or win["pace"] is not None


RULER_PROBE = """
await until(() => document.querySelectorAll('#providerList .provider-item').length >= 5);
const snap = () => ({
  ruler: !document.getElementById('limitTicks').hidden && document.getElementById('limitTicks').offsetParent !== null,
  legend: !document.getElementById('limitLegend').hidden,
  timelineItems: [...document.querySelectorAll('#limitLegend .leg-time')].map(el => el.offsetParent !== null),
  levelItem: document.getElementById('limitLegendLevel').offsetParent !== null,
  levelLanes: document.querySelectorAll('#providerList .lane-track.is-level').length,
  timelineLanes: document.querySelectorAll('#providerList .lane:not(.is-muted) .lane-track:not(.is-level)').length,
});
const out = { levelOnly: snap() };
// one lane gets a forecast: the ruler and the timeline legend come back
state.report.providers[0].windows[0].pace = { state: 'tight', hoursToDry: 1.2, hoursDryBeforeReset: 1, runsDryAt: null };
renderReport(state.report, false);
out.withTimeline = snap();
// and go again when the forecast goes
state.report.providers[0].windows[0].pace = null;
renderReport(state.report, false);
out.levelOnlyAgain = snap();
report(out);
"""


@needs_chrome
@browser
def test_the_time_ruler_and_timeline_legend_only_show_while_a_lane_uses_the_time_axis(tmp_path):
    report = d2_report("level-only")
    # keep it simple: no balance row or exhausted lane, just the measured windows
    html = build_page(pref="light", report=report, probe=RULER_PROBE, extra_css=FAST)
    r = probe(html, 440, tmp_path, height=640)

    for case in ("levelOnly", "levelOnlyAgain"):
        s = r[case]
        assert s["timelineLanes"] == 0 and s["levelLanes"] > 0, (case, s)
        assert s["ruler"] is False and s["timelineItems"] == [False, False, False], (case, s)
        assert s["levelItem"] is True and s["legend"] is True, (case, s)
    s = r["withTimeline"]
    assert s["timelineLanes"] == 1
    assert s["ruler"] is True and s["timelineItems"] == [True, True, True] and s["levelItem"] is True
