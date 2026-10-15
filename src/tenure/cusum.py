"""Bernoulli CUSUM for drift demotion, with Brook-Evans ARL design (master §6)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np


def llr_weights(p0: float, p1: float) -> tuple[float, float]:
    """Log-likelihood-ratio increments for a disagreement (w1 > 0) and an agreement (w0 < 0)."""
    if not 0.0 < p0 < p1 < 1.0:
        raise ValueError("require 0 < p0 < p1 < 1")
    return math.log(p1 / p0), math.log((1.0 - p1) / (1.0 - p0))


def arl(w1: float, w0: float, p: float, h: float, m: int = 400) -> float:
    """Average run length from S=0 to S>=h when P(disagreement)=p (Markov-chain approximation).

    State 0 holds S = 0 exactly; states 1..m discretise (0, h) into cells of width h/m,
    represented by their midpoints. Accuracy against Monte Carlo is within about 5%.
    """
    if h <= 0:
        raise ValueError("h must be positive")
    width = h / m
    values = np.concatenate(([0.0], (np.arange(1, m + 1) - 0.5) * width))
    r = np.zeros((m + 1, m + 1))
    for i, v in enumerate(values):
        for inc, prob in ((w1, p), (w0, 1.0 - p)):
            nv = v + inc
            if nv >= h:
                continue  # absorbed: signal
            j = 0 if nv <= 0.0 else min(m, max(1, math.ceil(nv / width)))
            r[i, j] += prob
    try:
        lengths = np.linalg.solve(np.eye(m + 1) - r, np.ones(m + 1))
    except np.linalg.LinAlgError:
        return math.inf
    out = float(lengths[0])
    return out if math.isfinite(out) and out > 0 else math.inf


@dataclass(frozen=True, slots=True)
class CusumDesign:
    p0: float
    p1: float
    h: float
    w1: float
    w0: float
    arl0: float  # expected labelled outcomes between false alarms when in control
    arl1: float  # expected labelled outcomes to detect a shift to p1


@lru_cache(maxsize=4096)
def design(p0: float, p1: float, arl0_target: float = 500.0, m: int = 400, tol: float = 1e-4) -> CusumDesign:
    """Smallest h whose in-control ARL reaches arl0_target (doubling bracket, then bisection)."""
    w1, w0 = llr_weights(p0, p1)
    lo, hi = 0.0, w1
    for _ in range(64):
        if arl(w1, w0, p0, hi, m) >= arl0_target:
            break
        lo, hi = hi, hi * 2.0
    else:
        raise RuntimeError("could not bracket the CUSUM threshold")
    while hi - lo > tol:
        mid = 0.5 * (lo + hi)
        if arl(w1, w0, p0, mid, m) >= arl0_target:
            hi = mid
        else:
            lo = mid
    return CusumDesign(p0, p1, hi, w1, w0, arl(w1, w0, p0, hi, m), arl(w1, w0, p1, hi, m))


def shift_for_cell(p_agree: float, baseline_rate: float, delta: float, floor: float = 0.01,
                   min_shift: float = 0.05, cap: float = 0.5) -> tuple[float, float]:
    """In-control and out-of-control disagreement rates for a newly promoted cell.

    p0 is the cell's current disagreement rate. p1 is the rate at which the cell would no longer
    be non-inferior (1 - (baseline - delta)), and at least p0 + min_shift. Rounded for caching.
    """
    p0 = min(cap - min_shift, max(floor, 1.0 - p_agree))
    p1 = min(cap, max(1.0 - (baseline_rate - delta), p0 + min_shift))
    return round(p0, 3), round(p1, 3)


@dataclass(slots=True)
class CusumMonitor:
    d: CusumDesign
    s: float = 0.0
    n: int = 0
    last_zero: int = 0  # outcome index where S was last 0: the change-point estimate
    trace: list[float] = field(default_factory=list)

    def update(self, disagree: bool) -> bool:
        self.n += 1
        self.s = max(0.0, self.s + (self.d.w1 if disagree else self.d.w0))
        if self.s == 0.0:
            self.last_zero = self.n
        self.trace.append(self.s)
        return self.s >= self.d.h
