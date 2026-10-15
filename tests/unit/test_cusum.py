"""Bernoulli CUSUM and its Brook–Evans design (master §6, R.8; Appendix T golden tests verbatim)."""
import math

import numpy as np
import pytest
from tenure.cusum import CusumDesign, CusumMonitor, arl, design, llr_weights, shift_for_cell

pytestmark = [pytest.mark.unit, pytest.mark.gate_g05]


# --- Appendix T (verbatim) ---------------------------------------------------------------------------------------

def test_cusum_design_matches_monte_carlo():
    d = design(0.05, 0.12, 500.0)
    assert d.h == pytest.approx(2.556, abs=0.01)
    assert d.arl0 >= 500.0 and d.arl1 == pytest.approx(52.8, rel=0.03)
    rng = np.random.default_rng(3)
    runs = []
    for _ in range(3000):
        m, t = CusumMonitor(d), 0
        while True:
            t += 1
            if m.update(rng.random() < 0.12):
                break
        runs.append(t)
    assert np.mean(runs) == pytest.approx(d.arl1, rel=0.07)


def test_cusum_weights_and_shift():
    w1, w0 = llr_weights(0.05, 0.12)
    assert w1 > 0 > w0
    p0, p1 = shift_for_cell(0.97, 0.92, 0.05)
    assert (p0, p1) == (0.03, 0.13)
    p0, p1 = shift_for_cell(1.0, 0.99, 0.02)
    assert p0 == 0.01 and p1 >= p0 + 0.05 - 1e-9
    assert arl(w1, w0, 0.12, 3.0) < arl(w1, w0, 0.05, 3.0)


# --- RUNBOOK T05 ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("p0", [0.01, 0.05, 0.10])
@pytest.mark.parametrize("shift", [0.05, 0.10, 0.15, 0.20])
def test_design_bracket_is_safe(p0, shift):
    """The doubling bracket keeps the solve well conditioned at every promoted-cell shape (master §6.3)."""
    p1 = round(p0 + shift, 3)
    d = design(p0, p1, 500.0)
    assert math.isfinite(d.h) and d.h > 0
    assert math.isfinite(d.arl0) and d.arl0 >= 500.0
    assert math.isfinite(d.arl1) and 1.0 <= d.arl1 < d.arl0


@pytest.mark.parametrize("p0,p1,a0,h,arl1", [
    (0.05, 0.12, 500, 2.5561, 52.8), (0.03, 0.10, 500, 2.6413, 41.7), (0.08, 0.15, 500, 2.4571, 66.1),
    (0.05, 0.12, 1000, 3.2127, 68.0), (0.10, 0.20, 300, 2.3765, 40.0)])
def test_verified_designs_table(p0, p1, a0, h, arl1):
    """Master §6.4: Brook–Evans designs; T05 tests ARL1 within 7%. ARL is a step function of h (the chain's
    increments are discrete), so the smallest h meeting A0 can sit a step away from the tabulated h."""
    d = design(p0, p1, float(a0))
    assert d.h == pytest.approx(h, abs=0.1)
    assert d.arl0 >= a0
    assert d.arl1 == pytest.approx(arl1, rel=0.07)


def test_design_is_cached_and_frozen():
    a, b = design(0.05, 0.12, 500.0), design(0.05, 0.12, 500.0)
    assert a is b and isinstance(a, CusumDesign)
    with pytest.raises(AttributeError):
        a.h = 1.0  # type: ignore[misc]


@pytest.mark.parametrize("p0,p1", [(0.0, 0.1), (0.1, 0.1), (0.2, 0.1), (0.1, 1.0)])
def test_llr_weights_rejects_bad_rates(p0, p1):
    with pytest.raises(ValueError):
        llr_weights(p0, p1)


def test_arl_rejects_nonpositive_h():
    w1, w0 = llr_weights(0.05, 0.12)
    with pytest.raises(ValueError):
        arl(w1, w0, 0.05, 0.0)


def test_shift_caps_and_floors():
    assert shift_for_cell(0.40, 0.90, 0.05) == (0.45, 0.5)  # p0 capped at cap - min_shift, p1 at cap
    p0, p1 = shift_for_cell(0.90, 0.95, 0.10)  # 1 - (0.95 - 0.10) = 0.15 is above p0 + 0.05
    assert (p0, p1) == (0.1, 0.15)


def test_monitor_score_change_point_and_signal():
    d = design(0.05, 0.12, 500.0)
    m = CusumMonitor(d)
    assert m.update(False) is False and m.s == 0.0 and m.last_zero == 1
    m.update(True)
    assert m.s == pytest.approx(d.w1) and m.last_zero == 1
    m.update(False)
    assert m.s == pytest.approx(max(0.0, d.w1 + d.w0))
    hits = 0
    while not m.update(True):
        hits += 1
    assert m.s >= d.h and m.trace[-1] == m.s and len(m.trace) == m.n
    assert m.last_zero <= m.n - hits - 1  # the change-point estimate precedes the run of disagreements
