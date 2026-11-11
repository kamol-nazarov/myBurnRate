"""Render the real HUD as a self-contained, interactive sample-data preview.

No server, provider requests, or application data are involved. Regenerate after
editing the HUD: ``python scripts/preview-hud.py``. The output can be opened as a
local HTML file or passed to T3's html_preview/html_render tools.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from spend_app.hud import render_hud


BOOT = r"""
(() => {
  // This document never reads or writes the application's saved preferences.
  const values = new Map([
    ['burnrate:hud-mini', '1'], ['burnrate:hud-density', DENSITY],
    ['burnrate:hud-theme', THEME === 'host' ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : THEME], ['burnrate:hud-pins', '[]']
  ]);
  Object.defineProperty(window, 'localStorage', {configurable: true, value: {
    getItem: key => values.get(String(key)) ?? null,
    setItem: (key, value) => values.set(String(key), String(value)),
    removeItem: key => values.delete(String(key)), clear: () => values.clear(),
    key: index => [...values.keys()][index] ?? null, get length() {return values.size;}
  }});
  document.documentElement.classList.add('is-glass');
  if (THEME === 'host') document.documentElement.dataset.previewTheme = 'host';
  window.EventSource = undefined;
  const previewNotice = text => {
    const node = document.getElementById('hud-notice');
    if (node) {node.textContent = text; node.hidden = false;}
  };
  window.__TAURI_INTERNALS__ = {invoke: async (command, args = {}) => {
    if (command === 'pending_update') return null;
    if (command === 'set_hud_pin') return Boolean(args.pinned);
    if (command === 'hide_hud') previewNotice('Sample preview stays open. The desktop app would hide this window.');
    if (command === 'show_dashboard') previewNotice('The desktop app opens the dashboard here. This preview uses sample data.');
    if (command === 'open_setup_wizard') previewNotice('The desktop app opens source connections here. No account is connected in this preview.');
    return true;
  }};
  window.open = () => {previewNotice('This destination is available in the desktop app.'); return null;};
  window.close = () => {};
  const stamp = offset => new Date(Date.now() + offset * 1000).toISOString();
  const polled = stamp(-180);
  const reads = new Map();
  const row = (limitKey, label, pct, remainingHours, extra = {}) => ({
    limitKey, label, pct, unit: 'pct', resetsAt: stamp(remainingHours * 3600),
    polledAt: stamp(-8), ...extra
  });
  const plan = (providerKey, providerName, rows, extra = {}) => ({
    providerKey, providerName, rows, polledAt: stamp(-8), ...extra
  });
  const live = machine => {
    const count = reads.get(machine) || 0;
    reads.set(machine, count + 1);
    const factor = machine === 'laptop' ? .4 : machine === 'desktop' ? .75 : 1;
    const heldPct = count ? null : 45;
    return {
      generatedAt: stamp(0), lastIngestAgeSeconds: 8, status: 'live',
      activity: Array.from({length: machine === 'laptop' ? 1 : 3}, (_, index) => ({
        id: 'sample-' + index, state: 'LIVE', name: ['Northwind web', 'Contoso API', 'Sample tests'][index],
        modelKey: ['gpt-6-astra', 'claude-opus-5-5', 'gpt-6.1-sol'][index],
        machineName: machine === 'laptop' ? 'Laptop' : 'Desktop'
      })),
      capacity: [
        plan('opencode-go', 'OpenCode Go', [row('monthly','Monthly',80 * factor,13 * 24),
          row('weekly','Weekly',15 * factor,4 * 24),row('5h','5-hour',15 * factor,3)], {plan:'Go'}),
        plan('codex','Codex',[row('weekly','Weekly',43 * factor,4 * 24)], {plan:'Pro'}),
        plan('claude-code','Claude Code',[row('weekly','Weekly',28 * factor,39),row('5h','5-hour',9 * factor,3)], {plan:'Max'}),
        plan('claude-code-work','Work',[row('weekly','Weekly',6 * factor,3 * 24)], {plan:'Pro'}),
        plan('opencode','Z.AI',[row('weekly','Weekly',3 * factor,6 * 24)], {plan:'Coding plan'}),
        plan('opencode-go-2','Profile 2',[row('monthly','Monthly',7 * factor,15 * 24)], {plan:'Go'}),
        plan('muse','Muse',[row('5h','5-hour',0,3),row('weekly','Weekly',0,6 * 24)], {plan:'Power'}),
        plan('grok','Grok',[row('weekly','Weekly',null,0,{resetsAt:null})]),
        plan('antigravity','Antigravity',[
          row('gemini-5h','Gemini 5-hour',0,3),row('gemini-weekly','Gemini weekly',24 * factor,4 * 24),
          row('other-5h','Claude / Other 5-hour',0,3),
          row('other-weekly','Claude / Other weekly',heldPct == null ? null : heldPct * factor,4 * 24,{polledAt:polled,held:true})
        ], {plan:'Ultra'}),
        plan('openrouter','OpenRouter',[{limitKey:'balance',label:'Balance',unit:'usd',isPayg:true,
          used:16,pct:null,polledAt:stamp(-40),runway:{days:8,dailyUsd:2,basisDays:7,state:'steady',line:'About 8 days at recent usage'}}],{isPayg:true}),
        plan('deepseek','DeepSeek',[{limitKey:'balance',label:'Balance',unit:'usd',isPayg:true,
          used:55,pct:null,polledAt:stamp(-40),runway:{days:92,dailyUsd:.60,basisDays:7,state:'steady',line:'About 3 months at recent usage'}}],{isPayg:true})
      ]
    };
  };
  const usage = machine => {
    const factor = machine === 'laptop' ? .25 : machine === 'desktop' ? .75 : 1;
    return {generatedAt:stamp(0),lastIngestAgeSeconds:8,historyUpdatedAt:stamp(-8),
      navigation:{todayUsd:Number((156.49 * factor).toFixed(2)),todayTokens:Math.round(32400000 * factor),
        burnRatePerDay:188 * factor,todayComplete:false,todayUnpricedTokens:Math.round(2400000 * factor),
        todayUnpricedModels:['sample-model-without-a-verified-rate']},
      totals:{cacheReusePct:96},
      repos:{totalUsd:156.49 * factor,rows:[{project:'northwind-web',usd:96 * factor,tokens:18000000 * factor},
        {project:'contoso-api',usd:60.49 * factor,tokens:14400000 * factor}]},windowTokens:{}};
  };
  window.fetch = async (input, options = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, 'https://preview.invalid');
    const machine = url.searchParams.get('machine') || 'all';
    let data;
    if (url.pathname === '/api/spend/hud') data = live(machine);
    else if (url.pathname === '/api/spend/hud/usage') data = usage(machine);
    else if (url.pathname === '/api/machines') data = {machines:[
      {id:'desktop',name:'Desktop',isLocal:true,lastSeenAt:stamp(0)},
      {id:'laptop',name:'Laptop',isLocal:false,lastSeenAt:stamp(-25)}]};
    else if (url.pathname === '/api/spend/freshness') data = {
      status:'warn',notes:[],ingest:{lastPassSeconds:1.3,lastPassAgeSeconds:8,stalled:false},
      issues:[{kind:'source',key:'antigravity_local',name:'Antigravity',short:'Antigravity',
        state:'held',severity:'warn',ageSeconds:180,lastSuccessAt:polled,
        detail:'The latest quota poll did not return a reading. The previous reading is still shown.',
        nextAction:'Open Antigravity, then poll again.'}]};
    else if (url.pathname === '/api/spend/share/week') data = {
      from:stamp(-7 * 86400),to:stamp(0),days:7,usd:1095.43,planUsd:210,complete:false,
      tokens:226800000,sessions:42,cacheReusePct:96,
      tools:[{name:'Codex',usd:600},{name:'Claude Code',usd:300},{name:'OpenCode',usd:195.43}]};
    else if (url.pathname === '/api/spend/quotas/refresh') data = {ok:true};
    else if (url.pathname === '/api/hud/native') data = {ok:true,pinned:true};
    else data = {ok:true};
    if (options.signal?.aborted) throw new DOMException('Aborted','AbortError');
    return new Response(JSON.stringify(data), {status:200,headers:{'Content-Type':'application/json'}});
  };
  // A second snapshot omits one quota value, exercising the real held-reading
  // path rather than adding a simulated visual state to the DOM.
  window.addEventListener('DOMContentLoaded', () => {
    // T3 provides theme variables; a standalone local file uses the actual
    // product palette instead. Explicit theme switching always uses the HUD.
    if (!getComputedStyle(document.documentElement).getPropertyValue('--card').trim()) {
      delete document.documentElement.dataset.previewTheme;
    }
    document.getElementById('hud-theme-btn')?.addEventListener('click', () => {
      delete document.documentElement.dataset.previewTheme;
    }, {capture:true});
    setTimeout(() => window.dispatchEvent(new Event('focus')), 350);
  });
})();
"""

PREVIEW_CSS = """
html[data-preview-theme="host"] {
  --bg: var(--card); --surface: var(--card); --sunken: var(--secondary);
  --text: var(--foreground); --hud-secondary: var(--muted-foreground);
  --hud-muted: var(--muted-foreground); --dim: var(--muted-foreground);
  --line: var(--border); --hair: var(--border); --hud-accent: var(--warning);
  --hud-accent-soft: var(--warning-surface); --green: var(--success);
  --danger: var(--destructive); --meter: var(--foreground); --track: var(--border);
  --tip-bg: var(--popover); --tip-line: var(--border); --glass: var(--card);
  --glass-hover: var(--card); --glass-line: var(--border);
  --sans: var(--font-sans); --mono: var(--font-mono);
}
html, html.is-glass, html[data-theme], body, html.is-glass body {
  min-height: 0 !important; height: auto !important; background: transparent !important;
  border: 0 !important; margin: 0; padding: 0 !important; overflow: visible;
}
html .page {max-width: 100%; margin-inline: auto;}
.preview-caption {margin: 10px auto 0; padding: 0; max-width: 420px; text-align: center;
  color: var(--hud-muted); font: 11px/1.5 var(--sans); cursor: default;}
"""


def preview_html(theme: str = "host", density: str = "compact") -> str:
    html = render_hud(dashboard=False)
    # Avoid shadowing the host's names while retaining the product's palette
    # for the real light/dark switch and for standalone local previews.
    for token in ("accent", "muted", "secondary"):
        html = html.replace("--" + token, "--hud-" + token)
    boot = BOOT.replace("THEME", json.dumps(theme)).replace("DENSITY", json.dumps(density))
    html = html.replace("<script>", "<script>" + boot, 1)
    html = re.sub(r"min-height:\s*100vh", "min-height: 0", html)
    html = html.replace("</head>", "<style>" + PREVIEW_CSS + "</style></head>")
    favicon = "data:image/svg+xml;base64," + base64.b64encode((ROOT / "spend_web/favicon.svg").read_bytes()).decode()
    html = html.replace("/favicon.svg", favicon)
    html = html.replace('"/logos.svg#" + id', '"#" + id')
    sprite = (ROOT / "spend_web/logos.svg").read_text(encoding="utf-8")
    sprite = sprite.replace('<svg xmlns=', '<svg aria-hidden="true" style="position:absolute;width:0;height:0;overflow:hidden" xmlns=', 1)
    html = html.replace("<body>", "<body>" + sprite, 1)
    html = html.replace("</body>", '<p class="preview-caption">Sample data · interactive preview</p></body>')
    return html


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "validation/hud-ui-preview.html")
    parser.add_argument("--theme", choices=("host", "light", "dark"), default="host")
    parser.add_argument("--density", choices=("compact", "comfortable"), default="compact")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(preview_html(args.theme, args.density), encoding="utf-8")
    print(args.output.resolve())


if __name__ == "__main__":
    main()
