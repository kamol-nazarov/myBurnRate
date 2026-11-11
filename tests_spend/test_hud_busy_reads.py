"""Native HUD polls must recover from contention for the backend's one read slot."""

import json
import subprocess

from spend_app.hud import render_hud


def source(end="        let livePending"):
    html = render_hud()
    start = "        const FETCH_TIMEOUT_MS"
    return start + html.split(start, 1)[1].split(end, 1)[0]


def node(script):
    result = subprocess.run(["node", "-e", script], text=True, encoding="utf-8",
                            capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_busy_usage_poll_retries_without_another_user_action_or_parallel_poll():
    report = node("""
const realSetTimeout = global.setTimeout;
const delays = [], requests = [];
let busy = false, cancelled = 0, usageValue = null;
let selectedMachine = 'all';
const statusView = {};
const feedHealth = {};
const paintStatus = () => {};
const renderSummary = () => {};
const renderUsage = data => { usageValue = data.navigation.todayUsd; };
const setTimeout = (fn, delay) => {
  if (delay !== 12000) delays.push(delay);
  return realSetTimeout(fn, delay === 12000 ? delay : 0);
};
const fetch = async url => {
  requests.push(url);
  if (busy) return {ok:false,status:503,headers:new Headers({'Retry-After':'1'}),
    body:{cancel:async()=>{cancelled++;}}};
  busy = true;
  if (!url.includes('/usage')) return new Promise(resolve => realSetTimeout(() => {
    busy = false; resolve({ok:true,status:200,json:async()=>({capacity:[]})});
  }, 0));
  busy = false;
  return {ok:true,status:200,json:async()=>({navigation:{todayUsd:156.49}})};
};
""" + source("        async function refreshMachines") + """
(async()=>{
  const live = refresh();
  const usage = refreshUsage();
  await refreshUsage(); // Must not release/admit another in-flight usage poll.
  await Promise.all([live,usage]);
  console.log(JSON.stringify({requests,delays,cancelled,usageValue,pending:usagePending}));
})().catch(e=>{console.error(e);process.exitCode=1;});
""")
    assert report["usageValue"] == 156.49
    assert report["cancelled"] == 1
    assert report["pending"] is False
    assert len(report["requests"]) == 3
    assert len(report["delays"]) == 1 and 1000 <= report["delays"][0] <= 1200


def test_busy_mutations_are_never_retried():
    report = node("""
let calls=0;
const fetch=async()=>{calls++;return {ok:false,status:503};};
""" + source() + """
(async()=>{
  let error;
  try {await hudFetch('/mutation',{method:'POST'});} catch(e){error=e.message;}
  console.log(JSON.stringify({calls,error}));
})().catch(e=>{console.error(e);process.exitCode=1;});
""")
    assert report == {"calls": 1, "error": "503"}


def test_usage_recovers_after_four_busy_responses_without_exhausting_budget():
    report = node("""
const realSetTimeout=global.setTimeout;
let calls=0, cancelled=0;
const setTimeout=(fn,delay)=>realSetTimeout(fn,delay===12000?delay:0);
const fetch=async()=>{
  calls++;
  if(calls<=4)return {ok:false,status:503,headers:new Headers({'Retry-After':'1'}),body:{cancel:async()=>{cancelled++;}}};
  return {ok:true,status:200,json:async()=>({value:156.49})};
};
""" + source() + """
(async()=>{
  const result=await hudFetch('/usage');
  console.log(JSON.stringify({calls,cancelled,value:result.value}));
})().catch(e=>{console.error(e);process.exitCode=1;});
""")
    assert report == {"calls": 5, "cancelled": 4, "value": 156.49}


def test_busy_retry_count_is_bounded_even_with_fast_mock_responses():
    report = node("""
const realSetTimeout=global.setTimeout;
let calls=0;
const setTimeout=(fn,delay)=>realSetTimeout(fn,delay===12000?delay:0);
const fetch=async()=>{calls++;return {ok:false,status:503,headers:new Headers({'Retry-After':'0'}),body:{cancel:async()=>{}}};};
""" + source() + """
(async()=>{
  let error;
  try {await hudFetch('/usage');}catch(e){error=e.message;}
  console.log(JSON.stringify({calls,error}));
})().catch(e=>{console.error(e);process.exitCode=1;});
""")
    assert report == {"calls": 9, "error": "503"}


def test_busy_backoff_uses_the_existing_request_deadline():
    report = node("""
let deadline, retry, calls=0, retryCleared=false;
const setTimeout=(fn,delay)=>{if(delay===12000){deadline=fn;return 'deadline';}retry=fn;return 'retry';};
const clearTimeout=id=>{if(id==='retry')retryCleared=true;};
const fetch=async()=>{calls++;return {ok:false,status:503,headers:new Headers({'Retry-After':'60'}),body:{cancel:async()=>{}}};};
""" + source() + """
(async()=>{
  const pending=hudFetch('/usage').catch(e=>e.name);
  while(!retry) await Promise.resolve();
  deadline();
  const error=await pending;
  console.log(JSON.stringify({calls,error,retryCleared}));
})().catch(e=>{console.error(e);process.exitCode=1;});
""")
    assert report == {"calls": 1, "error": "AbortError", "retryCleared": True}
