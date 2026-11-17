"""Review round 3: headlines never draw past their segment (compact money, long names)."""

import json

import pytest

from spend_app import tab_window
from spend_app.tab_window import TabModel
from tests_spend.browser_runtime import CHROME
from tests_spend.test_tab_page import run_page
from tests_spend.test_tab_page_fit import BUDGETS, datasets
from tests_spend.test_tab_review import providers
from tests_spend.test_tab_window import NOW, card


def balance_card(key, usd):
    return {"providerKey": key, "title": key.title(), "isPayg": True, "fundsRemainingUsd": usd, "rows": []}


# ---- the headline rules ----------------------------------------------------------------

@pytest.mark.parametrize(
    "amount, normal, compact",
    [
        (0, "$0.00", "$0.00"), (23.4, "$23.40", "$23.40"), (99.99, "$99.99", "$99.99"),
        (100, "$100.00", "$100"), (457.5, "$457.50", "$458"), (999.99, "$999.99", "$1,000"),
        (2000, "$2,000.00", "$2.0K"), (9999.99, "$9,999.99", "$10.0K"), (12345, "$12.3K", "$12.3K"),
        (999_949, "$999.9K", "$999.9K"), (999_950, "$1.0M", "$1.0M"), (1_234_567, "$1.2M", "$1.2M"),
    ],
)
def test_money_headlines_abbreviate_only_when_they_would_not_fit(amount, normal, compact):
    assert tab_window.abbreviate_money(amount) == normal
    assert tab_window.abbreviate_money(amount, compact=True) == compact


def test_compact_segments_abbreviate_but_keep_the_exact_amount_elsewhere():
    model = TabModel()
    model.publish_report({"providers": providers(balance_card("openrouter", 2000.0), balance_card("deepseek", 3000.0))})
    roomy = model.payload(dark=False, budget=2000)
    assert [s["big"] for s in roomy["segments"]] == ["$2,000.00", "$3,000.00"]  # exact while it fits
    compact = model.payload(dark=False, budget=260)
    assert compact["layout"]["mode"]["compact"] is True
    assert [s["big"] for s in compact["segments"]] == ["$2.0K"]  # the other balance is in the fine pill
    seg = compact["segments"][0]
    assert seg["exact"] == "$2,000.00"
    assert seg["exact"] in seg["aria"]  # the aria-label (and so the tooltip) keeps the exact amount
    assert seg["card"]["balance"]["amount"] == 2000.0  # and so does the hover card
    assert [i["text"] for i in compact["fine"]["items"]] == ["$3,000.00 balance"]  # pill lists use exact amounts


def test_long_idle_names_are_shortened_with_the_full_name_in_the_aria_label():
    items = providers({"providerKey": "zcode", "title": "Z.AI Coding Plan", "plan": "Max",
                       "rows": [{"limitKey": "weekly", "label": "Weekly — Waiting", "unavailable": True}]})
    model = TabModel()
    model.publish_report({"providers": items})
    seg = model.payload(dark=False, budget=2000)["segments"][0]
    assert seg["big"].endswith("…") and len(seg["big"]) <= tab_window.NAME_CHARS
    assert len(seg["bigCompact"]) <= tab_window.COMPACT_NAME_CHARS
    assert "Z.AI Coding Plan" in seg["aria"] and seg["exact"].startswith("Z.AI Coding Plan")


# ---- real Chrome: nothing overflows -------------------------------------------------------

pytestmark_browser = pytest.mark.browser


def money_datasets():
    rich = providers(
        balance_card("openrouter", 2000.0), balance_card("deepseek", 3000.0),
        balance_card("anthropic-api", 1234567.0), balance_card("zcode-api", 99999.99),
        balance_card("muse-api", 457.5), balance_card("grok-api", 0.0),
        card("codex", 5.0, None, resets=100.0, title="A Very Long Provider Name Indeed"),
    )
    idle = providers({"providerKey": "zai-coding-plan", "title": "Z.AI Coding Plan", "plan": "Max",
                      "rows": [{"limitKey": "weekly", "label": "Weekly — Waiting", "unavailable": True}]})
    out = {}
    for name, items in (("balances", rich), ("idle names", idle)):
        model = TabModel()
        model.publish_report({"providers": items})
        out[name] = model
    return out


@pytest.mark.browser
def test_headlines_and_segments_never_overflow_at_any_width_even_with_huge_text(tmp_path):
    if not CHROME.exists():
        pytest.skip("Google Chrome is needed for the tab page checks")
    cases = []
    models = money_datasets()
    models.update(datasets())
    for name, model in models.items():
        for budget in BUDGETS:
            payload = model.payload(dark=False, now=NOW, budget=budget)
            cases.append({"name": name, "budget": budget, "payload": payload})
            if payload["segments"]:  # the same strip with absurd headline text injected
                stress = json.loads(json.dumps(payload))
                for seg in stress["segments"]:
                    seg["big"] = seg["bigCompact"] = "131.4K cr · " + seg["big"] + " 1,234,567.89"
                cases.append({"name": name + " stress", "budget": budget, "payload": stress})
    out = run_page(tmp_path, f"""
      var cases = {json.dumps(cases)};
      report.rows = [];
      for (var i = 0; i < cases.length; i++) {{
        var c = cases[i];
        document.body.style.width = c.payload.layout.width + 'px';
        window.burnrateTab.update(c.payload);
        document.querySelectorAll('#segs .seg').forEach(function (seg) {{
          var b = seg.querySelector('.top b'), txt = seg.querySelector('.txt');
          var s = seg.getBoundingClientRect(), t = txt.getBoundingClientRect(), h = b.getBoundingClientRect();
          report.rows.push({{ name: c.name, budget: c.budget, text: b.textContent,
            segOver: seg.scrollWidth - seg.clientWidth,
            headOver: b.scrollWidth - b.clientWidth,
            pastTxt: h.right - t.right, pastSeg: h.right - s.right }});
        }});
      }}
      """)
    assert "error" not in out, out.get("error")
    rows = out["rows"]
    assert len(rows) > 150
    bad = [r for r in rows if r["segOver"] > 0 or r["headOver"] > 0 or r["pastTxt"] > 0.5 or r["pastSeg"] > 0]
    assert not bad, bad[:6]
    assert any(r["text"].endswith("…") for r in rows if "stress" in r["name"])  # long text was cut, not overflowing


@pytest.mark.browser
def test_the_reviewers_case_two_healthy_balances_at_240(tmp_path):
    if not CHROME.exists():
        pytest.skip("Google Chrome is needed for the tab page checks")
    model = TabModel()
    model.publish_report({"providers": providers(balance_card("openrouter", 2000.0), balance_card("deepseek", 3000.0))})
    payload = model.payload(dark=True, now=NOW, budget=240)
    out = run_page(tmp_path, f"""
      document.body.style.width = {payload["layout"]["width"]} + 'px';
      window.burnrateTab.update({json.dumps(payload)});
      var seg = document.querySelector('.seg');
      report.has = !!seg;
      if (seg) {{
        var f = document.getElementById('fine').getBoundingClientRect(),
            b = seg.querySelector('.top b').getBoundingClientRect();
        report.gapToFine = f.left - b.right; report.text = seg.querySelector('.top b').textContent;
        report.aria = seg.getAttribute('aria-label');
      }}
      """, theme="dark")
    assert "error" not in out, out.get("error")
    if out["has"]:
        assert out["gapToFine"] >= 0  # nothing drawn into the fine pill
        assert out["text"] in ("$2.0K", "$3.0K")
        assert "$2,000.00" in out["aria"] or "$3,000.00" in out["aria"]  # the exact amount stays in the aria-label
