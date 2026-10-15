"""`tenure evaluate --run <id>` on a fake-LLM replay run (master §3–§5, RUNBOOK T04): per-cell evidence,
baselines, thresholds per level, cost mean and UCB, written to var/runs/<id>/evaluation.json, deterministic."""
import asyncio
import json
from collections import Counter
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from tenure.baselines import cell_baselines, pair_table
from tenure.cli import app, humans_table
from tenure.cost import CostParams, error_components, total_cost
from tenure.domain import Action
from tenure.humans import PolicySchedule
from tenure.runs import ReplayRun
from tenure.schema import AgentDecision
from tenure.stats import wilson_cc_interval, wilson_cc_lower
from tenure.store import OutcomeStore
from tests.fixtures.frames import cases_frame
from tests.fixtures.replay_fake import FAKE_ENV, ROOT, SEED, FakeLLM, config, derived_cases, primary_humans

pytestmark = [pytest.mark.integration, pytest.mark.gate_g04]

RUN_ID = "R-eval"
N_BAD = 6


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("evaluate")
    cases = derived_cases(tmp, 200)
    bad = frozenset(sorted(c.case_id for c in cases)[:N_BAD])
    runs = tmp / "var" / "runs"
    run = ReplayRun(config(), "token_factory", "replay", root=ROOT, env=FAKE_ENV, cases=cases,
                    humans=primary_humans(cases), store=OutcomeStore(tmp / "outcomes.db"), run_dir=runs / RUN_ID,
                    transport=FakeLLM(cases, malformed=bad).transport(), concurrency=8, tick_k=50)
    asyncio.run(run.run())
    train = [replace(c, case_id=f"t{i:05d}") for i, c in enumerate(derived_cases(tmp / "train", 600))]
    frame = pd.concat([cases_frame(cases, "replay"), cases_frame(train, "train")], ignore_index=True)
    derived = tmp / "derived"
    derived.mkdir()
    frame.to_parquet(derived / "cases.parquet", index=False)
    hum = humans_table(frame, PolicySchedule(), SEED)
    hum.to_parquet(derived / "humans.parquet", index=False)
    env = {"TENURE_DERIVED_DIR": str(derived), "TENURE_RUNS_DIR": str(runs)}
    return {"cases": cases, "frame": frame, "humans": hum, "runs": runs, "env": env}


def evaluate(world, run_id: str = RUN_ID):
    return CliRunner().invoke(app, ["evaluate", "--run", run_id, "--config", str(ROOT / "tenure.yaml")],
                              env=world["env"])


def records(world) -> list[dict]:
    text = (world["runs"] / RUN_ID / "decisions.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_evaluate_run_report(world):
    res = evaluate(world)
    assert res.exit_code == 0, res.output
    path = world["runs"] / RUN_ID / "evaluation.json"
    first = path.read_bytes()
    ev = json.loads(first)
    recs = records(world)
    cfg = config()
    n_by, x_by = Counter(r["cell"] for r in recs), Counter(r["cell"] for r in recs if r["agree"])
    assert ev["run_id"] == RUN_ID and ev["records"] == 200 and ev["complete"] is True
    assert ev["overall"]["n"] == 200 and ev["overall"]["x"] == sum(r["agree"] for r in recs)
    assert ev["overall"]["valid"] == 200 - N_BAD
    assert (ev["overall"]["L_cc"], ev["overall"]["U_cc"]) == pytest.approx(
        wilson_cc_interval(ev["overall"]["x"], 200))
    assert set(ev["cells"]) == set(n_by)

    table = pair_table(world["frame"], world["humans"], CostParams(**cfg.cost.model_dump()))
    assert ev["baseline_pairs"]["pairs"] == table.pairs and ev["baseline_pairs"]["agree"] == table.agree
    p = CostParams(**cfg.cost.model_dump())
    by_id = {c.case_id: c for c in world["cases"]}
    humans = primary_humans(world["cases"])
    for cell, e in ev["cells"].items():
        n, x = n_by[cell], x_by[cell]
        assert (e["n"], e["x"]) == (n, x)
        assert e["p_hat"] == pytest.approx(x / n)
        assert e["L_cc"] == pytest.approx(wilson_cc_lower(x, n))
        b, cb = cell_baselines(table, cell, cfg.ladder.baseline_n_min)
        assert e["baseline"] == pytest.approx(b.rate) and e["scope"] == b.scope
        assert (e["baseline_x"], e["baseline_n"]) == (b.x, b.n)
        assert e["cost_baseline_cents"] == pytest.approx(cb.mean_cents) and e["cost_scope"] == cb.scope
        for level, g in cfg.ladder.gates.items():
            lv = e["levels"][level]
            assert lv["threshold"] == pytest.approx(e["baseline"] - g.delta)
            assert lv["ni_passed"] == (e["L_cc"] >= lv["threshold"])
            assert ("N_MIN" in lv["blockers"]) == (n < g.n_min)
            assert ("NON_INFERIORITY" in lv["blockers"]) == (not lv["ni_passed"])
            assert ("VALID_RATE" in lv["blockers"]) == (e["valid"] / n < g.min_valid_rate)
        if cell.split("|")[0] in cfg.ladder.ceilings:
            assert "CEILING" in e["levels"]["L2"]["blockers"]
        comps = []
        for r in recs:
            if r["cell"] != cell:
                continue
            d = r["decision"]
            agent = None if d is None else AgentDecision(Action(d["action"]), d["amount_cents"], d["requires_return"],
                                                         d["clause"], d["confidence_ppm"], "")
            comps.append(error_components(agent, humans[r["case_id"]], by_id[r["case_id"]], p))
        mean = float(total_cost(np.asarray(comps, dtype=float), p).mean())
        assert e["cost_mean_cents"] == pytest.approx(mean)
        assert 0.0 <= e["cost_mean_cents"] <= e["cost_ucb_cents"]
    assert sum(e["n"] - e["valid"] for e in ev["cells"].values()) == N_BAD  # I5: invalids stay in n

    for level in ("L1", "L2", "L3"):
        s = ev["summary"][level]
        clear = sorted(c for c, e in ev["cells"].items()
                       if not set(e["levels"][level]["blockers"]) - {"COST"})
        assert s["ni_pass"] == clear
        assert set(s["pass"]) <= set(s["ni_pass"])

    again = evaluate(world)
    assert again.exit_code == 0, again.output
    assert path.read_bytes() == first  # deterministic across two invocations


def test_evaluate_stopped_run_and_missing_run(world):
    """A run stopped before its manifest's "finished" (D-k2) is evaluated on its committed records."""
    src = world["runs"] / RUN_ID
    dst = world["runs"] / "R-stopped"
    dst.mkdir()
    manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    manifest.pop("finished")
    manifest["run_id"] = "R-stopped"
    (dst / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    lines = [line.replace(f'"run_id": "{RUN_ID}"', '"run_id": "R-stopped"')
             for line in (src / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[:120]]
    (dst / "decisions.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    res = evaluate(world, "R-stopped")
    assert res.exit_code == 0, res.output
    ev = json.loads((dst / "evaluation.json").read_text(encoding="utf-8"))
    assert ev["complete"] is False and ev["records"] == 120 and ev["planned_cases"] == 200
    assert sum(e["n"] for e in ev["cells"].values()) == 120

    missing = evaluate(world, "R-none")
    assert missing.exit_code != 0
