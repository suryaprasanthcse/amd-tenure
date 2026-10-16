"""Recorded governance dashboard and business-impact panel (master §8, §20; RUNBOOK T07, recorded mode).

Runs offline from committed files only: the ledger export `exports/R-20261015-01/{ledger.jsonl,head.json}` and the
dashboard fixture `exports/L-20261015-01/dashboard_data.json`, which `build_fixture` derives from the recorded live
D-POLICY run. The page is one self-contained HTML file (inline CSS, JS and SVG, no network, no dependencies).

    python -m tenure.dashboard                      # serve on http://127.0.0.1:8765 and open it
    python -m tenure.dashboard --out dashboard.html # write the static page
    python -m tenure.dashboard verify               # print the ledger verification badge
    python -m tenure.dashboard build                # regenerate the fixture from var/runs (needs local run data)

The drift scenario shown here is the MOCK agent (policy oracle with 2% deterministic errors), not Gemma: it
describes the ladder mechanics. The page says so, and so does every business figure derived from it.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import webbrowser
from collections import defaultdict
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .cusum import CusumMonitor, design
from .governance import read_jsonl, verify_jsonl
from .ledger import Head

ROOT = Path(__file__).resolve().parents[2]
LEDGER = ROOT / "exports" / "R-20261015-01" / "ledger.jsonl"
HEAD = ROOT / "exports" / "R-20261015-01" / "head.json"
FIXTURE = ROOT / "exports" / "L-20261015-01" / "dashboard_data.json"
LEDGER_DB = ROOT / "var" / "ledger.db"
LEVELS = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}

# ASSUMPTIONS (master §8.5, tenure.yaml `business`; sliders on the page change them). Nothing here is measured
# on a real merchant: the economics are the team's own practice priced with these numbers.
DEFAULT_ASSUMPTIONS = {
    "handle_minutes": 19 / 3,   # mean of Triangular(3, 6, 10)
    "wage_eur_per_hour": 22.0,
    "error_cost_eur": 25.0,     # cost.py CostParams.missed_cents, the pessimistic error type
    "gpu_eur_per_hour": 2.00,
}


# --------------------------------------------------------------------------------------------- business impact

def impact(inputs: dict[str, Any], a: dict[str, float] | None = None) -> dict[str, float]:
    """Monthly (30-day) impact of the recorded run's autonomy, relative to people handling every case.

    Every case is scaled to a 30-day month. Autonomous cases skip the human, except audited ones, which a person
    still checks. Errors are priced at `error_cost_eur` at the rate observed on labelled outcomes. Inference is
    charged on every case (the agent shadows at L0 too): GPU €/h divided by measured cases per hour.
    """
    a = {**DEFAULT_ASSUMPTIONS, **(a or {})}
    scale = 30.0 / inputs["days"]
    volume = inputs["cases"] * scale
    autonomous = inputs["autonomous"] * scale
    audited = inputs["audited"] * scale
    error_rate = 1.0 - inputs["observed_agree"] / inputs["observed"]
    human_cost = volume * a["handle_minutes"] / 60.0 * a["wage_eur_per_hour"]
    human_saved = (autonomous - audited) * a["handle_minutes"] / 60.0 * a["wage_eur_per_hour"]
    error_cost = autonomous * error_rate * a["error_cost_eur"]
    inference = volume * a["gpu_eur_per_hour"] / inputs["measured_cases_per_hour"]
    autonomous_share = autonomous / volume
    human_seconds = a["handle_minutes"] * 60.0
    mean_resolution_s = (1.0 - autonomous_share) * human_seconds + autonomous_share * inputs["agent_seconds"]
    return {
        "volume": volume, "autonomous_cases": autonomous, "audited_cases": audited,
        "error_rate": error_rate, "human_only_cost": human_cost, "human_saved": human_saved,
        "error_cost": error_cost, "inference_cost": inference,
        "net": human_saved - error_cost - inference,
        "escalation_offload_pct": 100.0 * (autonomous - audited) / volume,
        "autonomous_share_pct": 100.0 * autonomous_share,
        "latency_reduction_pct": 100.0 * (1.0 - mean_resolution_s / human_seconds),
        "mean_resolution_s": mean_resolution_s,
    }


# ------------------------------------------------------------------------------------------------ verification

def _db_lines(db: Path) -> list[str]:
    con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT seq, ts_utc, kind, actor, payload, payload_sha, prev_hash, hash "
                           "FROM ledger ORDER BY seq").fetchall()
    finally:
        con.close()
    return [json.dumps({"seq": s, "ts_utc": t, "kind": k, "actor": a, "payload": json.loads(p), "payload_sha": ps,
                        "prev_hash": ph, "hash": h}) for s, t, k, a, p, ps, ph, h in rows]


def verify_sources(ledger: Path = LEDGER, head_path: Path = HEAD, db: Path | None = LEDGER_DB) -> dict[str, Any]:
    """Recompute the SHA-256 chain from genesis now. The committed export is checked against head.json; a local
    var/ledger.db, when present, is re-verified too and must contain the anchored head at its sequence number.
    `db=None` checks the committed export only (the static page uses this, so it is reproducible anywhere)."""
    h = json.loads(head_path.read_text(encoding="utf-8"))
    anchor = Head(int(h["seq"]), str(h["hash"]))
    rep = verify_jsonl(ledger.read_text(encoding="utf-8").splitlines(), anchor)
    out: dict[str, Any] = {"ok": rep.ok, "entries": rep.entries, "head_seq": anchor.seq, "head_hash": anchor.hash,
                           "reason": rep.reason, "sources": [{"name": ledger.name, "ok": rep.ok,
                                                              "detail": f"{rep.entries} entries vs head.json"}]}
    if db is None:
        pass
    elif db.exists():
        lines = _db_lines(db)
        drep = verify_jsonl(lines)
        entry = json.loads(lines[anchor.seq - 1]) if len(lines) >= anchor.seq else None
        anchored = entry is not None and entry["hash"] == anchor.hash
        ok = drep.ok and anchored
        out["sources"].append({"name": "var/ledger.db", "ok": ok,
                               "detail": f"{drep.entries} entries, chain {'intact' if drep.ok else drep.reason}, "
                                         f"anchored head seq {anchor.seq} {'present' if anchored else 'NOT FOUND'}"})
        out["ok"] = out["ok"] and ok
    else:
        out["sources"].append({"name": "var/ledger.db", "ok": None, "detail": "not present (not committed); skipped"})
    return out


# ------------------------------------------------------------------------------------------------ fixture build

def _cusum_windows(routes: list[dict], promotes: list[dict]) -> list[dict]:
    """Re-run the CUSUM monitor over a cell's observed outcomes, one window per promotion (R.9 semantics: a
    monitor starts at each promotion and is dropped at a demotion to L0)."""
    windows: list[dict] = []
    promos = iter(promotes)
    prev_level, cur, monitor = 0, None, None
    for r in routes:
        lvl = LEVELS[r["level"]]
        if lvl >= 1 and (lvl != prev_level):
            if lvl > prev_level:
                ev = next(promos, None)
                monitor = None
                if ev is not None:
                    e = ev["payload"]["evidence"]
                    d = design(float(e["cusum_p0"]), float(e["cusum_p1"]))
                    monitor = CusumMonitor(d)
                    cur = {"level": r["level"], "p0": d.p0, "p1": d.p1, "h": round(d.h, 4), "start": r["date"],
                           "trace": [], "signal": False}
                    windows.append(cur)
            else:
                monitor = None  # demoted from L2 to L1: R.9 drops the monitor
        prev_level = lvl
        if lvl >= 1 and monitor is not None and r["observed"]:
            fired = monitor.update(not r["agree"])
            cur["trace"].append(round(monitor.s, 4))
            if fired:
                cur["signal"] = True
                monitor = None
    return windows


def build_fixture(run_dir: Path, spikes: Path, ledger: Path = LEDGER) -> dict[str, Any]:
    """Derive the dashboard fixture from a recorded live run's routes and summary (local run data required)."""
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    routes = [json.loads(x) for x in (run_dir / "routes.jsonl").read_text(encoding="utf-8").splitlines() if x]
    entries = read_jsonl(ledger)
    seed_id = manifest["seed_run"]
    seed_manifest = json.loads((run_dir.parent / seed_id / "manifest.json").read_text(encoding="utf-8"))
    events = [json.loads(x) for x in (run_dir.parent / seed_id / "events.jsonl").read_text(encoding="utf-8").splitlines()
              if x]
    # the frozen replay is the first 500 committed cases (D-k2); a later attempt ran past it
    upto = [e["data"] for e in events if e.get("event") == "progress" and e["data"]["committed"] <= 500]
    last = upto[-1]
    s2 = json.loads((spikes / "S2.json").read_text(encoding="utf-8"))

    days = sorted({r["date"] for r in routes})
    cells = sorted({r["cell"] for r in routes} | set(summary["levels"]))
    by_cell_day: dict[str, dict[str, int]] = defaultdict(dict)
    by_cell: dict[str, list[dict]] = defaultdict(list)
    for r in routes:
        by_cell_day[r["cell"]][r["date"]] = LEVELS[r["level"]]
        by_cell[r["cell"]].append(r)
    levels: dict[str, list[int]] = {}
    for c in cells:
        cur, row = 0, []
        for d in days:
            cur = by_cell_day[c].get(d, cur)
            row.append(cur)
        levels[c] = row
    promos: dict[str, list[dict]] = defaultdict(list)
    signals: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        p = e["payload"]
        if e["kind"] == "LEVEL_CHANGE" and p["reason"] == "PROMOTE":
            promos[p["cell"]].append(e)
        if e["kind"] == "CUSUM_SIGNAL":
            signals[p["cell"]].append({"seq": e["seq"], "s": p["s"], "h": p["h"], "n": p["outcomes_monitored"]})
    cusum = {}
    for c in cells:
        w = _cusum_windows(by_cell[c], promos.get(c, []))
        if w:
            cusum[c] = {"windows": w, "ledger_signals": signals.get(c, [])}
    return {
        "run_id": manifest["run_id"], "agent": manifest["agent"], "mock": manifest["agent"].startswith("mock"),
        "drift": summary["drift"], "days": days, "cells": cells, "levels": levels, "cusum": cusum,
        "demotions": summary["demotions"],
        "impact_inputs": {
            "days": summary["days"], "cases": summary["cases"], "autonomous": summary["autonomous"],
            "audited": summary["audited"], "observed": summary["observed"], "observed_agree": summary["observed_agree"],
            "measured_cases_per_hour": round(3600.0 * last["cases_per_s"], 1),
            "replay_committed": last["committed"],
            "agent_seconds": round(next(x["p50_ms"] for x in s2["sweep"] if x["concurrency"] == 1) / 1000.0, 3),
            "provenance": {"run": manifest["run_id"], "seed_run": seed_id, "seed_run_key": seed_manifest.get("run_key")},
        },
    }


# ------------------------------------------------------------------------------------------------ page rendering

def load_data(fixture: Path = FIXTURE) -> dict[str, Any]:
    return json.loads(fixture.read_text(encoding="utf-8"))


def render_html(data: dict[str, Any], badge: dict[str, Any], served: bool = False) -> str:
    payload = {"data": data, "badge": badge, "assumptions": DEFAULT_ASSUMPTIONS, "served": served,
               "default_impact": impact(data["impact_inputs"])}
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return _TEMPLATE.replace("__PAYLOAD__", blob)


class _Handler(BaseHTTPRequestHandler):
    def __init__(self, *args: Any, data: dict[str, Any], **kw: Any):
        self.data = data
        super().__init__(*args, **kw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.startswith("/api/verify"):
            body, ctype = json.dumps(verify_sources()).encode(), "application/json"
        else:
            body, ctype = render_html(self.data, verify_sources(), served=True).encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a: Any) -> None:  # quiet
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tenure.dashboard", description=__doc__.split("\n")[0])
    ap.add_argument("command", nargs="?", default="serve", choices=["serve", "verify", "build"])
    ap.add_argument("--out", type=Path, help="write the static page here instead of serving")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--run-dir", type=Path, default=ROOT / "var" / "runs" / "L-20261015-01")
    ns = ap.parse_args(argv)
    if ns.command == "verify":
        v = verify_sources()
        print(f"Ledger {'VERIFIED' if v['ok'] else 'FAILED'} | {v['entries']} entries | head seq {v['head_seq']} | {v['head_hash']}")
        for s in v["sources"]:
            print(f"  {s['name']}: {s['detail']}")
        return 0 if v["ok"] else 1
    if ns.command == "build":
        data = build_fixture(ns.run_dir, spikes=ROOT / "var" / "spikes")
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8", newline="\n")
        print(f"wrote {FIXTURE}")
        return 0
    data = load_data()
    if ns.out:
        # the static page verifies the committed export only, never a local var/ledger.db
        ns.out.write_text(render_html(data, verify_sources(db=None)), encoding="utf-8", newline="\n")
        print(f"wrote {ns.out}")
        return 0
    server = ThreadingHTTPServer(("127.0.0.1", ns.port), partial(_Handler, data=data))
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"TENURE dashboard (recorded, offline) at {url} (Ctrl+C to stop)")
    if not ns.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TENURE · earned autonomy</title>
<style>
:root{--bg:#fafaf7;--fg:#1b1d1f;--mut:#5d646b;--card:#fff;--line:#d9dcdf;--l0:#e6e8ea;--l1:#bcd9f2;--l2:#6aa9dc;--l3:#1f5f99;--bad:#b3261e;--ok:#1b7a43;--warn:#8a5a00}
@media(prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e8eaec;--mut:#9aa3ab;--card:#1c1f24;--line:#33383f;--l0:#2a2e34;--l1:#27496b;--l2:#3d7db8;--l3:#8fc0ec;--bad:#ff8a80;--ok:#66d28c;--warn:#f0b956}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:16px}h1{font-size:22px;margin:8px 0 2px}h2{font-size:17px;margin:0 0 8px}
.sub{color:var(--mut);margin:0 0 12px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:14px 0}
.banner{border-left:4px solid var(--warn);padding:8px 12px;background:var(--card);border-radius:6px;margin:10px 0}
.badge{display:inline-flex;gap:8px;align-items:center;padding:4px 10px;border-radius:999px;border:1px solid var(--line);font-weight:600}
.ok{color:var(--ok)}.fail{color:var(--bad)}.mut{color:var(--mut)}.small{font-size:13px}
button{font:inherit;padding:4px 10px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
table.heat{border-collapse:separate;border-spacing:3px;width:100%}.heat th{font-size:12px;font-weight:600;color:var(--mut);text-align:left;white-space:nowrap}
.heat td{text-align:center;border-radius:5px;font-size:12px;font-weight:700;height:26px;min-width:52px}
.c0{background:var(--l0)}.c1{background:var(--l1)}.c2{background:var(--l2);color:#fff}.c3{background:var(--l3);color:#fff}
.dark .c3,.c3{color:#fff}@media(prefers-color-scheme:dark){.c2,.c3{color:#0d1013}}
.na{background:transparent;color:var(--mut);font-weight:400}
input[type=range]{width:100%}.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
.kpi{font-size:24px;font-weight:700}.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:10px 0}
svg{width:100%;height:auto;display:block}svg text{fill:var(--mut);font-size:11px}
label{display:block;font-size:13px;color:var(--mut)}select{font:inherit;padding:3px;background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px}
.tag{font-size:11px;border:1px solid var(--warn);color:var(--warn);border-radius:4px;padding:0 4px;margin-left:4px}
</style></head><body><main>
<h1>TENURE <span class="mut">· earned autonomy for customer-resolution agents</span></h1>
<p class="sub">Recorded mode · runs offline from committed files · Track 3: Reinvent Commerce</p>
<div class="banner" id="banner"></div>
<div class="card"><h2>Cryptographic verification</h2><div id="badge"></div></div>
<div class="card"><h2>Cell autonomy heat map <span class="mut small" id="hday"></span></h2>
<div class="small mut">Case type × order-value band. Cell text is the autonomy level (L0 human decides, shadowed · L1 suggests · L2 acts with audits · L3 acts, light audit). Drag the slider to replay the 62 days.</div>
<input type="range" id="day" min="0" value="0" aria-label="Day"><div id="heat"></div></div>
<div class="card"><h2>Autonomy ladder timeline</h2><div id="timeline"></div></div>
<div class="card"><h2>CUSUM drift detection <span class="mut small">S against threshold h</span></h2>
<label>Cell <select id="cusumcell"></select></label><div id="cusum"></div><div class="small mut" id="cusumnote"></div></div>
<div class="card"><h2>Business impact calculator <span class="tag">ASSUMPTIONS</span></h2>
<div class="small mut" id="impactnote"></div><div class="kpis" id="kpis"></div><div class="grid2" id="sliders"></div>
<div class="small mut" id="assume"></div></div>
<p class="small mut">Method, assumptions and limits are in the README. The mock agent shows ladder mechanics, not Gemma's accuracy. The ledger anchors the replay run R-20261015-01 and the live run L-20261015-01.</p>
</main><script id="payload" type="application/json">__PAYLOAD__</script><script>
"use strict";
const P=JSON.parse(document.getElementById('payload').textContent),D=P.data,$=id=>document.getElementById(id);
const fmt=(x,d=0)=>Number(x).toLocaleString('en-GB',{minimumFractionDigits:d,maximumFractionDigits:d});
const el=(t,a={},h='')=>{const e=document.createElement(t);for(const k in a)e.setAttribute(k,a[k]);e.innerHTML=h;return e};
$('banner').innerHTML='<b>Read this first.</b> The ladder and drift views below are the <b>'+D.agent+'</b> agent (a policy oracle with 2% deterministic errors) on 6,454 held-out cases under the D-POLICY drift scenario. They show how autonomy is earned and lost. They are <b>not</b> a Gemma accuracy result. On Gemma-4-31B-IT the real 500-case replay (466/500 agree, Wilson 95% [0.905, 0.952]) promoted no cell above L0.';
function renderBadge(v){const ok=v.ok;let h='<span class="badge '+(ok?'ok':'fail')+'">'+(ok?'✔ VERIFIED':'✖ FAILED')+' · '+v.entries+' entries · head seq '+v.head_seq+'</span> <code class="small">'+v.head_hash+'</code><ul class="small">';
 for(const s of v.sources)h+='<li><b>'+s.name+'</b>: '+(s.ok===null?'<span class="mut">':s.ok?'<span class="ok">':'<span class="fail">')+s.detail+'</span></li>';
 h+='</ul>';if(P.served)h+='<button id="re">Re-verify now</button>';else h+='<span class="small mut">Static page: checked when generated. Run <code>python -m tenure.dashboard</code> to re-verify live.</span>';
 $('badge').innerHTML=h;if(P.served)$('re').onclick=()=>{$('badge').textContent='verifying…';fetch('/api/verify').then(r=>r.json()).then(renderBadge)}}
renderBadge(P.badge);
// heat map
const types=[...new Set(D.cells.map(c=>c.split('|')[0]))],bands=['V1','V2','V3','V4'],nd=D.days.length;
$('day').max=nd-1;$('day').value=nd-1;
function heat(){const i=+$('day').value;$('hday').textContent='· '+D.days[i]+(D.days[i]>=D.drift.at_date?' (after drift)':'');
 let h='<table class="heat"><tr><th>case type</th>'+bands.map(b=>'<th>'+b+'</th>').join('')+'</tr>';
 for(const t of types){h+='<tr><th>'+t+'</th>';for(const b of bands){const k=t+'|'+b,r=D.levels[k];h+=r?'<td class="c'+r[i]+'" title="'+k+' L'+r[i]+'">L'+r[i]+'</td>':'<td class="na">–</td>'}h+='</tr>'}
 $('heat').innerHTML=h+'</table>'}
$('day').oninput=heat;heat();
// timeline
(function(){const W=1000,H=210,L=40,R=10,T=14,B=26,pw=W-L-R,ph=H-T-B,n=nd;const cnt=l=>D.days.map((_,i)=>D.cells.filter(c=>D.levels[c][i]>=l).length);
 const a1=cnt(1),a2=cnt(2),mx=Math.max(4,...a1),X=i=>L+pw*i/(n-1),Y=v=>T+ph*(1-v/mx);
 const pth=a=>a.map((v,i)=>(i?'L':'M')+X(i).toFixed(1)+' '+Y(v).toFixed(1)).join('');
 let s='<svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="Cells at or above each level per day">';
 for(let g=0;g<=mx;g+=Math.ceil(mx/5))s+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+Y(g)+'" y2="'+Y(g)+'" stroke="var(--line)"/><text x="4" y="'+(Y(g)+4)+'">'+g+'</text>';
 const di=D.days.findIndex(d=>d>=D.drift.at_date);
 if(di>=0)s+='<line x1="'+X(di)+'" x2="'+X(di)+'" y1="'+T+'" y2="'+(T+ph)+'" stroke="var(--bad)" stroke-dasharray="4 3"/><text x="'+(X(di)+4)+'" y="'+(T+10)+'" style="fill:var(--bad)">drift injected '+D.drift.at_date+'</text>';
 s+='<path d="'+pth(a1)+'" fill="none" stroke="var(--l2)" stroke-width="2.5"/><path d="'+pth(a2)+'" fill="none" stroke="var(--l3)" stroke-width="2.5" stroke-dasharray="6 3"/>';
 for(const m of D.demotions){const i=D.days.indexOf(m.date);if(i>=0)s+='<circle cx="'+X(i)+'" cy="'+Y(a1[i])+'" r="5" fill="var(--bad)"><title>'+m.cell+' demoted by CUSUM '+m.date+'</title></circle>'}
 s+='<text x="'+L+'" y="'+(H-6)+'">'+D.days[0]+'</text><text x="'+(W-R-60)+'" y="'+(H-6)+'">'+D.days[n-1]+'</text></svg>';
 $('timeline').innerHTML=s+'<div class="small mut"><b style="color:var(--l2)">━</b> cells at ≥ L1 &nbsp; <b style="color:var(--l3)">╌</b> cells at ≥ L2 &nbsp; <b style="color:var(--bad)">●</b> CUSUM demotion ('+D.demotions.length+': '+D.demotions.map(m=>m.cell+' '+m.date).join(', ')+')</div>'})();
// cusum
const cc=Object.keys(D.cusum);cc.sort((a,b)=>(D.cusum[b].ledger_signals.length-D.cusum[a].ledger_signals.length)||a.localeCompare(b));
$('cusumcell').innerHTML=cc.map(c=>'<option>'+c+'</option>').join('');
function cusum(){const c=$('cusumcell').value,e=D.cusum[c];if(!e){return}const W=1000,H=240,L=40,R=10,T=14,B=26,pw=W-L-R,ph=H-T-B;
 const ws=e.windows,N=Math.max(20,...ws.map(w=>w.trace.length)),Hm=Math.max(...ws.map(w=>w.h))*1.25,X=i=>L+pw*i/(N-1),Y=v=>T+ph*(1-v/Hm);
 let s='<svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="CUSUM S against h for '+c+'">';
 const cols=['var(--l2)','var(--l3)','var(--warn)'];
 ws.forEach((w,k)=>{s+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+Y(w.h)+'" y2="'+Y(w.h)+'" stroke="var(--bad)" stroke-dasharray="5 4"/><text x="'+(W-R-90)+'" y="'+(Y(w.h)-4)+'" style="fill:var(--bad)">h = '+w.h+' ('+w.level+')</text>';
  s+='<path d="'+w.trace.map((v,i)=>(i?'L':'M')+X(i).toFixed(1)+' '+Y(v).toFixed(1)).join('')+'" fill="none" stroke="'+cols[k%3]+'" stroke-width="2"/>';
  if(w.signal){const i=w.trace.length-1;s+='<circle cx="'+X(i)+'" cy="'+Y(w.trace[i])+'" r="5" fill="var(--bad)"><title>signal</title></circle>'}});
 $('cusum').innerHTML=s+'<text x="'+L+'" y="'+(H-6)+'">monitored labelled outcomes →</text></svg>';
 $('cusumnote').innerHTML=ws.map((w,k)=>'Window '+(k+1)+': from '+w.start+', level '+w.level+', p0 '+w.p0+' → p1 '+w.p1+', '+w.trace.length+' outcomes'+(w.signal?', <b class="fail">signal</b>':', no signal')).join(' · ')+
  (e.ledger_signals.length?'<br>Ledger CUSUM_SIGNAL entries: '+e.ledger_signals.map(s=>'seq '+s.seq+' (S '+s.s+' ≥ h '+s.h+', n '+s.n+')').join('; '):'')}
$('cusumcell').onchange=cusum;cusum();
// impact (same formula as tenure.dashboard.impact)
const I=D.impact_inputs,A={...P.assumptions};
function impact(a){const sc=30/I.days,V=I.cases*sc,Au=I.autonomous*sc,Ad=I.audited*sc,er=1-I.observed_agree/I.observed,hs=a.handle_minutes*60;
 const hc=V*a.handle_minutes/60*a.wage_eur_per_hour,sv=(Au-Ad)*a.handle_minutes/60*a.wage_eur_per_hour,ec=Au*er*a.error_cost_eur,inf=V*a.gpu_eur_per_hour/I.measured_cases_per_hour,sh=Au/V,mr=(1-sh)*hs+sh*I.agent_seconds;
 return{volume:V,error_rate:er,human_only_cost:hc,human_saved:sv,error_cost:ec,inference_cost:inf,net:sv-ec-inf,escalation_offload_pct:100*(Au-Ad)/V,autonomous_share_pct:100*sh,latency_reduction_pct:100*(1-mr/hs),mean_resolution_s:mr}}
const SL=[['handle_minutes','Human handling time per case (min)',1,15,0.5],['wage_eur_per_hour','Loaded wage (€/h)',10,50,1],['error_cost_eur','Cost of one agent error (€)',5,100,1],['gpu_eur_per_hour','GPU price (€/h)',0.5,10,0.5]];
function kp(){const r=impact(A);$('kpis').innerHTML=[['Net saving / month','€'+fmt(r.net),'vs. people handling every case (€'+fmt(r.human_only_cost)+')'],['Human escalation offload',fmt(r.escalation_offload_pct,1)+'%','of cases need no person, after audits'],['Mean time to resolution',fmt(r.latency_reduction_pct,1)+'% faster','from '+fmt(A.handle_minutes*60)+' s to '+fmt(r.mean_resolution_s)+' s'],['Error cost / month','€'+fmt(r.error_cost),fmt(r.error_rate*100,1)+'% observed disagreement']].map(k=>'<div class="card" style="margin:0"><div class="small mut">'+k[0]+'</div><div class="kpi">'+k[1]+'</div><div class="small mut">'+k[2]+'</div></div>').join('')}
$('sliders').innerHTML='';for(const [k,t,lo,hi,st] of SL){const w=el('div'),i=el('input',{type:'range',min:lo,max:hi,step:st,value:A[k],'aria-label':t}),l=el('label',{},t+': <b>'+A[k]+'</b> <span class="tag">ASSUMPTION</span>');i.oninput=()=>{A[k]=+i.value;l.querySelector('b').textContent=(+A[k]).toFixed(2).replace(/\.?0+$/,'');kp()};w.append(l,i);$('sliders').append(w)}
kp();
$('impactnote').innerHTML='Scaled to 30 days from the recorded <b>'+D.agent+'</b> run ('+fmt(I.cases)+' cases over '+I.days+' days: '+fmt(I.autonomous)+' decided by the agent, '+fmt(I.audited)+' of those audited by a person). Relative to the team’s own practice, priced with the assumptions below. Not a claim about real Olist economics.';
$('assume').innerHTML='<b>Measured:</b> case volumes, autonomous and audited counts, observed agreement ('+I.observed_agree+'/'+I.observed+') from the mock-agent run; throughput '+I.measured_cases_per_hour+' cases/h from the live Gemma replay on a rate-limited shared endpoint (an upper bound on cost per case, not a capacity figure); agent latency '+I.agent_seconds+' s median from the S2 throughput spike. <b>Assumed (sliders):</b> handling time (Triangular 3–10 min, mean 6.3), wage, error cost (the missed-escalation cost of cost.py), GPU price. Audited cases still take a person’s time; inference is charged on every case.'
</script></body></html>
"""

if __name__ == "__main__":
    sys.exit(main())
