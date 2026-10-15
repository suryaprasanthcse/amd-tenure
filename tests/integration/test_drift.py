"""Drift scenarios, the policy-update procedure, the global alarm and the ladder sensitivity table
(master §6.5, §6.7, §17.2, §17.3, I4; RUNBOOK T05)."""
import json
import math
from dataclasses import replace
from datetime import date, timedelta

import pandas as pd
import pytest
from typer.testing import CliRunner

from tenure.cli import app
from tenure.domain import Action, CaseType, Level, unit_hash
from tenure.humans import PolicySchedule, label_case
from tenure.policy import POLICY_V1, POLICY_V2, resolve
from tenure.runs import Drift, LiveRun, PolicyUpdate, ladder_config, ladder_from_run
from tenure.schema import AgentDecision
from tests.fixtures.ladder_fx import cell_case, make_ladder, oracle
from tests.fixtures.ladder_world import RUN_ID, build_world
from tests.fixtures.replay_fake import ROOT, SEED, config

pytestmark = [pytest.mark.integration, pytest.mark.gate_g05]

DRIFT_AT = date(2018, 8, 1)
V1_ONLY = PolicySchedule()
V2_FROM_AUG = PolicySchedule(((date.min, POLICY_V1), (DRIFT_AT, POLICY_V2)))


def p1_case(i: int, d: date, case_type: CaseType = CaseType.DELAY):
    """A delivered order two days late with EUR 15 freight: Policy v1 refunds the freight, v2 a flat EUR 5 (P1)."""
    return cell_case(case_type, "V2", i, contact_date=d, order_status="delivered", days_late=2,
                     days_since_delivery=3, days_past_estimate=2, order_total_cents=3_000, items_total_cents=1_500,
                     freight_total_cents=1_500, repeat_customer=False, photo_evidence=True)


def seed_cell(lad, make, schedule, target: Level, start: int = 0):
    """Promote a cell with v1-period outcomes (June 2018) through observe/evaluate until it reaches target."""
    i = start
    while True:
        for _ in range(50):
            c = make(i, date(2018, 6, 1) + timedelta(days=(i - start) % 30))
            lad.observe(c, oracle(c), label_case(c, schedule, SEED).primary)
            i += 1
        lad.evaluate()
        st = lad.state(c.cell)
        if st.level >= target:
            return st
        assert i - start < 5_000, f"{c.cell} did not reach {target.name}"


def delay_stream(per_day: int = 20, days: int = 62):
    return [p1_case(100_000 + k, date(2018, 7, 1) + timedelta(days=k // per_day)) for k in range(per_day * days)]


def run_delay(schedule, drift=None):
    lad = make_ladder(["DELAY|V2"], ceilings={"DELAY": Level.L2})
    st = seed_cell(lad, p1_case, V1_ONLY, Level.L2)
    assert st.level is Level.L2 and st.monitor is not None
    arl1 = st.monitor.d.arl1
    window = st.window_id
    cases = delay_stream()
    humans = {c.case_id: label_case(c, schedule, SEED).primary for c in cases}
    live = LiveRun(config(), cases=cases, humans=humans, agent=oracle, ladder=lad, drift=drift)
    return lad, live, live.run(), arl1, window


def test_drift_policy_demotes():
    lad, live, summary, arl1, window = run_delay(V2_FROM_AUG, Drift("D-POLICY", DRIFT_AT, {"policy": "v2"}))
    kinds = [e.kind for e in live.events]
    assert kinds.count("DRIFT_INJECTED") == 1 and kinds.index("DRIFT_INJECTED") < kinds.index("CUSUM_SIGNAL")
    inj = live.events[kinds.index("DRIFT_INJECTED")].payload
    assert inj == {"scenario": "D-POLICY", "at_date": "2018-08-01", "detail": {"policy": "v2"}}
    demotions = summary["demotions"]
    assert len(demotions) >= 1
    first = demotions[0]
    assert (first["cell"], first["from"], first["to"], first["reason"]) == ("DELAY|V2", "L2", "L1", "CUSUM")
    assert date.fromisoformat(first["date"]) >= DRIFT_AT  # quiet before the injection
    idx = [r["case_id"] for r in live.routes].index(first["case_id"])
    audits = sum(r["audited"] for r in live.routes[: idx + 1]
                 if r["cell"] == "DELAY|V2" and date.fromisoformat(r["date"]) >= DRIFT_AT)
    assert 1 <= audits <= 3 * arl1
    st = lad.state("DELAY|V2")
    assert st.window_id >= window + 1  # I4: the window was reset at the signal
    after = sum(r["observed"] for r in live.routes[idx + 1:] if r["cell"] == "DELAY|V2")
    if len(demotions) == 1:
        assert st.level is Level.L1 and st.n == after  # only post-signal evidence


def test_drift_none_quiet():
    lad, live, summary, _, _ = run_delay(V1_ONLY)
    assert summary["demotions"] == [] and not [e for e in live.events if e.kind == "CUSUM_SIGNAL"]
    assert lad.state("DELAY|V2").level is Level.L2
    assert summary["days"] == 62 and summary["global_alarm"] is None


def test_policy_update_procedure():
    """§17.3: POLICY_VERSION with P1 changed → cells using P1 capped at L1, windows reset; others untouched;
    re-qualification uses only v2 labels, and the agent decides under v2 from the update."""
    lad = make_ladder(["DELAY|V2", "NOT_RECEIVED|V2", "POSITIVE|V2"], ceilings={"DELAY": Level.L2,
                                                                                "NOT_RECEIVED": Level.L2,
                                                                                "POSITIVE": Level.L2})
    seed_cell(lad, p1_case, V1_ONLY, Level.L2)
    seed_cell(lad, lambda i, d: replace(p1_case(i, d, CaseType.NOT_RECEIVED), order_status="shipped",
                                        days_late=None, days_since_delivery=None), V1_ONLY, Level.L2, 50_000)
    seed_cell(lad, lambda i, d: p1_case(i, d, CaseType.POSITIVE), V1_ONLY, Level.L2, 60_000)
    before = {cell: (st.level, st.window_id) for cell, st in lad.cells.items()}
    cases = [p1_case(200_000 + k, date(2018, 7, 20) + timedelta(days=k // 30)) for k in range(900)]
    humans = {c.case_id: label_case(c, V2_FROM_AUG, SEED).primary for c in cases}
    update = PolicyUpdate(DRIFT_AT, ("P1",), "a" * 64, "v2", lambda c: oracle(c, POLICY_V2))
    live = LiveRun(config(), cases=cases, humans=humans, agent=oracle, ladder=lad, policy_update=update)
    summary = live.run()
    kinds = [e.kind for e in live.events]
    assert "POLICY_VERSION" in kinds
    pv = live.events[kinds.index("POLICY_VERSION")]
    assert pv.payload == {"policy_id": "v2", "policy_sha": "a" * 64, "changed_clauses": ["P1"]}
    capped = [e.payload for e in live.events if e.kind == "LEVEL_CHANGE" and e.payload["reason"] == "POLICY_CHANGE"]
    assert [(p["cell"], p["from"], p["to"]) for p in capped] == [("DELAY|V2", "L2", "L1")]
    st = lad.state("DELAY|V2")
    assert st.window_id == before["DELAY|V2"][1] + 1
    upd = next(i for i, r in enumerate(live.routes) if date.fromisoformat(r["date"]) >= DRIFT_AT)
    assert st.n == sum(r["observed"] for r in live.routes[upd:] if r["cell"] == "DELAY|V2")  # v2 labels only
    assert all(r["agent_policy"] == ("v2" if date.fromisoformat(r["date"]) >= DRIFT_AT else "v1")
               for r in live.routes)
    for cell in ("NOT_RECEIVED|V2", "POSITIVE|V2"):
        assert (lad.state(cell).level, lad.state(cell).window_id) == before[cell]
    assert summary["policy_update"] == {"date": "2018-08-01", "policy_id": "v2", "changed_clauses": ["P1"]}


def _alarm_world(alarm: bool):
    types = (CaseType.POSITIVE, CaseType.DAMAGED, CaseType.WRONG_ITEM, CaseType.MISSING_ITEM)
    lad = make_ladder([f"{t.value}|V1" for t in types] + ["SELLER_SERVICE|V1"],
                      ceilings={t.value: Level.L2 for t in types})

    def make(t):
        return lambda i, d: cell_case(t, "V1", i, contact_date=d, order_status="delivered", days_late=0,
                                      days_since_delivery=3, days_past_estimate=0, photo_evidence=True)

    def agent(c):
        late = c.contact_date >= date(2018, 7, 11)
        if c.case_type is not CaseType.SELLER_SERVICE and unit_hash("alarm", c.case_id) < (0.09 if late else 0.02):
            return AgentDecision(Action.ESCALATE, None, False, "E6", 900_000, "")
        return oracle(c)

    for k, t in enumerate(types):
        i = 300_000 + 10_000 * k
        while lad.state(f"{t.value}|V1").level < Level.L2:
            for _ in range(50):
                c = make(t)(i, date(2018, 6, 1))
                lad.observe(c, agent(c), resolve(c))
                i += 1
            lad.evaluate()
    cases = [make(t)(400_000 + 1_000 * j + n, date(2018, 7, 1) + timedelta(days=j))
             for j in range(50) for n, t in enumerate(types * 25)]
    cases += [make(CaseType.SELLER_SERVICE)(500_000 + k, date(2018, 8, 1) + timedelta(days=k // 20))
              for k in range(200)]
    humans = {c.case_id: resolve(c) for c in cases}
    live = LiveRun(config(), cases=cases, humans=humans, agent=agent, ladder=lad, global_alarm=alarm)
    return lad, live, live.run()


def test_global_alarm_freezes_promotions_not_levels():
    lad, live, summary = _alarm_world(True)
    kinds = [e.kind for e in live.events]
    assert "GLOBAL_ALARM" in kinds and summary["global_alarm"] is not None
    k = kinds.index("GLOBAL_ALARM")
    alarm = live.events[k].payload
    assert set(alarm) == {"s", "h", "outcomes_monitored"} and float(alarm["s"]) >= float(alarm["h"])
    assert date.fromisoformat(summary["global_alarm"]["date"]) < date(2018, 8, 1)  # before the L0 cell's cases
    later = live.events[k + 1:]
    assert not [e for e in later if e.kind == "LEVEL_CHANGE" and e.payload["reason"] == "PROMOTE"]
    signalled = set()
    for e in later:  # the alarm itself never demotes: only a cell's own CUSUM signal does
        if e.kind == "CUSUM_SIGNAL":
            signalled.add(e.payload["cell"])
        if e.kind == "LEVEL_CHANGE":
            assert e.payload["reason"] == "CUSUM" and e.payload["cell"] in signalled
    for cell, st in lad.cells.items():
        if cell != "SELLER_SERVICE|V1" and cell not in signalled and not any(
                e.kind == "CUSUM_SIGNAL" and e.payload["cell"] == cell for e in live.events):
            assert st.level is Level.L2
    assert lad.state("SELLER_SERVICE|V1").level is Level.L0 and lad.assess("SELLER_SERVICE|V1").passed
    assert live.frozen

    lad2, _live2, summary2 = _alarm_world(False)  # control: without the alarm the same evidence promotes
    assert summary2["global_alarm"] is None and lad2.state("SELLER_SERVICE|V1").level >= Level.L1


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return build_world(tmp_path_factory.mktemp("ladder_world"))


def invoke(world, *args):
    return CliRunner().invoke(app, [*args, "--config", str(ROOT / "tenure.yaml")], env=world["env"])


def test_sensitivity_table(world):
    res = invoke(world, "ladder", "sensitivity", "--run", RUN_ID)
    assert res.exit_code == 0, res.output
    md_path, js_path = world["run_dir"] / "sensitivity.md", world["run_dir"] / "sensitivity.json"
    first_md, first_js = md_path.read_bytes(), js_path.read_bytes()
    rep = json.loads(first_js)
    cfg = config()
    g = cfg.ladder.gates
    rows = rep["rows"]
    assert len(rows) == 12
    assert {r["max_detect_days"] for r in rows} == {30, 60, 90, "inf"}
    assert (rows[0]["delta_l2"], rows[0]["delta_l3"]) == (g["L2"].delta, g["L3"].delta)
    assert len({(r["delta_l2"], r["delta_l3"]) for r in rows}) == 3
    frame, humans = world["frame"], world["humans"]
    for r in rows:
        dmax = math.inf if r["max_detect_days"] == "inf" else float(r["max_detect_days"])
        lcfg = ladder_config(cfg, max_detect_days=dmax, deltas={Level.L2: r["delta_l2"], Level.L3: r["delta_l3"]})
        lad, _ = ladder_from_run(world["run_dir"], cfg, frame, humans, lcfg)
        assert r["ge_l2"] == sorted(c for c, s in lad.cells.items() if s.level >= Level.L2)
        assert r["ge_l3"] == sorted(c for c, s in lad.cells.items() if s.level >= Level.L3)
        assert set(r["ge_l3"]) <= set(r["ge_l2"])
    default = next(r for r in rows if r["max_detect_days"] == 60 and r["delta_l2"] == g["L2"].delta)
    state = json.loads((world["run_dir"] / "ladder_state.json").read_text(encoding="utf-8"))
    assert sorted(c for c, v in state["cells"].items() if Level[v["level"]] >= Level.L2) == default["ge_l2"]
    assert all({"level", "n", "x", "blockers", "ceiling"} <= set(v) for v in state["cells"].values())
    loosest = next(r for r in rows if r["max_detect_days"] == "inf" and r["delta_l2"] == g["L2"].delta)
    assert loosest["ge_l2"], "the synthetic world must promote at least one cell to L2 with D_max = inf"
    text = first_md.decode("utf-8")
    assert "| δ L2 / L3 | D_max (days) | cells ≥ L2 | cells ≥ L3 |" in text
    body = [line for line in text.splitlines() if line.startswith("| 0.")]
    assert len(body) == 12
    for line, r in zip(body, rows):
        cells_ = [s.strip() for s in line.strip("|").split("|")]
        assert cells_[1] == str(r["max_detect_days"])
        assert int(cells_[2]) == len(r["ge_l2"]) and int(cells_[3]) == len(r["ge_l3"])
    again = invoke(world, "ladder", "sensitivity", "--run", RUN_ID)
    assert again.exit_code == 0, again.output
    assert md_path.read_bytes() == first_md and js_path.read_bytes() == first_js  # deterministic


def test_live_cli_d_policy(world):
    invoke(world, "ladder", "sensitivity", "--run", RUN_ID)
    out = world["runs"] / "L-test"
    res = invoke(world, "live", "--from-run", RUN_ID, "--drift", "policy", "--agent", "mock", "--record", str(out))
    assert res.exit_code == 0, res.output
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "live" and manifest["seed_run"] == RUN_ID and manifest["drift"] == "D-POLICY"
    assert manifest["agent"] == "mock-oracle-v1"  # never reported as a Gemma result
    live_ids = set(world["frame"].loc[world["frame"]["split"] == "live", "case_id"])
    assert summary["cases"] == len(live_ids)
    routes = [json.loads(line) for line in (out / "routes.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {r["case_id"] for r in routes} == live_ids
    events = [json.loads(line) for line in (out / "ladder_events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["kind"] for e in events].count("DRIFT_INJECTED") == 1
    state = json.loads((world["run_dir"] / "ladder_state.json").read_text(encoding="utf-8"))
    assert summary["levels_at_start"] == {c: v["level"] for c, v in state["cells"].items()}
    drift_humans = pd.read_parquet(world["derived"] / "humans_drift.parquet")
    assert summary["humans"] == "humans_drift.parquet" and len(drift_humans) == len(live_ids)
    bad = invoke(world, "live", "--from-run", "R-none", "--drift", "policy", "--record", str(out / "x"))
    assert bad.exit_code != 0
