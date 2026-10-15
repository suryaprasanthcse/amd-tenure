"""Live routing by effective level with deterministic audits (master §7.1, §17.1, I3), and the replay wiring
(commit → ladder.observe, every tick_k → ladder.evaluate; master §7.5)."""
import asyncio
from collections import Counter
from datetime import date, timedelta

import pytest

from tenure.domain import CaseType, Level, unit_hash
from tenure.runs import LadderBridge, LiveRun, ReplayRun
from tenure.store import OutcomeStore
from tests.fixtures.ladder_fx import HUMAN, as_agent, cell_case, drive_to, make_ladder
from tests.fixtures.replay_fake import FAKE_ENV, ROOT, FakeLLM, config, derived_cases, primary_humans

pytestmark = [pytest.mark.integration, pytest.mark.gate_g05]

L2_CELL, L3_CELL = "POSITIVE|V1", "DELAY|V2"


def seeded_ladder():
    a, b = cell_case(CaseType.POSITIVE, "V1", 0), cell_case(CaseType.DELAY, "V2", 1)
    lad = make_ladder([a.cell, b.cell], ceilings={a.cell: Level.L2})
    drive_to(lad, a, Level.L2)
    drive_to(lad, b, Level.L3)
    return lad


def live_stream(n_per_cell: int = 2_500, fraud: int = 100):
    cases = []
    for k in range(n_per_cell):
        d = date(2018, 7, 1) + timedelta(days=k // 50)
        cases.append(cell_case(CaseType.POSITIVE, "V1", 10_000 + k, contact_date=d))
        cases.append(cell_case(CaseType.DELAY, "V2", 20_000 + k, contact_date=d))
    for k in range(fraud):
        cases.append(cell_case(CaseType.DELAY, "V2", 30_000 + k, contact_date=date(2018, 7, 1) + timedelta(days=k % 50),
                               fraud_flag=True))
    return cases


def run_live(lad, cases):
    humans = {c.case_id: HUMAN for c in cases}
    live = LiveRun(config(), cases=cases, humans=humans, agent=lambda c: as_agent(humans[c.case_id]), ladder=lad)
    return live, live.run()


def test_live_routing():
    lad = seeded_ladder()
    n0 = {cell: lad.state(cell).n for cell in (L2_CELL, L3_CELL)}
    cases = live_stream()
    live, summary = run_live(lad, cases)
    cfg = config()
    routes = live.routes
    assert len(routes) == len(cases) == summary["cases"]
    assert [r["case_id"] for r in routes] == [c.case_id for c in sorted(cases, key=lambda c: (c.contact_date,
                                                                                             c.case_id))]
    by_id = {c.case_id: c for c in cases}
    for r in routes:
        c = by_id[r["case_id"]]
        assert r["effective_level"] == min(Level[r["level"]], Level.L1 if c.fraud_flag else Level.L3).name  # I3
        if r["effective_level"] in ("L2", "L3"):
            rate = cfg.ladder.audit_rate[r["effective_level"]]
            assert r["decided_by"] == "agent"
            assert r["audited"] == (unit_hash(str(cfg.seed), c.case_id, "audit") < rate)
            assert r["observed"] == r["audited"]  # L2/L3 cells observe only audited cases
        else:
            assert r["decided_by"] == "human" and r["observed"] and not r["audited"]
    for cell, level in ((L2_CELL, "L2"), (L3_CELL, "L3")):
        auto = [r for r in routes if r["cell"] == cell and r["effective_level"] == level]
        share = sum(r["audited"] for r in auto) / len(auto)
        assert abs(share - cfg.ladder.audit_rate[level]) <= 0.02, (cell, share)
        observed = sum(r["observed"] for r in routes if r["cell"] == cell)
        assert lad.state(cell).n == n0[cell] + observed  # unaudited autonomous cases leave the stats alone
    fraud = [r for r in routes if by_id[r["case_id"]].fraud_flag]
    assert len(fraud) == 100 and all(r["effective_level"] == "L1" and r["decided_by"] == "human" for r in fraud)
    counts = Counter((r["decided_by"], r["observed"]) for r in routes)
    assert summary["autonomous"] == counts[("agent", True)] + counts[("agent", False)]
    assert summary["audited"] == sum(r["audited"] for r in routes)
    assert summary["observed"] == sum(r["observed"] for r in routes)
    assert summary["levels"] == {L2_CELL: "L2", L3_CELL: "L3"}

    again, _ = run_live(seeded_ladder(), cases)
    assert again.routes == routes  # deterministic audits and routing


def test_replay_run_drives_ladder(tmp_path):
    """Every commit reaches ladder.observe; ladder.evaluate runs once per tick_k, never twice on the same count."""
    cases = derived_cases(tmp_path, 200)
    humans = primary_humans(cases)
    lad = make_ladder(sorted({c.cell for c in cases}))

    class Counting(LadderBridge):
        evaluations = 0

        def evaluate(self):
            Counting.evaluations += 1
            return super().evaluate()

    bridge = Counting(lad, humans)
    run = ReplayRun(config(), "token_factory", "replay", root=ROOT, env=FAKE_ENV, cases=cases, humans=humans,
                    store=OutcomeStore(tmp_path / "o.db"), run_dir=tmp_path / "runs" / "R-lad",
                    transport=FakeLLM(cases).transport(), concurrency=8, tick_k=50, ladder=bridge)
    seen = []
    run.bus.add_sink(lambda name, data: seen.append((name, data)))
    asyncio.run(run.run())
    assert Counting.evaluations == 4
    per_cell = Counter(c.cell for c in cases)
    assert {cell: lad.state(cell).n for cell in per_cell} == dict(per_cell)
    assert all(bridge.level(cell) == lad.state(cell).level.name for cell in per_cell)
    cells = [d for name, d in seen if name == "cell"]
    assert cells and all(d["level"] == lad.state(d["cell"]).level.name for d in cells)
    assert all(d["ceiling"] == lad.state(d["cell"]).ceiling.name for d in cells)
    assert all(isinstance(d["blockers"], list) and d["blockers"] for d in cells)  # nothing passes on 200 cases
