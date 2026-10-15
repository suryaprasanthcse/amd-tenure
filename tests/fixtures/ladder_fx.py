"""Ladder helpers for the T05 tests: a fixed in-cell case, agent decisions, and driving a cell to a level
through the public observe/evaluate API only (no state is written by hand)."""
from __future__ import annotations

import random
from dataclasses import replace

from tenure.cost import CostParams
from tenure.domain import Action, Case, CaseType, Level, Resolution
from tenure.ladder import Ladder, LadderConfig
from tenure.policy import POLICY_V1, PolicyParams, resolve
from tenure.schema import AgentDecision
from tenure.stats import Baseline
from tests.fixtures.cases import make_case

HUMAN = Resolution(Action.DENY, None, False, "D2")
GOOD = AgentDecision(Action.DENY, None, False, "D2", 900_000, "")
BAD = AgentDecision(Action.REFUND_FULL, None, False, "F1", 900_000, "")
BASELINE = Baseline(900, 1000, "cell")


def cell_case(case_type: CaseType = CaseType.DELAY, band: str = "V1", i: int = 0, **kw) -> Case:
    """A case of the given cell with no per-case ceiling (no fraud, no legal threat, low value)."""
    c = make_case(random.Random(1_000 + i), i, case_type)
    fields = {"value_band": band, "order_total_cents": 1_000, "items_total_cents": 900, "freight_total_cents": 100,
              "fraud_flag": False, "legal_threat": False}
    fields.update(kw)
    return replace(c, **fields)


def make_ladder(cells: list[str], volume: float = 300.0, cfg: LadderConfig | None = None,
                ceilings: dict[str, Level] | None = None, baseline: Baseline = BASELINE,
                hh_cost: float = 120.0) -> Ladder:
    cfg = cfg or LadderConfig(bootstrap_b=300)
    return Ladder(cfg, CostParams(), {c: baseline for c in cells}, {c: hh_cost for c in cells},
                  {c: volume for c in cells}, ceilings)


def drive_to(lad: Ladder, case: Case, target: Level, agent: AgentDecision = GOOD, human: Resolution = HUMAN,
             every: int = 50, limit: int = 5_000) -> list:
    """Feed agreeing outcomes and evaluate every `every` until the case's cell reaches `target`."""
    events = []
    for i in range(limit):
        if lad.state(case.cell).level >= target:
            return events
        events += lad.observe(case, agent, human)
        if i % every == every - 1:
            events += lad.evaluate()
    raise AssertionError(f"{case.cell} did not reach {target.name} in {limit} outcomes")


def as_agent(r: Resolution) -> AgentDecision:
    return AgentDecision(r.action, r.amount_cents, r.requires_return, r.clause, 900_000, "")


def oracle(c: Case, p: PolicyParams = POLICY_V1) -> AgentDecision:
    return as_agent(resolve(c, p))
