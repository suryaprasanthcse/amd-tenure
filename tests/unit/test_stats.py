"""Wilson-cc bounds, Newcombe difference and non-inferiority (R.7, master §3, §4): Appendix T golden tests,
hypothesis properties, and the gold-set evaluation's Wilson-cc intervals."""
import math

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from tenure.domain import Action, Resolution
from tenure.gold import agreement_intervals, raw_agreement
from tenure.stats import (
    Z95,
    Baseline,
    NIResult,
    newcombe_diff_cc,
    non_inferiority,
    select_baseline,
    wilson_cc_interval,
    wilson_cc_lower,
)

pytestmark = [pytest.mark.unit, pytest.mark.gate_g04]

# --- Appendix T (verbatim) ------------------------------------------------------------------------------------

WILSON_VECTORS = [
    (0, 10, 0.000000, 0.344537), (1, 10, 0.005242, 0.458846), (9, 10, 0.541154, 0.994758),
    (10, 10, 0.655463, 1.000000), (81, 263, 0.253509, 0.368176), (95, 100, 0.881701, 0.981447),
    (100, 100, 0.953899, 1.000000), (180, 200, 0.847703, 0.936337), (470, 500, 0.914489, 0.958485),
    (950, 1000, 0.934125, 0.962305), (29, 30, 0.809470, 0.998258), (30, 30, 0.858680, 1.000000),
]


@pytest.mark.parametrize("x,n,lo,hi", WILSON_VECTORS)
def test_wilson_vectors(x, n, lo, hi):
    a, b = wilson_cc_interval(x, n)
    assert a == pytest.approx(lo, abs=1e-6) and b == pytest.approx(hi, abs=1e-6)


def test_wilson_solves_cc_score_equation():
    z = 1.959963984540054
    for n in (5, 37, 400):
        for x in range(1, n):
            lo, hi = wilson_cc_interval(x, n)
            assert (x - 0.5 - n * lo) == pytest.approx(z * math.sqrt(n * lo * (1 - lo)), abs=1e-7)
            assert (n * hi - x - 0.5) == pytest.approx(z * math.sqrt(n * hi * (1 - hi)), abs=1e-7)


def test_wilson_monotone_in_x_and_n():
    for n in (20, 200):
        lows = [wilson_cc_interval(x, n)[0] for x in range(n + 1)]
        assert all(a <= b for a, b in zip(lows, lows[1:]))
    assert wilson_cc_interval(95, 100)[0] < wilson_cc_interval(950, 1000)[0]


@pytest.mark.parametrize("args,lo,hi", [((56, 70, 48, 80), 0.042768, 0.342186), ((9, 10, 3, 10), 0.101287, 0.838669),
                                        ((5, 56, 0, 29), -0.066709, 0.203698)])
def test_newcombe_vectors(args, lo, hi):
    a, b = newcombe_diff_cc(*args)
    assert a == pytest.approx(lo, abs=1e-6) and b == pytest.approx(hi, abs=1e-6)


def test_non_inferiority_decision():
    base = Baseline(180, 200, "cell")  # 0.90
    r = non_inferiority(470, 500, base, 0.05)  # lower 0.9145 >= 0.85
    assert r.passed and r.threshold == pytest.approx(0.85)
    r2 = non_inferiority(29, 30, base, 0.05)  # lower 0.8095 < 0.85
    assert not r2.passed


def test_select_baseline_pools_when_sparse():
    pairs = {"DELAY|V1": (50, 55), "DELAY|V2": (40, 45), "POSITIVE|V1": (300, 310)}
    assert select_baseline(pairs, "POSITIVE|V1").scope == "cell"
    b = select_baseline(pairs, "DELAY|V1")
    assert b.scope == "case_type" and (b.x, b.n) == (90, 100)
    assert select_baseline(pairs, "BILLING|V3").scope == "global"


# --- hypothesis properties (RUNBOOK T04) -----------------------------------------------------------------------

@st.composite
def x_and_n(draw, n_max: int = 5_000):
    n = draw(st.integers(min_value=1, max_value=n_max))
    return draw(st.integers(min_value=0, max_value=n)), n


@given(x_and_n())
def test_wilson_bounds_bracket_the_estimate(xn):
    x, n = xn
    lo, hi = wilson_cc_interval(x, n)
    assert 0.0 <= lo <= x / n <= hi <= 1.0


@given(x_and_n())
def test_wilson_lower_increases_with_x(xn):
    x, n = xn
    assume(x < n)
    assert wilson_cc_lower(x, n) < wilson_cc_lower(x + 1, n)


@given(x_and_n(), x_and_n(), st.floats(min_value=0.0, max_value=0.5), st.floats(min_value=0.0, max_value=0.5))
def test_ni_pass_is_monotone_in_delta(agent, team, d1, d2):
    (x, n), (bx, bn) = agent, team
    base = Baseline(bx, bn, "cell")
    small, large = sorted((d1, d2))
    a, b = non_inferiority(x, n, base, small), non_inferiority(x, n, base, large)
    assert b.threshold <= a.threshold
    if a.passed:
        assert b.passed
    if a.passed_strict:
        assert b.passed_strict


# --- R.7 behaviour beyond the golden vectors ------------------------------------------------------------------

def test_wilson_rejects_bad_input():
    for x, n in ((0, 0), (1, -3), (-1, 10), (11, 10)):
        with pytest.raises(ValueError):
            wilson_cc_interval(x, n)


def test_wilson_lower_is_the_interval_lower_and_z_is_two_sided_95():
    assert Z95 == pytest.approx(1.959964, abs=1e-6)
    for x, n, lo, _ in WILSON_VECTORS:
        assert wilson_cc_lower(x, n) == wilson_cc_interval(x, n)[0]


def test_non_inferiority_result_fields():
    base = Baseline(180, 200, "case_type")
    r = non_inferiority(470, 500, base, 0.05)
    assert isinstance(r, NIResult)
    assert (r.x, r.n, r.baseline_scope, r.delta) == (470, 500, "case_type", 0.05)
    assert r.p_hat == pytest.approx(0.94) and r.baseline == pytest.approx(0.90)
    assert r.lower == pytest.approx(0.914489, abs=1e-6)
    assert r.diff_lower == pytest.approx(newcombe_diff_cc(470, 500, 180, 200)[0])
    assert r.passed_strict == (r.diff_lower >= -0.05)
    with pytest.raises(ValueError):
        non_inferiority(0, 0, base, 0.05)


def test_select_baseline_rate_and_empty_pairs():
    b = select_baseline({"DELAY|V1": (54, 60)}, "DELAY|V1")
    assert b.scope == "cell" and b.rate == pytest.approx(0.9)
    assert select_baseline({"DELAY|V1": (54, 60)}, "DELAY|V2", n_min=61).scope == "global"
    with pytest.raises(ValueError):
        select_baseline({}, "DELAY|V1")


# --- gold evaluation with Wilson-cc intervals (RUNBOOK T02 → T04) ------------------------------------------------

def test_gold_agreement_intervals():
    gold = {"a": Resolution(Action.DENY, None, False, ""), "b": Resolution(Action.REFUND_PARTIAL, 1_000, False, ""),
            "c": Resolution(Action.ESCALATE, None, False, ""), "d": Resolution(Action.NO_ACTION, None, False, "")}
    other = {"a": Resolution(Action.DENY, None, False, "D2"), "b": Resolution(Action.REFUND_PARTIAL, 1_090, False, "P1"),
             "c": Resolution(Action.REQUEST_INFO, None, False, "I2")}
    types = {"a": "DELAY", "b": "DELAY", "c": "OTHER", "d": "POSITIVE"}
    out = agreement_intervals(raw_agreement(gold, other, types))
    lo, hi = wilson_cc_interval(2, 3)
    assert out["overall"] == {"x": 2, "n": 3, "p_hat": pytest.approx(2 / 3), "L_cc": lo, "U_cc": hi}
    assert set(out["by_type"]) == {"DELAY", "OTHER"}
    assert out["by_type"]["DELAY"]["L_cc"] == wilson_cc_interval(2, 2)[0]
    assert out["by_type"]["OTHER"]["L_cc"] == 0.0 and out["by_type"]["OTHER"]["U_cc"] < 1.0
    empty = agreement_intervals(raw_agreement(gold, {}, types))
    assert empty["overall"] == {"x": 0, "n": 0, "p_hat": None, "L_cc": None, "U_cc": None}
    assert empty["by_type"] == {}
