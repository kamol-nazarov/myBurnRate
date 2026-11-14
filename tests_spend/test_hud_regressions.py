"""Compact HUD readability and failures after HTTP response headers arrive."""

import json
import subprocess

import pytest

from spend_app.hud import render_hud


def _node(script):
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                            encoding="utf-8", timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _block(start, end):
    return start + render_hud().split(start, 1)[1].split(end, 1)[0]


def test_missing_ingest_age_is_not_a_fresh_collection():
    block = _block("        const FEED_LATE_MS", "        // Stat tips")
    result = _node("""
const node = {textContent: '', classList: {toggle() {}, remove() {}}};
const $ = () => node;
const ago = seconds => seconds + 's';
""" + block + """
feedHealth.live = Date.now();
for (const age of [null, undefined, '']) {
  noteDataAge({lastIngestAgeSeconds: age, generatedAt: new Date().toISOString()});
}
paintUpdated();
const missing = {dataAt: feedHealth.dataAt, text: node.textContent};
noteDataAge({lastIngestAgeSeconds: 0, generatedAt: new Date().toISOString()});
paintUpdated();
console.log(JSON.stringify({missing, zero: node.textContent}));
""")
    assert result["missing"] == {"dataAt": None, "text": "Waiting for collection"}
    assert result["zero"] == "Updated now"


def test_usage_feed_that_never_succeeds_becomes_stale():
    block = _block("        const FEED_LATE_MS", "        // Stat tips")
    result = _node("""
let now = 100000;
Date.now = () => now;
""" + block + """
const initial = feedLate();
now += USAGE_LATE_MS + 1;
feedHealth.live = now;
const missingUsage = feedLate();
feedHealth.usage = now;
console.log(JSON.stringify({initial, missingUsage, recovered: feedLate()}));
""")
    assert result == {"initial": False, "missingUsage": True, "recovered": False}


def test_hud_timeout_covers_the_response_body_and_releases_poll_guard():
    block = _block("        const FETCH_TIMEOUT_MS", "        async function refreshUsage")
    result = _node("""
let timer, cleared = 0, requests = 0, rendered = [];
let selectedMachine = 'all';
const statusView = {};
const paintStatus = () => {};
const renderSummary = data => rendered.push(data);
const setTimeout = callback => { timer = callback; return 1; };
const clearTimeout = () => { cleared++; };
const fetch = async (url, options) => {
  requests++;
  if (requests > 1) return {ok: true, json: async () => ({recovered: true})};
  return {ok: true, json: () => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new Error('aborted')));
  })};
};
""" + block + """
(async () => {
  const pending = refresh();
  await Promise.resolve();
  await Promise.resolve();
  const beforeAbort = {cleared, pending: livePending !== null};
  timer();
  await pending;
  const afterAbort = {cleared, pending: livePending !== null, offline: statusView.offline};
  await refresh();
  console.log(JSON.stringify({beforeAbort, afterAbort, requests, rendered}));
})().catch(error => {console.error(error); process.exitCode = 1;});
""")
    assert result["beforeAbort"] == {"cleared": 0, "pending": True}
    assert result["afterAbort"] == {"cleared": 1, "pending": False, "offline": True}
    assert result["requests"] == 2
    assert result["rendered"] == [{"recovered": True}]


def test_successful_unavailable_usage_clears_held_values_and_preserves_zero():
    block = _block("        let heldStats", "        let noticeTimer")
    result = _node("""
let selectedMachine = 'all', usageView = null, windowTokens = {};
const renderOwnerScope = () => {};
const stats = {};
const $ = () => ({textContent: ''});
const setStat = (id, value) => {stats[id] = value;};
const usd = v => v == null ? '—' : '$' + v;
const usd2 = usd;
const tok = v => String(v);
const noteDataAge = () => {}, renderRepos = () => {}, refreshTip = () => {};
""" + block + """
renderUsage({navigation: {todayUsd: 10, burnRatePerDay: 5, todayComplete: true}, totals: {cacheReusePct: 90}});
renderUsage({navigation: {todayUsd: null, burnRatePerDay: null, todayComplete: false}, totals: {cacheReusePct: null}});
const unavailable = {today: stats['hud-today'], burn: stats['hud-burn-rate'], cache: stats['hud-cache'], tooltip: usageView};
renderUsage({navigation: {todayUsd: 0, burnRatePerDay: 0, todayComplete: true}, totals: {cacheReusePct: 0}});
console.log(JSON.stringify({unavailable, zero: {today: stats['hud-today'], cache: stats['hud-cache']}}));
""")
    assert result["unavailable"]["today"] == "—"
    assert result["unavailable"]["burn"] == "—"
    assert result["unavailable"]["cache"] == "—"
    assert result["unavailable"]["tooltip"]["nav"]["todayUsd"] is None
    assert result["unavailable"]["tooltip"]["totals"]["cacheReusePct"] is None
    assert result["zero"] == {"today": "$0", "cache": "0%"}


def test_partial_value_tooltip_explains_missing_rates_or_token_details():
    block = _block("        function usageGapMessage", "        setTip")
    result = _node("const tok = value => String(value);\n" + block + """
const nav = {todayTokens: 100, todayUnpricedTokens: 100, todayComplete: false,
  todayUnpricedModels: ['codex:gpt-example']};
console.log(JSON.stringify({partial: usageGapMessage({...nav, todayUsd: 1}),
  unknown: usageGapMessage({...nav, todayUsd: null}), complete: usageGapMessage({todayComplete: true})}));
""")
    for key in ("partial", "unknown"):
        assert "cannot be valued because rates or token details are missing" in result[key]
        assert "gpt-example" in result[key]
        assert "no published price" not in result[key]
    assert result["partial"].startswith("At least this much.")
    assert result["unknown"].startswith("Value unavailable.")
    assert result["complete"] == ""


@pytest.mark.browser
@pytest.mark.parametrize("success", [True, False])
def test_machine_switch_clears_old_values_during_delayed_and_failed_polls(tmp_path, success):
    from tests_spend.browser_runtime import CHROME
    from tests_spend.test_frontend_viewports import _chrome_dump, _probe

    if not CHROME.exists():
        pytest.skip("Google Chrome is not installed")
    boot = """
localStorage.removeItem('burnrate:hud-mini');
window.EventSource = undefined;
window.__TAURI_INTERNALS__ = {invoke: async () => true};
window.deferDesktop = false;
window.pendingDesktop = [];
window.pendingLaptop = [];
const fixtureResponse = (machine, usage) => {
  const desktop = machine === 'desktop';
  const name = desktop ? 'Desktop' : 'Laptop';
  const now = new Date().toISOString();
  const data = usage ? {
    navigation: {todayUsd: desktop ? 1000 : 7, burnRatePerDay: desktop ? 500 : 2,
      todayComplete: true, todayTokens: desktop ? 100000 : 100},
    totals: {cacheReusePct: desktop ? 90 : 20},
    repos: {rows: [{project: name + 'Repo', usd: desktop ? 1000 : 7, tokens: 100}]}
  } : {status: 'live', activity: [{state: 'LIVE', name: name + ' Agent'}],
    capacity: [{providerKey: 'fixture-' + machine, providerName: name + ' Plan',
      rows: [{label: 'Weekly', limitKey: 'weekly', pct: desktop ? 80 : 10}]}]};
  return new Response(JSON.stringify({...data, generatedAt: now, lastIngestAgeSeconds: 0}), {status: 200});
};
window.fetch = async url => {
  if (url.startsWith('/api/machines')) return new Response(JSON.stringify({machines: [
    {id: 'desktop', name: 'Desktop', isLocal: true}, {id: 'laptop', name: 'Laptop'}]}), {status: 200});
  if (url.startsWith('/api/spend/hud?') || url.startsWith('/api/spend/hud/usage?')) {
    const machine = new URL(url, 'http://localhost').searchParams.get('machine');
    const usage = url.includes('/usage?');
    if (machine === 'desktop' && deferDesktop) return new Promise(resolve =>
      pendingDesktop.push(() => resolve(fixtureResponse(machine, usage))));
    if (machine === 'laptop') return new Promise((resolve, reject) => pendingLaptop.push(() => {
      if (SUCCEED) resolve(fixtureResponse(machine, usage));
      else reject(new Error('synthetic offline computer'));
    }));
    return fixtureResponse(machine, usage);
  }
  return new Response(JSON.stringify({status: 'live', issues: []}), {status: 200});
};
""".replace("SUCCEED", "true" if success else "false")
    probe = """
<script>
const switchProbe = {};
const snapshotMachine = () => ({selected: document.querySelector('.hud-machine-chip.active')?.textContent,
  today: document.getElementById('hud-today').textContent, mini: document.getElementById('hud-mini-today').textContent,
  cache: document.getElementById('hud-cache').textContent, tokens: document.getElementById('hud-tokens').textContent,
  repos: document.getElementById('hud-repo-rows').textContent,
  plans: [...document.querySelectorAll('.plan')].map(el => el.textContent),
  agents: document.getElementById('hud-agents').textContent,
  age: document.getElementById('hud-updated-foot').textContent,
  status: document.getElementById('hud-status-text').textContent});
setTimeout(() => {switchProbe.initial = snapshotMachine(); deferDesktop = true; window.dispatchEvent(new Event('focus'));}, 100);
setTimeout(() => {
  [...document.querySelectorAll('.hud-machine-chip')].find(el => el.textContent === 'Laptop').click();
  switchProbe.immediate = snapshotMachine();
}, 200);
setTimeout(() => pendingDesktop.splice(0).forEach(release => release()), 300);
setTimeout(() => {switchProbe.delayed = snapshotMachine(); pendingLaptop.splice(0).forEach(release => release());}, 400);
setTimeout(() => {switchProbe.final = snapshotMachine(); document.title = 'PROBE:' + JSON.stringify(switchProbe);}, 600);
</script>
"""
    page = tmp_path / "machine-hud.html"
    html = render_hud().replace("<script>", "<script>" + boot, 1)
    page.write_text(html.replace("</body>", probe + "</body>"), encoding="utf-8")
    report = _probe(_chrome_dump(page.as_uri() + "?machine=desktop", 380, tmp_path, budget_ms=1000))
    assert report["initial"]["today"] == "$1,000.00"
    assert report["initial"]["repos"] and report["initial"]["plans"] and report["initial"]["agents"]
    for state in ("immediate", "delayed") + (() if success else ("final",)):
        view = report[state]
        assert view["selected"] == "Laptop"
        assert view["today"] == view["mini"] == view["cache"] == view["tokens"] == "—"
        assert view["repos"] == view["agents"] == ""
        assert view["plans"] == []
        assert view["age"] == "Updating…"
        assert view["status"] in {"UPDATING", "OFFLINE"}
    if success:
        assert report["final"]["today"] == "$7.00"
        assert report["final"]["cache"] == "20%"
        assert "Desktop" not in json.dumps(report["final"])
    else:
        assert report["final"]["status"] == "OFFLINE"


@pytest.mark.browser
@pytest.mark.parametrize("success,value,complete,headline", [
    (True, 25, True, "$25"),
    (True, 25, False, "≥$25"),
    (True, None, False, "—"),
    (False, 25, True, None),
])
def test_share_button_uses_json_payload_and_reports_request_failure(tmp_path, success, value, complete, headline):
    from tests_spend.browser_runtime import CHROME
    from tests_spend.test_frontend_viewports import _chrome_dump, _probe

    if not CHROME.exists():
        pytest.skip("Google Chrome is not installed")
    boot = """
localStorage.removeItem('burnrate:hud-mini');
window.EventSource = undefined;
window.__TAURI_INTERNALS__ = {invoke: async () => true};
window.shareProbe = {requests: [], images: 0, clipboard: 0, errors: [], texts: []};
window.addEventListener('unhandledrejection', event => shareProbe.errors.push(String(event.reason)));
Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {
  write: async items => {shareProbe.clipboard += items.length;}
}});
window.ClipboardItem = class {constructor(data) {this.data = data;}};
const originalFillText = CanvasRenderingContext2D.prototype.fillText;
CanvasRenderingContext2D.prototype.fillText = function(text, ...args) {
  shareProbe.texts.push(text);
  return originalFillText.call(this, text, ...args);
};
HTMLCanvasElement.prototype.toBlob = callback => {
  shareProbe.images++;
  callback(new Blob(['synthetic image'], {type: 'image/png'}));
};
window.fetch = async url => {
  if (url.startsWith('/api/spend/share/week')) {
    shareProbe.requests.push(url);
    return new Response(JSON.stringify({usd: USD_VALUE, planUsd: 10, complete: COMPLETE_VALUE,
      from: '2026-11-02T00:00:00Z', to: '2026-11-09T00:00:00Z',
      days: 7, tokens: 100, sessions: 2, cacheReusePct: 90, tools: []}), {status: STATUS});
  }
  return new Response(JSON.stringify({status: 'live', capacity: [], activity: [], machines: [],
    navigation: {}, totals: {}, issues: []}), {status: 200});
};
""".replace("STATUS", "200" if success else "500").replace("USD_VALUE", json.dumps(value)).replace("COMPLETE_VALUE", json.dumps(complete))
    probe = """
<script>
setTimeout(() => document.getElementById('hud-share-btn').click(), 100);
setTimeout(() => {
  shareProbe.notice = document.getElementById('hud-notice').textContent;
  document.title = 'PROBE:' + JSON.stringify(shareProbe);
}, 500);
</script>
"""
    page = tmp_path / "share-hud.html"
    html = render_hud().replace("<script>", "<script>" + boot, 1)
    page.write_text(html.replace("</body>", probe + "</body>"), encoding="utf-8")
    report = _probe(_chrome_dump(page.as_uri() + "?machine=northwind", 380, tmp_path, budget_ms=1000))
    assert report["requests"] == ["/api/spend/share/week?machine=northwind"]
    assert not report["errors"]
    assert report["images"] == report["clipboard"] == int(success)
    if success:
        assert headline in report["texts"]
        if value is None:
            assert not any("the plan cost" in text or "on $" in text for text in report["texts"])
    assert report["notice"] == (
        "This week's card is on the clipboard · paste it anywhere" if success
        else "This week's card could not be made"
    )


@pytest.mark.browser
def test_failed_usage_poll_holds_values_but_explicit_null_clears_them(tmp_path):
    from tests_spend.browser_runtime import CHROME
    from tests_spend.test_frontend_viewports import _chrome_dump, _probe

    if not CHROME.exists():
        pytest.skip("Google Chrome is not installed")
    boot = """
window.EventSource = undefined;
window.__TAURI_INTERNALS__ = {invoke: async () => true};
window.usageMode = 'complete';
window.fetch = async url => {
  let data = {status: 'live', capacity: [], activity: [], machines: [], issues: []};
  if (url.startsWith('/api/spend/hud/usage')) {
    if (usageMode === 'failed') throw new Error('synthetic network failure');
    const complete = usageMode === 'complete';
    data = {navigation: {todayUsd: complete ? 10 : null, todayComplete: complete,
      burnRatePerDay: complete ? 5 : null, todayTokens: 100},
      totals: {cacheReusePct: complete ? 90 : null}};
  }
  return new Response(JSON.stringify(data), {status: 200});
};
"""
    probe = """
<script>
const captureUsage = () => ({today: document.getElementById('hud-today').textContent,
  cache: document.getElementById('hud-cache').textContent});
const usageProbe = {};
setTimeout(() => {usageProbe.initial = captureUsage(); usageMode = 'failed'; window.dispatchEvent(new Event('focus'));}, 100);
setTimeout(() => {usageProbe.failed = captureUsage(); usageMode = 'unavailable'; window.dispatchEvent(new Event('focus'));}, 300);
setTimeout(() => {usageProbe.unavailable = captureUsage(); document.title = 'PROBE:' + JSON.stringify(usageProbe);}, 500);
</script>
"""
    page = tmp_path / "usage-hud.html"
    html = render_hud().replace("<script>", "<script>" + boot, 1)
    page.write_text(html.replace("</body>", probe + "</body>"), encoding="utf-8")
    report = _probe(_chrome_dump(page.as_uri(), 380, tmp_path, budget_ms=1000))
    assert report["initial"] == report["failed"] == {"today": "$10.00", "cache": "90%"}
    assert report["unavailable"] == {"today": "—", "cache": "—"}


@pytest.mark.browser
def test_compact_hud_keeps_names_and_warning_readable(tmp_path):
    from tests_spend.browser_runtime import CHROME
    from tests_spend.test_frontend_viewports import _chrome_dump, _probe

    if not CHROME.exists():
        pytest.skip("Google Chrome is not installed")
    boot = """
localStorage.setItem('burnrate:hud-mini', '1');
window.EventSource = undefined;
window.__TAURI_INTERNALS__ = {invoke: async () => true};
const names = ['OpenCode Go', 'Antigravity', 'OpenRouter',
  'NorthwindResearchAndEngineeringAccountWithAnUnbrokenLongName'];
window.fetch = async url => {
  const stamp = new Date().toISOString();
  let data = {};
  if (url.startsWith('/api/spend/hud/usage')) data = {
    generatedAt: stamp, lastIngestAgeSeconds: 0,
    navigation: {todayUsd: 156.49, todayComplete: false, todayTokens: 100}, totals: {}
  };
  else if (url.startsWith('/api/spend/hud?')) data = {
    generatedAt: stamp, lastIngestAgeSeconds: 0, status: 'live',
    activity: [1, 2, 3].map(n => ({state: 'LIVE', name: 'Agent ' + n})),
    capacity: names.map((name, i) => ({providerKey: 'fixture-' + i,
      providerName: name, rows: [{label: 'Weekly', limitKey: 'weekly', pct: 25}]}))
  };
  else if (url.startsWith('/api/spend/freshness')) data = {status: 'warn', issues: [
    {short: 'Antigravity weekly', state: 'held', severity: 'warn', detail: 'Fixture'}
  ]};
  return new Response(JSON.stringify(data), {status: 200});
};
"""
    probe = """
<script>
setTimeout(() => {
  const shell = document.getElementById('hud-shell').getBoundingClientRect();
  const fits = el => {
    const box = el.getBoundingClientRect();
    return el.scrollWidth <= el.clientWidth + 1 && box.left >= shell.left && box.right <= shell.right;
  };
  const labels = [...document.querySelectorAll('.plan-head b')];
  const alert = document.getElementById('hud-mini-alert');
  const controls = document.querySelector('.hud-controls').getBoundingClientRect();
  const title = document.getElementById('hud-mini-today').getBoundingClientRect();
  const metersSeparate = [...document.querySelectorAll('.plan')].every(plan => {
    const meter = plan.querySelector('.windows').getBoundingClientRect();
    const name = plan.querySelector('.plan-head').getBoundingClientRect();
    return meter.left >= name.right || meter.top >= name.bottom;
  });
  document.title = 'PROBE:' + JSON.stringify({width: shell.width,
    labels: labels.map(el => ({text: el.textContent, fits: fits(el), wrap: getComputedStyle(el).whiteSpace})),
    alert: {text: alert.textContent, fits: fits(alert), below: alert.getBoundingClientRect().top >= title.bottom},
    controlsFit: controls.right <= shell.right && controls.left >= title.right,
    metersSeparate});
}, 250);
</script>
"""
    html = render_hud().replace("<script>", "<script>" + boot, 1)
    page = tmp_path / "hud.html"
    page.write_text(html.replace("</body>", probe + "</body>"), encoding="utf-8")
    report = _probe(_chrome_dump(page.as_uri() + "?glass=1", 296, tmp_path, budget_ms=1000))
    assert report["width"] == 296
    assert len(report["labels"]) == 4
    assert all(row["fits"] and row["wrap"] == "normal" for row in report["labels"])
    assert report["alert"] == {"text": "1 source needs attention", "fits": True, "below": True}
    assert report["controlsFit"] and report["metersSeparate"]
