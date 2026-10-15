"""Cost-weighted error (R.2, master §5): Appendix T golden test, the §5.2 rules on hand-built cases, and the
hypothesis properties (non-negative components, re-weighting equals resampling)."""
import random
from datetime import date

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from tenure.cost import (
    N_COMPONENTS,
    CostGate,
    CostParams,
    bootstrap_component_means,
    cost_gate,
    customer_value_cents,
    error_components,
    human_error_components,
    merchant_cost_cents,
    total_cost,
)
from tenure.domain import Action, Case, CaseType, Resolution
from tenure.policy import resolve
from tenure.schema import AgentDecision
from tests.fixtures.cases import make_case

pytestmark = [pytest.mark.unit, pytest.mark.gate_g04]


# --- Appendix T (verbatim) ------------------------------------------------------------------------------------

def test_cost_linear_decomposition_and_bootstrap():
    rng = random.Random(6)
    p = CostParams()
    comps = []
    for i in range(500):
        c = make_case(rng, i)
        h = resolve(c)
        a = AgentDecision(rng.choice(list(Action)), None, False, "N1", 0, "")
        if a.action is Action.REFUND_PARTIAL:
            a = AgentDecision(a.action, max(1, c.order_total_cents // 10), False, "P1", 0, "")
        comps.append(error_components(a, h, c, p))
    comps = np.asarray(comps, dtype=float)
    e = total_cost(comps, p)
    assert np.all(e >= 0)
    d1 = bootstrap_component_means(comps, 500, ("s", "cell"))
    d2 = bootstrap_component_means(comps, 500, ("s", "cell"))
    assert np.array_equal(d1, d2)
    assert (d1 @ p.weights()).mean() == pytest.approx(e.mean(), rel=0.05)
    g = cost_gate(comps, hh_mean_cents=e.mean() * 2, p=p, b=500, seed_parts=("s",))
    assert g.passed and g.ucb >= g.mean


# --- master §5.2 rules on hand-built cases (RUNBOOK T04 test_cost_rules) -----------------------------------------

ITEMS, FREIGHT = 8_000, 2_000
TOTAL = ITEMS + FREIGHT
P = CostParams()


def case() -> Case:
    return Case("h1", date(2018, 3, 1), CaseType.DAMAGED, "delivered", 0, 3, 0, ITEMS, FREIGHT, TOTAL, "V4",
                True, False, False, False, "chegou quebrado")


def agent(action: Action, amount: int | None = None, ret: bool = False) -> AgentDecision:
    return AgentDecision(action, amount, ret, "X1", 900_000, "")


def human(action: Action, amount: int | None = None, ret: bool = False) -> Resolution:
    return Resolution(action, amount, ret, "X1")


def test_cost_rules_agree():
    assert error_components(agent(Action.REFUND_FULL), human(Action.REFUND_FULL, ret=True), case(), P) == (0, 0, 0, 0)
    within = agent(Action.REFUND_PARTIAL, 1_040)  # within max(EUR 0.50, 10%) of 1,000
    assert error_components(within, human(Action.REFUND_PARTIAL, 1_000), case(), P) == (0.0, 0.0, 0, 0)


def test_cost_rules_invalid_output_falls_back_to_a_person():
    assert error_components(None, human(Action.REFUND_FULL), case(), P) == (0.0, 0.0, 1, 0)
    assert error_components(None, human(Action.ESCALATE), case(), P) == (0.0, 0.0, 1, 0)  # invalid wins


def test_cost_rules_missed_escalation():
    assert error_components(agent(Action.REFUND_FULL), human(Action.ESCALATE), case(), P) == (0.0, 0.0, 0, 1)


def test_cost_rules_unnecessary_escalation():
    assert error_components(agent(Action.ESCALATE), human(Action.DENY), case(), P) == (0.0, 0.0, 1, 0)


def test_cost_rules_leak():
    assert error_components(agent(Action.REFUND_FULL), human(Action.DENY), case(), P) == (TOTAL, 0.0, 0, 0)
    with_return = TOTAL + 800 - 0.5 * 0.6 * ITEMS  # T + C_ret - s·g·I
    leak, short, unesc, missed = error_components(agent(Action.REFUND_FULL, ret=True), human(Action.DENY), case(), P)
    assert leak == pytest.approx(with_return) and (short, unesc, missed) == (0.0, 0, 0)
    leak, short, _, _ = error_components(agent(Action.REFUND_PARTIAL, 3_000), human(Action.REFUND_PARTIAL, 1_000),
                                         case(), P)
    assert (leak, short) == (2_000.0, 0.0)


def test_cost_rules_short():
    assert error_components(agent(Action.DENY), human(Action.REFUND_FULL), case(), P) == (0.0, TOTAL, 0, 0)
    leak, short, _, _ = error_components(agent(Action.REFUND_PARTIAL, 2_500), human(Action.REFUND_FULL), case(), P)
    assert (leak, short) == (0.0, TOTAL - 2_500)
    # RESHIP instead of a full refund: cheaper for the merchant (g·I + F < T), the customer misses the freight
    leak, short, _, _ = error_components(agent(Action.RESHIP), human(Action.REFUND_FULL), case(), P)
    assert (leak, short) == (0.0, float(TOTAL - ITEMS))


def test_cost_rules_short_capped_at_the_order_total():
    over = human(Action.REFUND_PARTIAL, 3 * TOTAL)  # an entitlement above the order total is capped at T
    assert error_components(agent(Action.NO_ACTION), over, case(), P) == (0.0, float(TOTAL), 0, 0)


def test_customer_value_and_merchant_cost_table():
    c = case()
    assert customer_value_cents(Action.REFUND_FULL, None, c) == TOTAL
    assert customer_value_cents(Action.REFUND_PARTIAL, 1_234, c) == 1_234
    assert customer_value_cents(Action.REFUND_PARTIAL, None, c) == 0
    assert customer_value_cents(Action.RESHIP, None, c) == ITEMS
    for a in (Action.REQUEST_INFO, Action.DENY, Action.NO_ACTION, Action.ESCALATE):
        assert customer_value_cents(a, None, c) == 0
        assert merchant_cost_cents(a, None, False, c, P) == 0.0
    assert merchant_cost_cents(Action.REFUND_FULL, None, False, c, P) == TOTAL
    assert merchant_cost_cents(Action.REFUND_PARTIAL, 700, False, c, P) == 700.0
    assert merchant_cost_cents(Action.RESHIP, None, False, c, P) == pytest.approx(0.6 * ITEMS + FREIGHT)
    assert merchant_cost_cents(Action.RESHIP, None, True, c, P) == pytest.approx(0.6 * ITEMS + FREIGHT + 800)


def test_params_kappa_weights_and_total_cost():
    assert CostParams().kappa == pytest.approx(0.15) and N_COMPONENTS == 4
    p = CostParams(clv_multiple=1.0, churn_max=0.5, unesc_cents=300, missed_cents=1_000)
    assert list(p.weights()) == [1.0, 0.5, 300.0, 1_000.0]
    comps = np.array([[100.0, 200.0, 0, 0], [0.0, 0.0, 1, 0], [0.0, 0.0, 0, 1]])
    assert list(total_cost(comps, p)) == [200.0, 300.0, 1_000.0]


def test_human_error_components_compares_two_people():
    second, first = human(Action.REFUND_FULL), human(Action.DENY)
    assert human_error_components(second, first, case(), P) == (TOTAL, 0.0, 0, 0)
    assert human_error_components(first, first, case(), P) == (0.0, 0.0, 0, 0)
    assert human_error_components(human(Action.ESCALATE), first, case(), P) == (0.0, 0.0, 1, 0)


def test_bootstrap_rejects_an_empty_sample_and_is_seeded():
    with pytest.raises(ValueError):
        bootstrap_component_means(np.empty((0, 4)), 10, ("s",))
    comps = np.array([[1.0, 0, 0, 0], [3.0, 0, 0, 0]])
    a = bootstrap_component_means(comps, 300, ("a",), chunk=7)
    assert a.shape == (300, 4) and set(np.unique(a[:, 0])) <= {1.0, 2.0, 3.0}
    assert not np.array_equal(a, bootstrap_component_means(comps, 300, ("b",), chunk=7))
    assert np.array_equal(a, bootstrap_component_means(comps, 300, ("a",), chunk=7))


def test_cost_gate_threshold_and_failure():
    comps = np.array([[1_000.0, 0, 0, 0]] * 40 + [[0.0, 0, 0, 0]] * 60)  # mean 400 cents
    g = cost_gate(comps, hh_mean_cents=100.0, p=P, b=400, seed_parts=("s", "c"))
    assert isinstance(g, CostGate)
    assert g.mean == pytest.approx(400.0) and g.baseline_mean == 100.0
    assert g.threshold == pytest.approx(150.0)  # max(eps 50, 0.25 · 100) = 50
    assert g.ucb >= g.mean and not g.passed
    big = cost_gate(comps, hh_mean_cents=1_000.0, p=P, b=400, seed_parts=("s", "c"))
    assert big.threshold == pytest.approx(1_250.0) and big.passed  # rho · 1000 = 250 > 50


# --- hypothesis properties (RUNBOOK T04) -----------------------------------------------------------------------

params_st = st.builds(CostParams, clv_multiple=st.floats(0, 3), churn_max=st.floats(0, 1),
                      cogs_ratio=st.floats(0, 1), salvage_ratio=st.floats(0, 1),
                      return_shipping_cents=st.integers(0, 5_000), unesc_cents=st.integers(0, 5_000),
                      missed_cents=st.integers(0, 20_000))


@st.composite
def disagreement(draw):
    c = make_case(random.Random(draw(st.integers(0, 2**32))), draw(st.integers(0, 999_999)))
    a_act, h_act = draw(st.sampled_from(list(Action))), draw(st.sampled_from(list(Action)))
    amount = st.integers(1, c.order_total_cents)
    a = None if draw(st.booleans()) and draw(st.booleans()) else AgentDecision(
        a_act, draw(amount) if a_act is Action.REFUND_PARTIAL else None, draw(st.booleans()), "X1", 0, "")
    h = Resolution(h_act, draw(amount) if h_act is Action.REFUND_PARTIAL else None, draw(st.booleans()), "X1")
    return a, h, c


@given(disagreement(), params_st)
def test_error_components_are_non_negative(dec, p):
    a, h, c = dec
    comps = error_components(a, h, c, p)
    assert len(comps) == N_COMPONENTS and all(v >= 0 for v in comps)
    assert comps[1] <= c.order_total_cents
    assert float(total_cost(np.asarray([comps], dtype=float), p)[0]) >= 0


@given(st.lists(st.tuples(st.floats(0, 1e5), st.floats(0, 1e5), st.integers(0, 1), st.integers(0, 1)),
                min_size=1, max_size=60), params_st, st.integers(1, 50), st.text(max_size=8))
def test_reweighting_bootstrap_equals_resampling(rows, p, b, tag):
    comps = np.asarray(rows, dtype=float)
    seed = ("s", tag)
    reweighted = bootstrap_component_means(comps, b, seed) @ p.weights()
    resampled = bootstrap_component_means(total_cost(comps, p)[:, None], b, seed)[:, 0]
    assert np.allclose(reweighted, resampled, rtol=1e-9, atol=1e-6)
