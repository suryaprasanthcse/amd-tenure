"""Autonomy ladder state machine: promotion gates, ceilings, overrides, CUSUM demotion (master §7)."""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

import numpy as np

from .canonical import fx
from .cost import CostGate, CostParams, cost_gate, error_components
from .cusum import CusumDesign, CusumMonitor, design, shift_for_cell
from .domain import Case, Level, Resolution
from .schema import AgentDecision, agrees
from .stats import Baseline, NIResult, non_inferiority

AUDIT_RATE: Mapping[Level, float] = MappingProxyType(
    {Level.L0: 1.0, Level.L1: 1.0, Level.L2: 0.10, Level.L3: 0.02})


@dataclass(frozen=True, slots=True)
class LevelGate:
    n_min: int  # labelled outcomes in the evidence window
    delta: float  # non-inferiority margin
    min_valid_rate: float  # share of parseable, schema-valid agent outputs
    cost_gate: bool
    monitor_gate: bool  # CUSUM must detect a shift within max_detect_days at this level's audit rate
    dwell: int  # labelled outcomes observed at the current level before moving up


DEFAULT_GATES: Mapping[Level, LevelGate] = MappingProxyType({
    Level.L1: LevelGate(30, 0.10, 0.98, False, False, 0),
    Level.L2: LevelGate(150, 0.05, 0.98, True, True, 50),
    Level.L3: LevelGate(400, 0.03, 0.99, True, True, 100),
})


@dataclass(frozen=True, slots=True)
class LadderConfig:
    gates: Mapping[Level, LevelGate] = DEFAULT_GATES
    arl0_target: float = 500.0
    max_detect_days: float = 60.0
    cost_eps_abs_cents: float = 50.0
    cost_rho: float = 0.25
    bootstrap_b: int = 2_000
    seed: int = 20261018
    high_value_cents: int = 50_000
    confirm_ticks: int = 2  # consecutive passing evaluations required before a promotion


def case_ceiling(c: Case, high_value_cents: int) -> Level:
    """Per-case hard ceiling: fraud, legal threats and high-value orders always reach a person."""
    if c.fraud_flag or c.legal_threat or c.order_total_cents > high_value_cents:
        return Level.L1
    return Level.L3


@dataclass(slots=True)
class CellState:
    cell: str
    level: Level = Level.L0
    ceiling: Level = Level.L3
    pinned: bool = False
    volume_per_day: float = 0.0
    window_id: int = 0
    level_entry_n: int = 0
    pass_streak: int = 0
    agree: list[bool] = field(default_factory=list)
    valid: list[bool] = field(default_factory=list)
    comps: list[tuple[float, float, int, int]] = field(default_factory=list)
    monitor: CusumMonitor | None = None

    @property
    def n(self) -> int:
        return len(self.agree)

    @property
    def x(self) -> int:
        return sum(self.agree)

    @property
    def n_valid(self) -> int:
        return sum(self.valid)


@dataclass(frozen=True, slots=True)
class Assessment:
    cell: str
    current: Level
    target: Level | None
    passed: bool
    blockers: tuple[str, ...]
    ni: NIResult | None
    cost: CostGate | None
    cusum: CusumDesign | None
    detect_days: float | None


@dataclass(frozen=True, slots=True)
class LadderEvent:
    kind: str  # LEVEL_CHANGE | CUSUM_SIGNAL
    payload: dict


def assess(st: CellState, baseline: Baseline, hh_cost_mean: float, cfg: LadderConfig,
           cost_params: CostParams) -> Assessment:
    if st.level is Level.L3:
        return Assessment(st.cell, st.level, None, False, ("AT_TOP",), None, None, None, None)
    target = Level(st.level + 1)
    g = cfg.gates[target]
    blockers: list[str] = []
    if st.pinned:
        blockers.append("PINNED")
    if target > st.ceiling:
        blockers.append("CEILING")
    if st.n < g.n_min:
        blockers.append("N_MIN")
    if st.n == 0:
        return Assessment(st.cell, st.level, target, False, tuple(blockers), None, None, None, None)
    if st.n_valid / st.n < g.min_valid_rate:
        blockers.append("VALID_RATE")
    if st.n - st.level_entry_n < g.dwell:
        blockers.append("DWELL")
    ni = non_inferiority(st.x, st.n, baseline, g.delta)
    if not ni.passed:
        blockers.append("NON_INFERIORITY")
    p0, p1 = shift_for_cell(ni.p_hat, baseline.rate, g.delta)
    cus = design(p0, p1, cfg.arl0_target)
    detect_days: float | None = None
    if g.monitor_gate:
        audits_per_day = AUDIT_RATE[target] * st.volume_per_day
        detect_days = cus.arl1 / audits_per_day if audits_per_day > 0 else math.inf
        if detect_days > cfg.max_detect_days:
            blockers.append("MONITOR_POWER")
    cost: CostGate | None = None
    if g.cost_gate and not blockers:  # the bootstrap is the expensive check: run it last
        cost = cost_gate(np.asarray(st.comps, dtype=float), hh_cost_mean, cost_params, cfg.bootstrap_b,
                         (str(cfg.seed), st.cell, str(st.window_id), str(st.n)),
                         cfg.cost_eps_abs_cents, cfg.cost_rho)
        if not cost.passed:
            blockers.append("COST")
    return Assessment(st.cell, st.level, target, not blockers, tuple(blockers), ni, cost, cus, detect_days)


def _evidence(a: Assessment) -> dict:
    out: dict = {"blockers": list(a.blockers)}
    if a.ni is not None:
        out.update(n=a.ni.n, x=a.ni.x, lower=fx(a.ni.lower), baseline=fx(a.ni.baseline),
                   baseline_scope=a.ni.baseline_scope, delta=fx(a.ni.delta), threshold=fx(a.ni.threshold),
                   newcombe_diff_lower=fx(a.ni.diff_lower))
    if a.cost is not None:
        out.update(cost_mean_cents=fx(a.cost.mean, 2), cost_ucb_cents=fx(a.cost.ucb, 2),
                   cost_threshold_cents=fx(a.cost.threshold, 2))
    if a.cusum is not None:
        out.update(cusum_p0=fx(a.cusum.p0, 3), cusum_p1=fx(a.cusum.p1, 3), cusum_h=fx(a.cusum.h, 4),
                   arl0=fx(a.cusum.arl0, 1), arl1=fx(a.cusum.arl1, 1))
    if a.detect_days is not None:
        out["detect_days"] = fx(a.detect_days, 1) if math.isfinite(a.detect_days) else "inf"
    return out


class Ladder:
    def __init__(self, cfg: LadderConfig, cost_params: CostParams, baselines: Mapping[str, Baseline],
                 hh_cost_means: Mapping[str, float], volumes_per_day: Mapping[str, float],
                 ceilings: Mapping[str, Level] | None = None):
        self.cfg = cfg
        self.cost_params = cost_params
        self.baselines = baselines
        self.hh_cost_means = hh_cost_means
        self.volumes = volumes_per_day
        self.ceilings = ceilings or {}
        self.cells: dict[str, CellState] = {}

    def state(self, cell: str) -> CellState:
        st = self.cells.get(cell)
        if st is None:
            ceiling = self.ceilings.get(cell, self.ceilings.get(cell.split("|", 1)[0], Level.L3))
            st = CellState(cell, ceiling=ceiling, volume_per_day=self.volumes.get(cell, 0.0))
            self.cells[cell] = st
        return st

    def effective_level(self, c: Case) -> Level:
        return min(self.state(c.cell).level, case_ceiling(c, self.cfg.high_value_cents))

    def observe(self, c: Case, agent: AgentDecision | None, human: Resolution) -> list[LadderEvent]:
        """Record one labelled outcome (shadow, suggestion, or audit) for the case's cell."""
        st = self.state(c.cell)
        ok = agrees(agent, human)
        st.agree.append(ok)
        st.valid.append(agent is not None)
        st.comps.append(error_components(agent, human, c, self.cost_params))
        if st.monitor is not None and st.level >= Level.L1 and st.monitor.update(not ok):
            m = st.monitor
            signal = LadderEvent("CUSUM_SIGNAL", {
                "cell": st.cell, "level": st.level.name, "s": fx(m.s, 4), "h": fx(m.d.h, 4),
                "outcomes_monitored": m.n, "change_point_index": m.last_zero})
            return [signal, self._demote(st, "CUSUM")]
        return []

    def evaluate(self) -> list[LadderEvent]:
        """One promotion tick: every cell may move up at most one level."""
        events = []
        for cell in sorted(self.cells):
            st = self.cells[cell]
            a = self.assess(cell)
            st.pass_streak = st.pass_streak + 1 if a.passed else 0
            if a.passed and st.pass_streak >= self.cfg.confirm_ticks and a.target is not None and a.cusum is not None:
                prev = st.level
                st.pass_streak = 0
                st.level = a.target
                st.level_entry_n = st.n
                st.monitor = CusumMonitor(a.cusum)
                events.append(LadderEvent("LEVEL_CHANGE", {
                    "cell": cell, "from": prev.name, "to": a.target.name, "reason": "PROMOTE",
                    "evidence": _evidence(a)}))
        return events

    def assess(self, cell: str) -> Assessment:
        st = self.state(cell)
        return assess(st, self.baselines[cell], self.hh_cost_means[cell], self.cfg, self.cost_params)

    def _demote(self, st: CellState, reason: str) -> LadderEvent:
        prev = st.level
        st.level = Level.L1 if prev >= Level.L2 else Level.L0
        st.agree.clear()
        st.valid.clear()
        st.comps.clear()
        st.window_id += 1
        st.level_entry_n = 0
        st.pass_streak = 0
        st.monitor = None
        return LadderEvent("LEVEL_CHANGE", {"cell": st.cell, "from": prev.name, "to": st.level.name,
                                            "reason": reason, "evidence": {"window_reset": True}})

    def override(self, cell: str, op: str, actor: str, reason: str, level: Level | None = None) -> list[LadderEvent]:
        """Human controls. People may demote, pin and cap a cell; they cannot promote by fiat."""
        st = self.state(cell)
        prev = st.level
        if op == "demote":
            if level is None or level >= st.level:
                raise ValueError("demote needs a level below the current one")
            st.level, st.pinned = level, True
            st.level_entry_n = st.n
            st.monitor = None if level is Level.L0 else st.monitor
        elif op == "pin":
            st.pinned = True
        elif op == "unpin":
            st.pinned = False
        elif op == "set_ceiling":
            if level is None:
                raise ValueError("set_ceiling needs a level")
            st.ceiling = level
            if st.level > level:
                st.level = level
        else:
            raise ValueError(f"unknown override {op}")
        payload = {"cell": cell, "op": op, "actor": actor, "reason": reason,
                   "level": None if level is None else level.name, "from": prev.name, "to": st.level.name,
                   "pinned": st.pinned, "ceiling": st.ceiling.name}
        return [LadderEvent("OVERRIDE", payload)]
