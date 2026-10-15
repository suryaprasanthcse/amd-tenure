"""Synthetic team (R.6, master §14): Appendix T golden tests plus schedule, double coding and the table."""
import random
from collections import Counter
from datetime import date

import numpy as np
import pandas as pd
import pytest

from tenure.cli import HUMANS_COLUMNS, humans_table
from tenure.domain import CaseType
from tenure.humans import POLICY_V1, TEAM, PolicySchedule, human_decision, label_case
from tenure.policy import POLICY_V2
from tenure.schema import AgentDecision, agrees
from tests.fixtures.cases import make_case

pytestmark = [pytest.mark.unit, pytest.mark.gate_g02]


def test_humans_deterministic_and_order_independent():
    rng = random.Random(4)
    cases = [make_case(rng, i) for i in range(3_000)]
    sched = PolicySchedule()
    a = [label_case(c, sched, 7) for c in cases]
    b = [label_case(c, sched, 7) for c in reversed(cases)][::-1]
    assert a == b
    assert [label_case(c, sched, 8) for c in cases] != a


def test_human_agreement_is_realistic():
    rng = random.Random(5)
    cases = [make_case(rng, i) for i in range(20_000)]
    labels = [label_case(c, PolicySchedule(), 11) for c in cases]
    pairs = [(c, l) for c, l in zip(cases, labels) if l.secondary is not None]
    assert 0.12 < len(pairs) / len(cases) < 0.18
    rate = np.mean([agrees(AgentDecision(l.secondary.action, l.secondary.amount_cents, False, "N1", 0, ""),
                           l.primary) for _, l in pairs])
    assert 0.85 <= rate <= 0.97, rate
    by_type = Counter()
    hits = Counter()
    for c, l in pairs:
        by_type[c.case_type] += 1
        hits[c.case_type] += agrees(AgentDecision(l.secondary.action, l.secondary.amount_cents, False, "N1", 0, ""),
                                    l.primary)
    rates = [hits[t] / by_type[t] for t in by_type if by_type[t] >= 100]
    assert max(rates) - min(rates) >= 0.05


def test_schedule_switches_on_date():
    drift = date(2018, 8, 1)
    sched = PolicySchedule(((date.min, POLICY_V1), (drift, POLICY_V2)))
    assert sched.at(date(2018, 7, 31)) is POLICY_V1
    assert sched.at(drift) is POLICY_V2 and sched.at(date(2018, 8, 30)) is POLICY_V2
    assert PolicySchedule().at(date(2018, 8, 30)) is POLICY_V1  # baseline: v1 throughout


def test_drift_schedule_changes_only_p1_decisions_after_the_drift_date():
    rng = random.Random(6)
    drift = date(2018, 8, 1)
    sched = PolicySchedule(((date.min, POLICY_V1), (drift, POLICY_V2)))
    changed = 0
    for i in range(5_000):
        c = make_case(rng, i, CaseType.DELAY)
        for h in TEAM:
            a, b = human_decision(h, c, PolicySchedule(), 3), human_decision(h, c, sched, 3)
            if c.contact_date < drift:
                assert a == b
            elif a != b:
                changed += 1
    # make_case dates run Jan–Jun 2018, all before the drift date: shift a copy past it to see the change
    from dataclasses import replace
    late = [replace(make_case(rng, 10_000 + i, CaseType.DELAY), contact_date=date(2018, 8, 15)) for i in range(2_000)]
    diffs = [(h, c) for c in late for h in TEAM
             if human_decision(h, c, PolicySchedule(), 3) != human_decision(h, c, sched, 3)]
    assert changed == 0 and diffs
    assert all(human_decision(h, c, PolicySchedule(), 3).clause in ("P1", "G1", "S0") for h, c in diffs)


def test_secondary_is_a_different_person_and_share_is_15pct():
    rng = random.Random(7)
    labels = [label_case(make_case(rng, i), PolicySchedule(), 20261018) for i in range(20_000)]
    doubled = [l for l in labels if l.secondary is not None]
    assert 0.13 <= len(doubled) / len(labels) <= 0.17
    assert all(l.secondary_hid != l.primary_hid for l in doubled)
    assert all(l.secondary_hid is None for l in labels if l.secondary is None)
    assert len({l.primary_hid for l in labels}) == 6


def _frame(n: int) -> pd.DataFrame:
    rng = random.Random(8)
    rows = []
    for i in range(n):
        c = make_case(rng, i)
        rows.append({"case_id": c.case_id, "contact_date": c.contact_date, "case_type": c.case_type.value,
                     "order_status": c.order_status, "days_late": c.days_late,
                     "days_since_delivery": c.days_since_delivery, "days_past_estimate": c.days_past_estimate,
                     "items_total_cents": c.items_total_cents, "freight_total_cents": c.freight_total_cents,
                     "order_total_cents": c.order_total_cents, "value_band": c.value_band,
                     "photo_evidence": c.photo_evidence, "fraud_flag": c.fraud_flag, "legal_threat": c.legal_threat,
                     "repeat_customer": c.repeat_customer, "message": c.message, "split": "replay"})
    df = pd.DataFrame(rows)
    df["days_late"] = df["days_late"].astype("Int64")
    df["days_since_delivery"] = df["days_since_delivery"].astype("Int64")
    return df


def test_humans_table_columns_and_content():
    df = _frame(400)
    t = humans_table(df, PolicySchedule(), 20261018)
    assert list(t.columns) == list(HUMANS_COLUMNS)
    assert list(HUMANS_COLUMNS) == ["case_id", "primary_hid", "primary_action", "primary_amount_cents",
                                    "primary_requires_return", "primary_clause", "secondary_hid", "secondary_action",
                                    "secondary_amount_cents", "secondary_requires_return", "secondary_clause"]
    assert list(t["case_id"]) == sorted(df["case_id"])
    single = t[t["secondary_hid"].isna()]
    assert single["secondary_action"].isna().all() and single["secondary_amount_cents"].isna().all()
    partial = t["primary_action"] == "REFUND_PARTIAL"
    assert t.loc[partial, "primary_amount_cents"].notna().all() and t.loc[~partial, "primary_amount_cents"].isna().all()
    assert humans_table(df.iloc[::-1], PolicySchedule(), 20261018).equals(t)  # order-independent
    with pytest.raises(ValueError):
        humans_table(df.assign(case_type=""), PolicySchedule(), 20261018)  # intake must have run
