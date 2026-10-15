"""Agreement statistics: Wilson continuity-corrected bounds and non-inferiority (master §3, §4)."""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

Z95 = 1.959963984540054  # two-sided 95% (one-sided alpha = 0.025), the non-inferiority convention


def wilson_cc_interval(x: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval with continuity correction (Newcombe 1998, method 4)."""
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= x <= n:
        raise ValueError("x must lie in [0, n]")
    p = x / n
    q = 1.0 - p
    z2 = z * z
    denom = 2.0 * (n + z2)
    if x == 0:
        lo = 0.0
    else:
        rad = max(0.0, z2 - 2.0 - 1.0 / n + 4.0 * p * (n * q + 1.0))
        lo = (2.0 * n * p + z2 - 1.0 - z * math.sqrt(rad)) / denom
    if x == n:
        hi = 1.0
    else:
        rad = max(0.0, z2 + 2.0 - 1.0 / n + 4.0 * p * (n * q - 1.0))
        hi = (2.0 * n * p + z2 + 1.0 + z * math.sqrt(rad)) / denom
    return max(0.0, lo), min(1.0, hi)


def wilson_cc_lower(x: int, n: int, z: float = Z95) -> float:
    return wilson_cc_interval(x, n, z)[0]


def newcombe_diff_cc(x1: int, n1: int, x2: int, n2: int, z: float = Z95) -> tuple[float, float]:
    """Interval for p1 - p2 from continuity-corrected Wilson limits (Newcombe 1998b, method 11)."""
    p1, p2 = x1 / n1, x2 / n2
    l1, u1 = wilson_cc_interval(x1, n1, z)
    l2, u2 = wilson_cc_interval(x2, n2, z)
    d = p1 - p2
    return d - math.hypot(p1 - l1, u2 - p2), d + math.hypot(u1 - p1, p2 - l2)


@dataclass(frozen=True, slots=True)
class Baseline:
    x: int  # agreeing double-coded pairs
    n: int  # double-coded pairs
    scope: str  # "cell" | "case_type" | "global"

    @property
    def rate(self) -> float:
        return self.x / self.n


def select_baseline(pairs: Mapping[str, tuple[int, int]], cell: str, n_min: int = 60) -> Baseline:
    """Human-human agreement for a cell, pooling up to case type, then globally, when sparse."""
    x, n = pairs.get(cell, (0, 0))
    if n >= n_min:
        return Baseline(x, n, "cell")
    case_type = cell.split("|", 1)[0]
    tx = sum(v[0] for k, v in pairs.items() if k.split("|", 1)[0] == case_type)
    tn = sum(v[1] for k, v in pairs.items() if k.split("|", 1)[0] == case_type)
    if tn >= n_min:
        return Baseline(tx, tn, "case_type")
    gx = sum(v[0] for v in pairs.values())
    gn = sum(v[1] for v in pairs.values())
    if gn == 0:
        raise ValueError("no double-coded pairs: the human-human baseline is undefined")
    return Baseline(gx, gn, "global")


@dataclass(frozen=True, slots=True)
class NIResult:
    x: int
    n: int
    p_hat: float
    lower: float
    baseline: float
    baseline_scope: str
    delta: float
    threshold: float
    passed: bool
    diff_lower: float  # Newcombe lower limit of (agent - human); reported, not gated by default
    passed_strict: bool


def non_inferiority(x: int, n: int, baseline: Baseline, delta: float, z: float = Z95) -> NIResult:
    """H0: p_agent <= p_hh - delta. Passed when the Wilson-cc lower bound clears p_hh - delta."""
    if n <= 0:
        raise ValueError("n must be positive")
    lower = wilson_cc_lower(x, n, z)
    b = baseline.rate
    threshold = b - delta
    diff_lower, _ = newcombe_diff_cc(x, n, baseline.x, baseline.n, z)
    return NIResult(x, n, x / n, lower, b, baseline.scope, delta, threshold, lower >= threshold,
                    diff_lower, diff_lower >= -delta)
