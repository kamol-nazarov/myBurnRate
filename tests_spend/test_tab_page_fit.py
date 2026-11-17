"""Review round 2: the compact strip in a real browser, and focus when a provider folds."""

import json

import pytest

from spend_app.tab_window import TabModel
from tests_spend.browser_runtime import CHROME
from tests_spend.test_tab_page import demo_payload_json, run_page
from tests_spend.test_tab_review import demo_providers_list, error_model, many_urgent, providers
from tests_spend.test_tab_window import NOW

pytestmark = pytest.mark.browser

BUDGETS = [240, 250, 280, 320, 400, 480, 560, 700, 900, 1200]


@pytest.fixture(autouse=True)
def need_chrome():
    if not CHROME.exists():
        pytest.skip("Google Chrome is needed for the tab page checks")


def datasets():
    from tests_spend.test_tab_review import _error_only, _healthy

    return {
        "demo+error": error_model(demo_providers_list()),
        "demo+error without code": error_model(demo_providers_list(), code=None),
        "many urgent+error": error_model(providers(*many_urgent(9))),
        "demo": _healthy(demo_providers_list()),
        "empty": _healthy([]),
        "loading": TabModel(),
        "error first": _error_only(),
    }


def test_the_rendered_strip_never_overflows_its_window_at_any_budget(tmp_path):
    cases = []
    for name, model in datasets().items():
        for budget in BUDGETS:
            cases.append({"name": name, "budget": budget,
                          "payload": model.payload(dark=False, now=NOW, budget=budget)})
    out = run_page(tmp_path, f"""
      var cases = {json.dumps(cases)};
      report.rows = [];
      for (var i = 0; i < cases.length; i++) {{
        var c = cases[i];
        document.body.style.width = c.payload.layout.width + 'px';
        window.burnrateTab.update(c.payload);
        var tab = document.getElementById('tab');
        var over = [];
        tab.querySelectorAll('*').forEach(function (el) {{
          if (el.closest('[hidden]')) return;
          var r = el.getBoundingClientRect();
          if (r.width && r.right > c.payload.layout.width + 0.5) over.push(el.className || el.tagName);
        }});
        report.rows.push({{ name: c.name, budget: c.budget, width: c.payload.layout.width,
                           overflow: tab.scrollWidth - tab.clientWidth, outside: over.slice(0, 4),
                           mode: c.payload.layout.mode }});
      }}
      """)
    assert "error" not in out, out.get("error")
    rows = out["rows"]
    assert len(rows) == len(cases)
    bad = [r for r in rows if r["overflow"] > 0 or r["outside"]]
    assert not bad, bad[:6]
    assert all(r["width"] <= r["budget"] for r in rows)
    # the compact modes were really exercised
    assert any(r["mode"]["tight"] for r in rows) and any(r["mode"]["chip"] == "icon" for r in rows)
    assert any(r["mode"]["compact"] for r in rows) and any(r["mode"]["chip"] == "code" for r in rows)


def test_the_short_chip_keeps_its_full_text_in_the_tooltip_and_aria_label(tmp_path):
    payload = error_model(demo_providers_list()).payload(dark=False, now=NOW, budget=260)
    assert payload["layout"]["mode"]["chip"] in ("code", "icon")
    out = run_page(tmp_path, f"""
      window.burnrateTab.update({json.dumps(payload)});
      var chip = document.getElementById('chip');
      report.text = chip.textContent.trim();
      report.title = chip.title;
      report.aria = chip.getAttribute('aria-label');
      report.mode = document.getElementById('tab').dataset.chip;
      """)
    assert "error" not in out, out.get("error")
    assert out["text"] in ("", "NET-001")
    assert "NET-001" in out["aria"] and "Can't reach your hub" in out["title"]


def test_the_hover_card_fits_a_narrow_budget(tmp_path):
    payload = _healthy_demo(budget=260)
    out = run_page(tmp_path, f"""
      document.body.style.width = {payload["layout"]["width"]} + 'px';
      window.burnrateTab.update({json.dumps(payload)});
      var state = window.burnrateTab.state;
      window.burnrateTab.show(state.segments.length ? state.segments[0].key : '__fine__',
                              document.querySelector('.seg') || document.getElementById('fine'));
      await tick();
      var card = document.getElementById('card').getBoundingClientRect();
      report.cardRight = card.right; report.cardLeft = card.left;
      """)
    assert "error" not in out, out.get("error")
    # the window grows to hold the card, never past the budget
    assert out["cardLeft"] >= 0 and out["cardRight"] <= max(payload["layout"]["width"], payload["layout"]["cardWidth"]) + 0.5
    assert payload["layout"]["cardWidth"] <= 260


def _healthy_demo(budget):
    model = TabModel()
    model.publish_report({"providers": demo_providers_list()})
    return model.payload(dark=False, now=NOW, budget=budget)


# ---- R2-L01: focus goes to the pill that now contains the provider --------------

def test_focus_moves_to_the_fine_pill_when_the_focused_provider_folds(tmp_path):
    wide = json.loads(demo_payload_json())
    narrow = _healthy_demo(budget=520)
    keys_after = {s["key"] for s in narrow["segments"]}
    folded = next(s["key"] for s in wide["segments"]
                  if s["key"] not in keys_after and s["level"] in ("ok", "balance", "idle"))
    assert folded in {i["key"] for i in narrow["fine"]["items"]}
    out = run_page(tmp_path, f"""
      var wide = {json.dumps(wide)};
      window.burnrateTab.update(wide);
      var index = wide.segments.findIndex(function (s) {{ return s.key === {json.dumps(folded)}; }});
      document.querySelector('.seg[data-i="' + index + '"]').focus();
      await tick();
      window.burnrateTab.update({json.dumps(narrow)});
      await tick();
      report.active = describe(document.activeElement);
      report.cardHidden = document.getElementById('cardWrap').hidden;
      """)
    assert "error" not in out, out.get("error")
    assert out["active"]["id"] == "fine"
    assert out["cardHidden"] is True  # the folded provider's card is gone with it


def test_focus_moves_to_the_urgent_pill_when_an_urgent_provider_folds(tmp_path):
    model = TabModel()
    model.publish_report({"providers": providers(*many_urgent(9))})
    wide = model.payload(dark=False, now=NOW, budget=2400)
    narrow = model.payload(dark=False, now=NOW, budget=700)
    last_key = wide["segments"][-1]["key"]
    assert last_key in {i["key"] for i in narrow["urgentMore"]["items"]}
    out = run_page(tmp_path, f"""
      var wide = {json.dumps(wide)};
      window.burnrateTab.update(wide);
      document.querySelector('.seg[data-i="' + (wide.segments.length - 1) + '"]').focus();
      await tick();
      window.burnrateTab.update({json.dumps(narrow)});
      await tick();
      report.active = describe(document.activeElement);
      """)
    assert "error" not in out, out.get("error")
    assert out["active"]["id"] == "urgentPill"


def test_a_provider_that_vanishes_entirely_leaves_focus_on_the_page_not_a_stale_pill(tmp_path):
    wide = json.loads(demo_payload_json())
    gone = json.loads(demo_payload_json())
    gone["segments"] = [s for s in gone["segments"] if s["key"] != "claude-code"]
    gone["fine"] = None
    out = run_page(tmp_path, f"""
      window.burnrateTab.update({json.dumps(wide)});
      document.querySelector('.seg[data-i="0"]').focus();
      await tick();
      window.burnrateTab.update({json.dumps(gone)});
      await tick();
      report.active = describe(document.activeElement);
      """)
    assert "error" not in out, out.get("error")
    assert out["active"]["id"] not in ("fine", "urgentPill")


def test_the_urgent_pill_is_amber_for_warn_only_and_red_with_an_out_inside(tmp_path):
    from tests_spend.test_tab_review import out_cards

    warn_model = TabModel()
    warn_model.publish_report({"providers": providers(*many_urgent(9))})
    red_model = TabModel()
    red_model.publish_report({"providers": providers(*out_cards(8))})
    warn = warn_model.payload(dark=False, now=NOW, budget=700)
    red = red_model.payload(dark=False, now=NOW, budget=420)
    assert warn["urgentMore"]["severity"] == "warn" and red["urgentMore"]["severity"] == "soon"
    out = run_page(tmp_path, f"""
      function pill() {{
        var el = document.getElementById('urgentPill');
        var span = el.querySelector('span');
        return {{ text: span.textContent, warn: el.classList.contains('warn'),
                  bg: getComputedStyle(span).backgroundColor, aria: el.getAttribute('aria-label') }};
      }}
      window.burnrateTab.update({json.dumps(warn)});
      report.amber = pill();
      window.burnrateTab.update({json.dumps(red)});
      report.red = pill();
      """)
    assert "error" not in out, out.get("error")
    assert out["amber"]["warn"] is True and out["amber"]["text"].endswith("to watch")
    assert out["red"]["warn"] is False and out["red"]["text"].endswith("urgent")
    assert out["amber"]["bg"] != out["red"]["bg"]
    assert "to watch" in out["amber"]["aria"] and "about to run out" in out["red"]["aria"]
