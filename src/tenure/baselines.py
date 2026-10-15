"""Human–human baselines from the double-coded pairs (master §4.1, §4.2, §5.5).

The team's own consistency is the bar: per cell, the agreement of the second decision with the first, and the
mean cost of the second decision priced against the first (R.2 `human_error_components`). Pairs come from the
train + replay windows. Sparse cells pool up to the case type, then globally (R.7 `select_baseline`); the cost
baseline pools over the same pairs, and both record the scope used.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .cost import CostParams, human_error_components
from .domain import Action, Resolution
from .intake import case_from_record
from .schema import AgentDecision, agrees
from .stats import Baseline, select_baseline

TRAIN_REPLAY: tuple[str, ...] = ("train", "replay")  # master §4.2: people are people across the whole history


@dataclass(frozen=True, slots=True)
class PairTable:
    agreement: dict[str, tuple[int, int]]  # cell -> (agreeing pairs, pairs)
    cost: dict[str, tuple[float, int]]  # cell -> (summed pair cost in cents, pairs)

    @property
    def pairs(self) -> int:
        return sum(n for _, n in self.agreement.values())

    @property
    def agree(self) -> int:
        return sum(x for x, _ in self.agreement.values())

    def mean_cost(self) -> dict[str, float]:
        return {cell: total / n for cell, (total, n) in self.cost.items()}


@dataclass(frozen=True, slots=True)
class CostBaseline:
    mean_cents: float  # the team's own mean cost per pair
    n: int  # pairs
    scope: str  # "cell" | "case_type" | "global"


def _resolution(action: object, amount: object, ret: object, clause: object) -> Resolution:
    return Resolution(Action(str(action)), None if pd.isna(amount) else int(amount),
                      False if pd.isna(ret) else bool(ret), str(clause))


def pair_table(cases: pd.DataFrame, humans: pd.DataFrame, params: CostParams,
               splits: tuple[str, ...] = TRAIN_REPLAY, tol_abs_cents: int = 50, tol_rel_bp: int = 1_000) -> PairTable:
    """Agreement and summed cost per cell over the double-coded pairs of the given splits, in case_id order.

    `cases` holds typed cases.parquet rows (with split); `humans` holds humans.parquet rows (master §14.3)."""
    doubled = humans[humans["secondary_action"].notna()]
    rows = doubled.merge(cases[cases["split"].isin(splits)], on="case_id", how="inner", validate="one_to_one")
    weights = params.weights()
    agreement: dict[str, list[int]] = {}
    cost: dict[str, list[float]] = {}
    for rec in rows.sort_values("case_id", kind="stable").to_dict("records"):
        c = case_from_record(rec)
        first = _resolution(rec["primary_action"], rec["primary_amount_cents"], rec["primary_requires_return"],
                            rec["primary_clause"])
        second = _resolution(rec["secondary_action"], rec["secondary_amount_cents"],
                             rec["secondary_requires_return"], rec["secondary_clause"])
        as_agent = AgentDecision(second.action, second.amount_cents, second.requires_return, second.clause, 0, "")
        xn = agreement.setdefault(c.cell, [0, 0])
        xn[0] += int(agrees(as_agent, first, tol_abs_cents, tol_rel_bp))
        xn[1] += 1
        e = float(np.asarray(human_error_components(second, first, c, params), dtype=float) @ weights)
        sn = cost.setdefault(c.cell, [0.0, 0])
        sn[0] += e
        sn[1] += 1
    return PairTable({k: (v[0], v[1]) for k, v in sorted(agreement.items())},
                     {k: (float(v[0]), int(v[1])) for k, v in sorted(cost.items())})


def select_cost_baseline(cost: Mapping[str, tuple[float, int]], cell: str, n_min: int = 60) -> CostBaseline:
    """The team's mean pair cost for a cell, pooled exactly as R.7 `select_baseline` pools agreement."""
    total, n = cost.get(cell, (0.0, 0))
    if n >= n_min:
        return CostBaseline(total / n, n, "cell")
    case_type = cell.split("|", 1)[0]
    same = [v for k, v in sorted(cost.items()) if k.split("|", 1)[0] == case_type]
    tt, tn = sum(v[0] for v in same), sum(v[1] for v in same)
    if tn >= n_min:
        return CostBaseline(tt / tn, tn, "case_type")
    gt, gn = sum(v[0] for _, v in sorted(cost.items())), sum(v[1] for v in cost.values())
    if gn == 0:
        raise ValueError("no double-coded pairs: the human-human cost baseline is undefined")
    return CostBaseline(gt / gn, gn, "global")


def cell_baselines(table: PairTable, cell: str, n_min: int = 60) -> tuple[Baseline, CostBaseline]:
    return select_baseline(table.agreement, cell, n_min), select_cost_baseline(table.cost, cell, n_min)

