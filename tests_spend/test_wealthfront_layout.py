"""Native quota cards with the embedded font at compact window widths."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from spend_app.native_desktop import DemoClient
from spend_app.wealthfront_window import load_ui_html, merge_provider_entries
from tests_spend.browser_runtime import CHROME
from tests_spend.test_frontend_viewports import _chrome_dump, _probe
from tests_spend.test_wealthfront_ui import build_page, probe

pytestmark = pytest.mark.browser


def _quota_fixture() -> str:
    now = datetime.now(UTC)
    definitions = [
        ("gemini-5h", "Antigravity Gemini models · Five-hour remaining", 7, 26_300_000),
        ("gemini-weekly", "Antigravity Gemini models · Weekly remaining", 56, 565_400_000),
        ("3p-5h", "Antigravity Claude/GPT models · Five-hour remaining", 0, 0),
        ("3p-weekly", "Antigravity Claude/GPT models · Weekly remaining", 45, 46_000_000),
    ]
    rows = [
        {"limitKey": key, "label": label, "pct": used, "unit": "pct",
         "resetsAt": (now + timedelta(hours=3, minutes=34)).isoformat()}
        for key, label, used, _ in definitions
    ]
    report = DemoClient().snapshot("today", "all")
    report["providers"] = merge_provider_entries(
        {"tools": []},
        {"capacity": [{"providerKey": "antigravity", "title": "Antigravity", "rows": rows}]},
        {"windowTokens": {"antigravity": {
            key: {"tokens": tokens} for key, _, _, tokens in definitions
        }}},
    )
    setup = """
<style>.drawer-collapse,.drawer-box{transition:none!important;transform:none!important}</style>
<script>
state.firstRender = false;
state.expandedTools.add('antigravity');
renderReport(REPORT, false);
</script>
""".replace("REPORT", json.dumps(report))
    return load_ui_html().replace("</body>", setup + "</body>")


def test_native_quota_rows_fit_with_the_embedded_font(tmp_path):
    if not CHROME.exists():
        pytest.skip("Google Chrome is needed for native quota layout evidence")
    ui = _quota_fixture().encode("utf-8")
    wrapper = b"""<!doctype html><html><body>
<iframe id="native" src="/ui" style="border:0;width:364px;height:720px"></iframe>
<script>
document.querySelector('#native').addEventListener('load', async () => {
  const frame = document.querySelector('#native');
  const doc = frame.contentDocument;
  await doc.fonts.ready;
  const cases = [];
  for (const width of [304, 320, 336, 364, 380, 480]) {
    frame.style.width = width + 'px';
    const box = doc.querySelector('.drawer-box');
    const bounds = box.getBoundingClientRect();
    const clipped = [...box.querySelectorAll('.limit-head,.limit-title,.limit-val,.limit-meta,.limit-reset,.limit-used')]
      .filter(el => {
        const rect = el.getBoundingClientRect();
        return el.scrollWidth > el.clientWidth + 1 || rect.right > bounds.right + 1 || rect.left < bounds.left - 1;
      }).map(el => el.className);
    const overlap = [...box.querySelectorAll('.limit-head')].some(el =>
      el.querySelector('.limit-title').getBoundingClientRect().right >
      el.querySelector('.limit-val').getBoundingClientRect().left + 1);
    cases.push({width, actualWidth:frame.contentWindow.innerWidth, clipped, overlap,
      drawerFits:bounds.right <= frame.contentWindow.innerWidth});
  }
  const result = {fontLoaded:doc.fonts.check('600 11px "Manrope"'), cases,
    titles:[...doc.querySelectorAll('.limit-title')].map(el => el.textContent.trim()),
    values:[...doc.querySelectorAll('.limit-val')].map(el => el.textContent.trim())};
  document.title = 'PROBE:' + JSON.stringify(result);
});
</script></body></html>"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            body = ui if self.path == "/ui" else wrapper
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        html = _chrome_dump(f"http://127.0.0.1:{server.server_port}/", 800, tmp_path)
        report = _probe(html)
        assert report["fontLoaded"] is True
        assert report["titles"] == [
            "Gemini · Five-hour", "Gemini · Weekly",
            "Claude / GPT · Five-hour", "Claude / GPT · Weekly",
        ]
        assert report["values"] == ["93% left", "44% left", "100% left", "55% left"]
        for case in report["cases"]:
            assert case["actualWidth"] == case["width"], case
            assert case["clipped"] == [], case
            assert case["overlap"] is False, case
            assert case["drawerFits"] is True, case
    finally:
        server.shutdown()
        server.server_close()


FIT_PROBE = """
await until(() => document.querySelector('.provider-row'));
document.querySelector('.provider-row').click();            // keep one drawer open while measuring
await wait(40);
const out = [];
const clipped = (el) => el.scrollWidth > el.clientWidth + 1;
for (const width of [304, 320, 336, 364, 380, 480]) {
  await setWidth(width);
  const issues = [];
  const scroller = document.getElementById('scrollBody');
  if (scroller.scrollWidth > scroller.clientWidth + 1) issues.push('page-hscroll');
  if (document.documentElement.scrollWidth > window.innerWidth) issues.push('document-hscroll');
  const card = document.getElementById('providersCard').getBoundingClientRect();

  // provider tiles
  document.querySelectorAll('.provider-row').forEach((row) => {
    const key = row.dataset.rowKey;
    const rect = row.getBoundingClientRect();
    const ident = row.querySelector('.tile-ident').getBoundingClientRect();
    const afford = row.querySelector('.tile-affordance');
    if (afford && parseFloat(getComputedStyle(afford).opacity) > 0) {
      const ar = afford.getBoundingClientRect();
      if (ident.right > ar.left + 1) issues.push('row-overlap:' + key);
      if (ar.right > rect.right + 1) issues.push('afford-overflow:' + key);
    }
    if (rect.right > card.right + 1) issues.push('row-overflow:' + key);
    for (const sel of ['.provider-rem', '.provider-rem-sub', '.provider-name', '.provider-sub']) {
      const el = row.querySelector(sel);
      if (el && clipped(el)) issues.push('clipped:' + sel + ':' + key);
    }
    row.querySelectorAll('.tile-name-wrap > *').forEach((el) => {
      if (el.getBoundingClientRect().right > ident.right + 1) issues.push('name-line:' + key);
    });
    const nameBox = row.querySelector('.provider-name').getBoundingClientRect();
    const tagBox = row.querySelector('.acct-tag');
    if (tagBox) {
      const t = tagBox.getBoundingClientRect();
      const sameLine = t.top < nameBox.bottom - 2 && t.bottom > nameBox.top + 2;
      if (sameLine && t.left < nameBox.right - 1) issues.push('name-tag-overlap:' + key);
    }
  });

  // hero
  const hero = document.querySelector('.hero-inner').getBoundingClientRect();
  const heroMain = document.querySelector('.hero-main').getBoundingClientRect();
  const miniChart = document.querySelector('.hero-mini-chart').getBoundingClientRect();
  const splitSameRow = miniChart.top < heroMain.bottom - 2 && miniChart.bottom > heroMain.top + 2;
  if (splitSameRow && heroMain.right > miniChart.left + 1) issues.push('hero-split-overlap');
  if (miniChart.right > hero.right + 1) issues.push('hero-chart-overflow');
  for (const sel of ['.balance-value', '#heroTokens', '#heroTokensContext', '#heroMeta', '#subtotalPill']) {
    const el = document.querySelector(sel);
    if (!el || el.offsetParent === null) continue;
    if (el.getBoundingClientRect().right > hero.right + 1) issues.push('hero-overflow:' + sel);
    if (clipped(el)) issues.push('hero-clipped:' + sel);
  }
  const period = [...document.querySelectorAll('.period-pill')];
  if (period.some(clipped)) issues.push('period-clipped');
  const brand = document.querySelector('.brand-group').getBoundingClientRect();
  const actions = document.querySelector('.top-actions').getBoundingClientRect();
  const sameRow = brand.top < actions.bottom && brand.bottom > actions.top;
  if ((sameRow && brand.right > actions.left + 1) || actions.right > window.innerWidth + 1) issues.push('topbar');

  // summary strip
  const cells = [...document.querySelectorAll('.summary-cell')];
  cells.forEach((cell, i) => {
    const r = cell.getBoundingClientRect();
    for (const el of cell.children) {
      if (clipped(el) || el.getBoundingClientRect().right > r.right + 1) issues.push('summary-clipped:' + i);
    }
    const next = cells[i + 1];
    if (next && r.right > next.getBoundingClientRect().left + 1) issues.push('summary-overlap:' + i);
  });
  const stripCard = document.querySelector('.summary-grid').getBoundingClientRect();
  if (cells[cells.length - 1].getBoundingClientRect().right > stripCard.right + 1) issues.push('summary-overflow');

  // the token tiles keep their labels on one line and stay the same height
  const tiles = [...document.querySelectorAll('.provider-item.open .burn-stat')];
  if (tiles.length < 2) issues.push('no-tiles');
  tiles.forEach((tile) => {
    const label = tile.querySelector('.burn-stat-lbl');
    if (label.getBoundingClientRect().height > 18 || clipped(label)) issues.push('tile-label-wraps');
  });
  if (new Set(tiles.map((t) => Math.round(t.getBoundingClientRect().height))).size > 1) issues.push('tiles-uneven');

  // the open drawer still fits inside its card
  const box = document.querySelector('.provider-item.open .drawer-box');
  if (box && box.getBoundingClientRect().right > card.right + 1) issues.push('drawer-overflow');

  out.push({ width, actual: window.innerWidth, issues });
}
report({ out, tokens: document.querySelector('#metricRate').textContent });
"""


@pytest.mark.parametrize("pref", ["light", "dark"])
def test_native_rows_hero_and_summary_fit_at_compact_widths(tmp_path, pref):
    if not CHROME.exists():
        pytest.skip("Google Chrome is needed for native layout evidence")
    html = build_page(pref=pref, probe=FIT_PROBE, extra_css="*{animation:none!important;transition:none!important}")
    report = probe(html, 380, tmp_path, height=800)
    for case in report["out"]:
        assert case["actual"] == case["width"], case
        assert case["issues"] == [], case
