"""Real HUD interactions: density, disclosures, balances and stable provider pins."""

import json

import pytest

from spend_app.hud import render_hud


@pytest.mark.browser
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_hud_controls_preserve_context_and_readable_layout(tmp_path, theme):
    from tests_spend.browser_runtime import CHROME
    from tests_spend.test_frontend_viewports import _chrome_dump, _probe

    if not CHROME.exists():
        pytest.skip("Google Chrome is not installed")
    boot = r"""
localStorage.setItem('burnrate:hud-mini', '1');
localStorage.setItem('burnrate:hud-theme', THEME);
localStorage.setItem('burnrate:hud-density', 'compact');
localStorage.removeItem('burnrate:hud-pins');
window.EventSource = undefined;
window.__TAURI_INTERNALS__ = {invoke: async () => true};
const nativeMatch = window.matchMedia.bind(window);
window.matchMedia = query => query === '(prefers-reduced-motion: reduce)' ? {matches:true} : nativeMatch(query);
window.uiErrors = [];
window.addEventListener('error', event => uiErrors.push(event.message));
window.addEventListener('unhandledrejection', event => uiErrors.push(String(event.reason)));
window.rev = 0;
window.requests = [];
window.fetch = async url => {
  requests.push(url);
  const now = new Date().toISOString(), reset = new Date(Date.now()+3600000).toISOString();
  const quota = (limitKey, pct, extra={}) => ({limitKey, label: limitKey==='weekly'?'Weekly':'5-hour', pct, resetsAt:reset, polledAt:now, ...extra});
  let data = {};
  if (url.startsWith('/api/spend/hud/usage')) data = {navigation:{todayUsd:156.49,todayComplete:false,todayTokens:100000,
    todayUnpricedTokens:1000,todayUnpricedModels:['example:model'],burnRatePerDay:25},totals:{cacheReusePct:90}};
  else if (url.startsWith('/api/spend/hud?')) data = {status:'live',activity:[],capacity:[
    {providerKey:'alpha',providerName:'Alpha Research and Engineering',rows:[quota('5h',rev?96:10),quota('weekly',15)]},
    {providerKey:'beta',providerName:'Beta',rows:[quota('5h',rev?20:80),quota('weekly',30,{held:true,polledAt:new Date(Date.now()-180000).toISOString()})]},
    {providerKey:'openrouter',providerName:'OpenRouter',isPayg:true,rows:[{limitKey:'balance',label:'Balance',unit:'usd',used:16.25,
      isPayg:true,runway:{days:6,dailyUsd:2.7,basisDays:7,state:'steady'}}]}
  ]};
  else if (url.startsWith('/api/machines')) data = {machines:[{id:'desktop',name:'Desktop',isLocal:true},{id:'laptop',name:'Laptop'}]};
  else if (url.startsWith('/api/spend/freshness')) data = {status:'warn',issues:[{key:'beta',name:'Beta',short:'Beta',kind:'source',
    state:'held',severity:'warn',ageSeconds:180,detail:'The latest reading is unavailable.'}]};
  return new Response(JSON.stringify({...data,generatedAt:now,lastIngestAgeSeconds:0}),{status:200});
};
""".replace("THEME", json.dumps(theme))
    probe = r"""
<script>
const report = {};
const order = () => [...document.querySelectorAll('#hud-quotas .plan')].map(node=>node.dataset.providerKey);
setTimeout(() => {
  report.initial = {order:order(),width:document.getElementById('hud-shell').getBoundingClientRect().width,
    label:document.querySelector('.mini-today-label').textContent,
    machine:document.getElementById('hud-machine-label').textContent,
    fresh:document.getElementById('hud-updated-compact').textContent,
    balances:document.getElementById('hud-balances').textContent,
    balanceMeters:document.querySelectorAll('#hud-balances .meter').length,
    held:document.querySelector('.win-freshness').textContent};
  const partial=document.getElementById('hud-mini-coverage-btn');
  partial.focus(); partial.click();
  report.coverage={open:!document.getElementById('hud-coverage-panel').hidden,
    text:document.getElementById('hud-coverage-detail').textContent,expanded:partial.getAttribute('aria-expanded')};
  document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
  report.coverage.closed=document.getElementById('hud-coverage-panel').hidden;
  report.coverage.focus=document.activeElement===partial;
  const attention=document.getElementById('hud-mini-alert'); attention.click();
  attention.dispatchEvent(new PointerEvent('pointerover',{bubbles:true,clientX:20,clientY:20}));
  const tipWhileOpen=document.getElementById('hud-tip').classList.contains('on');
  report.attention={text:attention.textContent,open:!document.getElementById('hud-attention-panel').hidden,
    tipWhileOpen,
    detail:document.getElementById('hud-attention-content').textContent,
    actions:[...document.querySelectorAll('.issue-action')].map(node=>node.textContent)};
  document.getElementById('hud-attention-close').click();
  const plan=document.querySelector('#hud-quotas [data-provider-key="beta"]');
  const toggle=plan.querySelector('.plan-toggle');
  report.expansion={tag:toggle.tagName,initial:toggle.getAttribute('aria-expanded')};
  toggle.click();
  report.expansion.after=toggle.getAttribute('aria-expanded');
  report.expansion.visible=!document.getElementById(toggle.getAttribute('aria-controls')).hidden;
  document.getElementById('hud-mini-btn').click();
  const pin=document.querySelector('#hud-quotas [data-provider-key="beta"] .plan-pin'); pin.focus(); pin.click();
  report.pins={order:order(),saved:JSON.parse(localStorage.getItem('burnrate:hud-pins')),focus:document.activeElement.dataset.planFocus};
  document.getElementById('hud-mini-btn').click();
  document.getElementById('hud-context-disclosure').open=true;
  document.getElementById('hud-density-btn').click();
  report.density={saved:localStorage.getItem('burnrate:hud-density'),mini:document.documentElement.classList.contains('is-mini'),
    width:document.getElementById('hud-shell').getBoundingClientRect().width};
  rev=1; window.dispatchEvent(new Event('focus'));
},150);
setTimeout(()=>{
  report.refreshed={order:order(),urgent:document.getElementById('hud-urgent').textContent,
    expanded:document.querySelector('#hud-quotas [data-provider-key="beta"] .plan-toggle').getAttribute('aria-expanded')};
  const shell=document.getElementById('hud-shell').getBoundingClientRect();
  report.overflow=[...document.querySelectorAll('.plan-head,.win,.balance-row')].filter(node=>{
    const rect=node.getBoundingClientRect();return rect.width && (rect.left<shell.left-1||rect.right>shell.right+1||node.scrollWidth>node.clientWidth+1);
  }).map(node=>({class:node.className,width:node.clientWidth,scroll:node.scrollWidth,text:node.textContent,
    children:[...node.querySelectorAll('*')].map(child=>({class:child.className?.baseVal||child.className,
      width:child.getBoundingClientRect().width,display:getComputedStyle(child).display}))}));
  document.getElementById('hud-mini-btn').click();
  report.mode={full:document.querySelector('#hud-quotas [data-provider-key="alpha"] .plan-toggle').getAttribute('aria-expanded')};
  document.getElementById('hud-mini-btn').click();
  report.mode.mini=document.querySelector('#hud-quotas [data-provider-key="alpha"] .plan-toggle').getAttribute('aria-expanded');
  report.errors=uiErrors;
  document.title='PROBE:'+JSON.stringify(report);
},550);
</script>
"""
    html = render_hud().replace("<script>", "<script>" + boot, 1)
    page = tmp_path / "hud-ui.html"
    page.write_text(html.replace("</body>", probe + "</body>"), encoding="utf-8")
    result = _probe(_chrome_dump(page.as_uri() + "?glass=1", 440, tmp_path, budget_ms=1000))
    assert result["errors"] == []
    assert result["initial"]["order"] == ["alpha", "beta"]
    assert result["initial"]["width"] == 296
    assert "API" in result["initial"]["label"] and "today" in result["initial"]["label"]
    assert result["initial"]["machine"] == "All computers"
    assert "Updated" in result["initial"]["fresh"]
    assert "$16.25" in result["initial"]["balances"] and "remaining" in result["initial"]["balances"]
    assert result["initial"]["balanceMeters"] == 0
    assert result["initial"]["held"] == "Held · 3m ago"
    assert result["coverage"]["open"] and result["coverage"]["closed"] and result["coverage"]["focus"]
    assert result["coverage"]["expanded"] == "true"
    assert "not a bill" in result["coverage"]["text"]
    assert result["attention"]["text"] == "1 source needs attention"
    assert result["attention"]["open"]
    assert result["attention"]["tipWhileOpen"] is False
    assert "Beta" in result["attention"]["detail"] and "3m ago" in result["attention"]["detail"]
    assert result["attention"]["actions"] == ["Recheck", "Source settings"]
    assert result["expansion"] == {"tag":"BUTTON","initial":"false","after":"true","visible":True}
    assert result["pins"] == {"order":["beta","alpha"],"saved":["beta"],"focus":"pin:beta"}
    assert result["density"] == {"saved":"comfortable","mini":True,"width":344}
    assert result["refreshed"]["order"] == ["beta","alpha"]
    assert "Alpha" in result["refreshed"]["urgent"] and "96% used" in result["refreshed"]["urgent"]
    assert result["refreshed"]["expanded"] == "true"
    assert result["overflow"] == [], json.dumps(result["overflow"])
    assert result["mode"] == {"full":"true","mini":"false"}


@pytest.mark.browser
def test_realistic_eleven_provider_compact_hud_fits_without_scroll(tmp_path):
    from tests_spend.browser_runtime import CHROME
    from tests_spend.test_frontend_viewports import _chrome_dump, _probe

    if not CHROME.exists():
        pytest.skip("Google Chrome is not installed")
    boot = r"""
localStorage.setItem('burnrate:hud-mini','1');
localStorage.setItem('burnrate:hud-density','compact');
localStorage.removeItem('burnrate:hud-pins');
window.EventSource=undefined;
window.__TAURI_INTERNALS__={invoke:async()=>true};
window.fetch=async url=>{
  const now=new Date().toISOString(),reset=new Date(Date.now()+86400000*4).toISOString();
  const row=(limitKey,pct,label)=>({limitKey,pct,label:label||limitKey,resetsAt:reset,polledAt:now});
  let data={};
  if(url.startsWith('/api/spend/hud/usage'))data={navigation:{todayUsd:156.49,todayComplete:false,todayTokens:1000000,todayUnpricedTokens:1000},totals:{cacheReusePct:90}};
  else if(url.startsWith('/api/spend/hud?'))data={status:'live',activity:[],capacity:[
    {providerKey:'opencode-go',providerName:'OpenCode Go',rows:[row('monthly',80,'Monthly'),row('5h',12,'5-hour'),row('weekly',30,'Weekly')]},
    {providerKey:'antigravity',providerName:'Antigravity',rows:[row('gemini-5h',0,'Gemini 5-hour'),row('gemini-weekly',24,'Gemini weekly'),row('other-5h',0,'Claude 5-hour'),row('other-weekly',45,'Claude weekly')]},
    {providerKey:'codex',providerName:'Codex',rows:[row('weekly',43,'Weekly')]},
    {providerKey:'claude-code',providerName:'Claude',rows:[row('weekly',28,'Weekly'),row('5h',10,'5-hour')]},
    {providerKey:'opencode-go-profile2',providerName:'Profile 2',accountLabel:'Profile 2',rows:[row('monthly',7,'Monthly')]},
    {providerKey:'claude-code-work',providerName:'Work',accountLabel:'Work',rows:[row('weekly',6,'Weekly')]},
    {providerKey:'opencode',providerName:'Z.AI',rows:[row('weekly',3,'Weekly')]},
    {providerKey:'muse',providerName:'Muse',rows:[row('5h',0,'5-hour'),row('weekly',0,'Weekly')]},
    {providerKey:'grok',providerName:'Grok',rows:[row('weekly',null,'Weekly')]},
    {providerKey:'openrouter',providerName:'OpenRouter',isPayg:true,rows:[{limitKey:'balance',unit:'usd',used:16,isPayg:true}]},
    {providerKey:'deepseek',providerName:'DeepSeek',isPayg:true,rows:[{limitKey:'balance',unit:'usd',used:55,isPayg:true,runway:{days:400,state:'steady'}}]}
  ]};
  else if(url.startsWith('/api/machines'))data={machines:[{id:'desktop',name:'Desktop',isLocal:true},{id:'laptop',name:'Laptop'}]};
  else if(url.startsWith('/api/spend/freshness'))data={status:'warn',issues:[{name:'Antigravity',short:'Antigravity',key:'antigravity',state:'held',severity:'warn',detail:'Latest reading unavailable.'}]};
  return new Response(JSON.stringify({...data,generatedAt:now,lastIngestAgeSeconds:5}),{status:200});
};
"""
    probe = r"""
<script>
setTimeout(()=>{
 const shell=document.getElementById('hud-shell').getBoundingClientRect();
 const providers=[...document.querySelectorAll('#hud-quotas .plan,#hud-balances .balance-row')];
 const visible=node=>{const r=node.getBoundingClientRect();return r.width>0&&r.height>0&&r.bottom<=820;};
 const overs=[...document.querySelectorAll('.plan-head,.win,.balance-row')].filter(node=>{
   const r=node.getBoundingClientRect();return r.width&&(r.right>shell.right+1||node.scrollWidth>node.clientWidth+1);
 }).map(node=>({class:node.className,width:node.clientWidth,scroll:node.scrollWidth}));
 document.title='PROBE:'+JSON.stringify({width:shell.width,height:shell.height,providers:providers.length,
   visible:providers.every(visible),openExtras:document.querySelectorAll('.plan-extra:not([hidden])').length,
   contextOpen:document.getElementById('hud-context-disclosure').open,overflow:overs});
},250);
</script>
"""
    html = render_hud().replace("<script>", "<script>" + boot, 1)
    page = tmp_path / "hud-eleven-providers.html"
    page.write_text(html.replace("</body>", probe + "</body>"), encoding="utf-8")
    result = _probe(_chrome_dump(page.as_uri() + "?glass=1", 296, tmp_path, budget_ms=1000))
    assert result["providers"] == 11 and result["visible"]
    assert result["width"] == 296
    assert result["height"] <= 620
    assert result["openExtras"] == 0 and not result["contextOpen"]
    assert result["overflow"] == []
