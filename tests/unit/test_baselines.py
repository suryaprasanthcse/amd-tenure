"""Human–human baselines from double-coded pairs (master §4.1, §4.2, §5.5): agreement and mean cost per cell,
pooled cell → case type → global with the scope recorded."""
import random
from dataclasses import replace
from datetime import date

import numpy as np
import pandas as pd
import pytest

from tenure.baselines import TRAIN_REPLAY, CostBaseline, PairTable, cell_baselines, pair_table, select_cost_baseline
from tenure.cli import HUMANS_COLUMNS, humans_table
from tenure.cost import CostParams, human_error_components, total_cost
from tenure.domain import Action, Case, CaseType, Resolution
from tenure.humans import PolicySchedule, label_case
from tenure.schema import AgentDecision, agrees
from tenure.stats import select_baseline
from tests.fixtures.cases import make_case
from tests.fixtures.frames import cases_frame

pytestmark = [pytest.mark.unit, pytest.mark.gate_g04]

P = CostParams()
BASE = Case("x", date(2018, 2, 1), CaseType.DELAY, "delivered", 5, 2, 5, 1_000, 200, 1_200, "V1",
            False, False, False, False, "atrasou")


def mk(cid: str, ctype: CaseType, band: str, total: int) -> Case:
    return replace(BASE, case_id=cid, case_type=ctype, value_band=band, items_total_cents=total - 200,
                   order_total_cents=total)


def row(cid: str, primary: Resolution, secondary: Resolution | None) -> dict:
    s = secondary
    return dict(zip(HUMANS_COLUMNS, (cid, "H1", primary.action.value, primary.amount_cents, primary.requires_return,
                                     primary.clause, None if s is None else "H2", None if s is None else s.action.value,
                                     None if s is None else s.amount_cents, None if s is None else s.requires_return,
                                     None if s is None else s.clause)))


def frames(entries):
    """entries: (case, split, primary, secondary|None) → (cases frame, humans frame) shaped like the parquet files."""
    cases = pd.concat([cases_frame([c], split) for c, split, _, _ in entries], ignore_index=True)
    hum = pd.DataFrame([row(c.case_id, p, s) for c, _, p, s in entries], columns=list(HUMANS_COLUMNS))
    for col in ("primary_amount_cents", "secondary_amount_cents"):
        hum[col] = hum[col].astype("Int64")
    hum["secondary_requires_return"] = hum["secondary_requires_return"].astype("boolean")
    return cases, hum


DENY = Resolution(Action.DENY, None, False, "D1")
FULL = Resolution(Action.REFUND_FULL, None, False, "R1")
PART = Resolution(Action.REFUND_PARTIAL, 1_000, False, "P1")
NEAR = Resolution(Action.REFUND_PARTIAL, 1_060, False, "P1")  # within max(50, 10%) of 1,000
ESC = Resolution(Action.ESCALATE, None, False, "E1")


def hand_built():
    d1a, d1b, d1c = mk("d1a", CaseType.DELAY, "V1", 1_200), mk("d1b", CaseType.DELAY, "V1", 1_400), \
        mk("d1c", CaseType.DELAY, "V1", 1_300)
    return frames([
        (d1a, "train", DENY, DENY),
        (d1b, "replay", DENY, FULL),  # disagreement: the second person refunds 1,400 the first denied
        (d1c, "train", PART, NEAR),
        (mk("d1d", CaseType.DELAY, "V1", 1_000), "train", DENY, None),  # not double-coded
        (mk("d1e", CaseType.DELAY, "V1", 1_000), "live", DENY, FULL),  # outside train + replay
        (mk("d2a", CaseType.DELAY, "V2", 2_000), "replay", FULL, FULL),
        (mk("p1a", CaseType.POSITIVE, "V1", 900), "train", Resolution(Action.NO_ACTION, None, False, "N1"),
         Resolution(Action.NO_ACTION, None, False, "N1")),
        (mk("p1b", CaseType.POSITIVE, "V1", 800), "replay", DENY, ESC),  # unnecessary hand-off: 400 cents
    ])


def test_pair_table_counts_double_coded_train_and_replay_pairs():
    cases, hum = hand_built()
    t = pair_table(cases, hum, P)
    assert isinstance(t, PairTable) and TRAIN_REPLAY == ("train", "replay")
    assert t.agreement == {"DELAY|V1": (2, 3), "DELAY|V2": (1, 1), "POSITIVE|V1": (1, 2)}
    assert t.cost == {"DELAY|V1": (1_400.0, 3), "DELAY|V2": (0.0, 1), "POSITIVE|V1": (400.0, 2)}
    assert t.mean_cost() == {"DELAY|V1": pytest.approx(1_400 / 3), "DELAY|V2": 0.0, "POSITIVE|V1": 200.0}
    assert t.pairs == 6 and t.agree == 4
    with_live = pair_table(cases, hum, P, splits=("train", "replay", "live"))
    assert with_live.agreement["DELAY|V1"] == (2, 4)


def test_pair_table_uses_the_agreement_tolerance():
    cases, hum = hand_built()
    strict = pair_table(cases, hum, P, tol_abs_cents=0, tol_rel_bp=0)  # 1,060 vs 1,000 no longer agrees
    assert strict.agreement["DELAY|V1"] == (1, 3)
    assert strict.cost == pair_table(cases, hum, P).cost  # R.2 prices disagreements with its own predicate


def test_baselines_pool_cell_type_global_with_scope():
    cases, hum = hand_built()
    t = pair_table(cases, hum, P)
    b, cb = cell_baselines(t, "DELAY|V1", n_min=3)
    assert (b.x, b.n, b.scope) == (2, 3, "cell")
    assert isinstance(cb, CostBaseline) and (cb.n, cb.scope) == (3, "cell")
    assert cb.mean_cents == pytest.approx(1_400 / 3)
    b, cb = cell_baselines(t, "DELAY|V2", n_min=3)
    assert (b.x, b.n, b.scope) == (3, 4, "case_type")
    assert (cb.n, cb.scope) == (4, "case_type") and cb.mean_cents == pytest.approx(1_400 / 4)
    b, cb = cell_baselines(t, "BILLING|V3", n_min=3)
    assert (b.x, b.n, b.scope) == (4, 6, "global")
    assert (cb.n, cb.scope) == (6, "global") and cb.mean_cents == pytest.approx(1_800 / 6)


def test_select_cost_baseline_rejects_no_pairs():
    with pytest.raises(ValueError):
        select_cost_baseline({}, "DELAY|V1")


def test_baselines_from_pairs():
    """On team decisions from humans_table, the table matches R.6 pairs scored directly, pooled as R.7 does."""
    rng = random.Random(9)
    sched, seed = PolicySchedule(), 20261018
    train = [make_case(rng, i, CaseType.DELAY if i % 3 else None) for i in range(3_000)]
    live = [make_case(rng, 10_000 + i) for i in range(500)]
    cases = pd.concat([cases_frame(train, "train"), cases_frame(live, "live")], ignore_index=True)
    hum = humans_table(cases, sched, seed)
    t = pair_table(cases, hum, P)
    want_x: dict[str, list[int]] = {}
    want_e: dict[str, float] = {}
    for c in sorted(train, key=lambda c: c.case_id):
        lab = label_case(c, sched, seed)
        if lab.secondary is None:
            continue
        s = lab.secondary
        hit = agrees(AgentDecision(s.action, s.amount_cents, s.requires_return, s.clause, 0, ""), lab.primary)
        xn = want_x.setdefault(c.cell, [0, 0])
        xn[0] += hit
        xn[1] += 1
        e = float(total_cost(np.asarray([human_error_components(s, lab.primary, c, P)], dtype=float), P)[0])
        want_e[c.cell] = want_e.get(c.cell, 0.0) + e
    assert t.agreement == {k: tuple(v) for k, v in sorted(want_x.items())}
    assert set(t.cost) == set(want_e)
    for cell, (total, n) in t.cost.items():
        assert n == want_x[cell][1] and total == pytest.approx(want_e[cell])
    assert 0.12 < t.pairs / len(train) < 0.18
    for cell in ("DELAY|V2", "DELAY|V4", "QUALITY|V1", "SELLER_SERVICE|V4"):
        b, cb = cell_baselines(t, cell, n_min=60)
        ref = select_baseline(t.agreement, cell, 60)
        assert (b.x, b.n, b.scope) == (ref.x, ref.n, ref.scope)
        assert (cb.n, cb.scope) == (b.n, b.scope)  # the cost baseline pools over the same pairs
    assert cell_baselines(t, "DELAY|V4", n_min=60)[0].scope == "cell"
    assert cell_baselines(t, "DELAY|V2", n_min=60)[0].scope == "case_type"
