"""The tab page in a real browser: keyboard focus across refreshes and text contrast."""

import json

import pytest

from spend_app import tab_window
from spend_app.tab_window import TabModel
from tests_spend.browser_runtime import CHROME, _chrome_dump, _probe
from tests_spend.test_tab_review import demo_providers_list, many_urgent
from tests_spend.test_tab_window import NOW, card, dry, iso, providers

pytestmark = pytest.mark.browser

HELPERS = """
function tick(ms) { return new Promise(function (r) { setTimeout(r, ms || 30); }); }
function describe(el) {
  if (!el) return null;
  return { tag: el.tagName, cls: String(el.className || ''), i: el.dataset ? el.dataset.i || null : null,
           open: !!(el.hasAttribute && el.hasAttribute('data-open')), id: el.id || null };
}
"""


def run_page(tmp_path, script, *, theme="light"):
    html = tab_window.load_tab_html(theme)
    wrapper = f"""<script>{HELPERS}{CONTRAST}
window.addEventListener('load', async function () {{
  var report = {{}};
  try {{ {script}
  }} catch (e) {{ report.error = String(e && e.stack || e); }}
  var pre = document.createElement('pre'); pre.id = 'probe-report';
  pre.textContent = JSON.stringify(report); document.body.appendChild(pre);
}});
</script></body>"""
    page = tmp_path / "tab-page.html"
    page.write_text(html.replace("</body>", wrapper), encoding="utf-8")
    dump = _chrome_dump(page.resolve().as_uri(), 1300, tmp_path, 6000)
    return _probe(dump)


def demo_payload_json(*, dark=False):
    model = TabModel()
    model.publish_report({"providers": demo_providers_list()})
    return json.dumps(model.payload(dark=dark, now=NOW))


@pytest.fixture(autouse=True)
def need_chrome():
    if not CHROME.exists():
        pytest.skip("Google Chrome is needed for the tab page checks")


def test_a_refresh_keeps_keyboard_focus_on_the_same_segment_and_card_button(tmp_path):
    out = run_page(tmp_path, f"""
      var p = {demo_payload_json()};
      window.burnrateTab.update(p);
      var seg = document.querySelectorAll('.seg')[1];
      seg.focus();
      await tick();
      report.focusedSegment = describe(document.activeElement);
      report.cardOpenBefore = !document.getElementById('cardWrap').hidden;

      var before = document.activeElement;
      window.burnrateTab.update(JSON.parse(JSON.stringify(p)));      // the 20 s poll, nothing changed
      report.sameElementWhenUnchanged = document.activeElement === before;

      var changed = JSON.parse(JSON.stringify(p));                    // a real refresh
      changed.segments[3].big = '51%';
      changed.segments[1].status = 'dry in ~13h';
      window.burnrateTab.update(changed);
      await tick();
      report.afterSegmentRefresh = describe(document.activeElement);
      report.cardOpenAfter = !document.getElementById('cardWrap').hidden;
      report.openKeyStillSecond = document.querySelector('#card .ph b').textContent;

      var button = document.querySelector('#card [data-open]');
      button.focus();
      report.cardButtonFocused = describe(document.activeElement);
      var changedAgain = JSON.parse(JSON.stringify(changed));
      changedAgain.segments[1].card.rows[0].leftPct = 49;
      window.burnrateTab.update(changedAgain);
      await tick();
      report.afterCardRefresh = describe(document.activeElement);
      report.cardStillOpen = !document.getElementById('cardWrap').hidden;

      var open = document.getElementById('open'); open.focus();
      window.burnrateTab.update(changed);
      report.openButtonAfter = describe(document.activeElement);
      """)
    assert "error" not in out, out.get("error")
    assert out["focusedSegment"]["cls"].startswith("seg") and out["focusedSegment"]["i"] == "1"
    assert out["cardOpenBefore"] is True
    assert out["sameElementWhenUnchanged"] is True  # an unchanged poll does not touch the DOM
    assert out["afterSegmentRefresh"]["cls"].startswith("seg") and out["afterSegmentRefresh"]["i"] == "1"
    assert out["cardOpenAfter"] is True
    assert out["cardButtonFocused"]["open"] is True
    assert out["afterCardRefresh"]["open"] is True and out["cardStillOpen"] is True
    assert out["openButtonAfter"]["id"] == "open"


def test_focus_follows_a_provider_that_moves_to_another_slot(tmp_path):
    out = run_page(tmp_path, f"""
      var p = {demo_payload_json()};
      window.burnrateTab.update(p);
      document.querySelectorAll('.seg')[0].focus();
      await tick();
      var moved = JSON.parse(JSON.stringify(p));
      moved.segments.reverse();                                       // the focused provider is now last
      window.burnrateTab.update(moved);
      await tick();
      var active = document.activeElement;
      report.index = active.dataset.i;
      report.title = active.getAttribute('aria-label');
      report.last = String(moved.segments.length - 1);
      """)
    assert "error" not in out, out.get("error")
    assert out["index"] == out["last"] and out["title"].startswith("Claude Code")


def test_a_provider_that_disappears_does_not_leave_a_dead_card(tmp_path):
    out = run_page(tmp_path, f"""
      var p = {demo_payload_json()};
      window.burnrateTab.update(p);
      document.querySelectorAll('.seg')[0].focus();
      await tick();
      var gone = JSON.parse(JSON.stringify(p));
      gone.segments.shift();
      window.burnrateTab.update(gone);
      await tick();
      report.cardHidden = document.getElementById('cardWrap').hidden;
      report.active = describe(document.activeElement);
      """)
    assert "error" not in out, out.get("error")
    assert out["cardHidden"] is True


CONTRAST = """
function parse(c) {
  var m = String(c).match(/rgba?\\(([^)]+)\\)/); var v = m[1].split(',').map(parseFloat);
  return { r: v[0], g: v[1], b: v[2], a: v.length > 3 ? v[3] : 1 };
}
function over(fg, bg) { var a = fg.a; return { r: fg.r * a + bg.r * (1 - a), g: fg.g * a + bg.g * (1 - a), b: fg.b * a + bg.b * (1 - a), a: 1 }; }
function lum(c) {
  function f(v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }
  return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
}
function ratio(a, b) { var x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }
function backdrop(el) {
  var chain = [];
  for (var n = el; n; n = n.parentElement) {
    var bg = parse(getComputedStyle(n).backgroundColor);
    if (bg.a > 0) chain.push(bg);
    if (bg.a >= 1) break;
  }
  var acc = { r: 255, g: 255, b: 255, a: 1 };
  chain.reverse().forEach(function (c) { acc = over(c, acc); });
  return acc;
}
function opacity(el) { var o = 1; for (var n = el; n; n = n.parentElement) o *= parseFloat(getComputedStyle(n).opacity); return o; }
function ownText(el) {
  var t = ''; el.childNodes.forEach(function (c) { if (c.nodeType === 3) t += c.textContent; });
  return t.trim();
}
function audit(label) {
  var rows = [];
  document.querySelectorAll('#tab *, #cardWrap *').forEach(function (el) {
    var text = ownText(el);
    if (!text || el.closest('[hidden]') || el.classList.contains('sr')) return;
    var cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') return;
    var fg = parse(cs.color);
    fg.a *= opacity(el);                 // ancestor opacity fades the text too
    var bg = backdrop(el);
    rows.push({ label: label, text: text.slice(0, 40), size: cs.fontSize, ratio: ratio(over(fg, bg), bg) });
  });
  return rows;
}
"""


def audit_script(label, payload_json, *, hover=None):
    show = ""
    if hover:
        show = f"""
      var state = window.burnrateTab.state, seat;
      var idx = state.segments.findIndex(function (s) {{ return s.key === {json.dumps(hover)}; }});
      seat = document.querySelector('.seg[data-i="' + idx + '"]');
      window.burnrateTab.show({json.dumps(hover)}, seat);
      await tick();"""
    return f"""
      window.burnrateTab.update({payload_json});{show}
      report.rows = (report.rows || []).concat(audit({json.dumps(label)}));"""


def idle_payload(dark):
    cards = [
        {"providerKey": "claude-code", "title": "Claude Code", "plan": "Max",
         "rows": [{"limitKey": "5h", "label": "5h window — Waiting for a fresh reading", "unavailable": True}]},
        card("codex", 40.0, None, resets=100.0, title="Codex"),
        {"providerKey": "openrouter", "title": "OpenRouter", "isPayg": True, "rows": []},
    ]
    model = TabModel()
    model.publish_report({"providers": providers(*cards)})
    return model.payload(dark=dark, now=NOW)


def stale_payload(dark):
    model = TabModel()
    model.publish_report({"providers": demo_providers_list()})
    model.publish_error("Can't reach your hub. Check that this computer is on your network.", "NET-001")
    return model.payload(dark=dark, now=NOW)


def pill_payload(dark):
    model = TabModel()
    model.publish_report({"providers": providers(*many_urgent(9), card("grok", 10.0, None, resets=100.0))})
    return model.payload(dark=dark, now=NOW, budget=700)


def status_payload(state, dark):
    model = TabModel()
    if state == "empty":
        model.publish_report({"providers": []})
    elif state == "error":
        model.publish_error("BURNRATE's background service isn't running. Restart the app.", "APP-901")
    return model.payload(dark=dark, now=NOW)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_text_in_every_state_has_at_least_4_5_to_1_including_ancestor_opacity(tmp_path, theme):
    dark = theme == "dark"
    script = "".join([
        audit_script("demo", demo_payload_json(dark=dark)),
        audit_script("demo hover", demo_payload_json(dark=dark), hover="claude-code"),
        audit_script("idle", json.dumps(idle_payload(dark))),
        audit_script("idle hover", json.dumps(idle_payload(dark)), hover="claude-code"),
        audit_script("stale", json.dumps(stale_payload(dark))),
        audit_script("urgent pill", json.dumps(pill_payload(dark))),
        audit_script("empty", json.dumps(status_payload("empty", dark))),
        audit_script("loading", json.dumps(status_payload("loading", dark))),
        audit_script("error", json.dumps(status_payload("error", dark))),
    ])
    out = run_page(tmp_path, script, theme=theme)
    assert "error" not in out, out.get("error")
    rows = out["rows"]
    assert len(rows) > 60
    assert {r["label"] for r in rows} >= {"demo", "idle", "stale", "urgent pill", "empty", "error"}
    failing = [r for r in rows if r["ratio"] < 4.5]
    assert not failing, failing[:8]
    awaiting = [r for r in rows if r["text"] == "Awaiting reading" and r["label"] == "idle"]
    assert awaiting and min(r["ratio"] for r in awaiting) >= 4.5
