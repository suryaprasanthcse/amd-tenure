"""One test per row of the ladder transition table (master §7.3; I1, I2, I4; §17.3 for the policy row)."""
import pytest

from tenure.domain import CaseType, Level
from tenure.runs import CASE_TYPE_CLAUSES, apply_policy_update
from tests.fixtures.ladder_fx import BAD, GOOD, HUMAN, cell_case, drive_to, make_ladder

pytestmark = [pytest.mark.unit, pytest.mark.gate_g05]


def promote_events(events, to):
    return [e for e in events if e.kind == "LEVEL_CHANGE" and e.payload["reason"] == "PROMOTE"
            and e.payload["to"] == to]


@pytest.mark.parametrize("start,target", [(Level.L0, Level.L1), (Level.L1, Level.L2), (Level.L2, Level.L3)])
def test_row_gate_passes_twice_promotes_with_new_monitor(start, target):
    """Rows 1–3: L0→L1, L1→L2, L2→L3 on two consecutive passing evaluations, each with a new monitor."""
    c = cell_case()
    lad = make_ladder([c.cell])
    drive_to(lad, c, start)
    old = lad.state(c.cell).monitor
    events = drive_to(lad, c, target)
    st = lad.state(c.cell)
    assert st.level is target and len(promote_events(events, target.name)) == 1
    assert st.monitor is not None and st.monitor is not old and st.monitor.n == 0
    assert st.level_entry_n == st.n


def _signal(lad, c, limit=2_000):
    for _ in range(limit):
        ev = lad.observe(c, BAD, HUMAN)
        if ev:
            return ev
    raise AssertionError("no CUSUM signal")


def test_row_l1_cusum_signal_demotes_to_l0_with_window_reset():
    """Row 4 (I4): L1 + CUSUM signal → L0, empty window, new window id, no monitor."""
    c = cell_case()
    lad = make_ladder([c.cell])
    drive_to(lad, c, Level.L1)
    window = lad.state(c.cell).window_id
    ev = _signal(lad, c)
    assert [e.kind for e in ev] == ["CUSUM_SIGNAL", "LEVEL_CHANGE"]
    sig, change = ev[0].payload, ev[1].payload
    assert sig["level"] == "L1" and sig["cell"] == c.cell and float(sig["s"]) >= float(sig["h"])
    assert set(sig) == {"cell", "level", "s", "h", "outcomes_monitored", "change_point_index"}
    assert (change["from"], change["to"], change["reason"]) == ("L1", "L0", "CUSUM")
    assert change["evidence"] == {"window_reset": True}
    st = lad.state(c.cell)
    assert st.level is Level.L0 and st.n == 0 and st.agree == [] and st.valid == [] and st.comps == []
    assert st.window_id == window + 1 and st.monitor is None and st.pass_streak == 0 and st.level_entry_n == 0


@pytest.mark.parametrize("start", [Level.L2, Level.L3])
def test_row_l2_l3_cusum_signal_demotes_to_l1_with_window_reset(start):
    """Row 5 (I4): L2/L3 + CUSUM signal → L1, window reset."""
    c = cell_case()
    lad = make_ladder([c.cell])
    drive_to(lad, c, start)
    ev = _signal(lad, c)
    assert ev[1].payload["from"] == start.name and ev[1].payload["to"] == "L1"
    st = lad.state(c.cell)
    assert st.level is Level.L1 and st.n == 0 and st.monitor is None
    for _ in range(300):  # re-promotion needs fresh evidence and two passing ticks
        lad.observe(c, GOOD, HUMAN)
    assert lad.evaluate() == [] and lad.state(c.cell).level is Level.L1


def test_row_override_demote_pins_and_keeps_evidence():
    """Row 6: override demote → that level, pinned, evidence kept."""
    c = cell_case()
    lad = make_ladder([c.cell])
    drive_to(lad, c, Level.L3)
    n, x, window = lad.state(c.cell).n, lad.state(c.cell).x, lad.state(c.cell).window_id
    ev = lad.override(c.cell, "demote", "ops", "incident review", Level.L1)
    st = lad.state(c.cell)
    assert st.level is Level.L1 and st.pinned and (st.n, st.x, st.window_id) == (n, x, window)
    assert ev[0].payload["from"] == "L3" and ev[0].payload["to"] == "L1" and ev[0].payload["pinned"] is True
    for _ in range(300):
        lad.observe(c, GOOD, HUMAN)
    lad.evaluate()
    lad.evaluate()
    assert st.level is Level.L1 and "PINNED" in lad.assess(c.cell).blockers


def test_row_pin_and_unpin_freeze_and_resume_promotions():
    """Row 7: pin / unpin keep the level; promotions are frozen, then resumed."""
    c = cell_case()
    lad = make_ladder([c.cell])
    ev = lad.override(c.cell, "pin", "ops", "freeze")
    assert ev[0].payload["from"] == ev[0].payload["to"] == "L0" and lad.state(c.cell).pinned
    for _ in range(100):
        lad.observe(c, GOOD, HUMAN)
    for _ in range(3):
        assert lad.evaluate() == []
    assert lad.state(c.cell).level is Level.L0 and lad.assess(c.cell).blockers == ("PINNED",)
    ev = lad.override(c.cell, "unpin", "ops", "resume")
    assert ev[0].payload["pinned"] is False and lad.state(c.cell).level is Level.L0
    lad.evaluate()
    assert promote_events(lad.evaluate(), "L1") and lad.state(c.cell).level is Level.L1


@pytest.mark.parametrize("start,cap,expected", [(Level.L2, Level.L1, Level.L1), (Level.L1, Level.L3, Level.L1),
                                                (Level.L3, Level.L0, Level.L0)])
def test_row_set_ceiling_caps_level(start, cap, expected):
    """Row 8: set_ceiling k → min(level, k), ceiling recorded; promotion above it is blocked."""
    c = cell_case()
    lad = make_ladder([c.cell])
    drive_to(lad, c, start)
    ev = lad.override(c.cell, "set_ceiling", "ops", "cap", cap)
    st = lad.state(c.cell)
    assert st.level is expected and st.ceiling is cap and ev[0].payload["ceiling"] == cap.name
    if expected is cap:
        assert "CEILING" in lad.assess(c.cell).blockers


def test_row_policy_version_change_caps_touched_cells_and_resets_window():
    """Row 9 (§17.3): a policy change touching a cell's clauses → min(level, L1) and a window reset."""
    delay, positive, damaged = cell_case(CaseType.DELAY), cell_case(CaseType.POSITIVE, i=1), cell_case(
        CaseType.DAMAGED, i=2)
    lad = make_ladder([delay.cell, positive.cell, damaged.cell])
    for c in (delay, positive):
        drive_to(lad, c, Level.L2)
    drive_to(lad, damaged, Level.L1)
    before = {c.cell: (lad.state(c.cell).level, lad.state(c.cell).n, lad.state(c.cell).window_id)
              for c in (delay, positive, damaged)}
    ev = apply_policy_update(lad, ["P1"], "f" * 64, "v2")
    assert ev[0].kind == "POLICY_VERSION"
    assert ev[0].payload == {"policy_id": "v2", "policy_sha": "f" * 64, "changed_clauses": ["P1"]}
    changes = [e.payload for e in ev[1:]]
    assert [(p["cell"], p["from"], p["to"], p["reason"]) for p in changes] == [(delay.cell, "L2", "L1",
                                                                               "POLICY_CHANGE")]
    st = lad.state(delay.cell)
    assert st.level is Level.L1 and st.n == 0 and st.window_id == before[delay.cell][2] + 1 and st.monitor is None
    for c in (positive, damaged):
        s = lad.state(c.cell)
        assert (s.level, s.n, s.window_id) == before[c.cell]


def test_case_type_clauses_follow_master_1_1():
    assert CASE_TYPE_CLAUSES[CaseType.DELAY] == frozenset({"F1", "P1", "D2", "F3", "N3"})
    assert CASE_TYPE_CLAUSES[CaseType.NOT_RECEIVED] == frozenset({"E5", "F3", "N3"})
    assert set(CASE_TYPE_CLAUSES) == set(CaseType)
    assert [t for t, cl in CASE_TYPE_CLAUSES.items() if "P1" in cl] == [CaseType.DELAY]
