"""Headless-Chrome harness for the native Wealthfront page (spend_app/wealthfront_ui.html).

The page talks to Python only through ``window.pywebview.api``; the harness injects a stub
of that bridge ahead of the page, loads the page the way the host does (``load_ui_html``
plus the saved-theme rewrite) and lets a probe script report back through ``document.title``.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from spend_app.native_desktop import DemoClient, demo_hud, demo_hud_usage
from spend_app.wealthfront_window import load_ui_html, merge_provider_entries
from tests_spend.browser_runtime import CHROME
from tests_spend.test_frontend_viewports import _probe

THEME_ATTR = 'data-theme-pref="system"'


def demo_report(period: str = "today") -> dict:
    report = DemoClient().snapshot(period, "all")
    report["providers"] = merge_provider_entries(
        report,
        hud=demo_hud(),
        hud_usage=demo_hud_usage(),
        today_report=report,
        week_report=report,
    )
    return report


def empty_report() -> dict:
    report = demo_report()
    report["providers"] = []
    report["tools"] = []
    report["total"] = {"valueUsd": 0, "tokens": 0, "events": 0, "complete": True}
    return report


DEFAULT_INIT = {
    "period": "today",
    "topmost": False,
    "autoFade": True,
    "idleOpacity": 0.38,
    "providerOrder": [],
    "demo": True,
    "theme": "system",
    "closeToTray": True,
    "lowQuotaAlerts": False,
    "trayAvailable": True,
    "version": "0.5.4",
    "update": {"kind": "installer", "current": "0.5.4", "canAutoUpdate": True},
}

DEFAULT_ONBOARDING = {
    "ok": True,
    "tools": [
        {
            "source": "claude-code",
            "name": "Claude Code",
            "brand": "claude",
            "detected": True,
            "active": True,
            "detail": "Reading ~/.claude sessions",
        },
        {
            "source": "codex",
            "name": "Codex",
            "brand": "codex",
            "detected": True,
            "active": False,
            "detail": "Installed on this computer",
        },
        {
            "source": "opencode",
            "name": "OpenCode",
            "brand": "opencode",
            "detected": False,
            "active": False,
            "detail": "",
        },
    ],
    "apiSources": [
        {
            "source": "openrouter",
            "name": "OpenRouter",
            "brand": "openrouter",
            "connected": False,
            "credentialHelp": "Paste an OpenRouter management key",
            "revision": 3,
        },
        {
            "source": "deepseek",
            "name": "DeepSeek",
            "brand": "deepseek",
            "connected": False,
            "credentialHelp": "Paste a DeepSeek API key",
            "revision": 1,
        },
    ],
    "hubConnected": False,
}

# Stands in for the DesktopBridge. ``api_js`` runs with ``api`` in scope so a test can add,
# replace or delete methods before the page boots.
STUB = """<script>
window.__calls = [];
(function () {
  var REPORT = %(report)s, INIT = %(init)s, ONB = %(onboarding)s, SETTINGS = %(settings)s;
  var api = {
    get_initial_state: async function () { return INIT; },
    fetch_usage: async function () { return { ok: true, report: REPORT }; },
    get_settings_data: async function () { return SETTINGS; },
    save_provider_order: async function (o) { window.__calls.push(["save_provider_order", o]); return { ok: true }; },
    save_period: async function () { return { ok: true }; },
    set_ui_busy: function () {},
    set_topmost: async function (v) { return { ok: true }; },
    set_theme: async function (t) { window.__calls.push(["set_theme", t]); return { ok: true, theme: t }; },
    report_effective_theme: async function (d) { window.__calls.push(["report_effective_theme", d]); return { ok: true }; },
    set_close_to_tray: async function (v) { window.__calls.push(["set_close_to_tray", v]); return { ok: true, closeToTray: v }; },
    set_low_quota_alerts: async function (v) { window.__calls.push(["set_low_quota_alerts", v]); return { ok: true, lowQuotaAlerts: v }; },
    quit_app: function () { window.__calls.push(["quit_app"]); },
    close_window: function () { window.__calls.push(["close_window"]); },
    minimize_window: function () {},
    check_for_update: async function () {
      return { ok: true, current: "0.5.4", available: true, version: "0.6.0", notes: "Dark theme, tray, onboarding.",
               pageUrl: "https://example.invalid/download", canInstall: true };
    },
    install_update: async function () { return { ok: true, restarting: true }; },
    connect_source: async function (s, isApi, key, rev) {
      window.__calls.push(["connect_source", s, isApi, key, rev]);
      return { ok: false, error: "Key rejected by the provider." };
    }
  };
  if (ONB !== null) api.get_onboarding_data = async function () { return ONB; };
  %(api_js)s
  window.pywebview = { api: api };
})();
</script>"""


def build_page(
    *,
    pref: str = "light",
    report: dict | None = None,
    init: dict | None = None,
    onboarding: dict | None = DEFAULT_ONBOARDING,
    settings: dict | None = None,
    api_js: str = "",
    probe: str = "",
    extra_css: str = "",
) -> str:
    html = load_ui_html()
    assert html.count(THEME_ATTR) == 1, "theme attribute must appear exactly once"
    html = html.replace(THEME_ATTR, f'data-theme-pref="{pref}"', 1)
    stub = STUB % {
        "report": json.dumps(report if report is not None else demo_report()),
        "init": json.dumps(
            {**DEFAULT_INIT, **(init or {}), "theme": (init or {}).get("theme", pref)}
        ),
        "onboarding": json.dumps(onboarding),
        "settings": json.dumps(
            settings
            or {
                "ok": True,
                "connections": [],
                "machines": [],
                "setup": {},
                "centralServerUrl": "",
            }
        ),
        "api_js": api_js,
    }
    html = html.replace("<head>", "<head>" + stub, 1)
    tail = ""
    if extra_css:
        tail += f"<style>{extra_css}</style>"
    if probe:
        tail += (
            f"<script>{PROBE_PRELUDE}\n(async function () {{\n{probe}\n}})();</script>"
        )
    return html.replace("</body>", tail + "</body>")


PROBE_PRELUDE = """
const wait = (ms) => new Promise(r => setTimeout(r, ms));
async function until(fn, tries = 200) { for (let i = 0; i < tries; i++) { if (fn()) return true; await wait(10); } return false; }
function report(obj) { const top = window.parent || window; top.document.title = "PROBE:" + JSON.stringify(obj); }
function setWidth(px) { const f = window.parent.document.getElementById("f"); f.style.width = px + "px"; return wait(60); }
function lum(c) { const [r, g, b] = c.map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }); return 0.2126 * r + 0.7152 * g + 0.0722 * b; }
function parseColor(s) {
  const m = s.match(/rgba?\\(([^)]+)\\)/);
  if (m) { const p = m[1].split(/[ ,\\/]+/).filter(Boolean).map(Number); return { rgb: p.slice(0, 3), a: p.length > 3 ? p[3] : 1 }; }
  const c = document.createElement("canvas"); c.width = c.height = 1; const x = c.getContext("2d");
  x.fillStyle = s; x.fillRect(0, 0, 1, 1); const d = x.getImageData(0, 0, 1, 1).data;
  return { rgb: [d[0], d[1], d[2]], a: d[3] / 255 };
}
function contrast(fg, bg) { const a = lum(fg), b = lum(bg); return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05); }
function over(top, under) { const a = top.a; return { rgb: top.rgb.map((v, i) => v * a + under.rgb[i] * (1 - a)), a: 1 }; }
// Effective background colour behind an element: walk up compositing translucent layers.
function backdrop(el) {
  const stack = [];
  for (let n = el; n; n = n.parentElement) {
    const c = parseColor(getComputedStyle(n).backgroundColor);
    if (c.a > 0) { stack.push(c); if (c.a >= 1) break; }
  }
  let base = { rgb: [255, 255, 255], a: 1 };
  for (let i = stack.length - 1; i >= 0; i--) base = over(stack[i], base);
  return base;
}
function textContrast(el) {
  const bg = backdrop(el);
  const fg = parseColor(getComputedStyle(el).color);
  return contrast(over(fg, bg).rgb, bg.rgb);
}
function smallText() {
  const out = [];
  document.querySelectorAll("body *").forEach(el => {
    if (el.closest("svg")) return;
    const own = Array.from(el.childNodes).some(n => n.nodeType === 3 && n.textContent.trim());
    if (!own) return;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") return;
    const fs = parseFloat(cs.fontSize);
    if (fs < 10.5) out.push((el.className || el.tagName) + ":" + fs);
  });
  return out;
}
"""


def run_chrome(
    html: str,
    width: int,
    tmp: Path,
    *,
    height: int = 900,
    flags: tuple[str, ...] = (),
    screenshot: Path | None = None,
    name: str = "page",
    budget_ms: int = 8000,
) -> str:
    """Serve ``html`` inside a ``width`` x ``height`` iframe (headless Chrome will not go below
    ~500px wide on its own) and return the dumped DOM, or take a screenshot of the wrapper."""
    wrapper = (
        '<!doctype html><html><body style="margin:0;background:#888">'
        f'<iframe id="f" src="/ui" style="border:0;display:block;width:{width}px;height:{height}px"></iframe>'
        "</body></html>"
    ).encode()
    ui = html.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            body = ui if self.path.startswith("/ui") else wrapper
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = tmp / f"p{os.getpid()}"
    profile.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(CHROME),
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--mute-audio",
        "--disable-dev-shm-usage",
        f"--user-data-dir={profile}",
        f"--window-size={max(width, 500)},{height}",
        f"--virtual-time-budget={budget_ms}",
        "--force-device-scale-factor=1",
        *flags,
    ]
    if screenshot is not None:
        cmd.append(f"--screenshot={screenshot}")
    else:
        cmd.append("--dump-dom")
    cmd.append(f"http://127.0.0.1:{server.server_port}/")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
    out = result.stdout or ""
    if screenshot is None and "PROBE:" not in out and result.returncode != 0:
        raise AssertionError(
            f"chrome failed rc={result.returncode} stderr={result.stderr[-1500:]}"
        )
    return out


def probe(html: str, width: int, tmp: Path, **kw) -> dict:
    return _probe(run_chrome(html, width, tmp, **kw))


# ----------------------------------------------------------------------------- tests

import re
import shutil

import pytest

UI_HTML = Path(__file__).resolve().parents[1] / "spend_app" / "wealthfront_ui.html"
FAST = "*{animation:none!important;transition:none!important}"
needs_chrome = pytest.mark.skipif(
    not CHROME.exists(), reason="Google Chrome is needed for native UI evidence"
)
browser = pytest.mark.browser


def _source() -> str:
    return UI_HTML.read_text(encoding="utf-8")


def _block(text: str, start: str) -> tuple[int, int]:
    a = text.index(start)
    return a, text.index("\n}\n", a) + 3


def _outside_token_blocks(text: str) -> str:
    """The page with the two token tables and the brand palette table removed."""
    for start in (":root {", 'html[data-theme="dark"] {'):
        a, b = _block(text, start)
        text = text[:a] + text[b:]
    a = text.index("const MONO_DARK")
    b = text.index("\n};\n", text.index("const BRAND_STYLES")) + 4
    return text[:a] + text[b:]


# ---- static checks ----------------------------------------------------------------


def test_theme_attribute_is_unique_and_scripts_parse(tmp_path):
    text = _source()
    assert text.count(THEME_ATTR) == 1
    assert re.search(r'<html lang="en" data-theme-pref="system">', text)
    # The pre-paint theme script must run before the stylesheet is parsed.
    assert text.index("burnrateTheme") < text.index("<style>")
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is needed for the inline script syntax check")
    for i, script in enumerate(re.findall(r"<script>(.*?)</script>", text, re.DOTALL)):
        path = tmp_path / f"inline{i}.js"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run(
            [node, "--check", str(path)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr


def test_no_literal_colours_outside_token_tables():
    rest = _outside_token_blocks(_source())
    hexes = re.findall(r"(?<![&\w])#[0-9a-fA-F]{3,8}\b", rest)
    assert hexes == [], f"un-tokenised hex colours: {hexes[:12]}"
    functions = re.findall(r"\b(?:rgba?|hsla?)\(", rest)
    assert functions == [], "un-tokenised rgb()/hsl() colours"
    named = re.findall(
        r":\s*(?:white|black|red|blue|green|gray|grey|orange|purple)\s*[;}\"']", rest
    )
    assert named == []


def test_dark_tokens_override_only_defined_tokens_and_cover_surfaces():
    text = _source()
    root = dict(
        re.findall(r"(--[\w-]+):\s*([^;]+);", text[slice(*_block(text, ":root {"))])
    )
    dark = dict(
        re.findall(
            r"(--[\w-]+):\s*([^;]+);",
            text[slice(*_block(text, 'html[data-theme="dark"] {'))],
        )
    )
    assert set(dark) <= set(root), sorted(set(dark) - set(root))
    for token in (
        "--bg-hero",
        "--bg-canvas",
        "--surface",
        "--surface-subtle",
        "--ink",
        "--ink-secondary",
        "--ink-tertiary",
        "--accent",
        "--accent-soft",
        "--on-accent",
        "--mint",
        "--warn",
        "--danger",
        "--border",
        "--row-hover",
        "--track",
        "--switch-off",
        "--toast-bg",
        "--chart-grid",
    ):
        assert token in dark, token
    assert (
        dark["--bg-canvas"].strip() == "#101117"
    )  # the host paints its native window with this hex
    assert root["--bg-canvas"].strip() == "#f5f6fa"
    used = set(re.findall(r"var\((--[\w-]+)", text))
    assert used <= set(root) | {
        "--bl-bg",
        "--bl-fg",
        "--bl-ring",
        "--bd-bg",
        "--bd-fg",
        "--bd-ring",
    }, sorted(used - set(root))


def test_no_font_size_below_the_floor_in_source():
    sizes = [float(v) for v in re.findall(r"font-size:\s*([\d.]+)px", _source())]
    assert sizes and min(sizes) >= 10.5, sorted(set(sizes))[:5]


def test_reduced_motion_rule_silences_loops_and_springs():
    text = _source()
    media = text[text.index("@media (prefers-reduced-motion: reduce)") :]
    media = media[: media.index("</style>")]
    assert "animation: none !important" in media
    assert "transition: none !important" in media
    assert "prefersReducedMotion()" in text


# ---- browser checks ---------------------------------------------------------------

CONTRAST_PROBE = """
await until(() => document.querySelector('.provider-row'));
const pairs = {
  ink_on_hero: '.balance-value', ink_on_surface: '.provider-name', secondary: '.provider-sub',
  tertiary: '.summary-label', tertiary_hero: '.hero .stat-secondary', accent_on_soft: '.period-pill.active',
  tag_accent: '.acct-tag:not(.neutral)', mint_hero: '.stat-primary', mint_rem: '.provider-rem.tone-mint',
  warn_rem: '.provider-rem.tone-warn'
};
const out = {};
for (const [k, sel] of Object.entries(pairs)) { const el = document.querySelector(sel); out[k] = el ? textContrast(el) : null; }
await openSettings('general'); await wait(50);
for (const [k, sel] of Object.entries({ done_btn: '#settingsDoneBtn', active_tab: '.sheet-tab.active', pill: '.btn-secondary.active',
    meta: '.source-meta', name: '.source-name', quit: '.btn-danger-quiet', update_meta: '.update-block .source-meta' })) {
  const el = document.querySelector(sel); out[k] = el ? textContrast(el) : null;
}
out.theme = document.documentElement.dataset.theme;
out.canvas = getComputedStyle(document.documentElement).backgroundColor;
report(out);
"""


@needs_chrome
@browser
def test_dark_theme_text_meets_aa_contrast(tmp_path):
    html = build_page(pref="dark", probe=CONTRAST_PROBE, extra_css=FAST)
    result = probe(html, 380, tmp_path, height=700)
    assert result["theme"] == "dark"
    assert result["canvas"] == "rgb(16, 17, 23)"
    required = (
        "ink_on_hero",
        "ink_on_surface",
        "secondary",
        "tertiary",
        "tertiary_hero",
        "accent_on_soft",
        "tag_accent",
        "mint_hero",
        "mint_rem",
        "done_btn",
        "active_tab",
        "pill",
        "meta",
        "name",
        "quit",
    )
    for key in required:
        assert result[key] is not None, f"no element for {key}"
        assert result[key] >= 4.5, (key, result[key])
    if result["warn_rem"] is not None:
        assert result["warn_rem"] >= 4.5


FONT_PROBE = """
await until(() => document.querySelector('.provider-row'));
const out = {};
out.main = smallText();
document.querySelectorAll('.provider-row').forEach(r => r.click());
await wait(50);
out.drawer = smallText();
out.drawerOpen = document.querySelectorAll('.provider-item.open').length;
for (const tab of ['sources', 'computers', 'general']) {
  await openSettings(tab); await wait(80);
  if (tab === 'sources') { toggleSourceEdit('openrouter'); await wait(30); }
  if (tab === 'computers') { setPairMode('create'); await wait(30); }
  if (tab === 'general') { await checkForUpdates(); await wait(30); }
  out[tab] = smallText();
}
out.sheetNodes = document.querySelectorAll('#sheetBody *').length;
report(out);
"""

SETTINGS_FIXTURE = {
    "ok": True,
    "connections": [
        {
            "source": "openrouter",
            "name": "OpenRouter balance",
            "kind": "api",
            "state": "connected",
            "detail": "Prepaid credits connected",
            "revision": 2,
        },
        {
            "source": "claude-code",
            "name": "Claude Code",
            "kind": "local",
            "state": "receiving_usage",
            "location": "C:/Users/me/.claude",
            "revision": 1,
        },
    ],
    "machines": [
        {"id": "pc", "name": "Desk PC", "online": True},
        {"id": "lt", "name": "Laptop", "online": False},
    ],
    "setup": {
        "account": {"username": "me", "serverUrl": "https://hub.example"},
        "spokeConfig": {
            "enabled": True,
            "hubUrl": "https://hub.example",
            "lastSyncError": "Timed out",
        },
        "computerName": "Desk PC",
    },
    "centralServerUrl": "https://hub.example",
}


@needs_chrome
@browser
@pytest.mark.parametrize("pref", ["light", "dark"])
def test_no_computed_font_size_below_10_5px(tmp_path, pref):
    html = build_page(
        pref=pref,
        probe=FONT_PROBE,
        extra_css=FAST,
        settings=SETTINGS_FIXTURE,
        init={"demo": False},
    )
    result = probe(html, 380, tmp_path, height=800)
    assert result["drawerOpen"] >= 3
    assert result["sheetNodes"] > 20
    for scene in ("main", "drawer", "sources", "computers", "general"):
        assert result[scene] == [], (scene, result[scene][:8])


ONBOARDING_FONT_PROBE = """
await until(() => document.querySelector('.ob-group'));
const out = { ready: smallText() };
document.querySelector('[data-ob-toggle=openrouter]').click();
document.getElementById('obKey_openrouter').value = 'sk-test';
await connectOnboardingSource('openrouter');
await wait(30);
out.expanded = smallText();
out.error = !!document.querySelector('.ob-error');
report(out);
"""


@needs_chrome
@browser
@pytest.mark.parametrize("pref", ["light", "dark"])
def test_onboarding_has_no_small_text_and_shows_inline_error(tmp_path, pref):
    html = build_page(
        pref=pref, report=empty_report(), probe=ONBOARDING_FONT_PROBE, extra_css=FAST
    )
    result = probe(html, 380, tmp_path, height=900)
    assert result["ready"] == [] and result["expanded"] == []
    assert result["error"] is True


ONBOARDING_PROBE = """
await until(() => document.querySelector('.ob-group'));
const out = {};
out.title = document.getElementById('providersTitle').textContent;
out.hintHidden = document.getElementById('providersHint').hidden;
out.groups = [...document.querySelectorAll('.ob-label span:first-child')].map(e => e.textContent);
out.tools = [...document.querySelectorAll('.ob-item')].slice(0, 3).map(e => [
  e.querySelector('.source-name').textContent, e.querySelector('.state-badge').textContent.trim(),
  !!e.querySelector('svg use'), e.querySelector('.ob-row').classList.contains('is-missing')]);
out.hubLink = document.querySelector('[data-ob-hub]').textContent;
out.panelHidden = document.getElementById('obPanel_openrouter').hidden;
document.querySelector('[data-ob-toggle=openrouter]').click();
await wait(20);
const input = document.getElementById('obKey_openrouter');
out.type = input.type;
out.focused = document.activeElement === input;
out.expandedAttr = document.querySelector('[data-ob-toggle=openrouter]').getAttribute('aria-expanded');
input.value = 'sk-bad-key';
document.querySelector('[data-ob-connect=openrouter]').click();
await until(() => document.querySelector('.ob-error'));
out.error = document.querySelector('.ob-error').textContent;
out.keyKept = document.getElementById('obKey_openrouter').value;
out.calls = window.__calls.filter(c => c[0] === 'connect_source');
document.querySelector('[data-ob-hub]').click();
await wait(30);
out.settingsOpen = document.getElementById('settingsSheet').classList.contains('open');
out.settingsTab = state.settingsTab;
report(out);
"""


@needs_chrome
@browser
def test_onboarding_guides_the_first_run(tmp_path):
    html = build_page(
        pref="dark", report=empty_report(), probe=ONBOARDING_PROBE, extra_css=FAST
    )
    result = probe(html, 380, tmp_path, height=900)
    assert result["title"] == "Get started" and result["hintHidden"] is True
    assert result["groups"] == ["Found on this computer", "Add an API balance"]
    assert result["tools"] == [
        ["Claude Code", "Tracking", True, False],
        ["Codex", "Detected", True, False],
        ["OpenCode", "Not found", True, True],
    ]
    assert result["hubLink"] == "Sign in to your hub"
    assert result["panelHidden"] is True
    assert (
        result["type"] == "password"
        and result["focused"] is True
        and result["expandedAttr"] == "true"
    )
    assert result["error"] == "Key rejected by the provider."
    assert result["keyKept"] == "sk-bad-key"
    assert result["calls"] == [["connect_source", "openrouter", True, "sk-bad-key", 3]]
    assert result["settingsOpen"] is True and result["settingsTab"] == "computers"


ONBOARDING_SUCCESS_PROBE = """
await until(() => document.querySelector('.ob-group'));
document.querySelector('[data-ob-toggle=deepseek]').click();
document.getElementById('obKey_deepseek').value = 'sk-good';
await connectOnboardingSource('deepseek');
await until(() => window.__usageCalls > 1);
await wait(50);
report({ usageCalls: window.__usageCalls, expanded: state.onboarding.expanded, toast: document.getElementById('toast').textContent });
"""


@needs_chrome
@browser
def test_onboarding_success_refreshes_usage(tmp_path):
    api_js = """
    window.__usageCalls = 0;
    var base = api.fetch_usage;
    api.fetch_usage = async function () { window.__usageCalls++; return base(); };
    api.connect_source = async function () { return { ok: true }; };
    """
    html = build_page(
        pref="light",
        report=empty_report(),
        probe=ONBOARDING_SUCCESS_PROBE,
        api_js=api_js,
        extra_css=FAST,
    )
    result = probe(html, 380, tmp_path, height=900)
    assert result["usageCalls"] >= 2
    assert result["expanded"] is None
    assert "connected" in result["toast"]


FALLBACK_PROBE = """
await until(() => document.querySelector('#providerList .empty-state'));
await wait(50);
report({ text: document.getElementById('providerList').textContent.replace(/\\s+/g, ' ').trim(),
         button: !!document.querySelector('#providerList .empty-state button'), ob: !!document.querySelector('.ob') });
"""


@needs_chrome
@browser
@pytest.mark.parametrize("variant", ["missing", "failing", "bad-shape"])
def test_onboarding_falls_back_to_the_plain_message(tmp_path, variant):
    onboarding = None if variant == "missing" else DEFAULT_ONBOARDING
    api_js = {
        "missing": "",
        "failing": "api.get_onboarding_data = async function () { throw new Error('boom'); };",
        "bad-shape": "api.get_onboarding_data = async function () { return { ok: false, error: 'nope' }; };",
    }[variant]
    html = build_page(
        pref="light",
        report=empty_report(),
        onboarding=onboarding,
        api_js=api_js,
        probe=FALLBACK_PROBE,
        extra_css=FAST,
    )
    result = probe(html, 380, tmp_path, height=700)
    assert result["text"] == "No AI providers connected yet. Connect a source"
    assert result["button"] is True and result["ob"] is False


ESCAPE_PROBE = """
await until(() => document.querySelector('.provider-row'));
const esc = () => (document.activeElement || document.body).dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
const sheet = document.getElementById('settingsSheet');
const pop = document.getElementById('scopePopover');
const out = {};
out.initial = { sheetInert: sheet.inert, popInert: pop.inert, sheetVis: getComputedStyle(sheet).visibility,
                popVis: getComputedStyle(pop).visibility, shellInert: document.getElementById('appShell').inert };
// 1. scope popover closes first and hands focus back to its trigger
document.getElementById('scopeBtn').click(); await wait(30);
out.popOpen = { open: pop.classList.contains('open'), inert: pop.inert, focusInPop: pop.contains(document.activeElement),
                expanded: document.getElementById('scopeBtn').getAttribute('aria-expanded'),
                items: [...pop.querySelectorAll('[role=menuitemradio]')].length };
pop.querySelector('.scope-opt').dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
out.arrowMoved = document.activeElement !== pop.querySelector('.scope-opt');
esc(); await wait(30);
out.afterPopEsc = { popOpen: pop.classList.contains('open'), popInert: pop.inert, focus: document.activeElement.id };
// 2. settings sheet: focus moves in, editor closes before the sheet does
document.getElementById('settingsBtn').click(); await wait(80);
out.sheetOpen = { open: sheet.classList.contains('open'), inert: sheet.inert, shellInert: document.getElementById('appShell').inert,
                  focusInSheet: sheet.contains(document.activeElement), syncBusy: window.__busy.slice(-1)[0] };
toggleSourceEdit('openrouter'); await wait(30);
out.editing = !!document.querySelector('.inline-editor');
document.getElementById('srcInput_openrouter').focus();
esc(); await wait(30);
out.afterEditorEsc = { editor: !!document.querySelector('.inline-editor'), sheetOpen: sheet.classList.contains('open'),
                       focus: document.activeElement.id };
setSettingsTab('computers'); await wait(30);
setPairMode('join'); await wait(30);
out.pairOpen = !!document.getElementById('joinCodeInput');
esc(); await wait(30);
out.afterPairEsc = { pair: !!document.getElementById('joinCodeInput'), sheetOpen: sheet.classList.contains('open') };
esc(); await wait(30);
out.afterSheetEsc = { open: sheet.classList.contains('open'), inert: sheet.inert, shellInert: document.getElementById('appShell').inert,
                      focus: document.activeElement.id, vis: getComputedStyle(sheet).visibility, busy: window.__busy.slice(-1)[0] };
// 3. a popover opened while the sheet is closed, then Escape twice does nothing odd
esc(); await wait(10);
out.idleEsc = document.activeElement.id;
report(out);
"""


@needs_chrome
@browser
def test_escape_closes_layers_in_order_and_closed_layers_are_inert(tmp_path):
    api_js = (
        "window.__busy = []; api.set_ui_busy = function (v) { window.__busy.push(v); };"
    )
    html = build_page(
        pref="light",
        probe=ESCAPE_PROBE,
        extra_css="*{animation:none!important;transition:none!important}",
        settings=SETTINGS_FIXTURE,
        init={"demo": False},
        api_js=api_js,
    )
    r = probe(html, 380, tmp_path, height=700)
    assert r["initial"] == {
        "sheetInert": True,
        "popInert": True,
        "sheetVis": "hidden",
        "popVis": "hidden",
        "shellInert": False,
    }
    assert (
        r["popOpen"]["open"]
        and not r["popOpen"]["inert"]
        and r["popOpen"]["focusInPop"]
    )
    assert r["popOpen"]["expanded"] == "true" and r["popOpen"]["items"] >= 2
    assert r["arrowMoved"] is True
    assert r["afterPopEsc"] == {"popOpen": False, "popInert": True, "focus": "scopeBtn"}
    assert (
        r["sheetOpen"]["open"]
        and not r["sheetOpen"]["inert"]
        and r["sheetOpen"]["shellInert"]
    )
    assert r["sheetOpen"]["focusInSheet"] and r["sheetOpen"]["syncBusy"] is True
    assert r["editing"] is True
    assert r["afterEditorEsc"] == {
        "editor": False,
        "sheetOpen": True,
        "focus": "cfg_openrouter",
    }
    assert r["pairOpen"] is True
    assert r["afterPairEsc"] == {"pair": False, "sheetOpen": True}
    assert r["afterSheetEsc"] == {
        "open": False,
        "inert": True,
        "shellInert": False,
        "focus": "settingsBtn",
        "vis": "hidden",
        "busy": False,
    }
    assert r["idleEsc"] == "settingsBtn"


SEMANTICS_PROBE = """
await until(() => document.querySelector('.provider-row'));
await openSettings('general'); await wait(60);
const out = {};
out.switches = [...document.querySelectorAll('.switch')].map(b => [b.id, b.getAttribute('role'), b.getAttribute('aria-checked'),
  !!document.getElementById(b.getAttribute('aria-labelledby'))]);
out.theme = [...document.querySelectorAll('#sheetBody [role=radio]')].map(b => [b.textContent.trim(), b.getAttribute('aria-checked')]);
closeSettings(); await wait(20);
const unnamed = [...document.querySelectorAll('button, [role=button], a[href]')].filter(el => {
  if (el.closest('[inert]')) return false;
  const name = (el.getAttribute('aria-label') || el.textContent || '').trim();
  return !name;
}).map(el => el.id || el.className);
out.unnamed = unnamed;
out.rowAttrs = [...document.querySelectorAll('.provider-row')].slice(0, 2).map(r => [r.getAttribute('aria-expanded'), r.getAttribute('aria-keyshortcuts')]);
out.tabs = [...document.querySelectorAll('.sheet-tab')].map(t => [t.getAttribute('role'), t.getAttribute('aria-selected')]);
out.dialog = [document.getElementById('settingsSheet').getAttribute('role'), document.getElementById('settingsSheet').getAttribute('aria-modal')];
out.focusRule = [...document.styleSheets[0].cssRules].some(r => r.selectorText && r.selectorText.includes(':focus-visible'));
report(out);
"""


@needs_chrome
@browser
def test_controls_expose_roles_names_and_focus_rings(tmp_path):
    html = build_page(pref="light", probe=SEMANTICS_PROBE, extra_css=FAST)
    r = probe(html, 380, tmp_path, height=700)
    assert [s[0] for s in r["switches"]] == [
        "sw_topmost",
        "sw_autofade",
        "sw_tray",
        "sw_alerts",
    ]
    assert all(
        s[1] == "switch" and s[2] in ("true", "false") and s[3] for s in r["switches"]
    )
    assert r["theme"] == [["System", "false"], ["Light", "true"], ["Dark", "false"]]
    assert r["unnamed"] == []
    assert r["rowAttrs"][0] == ["false", "Alt+ArrowUp Alt+ArrowDown"]
    assert [t[1] for t in r["tabs"]] == ["false", "false", "true"] and r["tabs"][0][
        0
    ] == "tab"
    assert r["dialog"] == ["dialog", "true"]
    assert r["focusRule"] is True


REORDER_PROBE = """
await until(() => document.querySelector('.provider-row'));
const keys = () => [...document.querySelectorAll('.provider-item')].map(e => e.dataset.key);
const before = keys();
const row = document.querySelector('.provider-row');
row.focus();
row.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', altKey: true, bubbles: true }));
await wait(100);
const after = keys();
const moved = document.activeElement.dataset.rowKey;
const live1 = document.getElementById('liveRegion').textContent;
document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowUp', altKey: true, bubbles: true }));
await wait(30);
report({ before, after, moved, restored: keys(), calls: window.__calls.filter(c => c[0] === 'save_provider_order'),
         live: live1, firstEdge: (() => {
  const first = document.querySelector('.provider-row');
  first.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowUp', altKey: true, bubbles: true }));
  return keys();
})() });
"""


@needs_chrome
@browser
def test_alt_arrows_reorder_providers_and_persist(tmp_path):
    html = build_page(pref="light", probe=REORDER_PROBE, extra_css=FAST)
    r = probe(html, 380, tmp_path, height=800)
    before, after = r["before"], r["after"]
    assert after[0] == before[1] and after[1] == before[0] and after[2:] == before[2:]
    assert r["moved"] == before[0]  # focus stays with the row that moved
    assert r["restored"] == before
    assert r["calls"] == [
        ["save_provider_order", after],
        ["save_provider_order", before],
    ]
    assert "moved to position 2" in r["live"]
    assert r["firstEdge"] == before


REDUCED_PROBE = """
await until(() => document.querySelector('.provider-row'));
const cs = (sel) => getComputedStyle(document.querySelector(sel));
const out = { reduced: matchMedia('(prefers-reduced-motion: reduce)').matches };
out.dot = cs('.status-dot').animationName;
out.halo = cs('#endDotHalo').animationName;
out.enter = cs('.provider-item').animationName;
out.sheetTransition = cs('#settingsSheet').transitionDuration;
out.drawerTransition = cs('.drawer-collapse').transitionDuration;
out.scroll = cs('#scrollBody').scrollBehavior;
document.getElementById('refreshBtn').classList.add('spinning');
out.spin = getComputedStyle(document.querySelector('#refreshBtn svg')).animationName;
// hero number snaps: no intermediate frames
state.displayedUsd = 0;
const seen = [];
animateHeroNumber(1234.5, true, false);
seen.push(document.getElementById('heroValue').textContent);
await wait(120);
seen.push(document.getElementById('heroValue').textContent);
out.hero = seen;
out.chartAnim = !!document.querySelector('.chart-line-anim');
renderChart(true);
out.chartAnimAfter = !!document.querySelector('.chart-line-anim');
report(out);
"""


@needs_chrome
@browser
def test_reduced_motion_removes_loops_springs_and_counting(tmp_path):
    html = build_page(pref="light", probe=REDUCED_PROBE)
    r = probe(
        html, 380, tmp_path, height=700, flags=("--force-prefers-reduced-motion",)
    )
    assert r["reduced"] is True
    assert (r["dot"], r["halo"], r["enter"], r["spin"]) == (
        "none",
        "none",
        "none",
        "none",
    )
    assert r["sheetTransition"] == "0s" and r["scroll"] == "auto"
    assert r["drawerTransition"] in ("0.08s", "0s")
    assert r["hero"] == ["$1,234.50", "$1,234.50"]
    assert r["chartAnimAfter"] is False


@needs_chrome
@browser
def test_motion_stays_when_not_reduced(tmp_path):
    html = build_page(pref="light", probe=REDUCED_PROBE)
    r = probe(html, 380, tmp_path, height=700)
    assert r["reduced"] is False
    assert r["dot"] != "none" and r["halo"] != "none"


HOST_PROBE = """
await until(() => document.querySelector('.provider-row'));
const out = {};
out.hostKeys = Object.keys(window.burnrateHost).sort();
burnrateHost.openSettings(null); await wait(60);
out.nullTab = [state.settingsTab, document.getElementById('settingsSheet').classList.contains('open')];
closeSettings();
burnrateHost.openSettings('bogus'); await wait(40);
out.bogusTab = state.settingsTab;
burnrateHost.openSettings('general'); await wait(40);
out.generalTab = state.settingsTab;
burnrateHost.notify('Quota is low'); await wait(10);
out.toast = [document.getElementById('toast').textContent, document.getElementById('toast').classList.contains('show')];
out.dotBefore = document.getElementById('settingsBtn').classList.contains('has-update');
burnrateHost.setUpdate({ ok: true, current: '0.5.4', available: true, version: '0.6.0', notes: '', pageUrl: '', canInstall: true,
                         progress: { phase: 'downloading', percent: 42 } });
await wait(30);
out.dotAfter = document.getElementById('settingsBtn').classList.contains('has-update');
out.gearLabel = document.getElementById('settingsBtn').getAttribute('aria-label');
out.progress = (document.querySelector('.update-progress') || {}).textContent || null;
out.install = !!document.getElementById('updInstallBtn');
burnrateHost.setUpdate({ ok: true, current: '0.5.4', available: true, version: '0.6.0', canInstall: true, progress: { phase: 'verifying', percent: null } });
await wait(30);
out.indeterminate = (document.querySelector('.update-progress') || {}).textContent || null;
burnrateHost.setUpdate({ ok: true, current: '0.5.4', available: true, version: '0.6.0', canInstall: false, pageUrl: 'https://example.invalid/d' });
await wait(30);
out.page = (document.getElementById('updPageLink') || {}).textContent || null;
out.hasProgress = !!document.querySelector('.update-progress');
const refreshed = window.__usageCalls || 0;
await burnrateHost.refresh();
out.refreshed = (window.__usageCalls || 0) - refreshed;
report(out);
"""


@needs_chrome
@browser
def test_host_entry_points(tmp_path):
    api_js = """
    window.__usageCalls = 0; var base = api.fetch_usage;
    api.fetch_usage = async function () { window.__usageCalls++; return base(); };
    """
    html = build_page(pref="light", probe=HOST_PROBE, api_js=api_js, extra_css=FAST)
    r = probe(html, 380, tmp_path, height=700)
    assert r["hostKeys"] == ["notify", "openSettings", "refresh", "setUpdate"]
    assert (
        r["nullTab"] == ["sources", True]
        and r["bogusTab"] == "sources"
        and r["generalTab"] == "general"
    )
    assert r["toast"] == ["Quota is low", True]
    assert r["dotBefore"] is False and r["dotAfter"] is True
    assert "update available" in r["gearLabel"]
    assert r["progress"].startswith("Downloading 42%") and r["install"] is True
    assert r["indeterminate"] == "Verifying\u2026"
    assert r["page"] == "Open download page" and r["hasProgress"] is False
    assert r["refreshed"] == 1


GENERAL_PROBE = """
await until(() => document.querySelector('.provider-row'));
await openSettings('general'); await wait(60);
const out = {};
const text = () => document.getElementById('sheetBody').textContent.replace(/\\s+/g, ' ');
out.initial = { tray: !!document.getElementById('sw_tray'), alerts: !!document.getElementById('sw_alerts'), quit: !!document.getElementById('quitBtn'),
                version: text().includes('Version 0.5.4'), note: text().includes('keeps BURNRATE in the tray'),
                alertsHelp: text().includes('Warn when a plan or balance is about to run out') };
document.getElementById('sw_tray').focus(); document.getElementById('sw_tray').click(); await wait(30);
out.trayAfter = [document.getElementById('sw_tray').getAttribute('aria-checked'), !text().includes('keeps BURNRATE in the tray')];
document.getElementById('sw_alerts').focus(); document.getElementById('sw_alerts').click(); await wait(30);
out.alertsAfter = document.getElementById('sw_alerts').getAttribute('aria-checked');
out.focusKept = document.activeElement.id;
document.getElementById('theme_dark').click(); await wait(30);
out.theme = [document.documentElement.dataset.theme, document.documentElement.dataset.themePref, document.getElementById('theme_dark').getAttribute('aria-checked')];
document.getElementById('theme_light').click(); await wait(30);
out.themeLight = document.documentElement.dataset.theme;
await checkForUpdates(); await wait(30);
out.update = { text: text().includes('Version 0.6.0 available'), install: !!document.getElementById('updInstallBtn'), dot: document.getElementById('settingsBtn').classList.contains('has-update') };
document.getElementById('updInstallBtn').click(); await wait(60);
out.restarting = text().includes('Restarting BURNRATE');
out.calls = window.__calls.map(c => c[0] + ':' + c.slice(1).join(','));
document.getElementById('quitBtn').click();
out.quit = window.__calls.some(c => c[0] === 'quit_app');
report(out);
"""


@needs_chrome
@browser
def test_general_tab_theme_tray_alerts_and_updates(tmp_path):
    html = build_page(
        pref="system", probe=GENERAL_PROBE, extra_css=FAST, init={"theme": "system"}
    )
    r = probe(html, 380, tmp_path, height=800)
    assert r["initial"] == {
        "tray": True,
        "alerts": True,
        "quit": True,
        "version": True,
        "note": True,
        "alertsHelp": True,
    }
    assert r["trayAfter"] == ["false", True]
    assert r["alertsAfter"] == "true"
    assert r["focusKept"] == "sw_alerts"
    assert r["theme"] == ["dark", "dark", "true"] and r["themeLight"] == "light"
    assert r["update"] == {"text": True, "install": True, "dot": True}
    assert r["restarting"] is True
    calls = r["calls"]
    assert "set_close_to_tray:false" in calls and "set_low_quota_alerts:true" in calls
    assert "set_theme:dark" in calls and "set_theme:light" in calls
    assert (
        "report_effective_theme:true" in calls
        and "report_effective_theme:false" in calls
    )
    assert r["quit"] is True


ABSENT_PROBE = """
await until(() => document.querySelector('.provider-row'));
await openSettings('general'); await wait(60);
report({ ids: [...document.querySelectorAll('.switch')].map(s => s.id),
         update: document.getElementById('sheetBody').textContent.replace(/\\s+/g, ' ').includes('This copy updates through pip or git'),
         check: !!document.getElementById('updCheckBtn'), quit: !!document.getElementById('quitBtn'),
         heroOk: document.getElementById('heroValue').textContent.startsWith('$') });
"""


@needs_chrome
@browser
def test_general_tab_degrades_when_host_methods_are_absent(tmp_path):
    api_js = """
    ['set_close_to_tray', 'set_low_quota_alerts', 'check_for_update', 'install_update', 'quit_app', 'set_theme',
     'report_effective_theme'].forEach(function (m) { delete api[m]; });
    """
    init = {
        "trayAvailable": False,
        "update": {"kind": "source", "current": "0.5.4", "canAutoUpdate": False},
    }
    html = build_page(
        pref="light", probe=ABSENT_PROBE, api_js=api_js, init=init, extra_css=FAST
    )
    r = probe(html, 380, tmp_path, height=700)
    assert r["ids"] == ["sw_topmost", "sw_autofade"]
    assert (
        r["update"] is True
        and r["check"] is False
        and r["quit"] is False
        and r["heroOk"] is True
    )


@needs_chrome
@browser
def test_system_theme_follows_the_os_preference_live(tmp_path):
    # Headless Chrome cannot flip prefers-color-scheme from a flag, so stand in for the OS query.
    api_js = """
    window.__mq = { matches: true, listeners: [], addEventListener: function (t, f) { this.listeners.push(f); } };
    var realMatchMedia = window.matchMedia.bind(window);
    window.matchMedia = function (q) { return /prefers-color-scheme/.test(q) ? window.__mq : realMatchMedia(q); };
    """
    probe_js = """
    await until(() => document.querySelector('.provider-row'));
    const out = { first: document.documentElement.dataset.theme, pref: document.documentElement.dataset.themePref };
    window.__mq.matches = false;
    window.__mq.listeners.forEach(f => f({ matches: false }));
    await wait(30);
    out.second = document.documentElement.dataset.theme;
    out.reported = window.__calls.filter(c => c[0] === 'report_effective_theme').map(c => c[1]);
    selectTheme('dark');                       // an explicit choice stops following the OS
    window.__mq.matches = true;
    window.__mq.listeners.forEach(f => f({ matches: true }));
    selectTheme('light');
    window.__mq.listeners.forEach(f => f({ matches: true }));
    out.pinned = document.documentElement.dataset.theme;
    report(out);
    """
    r = probe(
        build_page(pref="system", probe=probe_js, api_js=api_js),
        380,
        tmp_path,
        height=700,
    )
    assert (r["first"], r["pref"], r["second"]) == ("dark", "system", "light")
    assert r["reported"][:2] == [True, False]
    assert r["pinned"] == "light"


SMOKE_PROBE = """
await until(() => document.querySelectorAll('#providerList .provider-item').length >= 8);
await document.fonts.ready;
report({ items: document.querySelectorAll('#providerList .provider-item').length, hero: document.getElementById('heroValue').textContent,
         font: document.fonts.check('700 30px "Plus Jakarta Sans"') });
"""


@needs_chrome
@browser
@pytest.mark.parametrize("pref", ["light", "dark"])
def test_host_smoke_contract_holds(tmp_path, pref):
    r = probe(build_page(pref=pref, probe=SMOKE_PROBE), 380, tmp_path, height=800)
    assert r["items"] >= 8 and r["hero"].startswith("$") and r["font"] is True


BADGE_PROBE = """
await until(() => document.querySelector('.provider-row'));
const out = {};
const bg = (el) => backdrop(el.parentElement);
const rows = [...document.querySelectorAll('.brand-chip')];
out.count = rows.length;
out.low = rows.map(el => {
  const chip = parseColor(getComputedStyle(el).backgroundColor);
  const under = bg(el);
  const glyph = parseColor(getComputedStyle(el).color);
  return { key: el.closest('.provider-item').dataset.key, vsBg: contrast(chip.rgb, under.rgb), glyph: contrast(glyph.rgb, chip.rgb),
           lum: lum(chip.rgb) };
});
report(out);
"""


@needs_chrome
@browser
def test_dark_badges_neither_vanish_nor_glare(tmp_path):
    r = probe(
        build_page(pref="dark", probe=BADGE_PROBE, extra_css=FAST),
        380,
        tmp_path,
        height=800,
    )
    assert r["count"] >= 8
    for badge in r["low"]:
        assert badge["glyph"] >= 3.0, (
            badge
        )  # logo glyph stays legible on its chip (graphic contrast)
        assert badge["lum"] < 0.45, badge  # no white-hot chips on a dark canvas


EMPTY_PROBE = """
await until(() => document.querySelector('.ob-group'));
const svg = document.getElementById('chartSvg');
const line = svg.querySelector('.ch-flat');
report({
  tokens: document.getElementById('heroTokens').textContent, context: document.getElementById('heroTokensContext').textContent,
  footerHidden: document.getElementById('footerStatus').hidden,
  dots: svg.querySelectorAll('#endDot, #endDotHalo, .halo-pulse').length,
  flatY: line ? [line.getAttribute('y1'), line.getAttribute('y2')] : null,
  paths: svg.querySelectorAll('path').length,
  hero: document.getElementById('heroValue').textContent,
});
"""


@needs_chrome
@browser
def test_first_run_hero_is_honest_when_nothing_is_recorded(tmp_path):
    r = probe(
        build_page(
            pref="dark", report=empty_report(), probe=EMPTY_PROBE, extra_css=FAST
        ),
        380,
        tmp_path,
        height=800,
    )
    assert r["tokens"] == "No usage recorded yet" and r["context"] == ""
    assert r["footerHidden"] is True
    assert r["dots"] == 0 and r["paths"] == 0 and r["flatY"][0] == r["flatY"][1]
    assert r["hero"] == "$0.00"


FORMS_PROBE = """
await until(() => document.querySelector('.provider-row'));
const out = {};
const bare = () => [...document.querySelectorAll('input[type=password]')].filter(i => !i.closest('form')).map(i => i.id);
const attrs = () => [...document.querySelectorAll('input[type=password]')].map(i => i.getAttribute('autocomplete'));
await openSettings('sources'); await wait(60);
toggleSourceEdit('openrouter'); await wait(30);
out.source = { bare: bare(), attrs: attrs() };
let saved = 0; window.saveSource = async () => { saved++; };
document.getElementById('srcInput_openrouter').closest('form').requestSubmit(); await wait(10);
out.sourceSubmit = saved;
toggleSourceEdit('openrouter');
setSettingsTab('computers'); await wait(30);
out.hub = { bare: bare(), attrs: attrs(), form: !!document.getElementById('hubAccountPass').closest('form') };
let hubSubmits = 0; window.submitHubAccount = async () => { hubSubmits++; };
document.getElementById('hubAccountPass').closest('form').requestSubmit(); await wait(10);
out.hubSubmit = hubSubmits;
report(out);
"""

ONB_FORM_PROBE = """
await until(() => document.querySelector('.ob-group'));
document.querySelector('[data-ob-toggle=openrouter]').click(); await wait(20);
const input = document.getElementById('obKey_openrouter');
input.value = 'sk-enter';
input.closest('form').requestSubmit();
await until(() => window.__calls.some(c => c[0] === 'connect_source'));
report({ inForm: !!input.closest('form'), autocomplete: input.getAttribute('autocomplete'),
         calls: window.__calls.filter(c => c[0] === 'connect_source').length });
"""


@needs_chrome
@browser
def test_secret_fields_are_in_forms_and_never_offered_for_saving(tmp_path):
    settings = {
        **SETTINGS_FIXTURE,
        "setup": {},
        "connections": [SETTINGS_FIXTURE["connections"][0]],
    }
    r = probe(
        build_page(
            pref="light",
            probe=FORMS_PROBE,
            extra_css=FAST,
            settings=settings,
            init={"demo": False},
        ),
        380,
        tmp_path,
        height=800,
    )
    assert r["source"] == {"bare": [], "attrs": ["new-password"]}
    assert r["sourceSubmit"] == 1
    assert (
        r["hub"]["bare"] == []
        and r["hub"]["form"] is True
        and r["hub"]["attrs"] == ["new-password"]
    )
    assert r["hubSubmit"] == 1
    o = probe(
        build_page(
            pref="light", report=empty_report(), probe=ONB_FORM_PROBE, extra_css=FAST
        ),
        380,
        tmp_path,
        height=800,
        name="onbform",
    )
    assert (
        o["inForm"] is True and o["autocomplete"] == "new-password" and o["calls"] == 1
    )
