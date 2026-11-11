"""Source-unit contracts complement production DOM request-race scenarios."""

import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def node(script):
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                            encoding="utf-8", timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_filter_identity_invalidates_cached_values_and_transport_ownership():
    source = (ROOT / "frontend_src/request-state.js").read_text(encoding="utf-8")
    report = node("""
const state={view:'overview',window:'1d',machine:'desktop',tool:'codex',request:0,
  summary:{window:{key:'1d'}},entity:{kind:'model',key:'model-a'},entityData:{window:{key:'1d'}}};
const nodes={};const $=id=>nodes[id]||(nodes[id]={classList:{toggle(){}},children:[],textContent:'',hidden:false});
const windowLabel=value=>value,setLoading=()=>{},renderNavbar=()=>{};
""" + source + """
state.summaryDataIdentity=queryIdentity();
state.entityDataIdentity=entityIdentity('model','model-a','1d');
const initial={summary:summaryMatches(),entity:entityMatches()};
const request=startViewRequest('overview');
state.machine='laptop';
const machine={summary:summaryMatches(),entity:entityMatches(),owner:ownsRender(request)};
state.machine='desktop';state.tool='all';
const tool={summary:summaryMatches(),entity:entityMatches(),owner:ownsRender(request)};
state.tool='codex';
const restored={summary:summaryMatches(),entity:entityMatches(),owner:ownsRender(request)};
console.log(JSON.stringify({initial,machine,tool,restored}));
""")
    assert report["initial"] == {"summary": True, "entity": True}
    assert report["machine"] == report["tool"] == {"summary": False, "entity": False, "owner": False}
    assert report["restored"] == {"summary": True, "entity": True, "owner": True}


def test_pricing_guidance_preserves_distinct_reasons_and_older_hub_fallback():
    source = (ROOT / "frontend_src/spend.js").read_text(encoding="utf-8")
    function = "function pricingGuidanceRows" + source.split("function pricingGuidanceRows", 1)[1].split("\nfunction ", 1)[0]
    report = node(function + """
console.log(JSON.stringify(pricingGuidanceRows({sources:[{source:'synthetic',pricingCoverage:{models:[
 {model:'known',records:2,reasons:[{code:'missing_historical_rate'},{code:'incomplete_telemetry'}]},
 {model:'unknown',records:1,reasons:[{code:'unknown_model_identity'},{code:'invalid_timestamp'}]},
 {model:'new',records:3,reasons:[{code:'missing_current_rate'}]},
 {model:'resolved',records:1,reasons:[]}
]}}],pricingGaps:['known','resolved','old-hub-model']})));
""")
    assert len(report) == 4
    assert report[0]["detail"] == "Historical rate unavailable · Token details incomplete"
    assert report[1]["detail"] == "Model identity unavailable · Usage date unavailable"
    assert report[2]["detail"] == "Current rate unavailable"
    assert report[3] == {"key": "legacy:old-hub-model", "label": "old-hub-model",
                         "detail": "Pricing unavailable", "records": None}
