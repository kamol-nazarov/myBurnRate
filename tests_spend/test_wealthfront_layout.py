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
  const result = {fontLoaded:doc.fonts.check('600 11px "Plus Jakarta Sans"'), cases,
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
