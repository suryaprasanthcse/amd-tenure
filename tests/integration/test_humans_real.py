"""Synthetic-team calibration on the real Olist case mix (master §14.5; marker data)."""
from collections import Counter
from pathlib import Path

import pandas as pd
import pytest

from tenure.domain import CaseType
from tenure.humans import PolicySchedule, label_case
from tenure.intake import case_from_record
from tenure.policy import resolve
from tenure.schema import AgentDecision, agrees

ROOT = Path(__file__).resolve().parents[2]
SEED = 20261018

pytestmark = [pytest.mark.data, pytest.mark.gate_g02]


def _as_agent(r):
    return AgentDecision(r.action, r.amount_cents, r.requires_return, r.clause, 0, "")


def test_real_team_calibration():
    df = pd.read_parquet(ROOT / "data" / "derived" / "cases.parquet")
    df = df[df["split"].isin(["train", "replay"])]
    assert (df["case_type"] != "").all(), "intake must be complete for train and replay"
    cases = [case_from_record(r) for r in df.to_dict("records")]
    labels = [label_case(c, PolicySchedule(), SEED) for c in cases]
    pairs = [(c, lab) for c, lab in zip(cases, labels) if lab.secondary is not None]
    assert 0.13 <= len(pairs) / len(cases) <= 0.17
    hh = [agrees(_as_agent(lab.secondary), lab.primary) for _, lab in pairs]
    overall = sum(hh) / len(hh)
    assert 0.85 <= overall <= 0.96, overall
    n, hit = Counter(), Counter()
    for (c, _), a in zip(pairs, hh):
        n[c.case_type] += 1
        hit[c.case_type] += a
    rates = {t: hit[t] / n[t] for t in n if n[t] >= 100}
    assert len(rates) >= 2 and max(rates.values()) - min(rates.values()) >= 0.05, rates
    oracle = sum(agrees(_as_agent(resolve(c)), lab.primary) for c, lab in zip(cases, labels)) / len(cases)
    assert oracle >= overall, (oracle, overall)
    assert {c.case_type for c in cases} <= set(CaseType)
