"""The 440 x 640 full window: limits first, refill timelines, profiles and states, light and dark."""

from __future__ import annotations

import pytest

from tests_spend.d2_fixtures import d2_report
from tests_spend.test_h9_desktop_analytics import _h9_api_js
from tests_spend.test_wealthfront_ui import (
    FAST,
    build_page,
    needs_chrome,
    probe,
)

browser = pytest.mark.browser

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
def test_two_profiles_of_one_provider_get_one_group_each(tmp_path):
    html = build_page(pref="dark", color_scheme="dark", report=d2_report("profiles"), probe=PROFILES_PROBE, extra_css=FAST)
    r = probe(html, 440, tmp_path, height=640)
    assert "Claude Code · Personal" in r["titles"] and "Claude Code · Work" in r["titles"]
    # most urgent first, but the Work profile sits right after the Personal one (the tab's rule), not by its own urgency
    assert r["keys"] == ["claude-code", "claude-code-work", "codex", "opencode-go", "grok", "openrouter"]
    assert r["hint"].startswith("most urgent first")
    assert r["badges"].count(None) == len(r["badges"])
    assert r["logos"] == ["#claude", "#claude"]


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
    assert r["foot"] == ["diagBtn", "shareWeekBtn", "pinBtn", "densityBtn", "settingsBtn"]
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
  noteGlitches: [...document.querySelectorAll('#providerList .lane-note')].filter(n => /pace|→|undefined|NaN/.test(n.textContent)).length,
  balance: [...document.querySelectorAll('#providerList .balrow')].map(b => b.textContent.trim().replace(/\s+/g, ' ')),
});
"""


@needs_chrome
@browser
@pytest.mark.parametrize("pref", ["light", "dark"])
def test_real_demo_data_orders_like_the_tab_and_renders_cleanly(tmp_path, pref):
    """The demo snapshot as the real ``merge_provider_entries`` sends it, not a hand-made fixture."""
    from spend_app.tab_window import tab_providers
    from tests_spend.test_wealthfront_ui import demo_report

    report = demo_report()
    html = build_page(pref=pref, color_scheme=pref, report=report, probe=REAL_DATA_PROBE, extra_css=FAST)
    r = probe(html, 440, tmp_path, height=640)
    # one rule for both surfaces: the tab's order, profiles of a provider kept together
    assert r["keys"] == [p["key"] for p in tab_providers(report["providers"])]
    assert r["keys"][:5] == ["claude-code", "claude-code-work", "codex", "opencode-go", "opencode-go-2"]
    assert r["titles"][:2] == ["Claude Code · Primary", "Claude Code · Work"]
    assert r["tags"][:2] == ["About to run out", None] and r["tags"][2:4] == ["Dry before refill", "Dry before refill"]
    assert r["claudeLane"] == "Dry in ~1h · refills in 2h"
    assert r["noteGlitches"] == 0
    assert any(line.startswith("$23.40 balance left · runway ~42d") for line in r["balance"])
