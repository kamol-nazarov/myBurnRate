"""The Option 4B corner pill page in a real browser: keyboard focus, revisions, and WCAG text contrast."""

import json

import pytest

from spend_app import tab_window
from spend_app.tab_window import TabModel
from tests_spend.browser_runtime import CHROME, _chrome_dump, _probe
from tests_spend.test_tab_review import demo_report_and_providers, many_urgent, out_cards
from tests_spend.test_tab_window import NOW, card, providers

pytestmark = pytest.mark.browser

HELPERS = """
function tick(ms) { return new Promise(function (r) { setTimeout(r, ms || 30); }); }
function describe(el) {
  if (!el) return null;
  return {
    tag: el.tagName,
    cls: String(el.className || ''),
    open: !!(el.hasAttribute && el.hasAttribute('data-open')),
    refresh: !!(el.hasAttribute && el.hasAttribute('data-refresh')),
    rest: !!(el.hasAttribute && el.hasAttribute('data-toggle-rest')),
    id: el.id || null
  };
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
    dump = _chrome_dump(page.resolve().as_uri(), 600, tmp_path, 6000)
    return _probe(dump)


def demo_payload_json(*, dark=False):
    report, items = demo_report_and_providers()
    report = dict(report, providers=items)
    model = TabModel()
    model.publish_report(report, today_report=report)
    return json.dumps(model.payload(dark=dark, now=NOW))


@pytest.fixture(autouse=True)
def need_chrome():
    if not CHROME.exists():
        pytest.skip("Google Chrome is needed for the tab page checks")


def test_a_refresh_keeps_keyboard_focus_on_the_pill_refresh_and_card_actions(tmp_path):
    out = run_page(tmp_path, f"""
      var p = {demo_payload_json()};
      window.burnrateTab.update(p);
      var pill = document.getElementById('pillMain');
      pill.focus();
      await tick();
      report.focusedPill = describe(document.activeElement);
      report.cardOpenBefore = !document.getElementById('cardWrap').hidden;

      var before = document.activeElement;
      window.burnrateTab.update(JSON.parse(JSON.stringify(p)));      // unchanged poll touches no DOM
      report.sameElementWhenUnchanged = document.activeElement === before;

      var changed = JSON.parse(JSON.stringify(p));                    // real refresh
      changed.primary.pillBig = '51%';
      changed.spend.exact = '$129.10';
      window.burnrateTab.update(changed);
      await tick();
      report.afterPillRefresh = describe(document.activeElement);
      report.cardOpenAfter = !document.getElementById('cardWrap').hidden;
      report.headSpend = document.querySelector('#card .ch-val').textContent;

      var openBtn = document.querySelector('#card [data-open]');
      openBtn.focus();
      var changedAgain = JSON.parse(JSON.stringify(changed));
      changedAgain.overview.highlighted[0].cardStatus = '49% · dry ~50m';
      window.burnrateTab.update(changedAgain);
      await tick();
      report.afterCardRefresh = describe(document.activeElement);
      report.cardStillOpen = !document.getElementById('cardWrap').hidden;

      var restBtn = document.querySelector('#card [data-toggle-rest]');
      restBtn.click();
      await tick();
      report.restExpandedCount = document.querySelectorAll('#card .rest-row').length;

      var refBtn = document.getElementById('refresh');
      refBtn.focus();
      window.burnrateTab.update(changed);
      report.refreshButtonAfter = describe(document.activeElement);
      """)
    assert "error" not in out, out.get("error")
    assert out["focusedPill"]["id"] == "pillMain"
    assert out["cardOpenBefore"] is True
    assert out["sameElementWhenUnchanged"] is True
    assert out["afterPillRefresh"]["id"] == "pillMain"
    assert out["cardOpenAfter"] is True and out["headSpend"] == "$129.10"
    assert out["afterCardRefresh"]["open"] is True and out["cardStillOpen"] is True
    assert out["restExpandedCount"] == 7
    assert out["refreshButtonAfter"]["id"] == "refresh"


def test_loading_state_closes_any_open_card(tmp_path):
    out = run_page(tmp_path, f"""
      var p = {demo_payload_json()};
      window.burnrateTab.update(p);
      document.getElementById('pillMain').focus();
      await tick();
      var loading = {json.dumps(status_payload("loading", False))};
      window.burnrateTab.update(loading);
      await tick();
      report.cardHidden = document.getElementById('cardWrap').hidden;
      """)
    assert "error" not in out, out.get("error")
    assert out["cardHidden"] is True


def test_an_old_poll_cannot_overwrite_a_new_push_or_current_scope(tmp_path):
    out = run_page(tmp_path, f"""
      var old = {demo_payload_json()};
      old.revision = 1;
      var depleted = JSON.parse(JSON.stringify(old));
      depleted.revision = 2;
      depleted.primary.pillBig = depleted.primary.big = '0%';
      depleted.primary.level = 'out';
      depleted.urgentCount = 2;
      var scope = JSON.parse(JSON.stringify(old));
      scope.revision = 3;
      scope.primary.pillBig = scope.primary.big = '70%';
      scope.urgentCount = 0;
      var fresh = JSON.parse(JSON.stringify(scope));
      fresh.revision = 4;
      fresh.primary.pillBig = fresh.primary.big = '80%';

      var replies = [];
      window.pywebview = {{ api: {{ get_state: function () {{
        return new Promise(function (resolve) {{ replies.push(resolve); }});
      }} }} }};
      var poll = function () {{ window.dispatchEvent(new Event('pywebviewready')); }};
      var read = function () {{
        var badge = document.getElementById('badge');
        return [document.querySelector('#primary .pbig').textContent, badge.hidden ? null : badge.textContent];
      }};

      window.burnrateTab.update(old);
      report.onLaunch = read();
      poll();
      window.burnrateTab.update(depleted);
      replies.shift()(old);
      await tick();
      report.afterOldPoll = read();

      var duplicate = JSON.parse(JSON.stringify(depleted));
      duplicate.primary.pillBig = duplicate.primary.big = '54%';
      window.burnrateTab.update(duplicate);
      report.afterDuplicate = read();

      poll();
      window.burnrateTab.update(scope);
      replies.shift()(depleted);
      await tick();
      report.afterScopeChange = read();

      poll();
      replies.shift()(fresh);
      await tick();
      report.afterNewPoll = read();

      var headline = document.querySelector('#primary .pbig');
      var unchanged = JSON.parse(JSON.stringify(fresh));
      unchanged.revision = 5;
      window.burnrateTab.update(unchanged);
      report.unchangedHeadline = headline === document.querySelector('#primary .pbig');
      """)
    assert "error" not in out, out.get("error")
    assert out["onLaunch"] == ["54%", "1"]
    assert out["afterOldPoll"] == out["afterDuplicate"] == ["0%", "2"]
    assert out["afterScopeChange"] == ["70%", None]
    assert out["afterNewPoll"] == ["80%", None]
    assert out["unchangedHeadline"] is True


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


def audit_script(label, payload_json, *, hover=False, expand_rest=False, expand_picker=False):
    show = ""
    if hover:
        extra = ""
        if expand_rest:
            extra = """
      var restBtn = document.querySelector('#card [data-toggle-rest]');
      if (restBtn) { restBtn.click(); await tick(); }"""
        elif expand_picker:
            extra = """
      var pickBtn = document.querySelector('#card [data-toggle-picker]');
      if (pickBtn) { pickBtn.click(); await tick(); }"""
        show = f"""
      window.burnrateTab.show();
      await tick();{extra}"""
    else:
        show = """
      window.burnrateTab.hide();"""
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


def single_payload(dark):
    report, items = demo_report_and_providers()
    single = [p for p in items if p["key"] == "claude-code"][:1]
    model = TabModel()
    model.publish_report(dict(report, providers=single), today_report=report)
    return model.payload(dark=dark, now=NOW)


def stale_payload(dark):
    report, items = demo_report_and_providers()
    model = TabModel()
    model.publish_report(dict(report, providers=items), today_report=report)
    model.publish_error("Can't reach your hub. Check that this computer is on your network.", "NET-001")
    return model.payload(dark=dark, now=NOW)


def red_more_payload(dark):
    model = TabModel()
    model.publish_report({"providers": providers(*out_cards(5))})
    return model.payload(dark=dark, now=NOW)


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
        audit_script("demo hover", demo_payload_json(dark=dark), hover=True, expand_rest=True),
        audit_script("demo picker", demo_payload_json(dark=dark), hover=True, expand_picker=True),
        audit_script("single hover", json.dumps(single_payload(dark)), hover=True),
        audit_script("idle", json.dumps(idle_payload(dark))),
        audit_script("idle hover", json.dumps(idle_payload(dark)), hover=True),
        audit_script("stale", json.dumps(stale_payload(dark))),
        audit_script("stale hover", json.dumps(stale_payload(dark)), hover=True),
        audit_script("urgent more red", json.dumps(red_more_payload(dark))),
        audit_script("empty", json.dumps(status_payload("empty", dark))),
        audit_script("empty hover", json.dumps(status_payload("empty", dark)), hover=True),
        audit_script("loading", json.dumps(status_payload("loading", dark))),
        audit_script("error", json.dumps(status_payload("error", dark))),
        audit_script("error hover", json.dumps(status_payload("error", dark)), hover=True),
    ])
    out = run_page(tmp_path, script, theme=theme)
    assert "error" not in out, out.get("error")
    rows = out["rows"]
    assert len(rows) > 80
    assert {r["label"] for r in rows} >= {"demo", "demo hover", "single hover", "idle", "stale", "empty", "error"}
    failing = [r for r in rows if r["ratio"] < 4.5]
    assert not failing, failing[:8]


def test_hover_rest_and_pill_picker_lists_never_overflow_horizontally_or_squish_rows(tmp_path):
    out = run_page(tmp_path, f"""
      var picked = [];
      var p = {demo_payload_json(dark=True)};
      window.pywebview = {{ api: {{
        set_pill_limit: function (val) {{ picked.push(val); return Promise.resolve({{ ok: true }}); }},
        expand: function () {{ return Promise.resolve({{ ok: true, up: false }}); }},
        collapse: function () {{ return Promise.resolve({{ ok: true }}); }},
      }} }};
      window.burnrateTab.update(p);
      window.burnrateTab.show();
      await tick();

      document.querySelector('#card [data-toggle-rest]').click();
      await tick();
      var restList = document.querySelector('#card .ov-rest-list');
      var restRows = [...document.querySelectorAll('#card .rest-row')];
      report.restNoHorizScroll = restList.scrollWidth <= restList.clientWidth + 1;
      report.restMinRowHeight = Math.min.apply(null, restRows.map(function (r) {{ return r.getBoundingClientRect().height; }}));

      document.querySelector('#card [data-toggle-picker]').click();
      await tick();
      var pickList = document.querySelector('#card .pill-picker-list');
      var pickRows = [...document.querySelectorAll('#card .pick-row')];
      report.pickNoHorizScroll = pickList.scrollWidth <= pickList.clientWidth + 1;
      report.pickMinRowHeight = Math.min.apply(null, pickRows.map(function (r) {{ return r.getBoundingClientRect().height; }}));
      report.pickCount = pickRows.length;

      var target = pickRows.find(function (r) {{ return r.getAttribute('data-pick-limit'); }});
      target.click();
      await tick();
      report.picked = picked;
    """, theme="dark")
    assert "error" not in out, out.get("error")
    assert out["restNoHorizScroll"] is True
    assert out["restMinRowHeight"] >= 22
    assert out["pickNoHorizScroll"] is True
    assert out["pickMinRowHeight"] >= 22
    assert out["pickCount"] >= 5
    assert len(out["picked"]) == 1 and "::" in out["picked"][0]
