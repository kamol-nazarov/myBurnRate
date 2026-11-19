"""Option 4B corner pill fit and hover-card layout checks in real Chrome."""

import json

import pytest

from spend_app.tab_window import TabModel
from tests_spend.browser_runtime import CHROME
from tests_spend.test_tab_page import run_page
from tests_spend.test_tab_review import (
    _error_only,
    _healthy,
    demo_report_and_providers,
    error_model,
    many_urgent,
    out_cards,
    providers,
)
from tests_spend.test_tab_window import NOW

pytestmark = pytest.mark.browser

BUDGETS = [240, 250, 260, 280, 320, 400, 560, 900, 1200]


@pytest.fixture(autouse=True)
def need_chrome():
    if not CHROME.exists():
        pytest.skip("Google Chrome is needed for the tab page checks")


def datasets():
    report, items = demo_report_and_providers()
    return {
        "demo+error": error_model(items, spend_report=report),
        "demo+error without code": error_model(items, code=None, spend_report=report),
        "many urgent+error": error_model(providers(*many_urgent(9))),
        "demo": _healthy(items, spend_report=report),
        "empty": _healthy([]),
        "loading": TabModel(),
        "error first": _error_only(),
    }


def test_the_rendered_pill_never_overflows_its_window_at_any_budget(tmp_path):
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
    # Compact fallback rungs were really exercised
    assert any(r["mode"]["chip"] == "code" for r in rows)
    assert any(r["mode"]["chip"] == "icon" for r in rows)
    assert any(r["mode"]["showSpend"] is False and r["name"] == "demo+error" for r in rows)


def test_the_short_chip_keeps_its_full_text_in_the_tooltip_and_aria_label(tmp_path):
    report, items = demo_report_and_providers()
    payload = error_model(items, spend_report=report).payload(dark=False, now=NOW, budget=260)
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
    report, items = demo_report_and_providers()
    payload = _healthy(items, spend_report=report).payload(dark=False, now=NOW, budget=260)
    out = run_page(tmp_path, f"""
      document.body.style.width = Math.max({payload["layout"]["width"]}, {payload["layout"]["cardWidth"]}) + 'px';
      window.burnrateTab.update({json.dumps(payload)});
      window.burnrateTab.show();
      await tick();
      var card = document.getElementById('card').getBoundingClientRect();
      report.cardRight = card.right; report.cardLeft = card.left;
      """)
    assert "error" not in out, out.get("error")
    assert out["cardLeft"] >= 0 and out["cardRight"] <= max(payload["layout"]["width"], payload["layout"]["cardWidth"]) + 0.5
    assert payload["layout"]["cardWidth"] <= 260


def test_the_more_pill_turns_red_only_when_another_out_or_soon_provider_is_hidden(tmp_path):
    report, items = demo_report_and_providers()
    calm_more = _healthy(items, spend_report=report).payload(dark=False, now=NOW)
    red_model = TabModel()
    red_model.publish_report({"providers": providers(*out_cards(5))})
    red_more = red_model.payload(dark=False, now=NOW)
    out = run_page(tmp_path, f"""
      function readMore() {{
        var el = document.getElementById('more');
        var chip = el.querySelector('.more-chip');
        return {{ text: chip.textContent, urgent: el.classList.contains('urgent'),
                  bg: getComputedStyle(chip).backgroundColor }};
      }}
      window.burnrateTab.update({json.dumps(calm_more)});
      report.calm = readMore();
      window.burnrateTab.update({json.dumps(red_more)});
      report.red = readMore();
      """)
    assert "error" not in out, out.get("error")
    assert out["calm"]["urgent"] is False and out["calm"]["text"] == "+9"
    assert out["red"]["urgent"] is True and out["red"]["text"] == "+4"
    assert out["calm"]["bg"] != out["red"]["bg"]
