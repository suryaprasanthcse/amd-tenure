"""Policy oracle (R.3): Appendix T golden tests plus the v1/v2 drift contract (master §12.1)."""
import random
from dataclasses import replace

import pytest

from tenure.domain import Action, CaseType, Resolution
from tenure.policy import CLAUSES, POLICY_V1, POLICY_V2, bp_of, resolve
from tests.fixtures.cases import make_case

pytestmark = [pytest.mark.unit, pytest.mark.gate_g01]


def test_policy_total_and_well_formed():
    rng = random.Random(1)
    for i in range(20_000):
        c = make_case(rng, i)
        for p in (POLICY_V1, POLICY_V2):
            r = resolve(c, p)
            assert r.clause in CLAUSES
            assert (r.amount_cents is not None) == (r.action is Action.REFUND_PARTIAL)
            if r.amount_cents is not None:
                assert 1 <= r.amount_cents <= c.order_total_cents


def test_policy_total_when_status_and_dates_disagree():
    rng = random.Random(21)
    for i in range(5_000):
        c = make_case(rng, i)
        c = replace(c, order_status=rng.choice(["delivered", "canceled", "shipped"]))
        assert resolve(c).clause in CLAUSES


def test_policy_specific_rules():
    rng = random.Random(2)
    base = make_case(rng, 0, CaseType.DELAY)
    c = replace(base, order_status="delivered", days_since_delivery=3, days_late=6, fraud_flag=False,
                legal_threat=False, order_total_cents=5_000, items_total_cents=4_000, freight_total_cents=1_000)
    assert resolve(c).clause == "F1"
    assert resolve(replace(c, days_late=5)) == Resolution(Action.REFUND_PARTIAL, 1_000, False, "P1")
    assert resolve(replace(c, days_late=5), POLICY_V2).amount_cents == 500
    assert resolve(replace(c, days_late=0)).action is Action.DENY
    assert resolve(replace(c, order_total_cents=50_001)).clause == "E3"
    assert resolve(replace(c, fraud_flag=True)).clause == "E1"
    r = resolve(replace(c, case_type=CaseType.REMORSE, days_since_delivery=31))
    assert r.clause == "D1" and r.action is Action.DENY
    assert resolve(replace(c, case_type=CaseType.WRONG_VARIANT)).amount_cents == 800


def test_v2_differs_only_in_p1():
    rng = random.Random(31)
    n_p1 = n_diff = 0
    for i in range(20_000):
        c = make_case(rng, i)
        r1, r2 = resolve(c, POLICY_V1), resolve(c, POLICY_V2)
        assert r1.clause == r2.clause
        if r1.clause == "P1":
            n_p1 += 1
            assert r2.action is Action.REFUND_PARTIAL
            assert r2.amount_cents == min(500, c.order_total_cents)
            n_diff += r1 != r2
        else:
            assert r1 == r2
    assert n_p1 > 100 and n_diff > 0


def test_policy_params_versions():
    assert (POLICY_V1.version, POLICY_V1.minor_delay_mode) == ("v1", "freight")
    assert (POLICY_V2.version, POLICY_V2.minor_delay_mode, POLICY_V2.minor_delay_fixed_cents) == ("v2", "fixed", 500)
    assert replace(POLICY_V2, version="v1", minor_delay_mode="freight") == POLICY_V1
    assert len(CLAUSES) == 25 == len(set(CLAUSES))


@pytest.mark.parametrize("cents,bp,out", [(0, 2_000, 0), (5_000, 2_000, 1_000), (4_000, 500, 200),
                                          (3, 5_000, 2), (1, 5_000, 1), (1, 4_999, 0), (12_345, 1_000, 1_235)])
def test_bp_of_rounds_half_up(cents, bp, out):
    assert bp_of(cents, bp) == out


def test_bp_of_rejects_negative():
    with pytest.raises(ValueError):
        bp_of(-1, 100)
    with pytest.raises(ValueError):
        bp_of(100, -1)


def test_partial_refund_is_clamped_to_one_cent_and_order_total():
    rng = random.Random(3)
    c = replace(make_case(rng, 0, CaseType.QUALITY), order_status="delivered", days_late=0, days_since_delivery=3,
                items_total_cents=4, freight_total_cents=0, order_total_cents=4, fraud_flag=False,
                legal_threat=False)
    assert resolve(c) == Resolution(Action.REFUND_PARTIAL, 1, False, "P3")  # 10% of 4 cents rounds to 0 → 1
    d = replace(c, case_type=CaseType.DELAY, days_late=2, items_total_cents=100, freight_total_cents=900,
                order_total_cents=300)
    assert resolve(d).amount_cents == 300  # freight above the order total is capped at the total
