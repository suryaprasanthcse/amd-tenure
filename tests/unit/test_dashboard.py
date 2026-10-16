"""Recorded dashboard (T07): fixture vs ledger, verification badge, impact formula, self-contained page."""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from tenure import dashboard as dash
from tenure.governance import read_jsonl



@pytest.fixture(scope="module")
def data():
    return dash.load_data()


def test_fixture_reproduces_ledger_cusum_signals(data):
    """The re-run monitors end exactly where the ledger's CUSUM_SIGNAL entries say (same S, h and outcome count)."""
    sigs = [e["payload"] for e in read_jsonl(dash.LEDGER) if e["kind"] == "CUSUM_SIGNAL"]
    assert len(sigs) == 3
    for p in sigs:
        signalled = [w for w in data["cusum"][p["cell"]]["windows"] if w["signal"]]
        assert any(len(w["trace"]) == p["outcomes_monitored"] and f"{w['trace'][-1]:.4f}" == p["s"]
                   and f"{w['h']:.4f}" == p["h"] for w in signalled), p


def test_fixture_final_levels_match_run_finished(data):
    fin = next(e["payload"] for e in read_jsonl(dash.LEDGER) if e["kind"] == "RUN_FINISHED" and "levels" in e["payload"])
    for cell, lvl in fin["levels"].items():
        row = data["levels"].get(cell)
        assert (row[-1] if row else 0) == int(lvl[1]), cell
    assert data["mock"] is True and data["agent"] == "mock-oracle-v1"


def test_verify_sources_ok_on_committed_export():
    v = dash.verify_sources(db=dash.ROOT / "no-such.db")
    assert v["ok"] and v["entries"] == v["head_seq"] == 532
    assert v["sources"][1]["ok"] is None  # no local database: skipped, not failed


def test_verify_sources_detects_tampering(tmp_path):
    lines = dash.LEDGER.read_text(encoding="utf-8").splitlines()
    e = json.loads(lines[10])
    e["payload"]["tampered"] = True
    lines[10] = json.dumps(e)
    bad = tmp_path / "ledger.jsonl"
    bad.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    v = dash.verify_sources(bad, dash.HEAD, tmp_path / "no.db")
    assert not v["ok"] and v["reason"] == "PAYLOAD_SHA"


def test_impact_hand_computed():
    inp = {"days": 30, "cases": 1000, "autonomous": 400, "audited": 40, "observed": 200, "observed_agree": 190,
           "measured_cases_per_hour": 500.0, "agent_seconds": 3.0}
    r = dash.impact(inp, {"handle_minutes": 6.0, "wage_eur_per_hour": 20.0, "error_cost_eur": 10.0,
                          "gpu_eur_per_hour": 2.0})
    assert r["human_only_cost"] == pytest.approx(1000 * 6 / 60 * 20)         # 2000
    assert r["human_saved"] == pytest.approx(360 * 6 / 60 * 20)              # 720
    assert r["error_cost"] == pytest.approx(400 * 0.05 * 10)                 # 200
    assert r["inference_cost"] == pytest.approx(1000 * 2 / 500)              # 4
    assert r["net"] == pytest.approx(720 - 200 - 4)
    assert r["escalation_offload_pct"] == pytest.approx(36.0)
    assert r["latency_reduction_pct"] == pytest.approx(100 * (1 - (0.6 * 360 + 0.4 * 3) / 360))


def test_page_is_self_contained_and_labels_the_mock(data):
    html = dash.render_html(data, dash.verify_sources())
    assert not re.search(r"""(src|href)\s*=\s*["']https?://""", html) and "fetch('http" not in html
    assert "<script src" not in html and "@import" not in html
    payload = json.loads(re.search(r'id="payload" type="application/json">(.*?)</script>', html, re.S).group(1)
                         .replace("<\\/", "</"))
    assert payload["data"]["agent"] == "mock-oracle-v1" and payload["badge"]["ok"]
    assert payload["default_impact"]["net"] == pytest.approx(dash.impact(data["impact_inputs"])["net"])
    assert "not</b> a Gemma accuracy result" in html and "ASSUMPTION" in html


def test_js_impact_matches_python(data):
    """The page recomputes the same formula in JavaScript; where node is installed the two agree to 1e-9."""
    node = shutil.which("node")
    if node is None:
        return
    html = dash.render_html(data, dash.verify_sources())
    fn = re.search(r"function impact\(a\)\{.*?\n return\{.*?\}\}", html, re.S).group(0)
    script = f"const I={json.dumps(data['impact_inputs'])};{fn};console.log(JSON.stringify(impact({json.dumps(dash.DEFAULT_ASSUMPTIONS)})))"
    out = json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True, check=True).stdout)
    py = dash.impact(data["impact_inputs"])
    for k, v in out.items():
        assert v == pytest.approx(py[k], rel=1e-9), k


def test_cli_verify_and_static_page(tmp_path, capsys):
    assert dash.main(["verify"]) == 0
    assert "VERIFIED" in capsys.readouterr().out
    out = tmp_path / "d.html"
    assert dash.main(["--out", str(out)]) == 0 and out.stat().st_size > 20_000
