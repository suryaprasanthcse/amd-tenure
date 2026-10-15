"""Runs persist to the ledger (master §16.2, §16.5, §19.1, I8): replay entries in order, rebuild of the ladder from
DECISION entries, resumption without duplicates, and the live simulation's ladder events."""
import asyncio
import json
from datetime import date, timedelta
from types import MappingProxyType

import pytest

from tenure.domain import Action, Level, Resolution
from tenure.ladder import DEFAULT_GATES, LadderConfig, LevelGate
from tenure.ledger import Ledger
from tenure.runs import Drift, LadderBridge, LiveRun, ReplayRun, decision_records, rebuild_ladder
from tenure.store import OutcomeStore
from tests.fixtures.ladder_fx import HUMAN, as_agent, cell_case, drive_to, make_ladder
from tests.fixtures.replay_fake import FAKE_ENV, ROOT, FakeLLM, config, derived_cases, primary_humans

pytestmark = [pytest.mark.integration, pytest.mark.gate_g06]

RUN = "R-ledger"
# Low L1 gate so a 200-case fake run promotes some cells and the ledger carries LEVEL_CHANGE entries.
EASY = LadderConfig(gates=MappingProxyType({**DEFAULT_GATES, Level.L1: LevelGate(3, 0.50, 0.50, False, False, 0)}),
                    bootstrap_b=200)


def ladder_for(cases):
    return make_ladder(sorted({c.cell for c in cases}), cfg=EASY)


def make_run(tmp_path, cases, ledger, run_id=RUN, cls=ReplayRun, ladder=None):
    humans = primary_humans(cases)
    bridge = LadderBridge(ladder or ladder_for(cases), humans)
    return cls(config(), "token_factory", "replay", root=ROOT, env=FAKE_ENV, cases=cases, humans=humans,
               store=OutcomeStore(tmp_path / "o.db"), run_dir=tmp_path / "runs" / run_id,
               transport=FakeLLM(cases).transport(), concurrency=8, tick_k=50, ladder=bridge, ledger=ledger), bridge


def kinds(led):
    return [k for (k,) in led.conn.execute("SELECT kind FROM ledger ORDER BY seq")]


def state(lad):
    return {cell: (s.level, s.ceiling, s.pinned, s.window_id, s.level_entry_n, s.pass_streak, list(s.agree),
                   list(s.valid), [tuple(x) for x in s.comps], None if s.monitor is None else
                   (s.monitor.s, s.monitor.n, s.monitor.last_zero))
            for cell, s in sorted(lad.cells.items())}


@pytest.fixture
def cases(tmp_path):
    return derived_cases(tmp_path, 200)


def test_run_writes_ledger(tmp_path, cases):
    led = Ledger(tmp_path / "ledger.db")
    run, bridge = make_run(tmp_path, cases, led)
    asyncio.run(run.run())
    ks = kinds(led)
    assert ks[:4] == ["CONFIG", "POLICY_VERSION", "MODEL_PROFILE", "RUN_STARTED"] and ks[-1] == "RUN_FINISHED"
    assert ks.count("DECISION") == 200 and ks.count("RUN_STARTED") == ks.count("RUN_FINISHED") == 1
    assert "LEVEL_CHANGE" in ks
    assert set(ks[4:-1]) <= {"DECISION", "LEVEL_CHANGE", "CUSUM_SIGNAL"}
    # each tick's ladder events follow that tick's decisions: a LEVEL_CHANGE sits after a multiple of tick_k
    for i, k in enumerate(ks):
        if k == "LEVEL_CHANGE":
            assert ks[:i].count("DECISION") % 50 == 0
    assert led.verify().ok
    recs = [json.loads(line) for line in run.decisions_path.read_text(encoding="utf-8").splitlines()]
    assert decision_records(led, RUN) == recs  # the ledger holds exactly the committed DECISION records
    changes = [json.loads(p) for (p,) in led.conn.execute("SELECT payload FROM ledger WHERE kind='LEVEL_CHANGE'")]
    assert changes == [e.payload for e in bridge.events if e.kind == "LEVEL_CHANGE"]
    cfg_row = json.loads(led.conn.execute("SELECT payload FROM ledger WHERE kind='CONFIG'").fetchone()[0])
    assert cfg_row["config_sha"] == run.manifest["config_sha"]
    started = json.loads(led.conn.execute("SELECT payload FROM ledger WHERE kind='RUN_STARTED'").fetchone()[0])
    assert started["run_id"] == RUN and started["run_key"] == run.run_key
    finished = json.loads(led.conn.execute("SELECT payload FROM ledger WHERE kind='RUN_FINISHED'").fetchone()[0])
    assert finished["run_id"] == RUN and finished["committed"] == 200 and finished["snapshot_sha"] == run.snapshot_sha()
    actors = dict(led.conn.execute("SELECT kind, actor FROM ledger GROUP BY kind").fetchall())
    assert actors["DECISION"] == "agent" and actors["LEVEL_CHANGE"] == "ladder" and actors["CONFIG"] == "system"


def test_rebuild_ladder_from_ledger(tmp_path, cases):
    led = Ledger(tmp_path / "ledger.db")
    run, bridge = make_run(tmp_path, cases, led)
    asyncio.run(run.run())
    rebuilt = ladder_for(cases)
    events = rebuild_ladder(led, RUN, cases, primary_humans(cases), rebuilt, 50)
    assert state(rebuilt) == state(bridge.ladder)  # §16.5: same order, same state
    assert [e.payload for e in events] == [e.payload for e in bridge.events]
    with pytest.raises(ValueError):
        rebuild_ladder(led, "R-unknown", cases, primary_humans(cases), ladder_for(cases), 50)


def test_resume_from_ledger_without_duplicates(tmp_path, cases):
    class Crashing(ReplayRun):
        def commit(self, i, case, outcome):
            if self.committed == 120:
                raise RuntimeError("injected crash")
            super().commit(i, case, outcome)

    led = Ledger(tmp_path / "ledger.db")
    crashing, _ = make_run(tmp_path, cases, led, cls=Crashing)
    with pytest.raises(RuntimeError):
        asyncio.run(crashing.run())
    flushed = kinds(led).count("DECISION")
    assert flushed == 100  # only whole ticks are persisted
    resumed, bridge = make_run(tmp_path, cases, led)
    asyncio.run(resumed.run())
    assert resumed.resumed_from == 100
    ks = kinds(led)
    assert ks.count("DECISION") == 200 and ks.count("RUN_STARTED") == 2 and ks.count("RUN_FINISHED") == 1
    ids = [r["case_id"] for r in decision_records(led, RUN)]
    assert len(ids) == len(set(ids)) == 200 and led.verify().ok

    clean_led = Ledger(tmp_path / "clean.db")
    clean, clean_bridge = make_run(tmp_path, cases, clean_led, run_id="R-clean")
    asyncio.run(clean.run())
    assert state(bridge.ladder) == state(clean_bridge.ladder)  # a resumed ladder matches an unbroken run


def test_live_run_persists_ladder_events(tmp_path):
    a = cell_case(i=0)
    lad = make_ladder([a.cell])
    drive_to(lad, a, Level.L1)
    stream = [cell_case(i=10_000 + k, contact_date=date(2018, 7, 1) + timedelta(days=k // 20)) for k in range(400)]
    bad = as_agent(Resolution(Action.REFUND_FULL, None, False, "F1"))
    humans = {c.case_id: HUMAN for c in stream}
    led = Ledger(tmp_path / "ledger.db")
    live = LiveRun(config(), cases=stream, humans=humans, agent=lambda c: bad, ladder=lad,
                   drift=Drift("D-MODEL", date(2018, 7, 5), {"note": "test"}), ledger=led, run_id="L-ledger")
    live.run()
    ks = kinds(led)
    assert ks[0] == "RUN_STARTED" and ks[-1] == "RUN_FINISHED"
    assert "DRIFT_INJECTED" in ks and "CUSUM_SIGNAL" in ks and "LEVEL_CHANGE" in ks
    body = [k for k in ks if k not in ("RUN_STARTED", "RUN_FINISHED")]
    assert body == [e.kind for e in live.events]  # every ladder event, in order
    assert led.verify().ok
