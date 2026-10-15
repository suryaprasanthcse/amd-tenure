"""Autonomy ladder: promotion gates, confirm ticks, ceilings, overrides (master §0.5 I1–I3, §4.5, §6.6, §7, R.9)."""
import random
from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tenure.ladder import AUDIT_RATE, DEFAULT_GATES, Ladder, LadderConfig, LevelGate, assess, case_ceiling

from tenure.cost import CostParams
from tenure.domain import Action, CaseType, Level, Resolution
from tenure.schema import AgentDecision
from tenure.stats import Baseline
from tests.fixtures.cases import make_case
from tests.fixtures.ladder_fx import BAD, GOOD, HUMAN, cell_case, drive_to, make_ladder

pytestmark = [pytest.mark.unit, pytest.mark.gate_g05]


# --- Appendix T (verbatim) ---------------------------------------------------------------------------------------

def test_ladder_promotes_demotes_and_respects_ceilings():
    rng = random.Random(10)
    cfg = LadderConfig(bootstrap_b=300, max_detect_days=60.0)
    cell = "DELAY|V1"
    lad = Ladder(cfg, CostParams(), {cell: Baseline(900, 1000, "cell")}, {cell: 120.0}, {cell: 300.0})
    template = make_case(rng, 0, CaseType.DELAY)
    from dataclasses import replace
    c = replace(template, value_band="V1", order_total_cents=1_000, items_total_cents=900, freight_total_cents=100,
                fraud_flag=False, legal_threat=False)
    human = Resolution(Action.DENY, None, False, "D2")
    good = AgentDecision(Action.DENY, None, False, "D2", 900_000, "")
    levels = []
    for i in range(1_200):
        lad.observe(c, good if rng.random() < 0.97 else AgentDecision(Action.NO_ACTION, None, False, "N1", 0, ""), human)
        if i % 50 == 49:
            lad.evaluate()
            levels.append(lad.state(cell).level)
    assert levels[-1] is Level.L3 and levels == sorted(levels)
    assert lad.effective_level(replace(c, fraud_flag=True)) is Level.L1
    events = []
    bad = AgentDecision(Action.REFUND_FULL, None, False, "F1", 900_000, "")
    for _ in range(400):
        events += lad.observe(c, bad if rng.random() < 0.5 else good, human)
        if lad.state(cell).level < Level.L3:
            break
    kinds = [e.kind for e in events]
    assert kinds == ["CUSUM_SIGNAL", "LEVEL_CHANGE"] and lad.state(cell).level is Level.L1
    assert lad.state(cell).n == 0
    with pytest.raises(ValueError):
        lad.override(cell, "demote", "ops", "test", Level.L2)
    lad.override(cell, "demote", "ops", "test", Level.L0)
    assert lad.state(cell).pinned and "PINNED" in assess(lad.state(cell), Baseline(900, 1000, "cell"), 120.0, cfg,
                                                          CostParams()).blockers


def test_monitor_power_blocks_low_volume_cells():
    cfg = LadderConfig(bootstrap_b=200)
    cell = "BILLING|V2"
    lad = Ladder(cfg, CostParams(), {cell: Baseline(900, 1000, "cell")}, {cell: 0.0}, {cell: 0.5})
    rng = random.Random(12)
    from dataclasses import replace
    c = replace(make_case(rng, 0, CaseType.BILLING), value_band="V2", order_total_cents=2_000, fraud_flag=False,
                legal_threat=False)
    h = Resolution(Action.ESCALATE, None, False, "E4")
    a = AgentDecision(Action.ESCALATE, None, False, "E4", 900_000, "")
    for i in range(600):
        lad.observe(c, a, h)
        if i % 50 == 49:
            lad.evaluate()
    st = lad.state(cell)
    assert st.level is Level.L1
    assert "MONITOR_POWER" in lad.assess(cell).blockers


# --- RUNBOOK T05 ---------------------------------------------------------------------------------------------------

def test_default_gates_match_master_4_5():
    assert DEFAULT_GATES[Level.L1] == LevelGate(30, 0.10, 0.98, False, False, 0)
    assert DEFAULT_GATES[Level.L2] == LevelGate(150, 0.05, 0.98, True, True, 50)
    assert DEFAULT_GATES[Level.L3] == LevelGate(400, 0.03, 0.99, True, True, 100)
    assert dict(AUDIT_RATE) == {Level.L0: 1.0, Level.L1: 1.0, Level.L2: 0.10, Level.L3: 0.02}
    cfg = LadderConfig()
    assert (cfg.confirm_ticks, cfg.arl0_target, cfg.max_detect_days, cfg.high_value_cents) == (2, 500.0, 60.0, 50_000)


def test_confirm_ticks():
    """I1: a gate that passes once and then fails does not promote; two consecutive passes do."""
    c = cell_case()
    lad = make_ladder([c.cell])
    for _ in range(30):
        lad.observe(c, GOOD, HUMAN)
    assert lad.assess(c.cell).passed
    assert lad.evaluate() == [] and lad.state(c.cell).pass_streak == 1
    for _ in range(10):
        lad.observe(c, BAD, HUMAN)
    assert not lad.assess(c.cell).passed
    assert lad.evaluate() == [] and lad.state(c.cell).pass_streak == 0
    assert lad.state(c.cell).level is Level.L0
    for _ in range(300):
        lad.observe(c, GOOD, HUMAN)
    assert lad.evaluate() == [] and lad.state(c.cell).level is Level.L0  # one pass after the failure
    events = lad.evaluate()
    assert [e.kind for e in events] == ["LEVEL_CHANGE"] and lad.state(c.cell).level is Level.L1
    assert events[0].payload["reason"] == "PROMOTE" and lad.state(c.cell).pass_streak == 0


def test_promotes_at_most_one_level_per_tick():
    c = cell_case()
    lad = make_ladder([c.cell])
    for _ in range(1_000):
        lad.observe(c, GOOD, HUMAN)
    seen = []
    for _ in range(8):
        lad.evaluate()
        seen.append(lad.state(c.cell).level)
    # tick 1 passes once, tick 2 promotes; L2 then waits for dwell, which needs new outcomes
    assert seen == [Level.L0] + [Level.L1] * 7


def test_no_manual_promotion():
    """I2: people may demote, pin and cap; they cannot promote. Every override is an OVERRIDE event."""
    c = cell_case()
    lad = make_ladder([c.cell])
    drive_to(lad, c, Level.L2)
    with pytest.raises(ValueError):
        lad.override(c.cell, "demote", "ops", "raise it", Level.L3)
    with pytest.raises(ValueError):
        lad.override(c.cell, "demote", "ops", "same level", Level.L2)
    with pytest.raises(ValueError):
        lad.override(c.cell, "promote", "ops", "no such op", Level.L3)
    with pytest.raises(ValueError):
        lad.override(c.cell, "set_ceiling", "ops", "missing level")
    assert lad.state(c.cell).level is Level.L2
    low = cell_case(CaseType.POSITIVE, "V1", 1)
    ev = lad.override(low.cell, "set_ceiling", "ops", "a raised ceiling never raises the level", Level.L3)
    assert lad.state(low.cell).level is Level.L0
    for op, level in (("pin", None), ("unpin", None), ("set_ceiling", Level.L3), ("demote", Level.L1)):
        ev = lad.override(c.cell, op, "alice", f"reason for {op}", level)
        assert len(ev) == 1 and ev[0].kind == "OVERRIDE"
        p = ev[0].payload
        assert p["actor"] == "alice" and p["reason"] == f"reason for {op}" and p["op"] == op and p["cell"] == c.cell
        assert set(p) == {"cell", "op", "actor", "reason", "level", "from", "to", "pinned", "ceiling"}
        assert Level[p["to"]] <= Level[p["from"]]


@given(seed=st.integers(0, 10_000), level=st.sampled_from(list(Level)), fraud=st.booleans(), legal=st.booleans(),
       total=st.integers(0, 120_000))
def test_effective_level_never_exceeds_case_ceiling(seed, level, fraud, legal, total):
    """I3: effective level = min(cell level, per-case ceiling); fraud, legal threat or > EUR 500 → at most L1."""
    c = replace(make_case(random.Random(seed), seed), fraud_flag=fraud, legal_threat=legal, order_total_cents=total)
    lad = make_ladder([c.cell])
    lad.state(c.cell).level = level
    eff = lad.effective_level(c)
    ceiling = case_ceiling(c, lad.cfg.high_value_cents)
    assert eff <= ceiling and eff <= level and eff == min(level, ceiling)
    if fraud or legal or total > 50_000:
        assert ceiling is Level.L1 and eff <= Level.L1
    else:
        assert ceiling is Level.L3 and eff == level


def test_blocker_at_top_and_ceiling():
    c = cell_case()
    lad = make_ladder([c.cell])
    drive_to(lad, c, Level.L3)
    a = lad.assess(c.cell)
    assert (a.target, a.passed, a.blockers) == (None, False, ("AT_TOP",))
    o = cell_case(CaseType.OTHER, "V1", 2)
    lad2 = make_ladder([o.cell], ceilings={"OTHER": Level.L1})
    assert lad2.state(o.cell).ceiling is Level.L1
    drive_to(lad2, o, Level.L1)
    for _ in range(400):
        lad2.observe(o, GOOD, HUMAN)
    for _ in range(4):
        lad2.evaluate()
    assert lad2.state(o.cell).level is Level.L1 and lad2.assess(o.cell).blockers == ("CEILING",)


def test_blocker_valid_rate_and_n_min():
    c = cell_case()
    lad = make_ladder([c.cell])
    assert lad.assess(c.cell).blockers == ("N_MIN",)  # no evidence yet
    for i in range(100):
        lad.observe(c, None if i % 20 == 0 else GOOD, HUMAN)  # 5 invalid outputs: valid rate 0.95
    a = lad.assess(c.cell)
    assert a.blockers == ("VALID_RATE",) and lad.state(c.cell).n_valid == 95
    assert lad.state(c.cell).x == 95  # I5: an invalid output is a disagreement


def test_blocker_dwell_after_promotion():
    c = cell_case()
    lad = make_ladder([c.cell])
    for _ in range(200):
        lad.observe(c, GOOD, HUMAN)
    lad.evaluate()
    lad.evaluate()
    st_ = lad.state(c.cell)
    assert st_.level is Level.L1 and st_.level_entry_n == 200
    assert lad.assess(c.cell).blockers == ("DWELL",)
    for _ in range(50):
        lad.observe(c, GOOD, HUMAN)
    assert lad.assess(c.cell).passed


def test_blocker_cost_runs_last():
    c = cell_case(CaseType.DELAY, "V3", 3, order_total_cents=40_000, items_total_cents=38_000,
                  freight_total_cents=2_000)
    lad = make_ladder([c.cell], hh_cost=0.0)
    for i in range(400):
        lad.observe(c, BAD if i % 33 == 0 else GOOD, HUMAN)
        if i % 50 == 49:
            lad.evaluate()
    assert lad.state(c.cell).level is Level.L1
    a = lad.assess(c.cell)
    assert a.blockers == ("COST",) and a.cost is not None and a.cost.ucb > a.cost.threshold


def test_promotion_evidence_records_every_number():
    c = cell_case()
    lad = make_ladder([c.cell])
    events = drive_to(lad, c, Level.L2)
    promo = [e for e in events if e.kind == "LEVEL_CHANGE" and e.payload["to"] == "L2"]
    assert len(promo) == 1
    p = promo[0].payload
    assert (p["cell"], p["from"], p["reason"]) == (c.cell, "L1", "PROMOTE")
    ev = p["evidence"]
    for key in ("blockers", "n", "x", "lower", "baseline", "baseline_scope", "delta", "threshold",
                "newcombe_diff_lower", "cost_mean_cents", "cost_ucb_cents", "cost_threshold_cents", "cusum_p0",
                "cusum_p1", "cusum_h", "arl0", "arl1", "detect_days"):
        assert key in ev, key
    assert ev["blockers"] == [] and isinstance(ev["lower"], str) and ev["delta"] == "0.050000"
    st_ = lad.state(c.cell)
    assert st_.monitor is not None and st_.level_entry_n == st_.n


def test_observe_without_monitor_never_signals():
    c = cell_case()
    lad = make_ladder([c.cell])
    for _ in range(500):
        assert lad.observe(c, BAD, HUMAN) == []
    assert lad.state(c.cell).level is Level.L0 and lad.state(c.cell).n == 500


def test_cell_state_takes_ceiling_and_volume():
    lad = Ladder(LadderConfig(), CostParams(), {}, {}, {"BILLING|V1": 4.0},
                 {"BILLING": Level.L1, "OTHER|V2": Level.L0})
    assert lad.state("BILLING|V1").ceiling is Level.L1 and lad.state("BILLING|V1").volume_per_day == 4.0
    assert lad.state("OTHER|V2").ceiling is Level.L0 and lad.state("POSITIVE|V1").ceiling is Level.L3
    assert lad.state("POSITIVE|V1").volume_per_day == 0.0
