"""Cost-weighted error: over-refund leakage vs under-refund churn risk (master §5).

Each disagreement is reduced to four components c = (leak, short, unesc, missed) so that

    E = leak + kappa * short + C_unesc * unesc + C_missed * missed,   kappa = clv_multiple * churn_max

is linear in the economic parameters. Bootstrap draws of component means can therefore be
re-weighted for any parameter setting without resampling (used by the dashboard sliders).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .domain import Action, Case, Resolution, int_hash
from .schema import AgentDecision, agrees

N_COMPONENTS = 4  # leak (cents), short (cents), unesc (count), missed (count)


@dataclass(frozen=True, slots=True)
class CostParams:
    clv_multiple: float = 0.60  # ASSUMPTION: future margin per customer, as a multiple of order total
    churn_max: float = 0.25  # ASSUMPTION: churn probability when the whole entitlement is withheld
    cogs_ratio: float = 0.60  # ASSUMPTION: cost of goods as a share of item price
    salvage_ratio: float = 0.50  # ASSUMPTION: share of cost recovered from returned goods
    return_shipping_cents: int = 800  # ASSUMPTION: EUR 8 reverse logistics
    unesc_cents: int = 400  # ASSUMPTION: cost of an unnecessary hand-off to a person
    missed_cents: int = 2_500  # ASSUMPTION: expected cost of auto-handling a case a person should see

    @property
    def kappa(self) -> float:
        return self.clv_multiple * self.churn_max

    def weights(self) -> np.ndarray:
        return np.array([1.0, self.kappa, float(self.unesc_cents), float(self.missed_cents)])


def customer_value_cents(action: Action, amount: int | None, c: Case) -> int:
    if action is Action.REFUND_FULL:
        return c.order_total_cents
    if action is Action.REFUND_PARTIAL:
        return amount or 0
    if action is Action.RESHIP:
        return c.items_total_cents
    return 0


def merchant_cost_cents(action: Action, amount: int | None, requires_return: bool, c: Case,
                        p: CostParams) -> float:
    cogs = p.cogs_ratio * c.items_total_cents
    if action is Action.REFUND_FULL:
        if requires_return:
            return c.order_total_cents + p.return_shipping_cents - p.salvage_ratio * cogs
        return float(c.order_total_cents)
    if action is Action.REFUND_PARTIAL:
        return float(amount or 0)
    if action is Action.RESHIP:
        return cogs + c.freight_total_cents + (p.return_shipping_cents if requires_return else 0)
    return 0.0


def error_components(agent: AgentDecision | None, human: Resolution, c: Case,
                     p: CostParams) -> tuple[float, float, int, int]:
    if agrees(agent, human):
        return (0.0, 0.0, 0, 0)
    if agent is None:  # invalid output: the case falls back to a person
        return (0.0, 0.0, 1, 0)
    if human.action is Action.ESCALATE:
        return (0.0, 0.0, 0, 1)
    if agent.action is Action.ESCALATE:
        return (0.0, 0.0, 1, 0)
    leak = max(0.0, merchant_cost_cents(agent.action, agent.amount_cents, agent.requires_return, c, p)
               - merchant_cost_cents(human.action, human.amount_cents, human.requires_return, c, p))
    short = min(float(c.order_total_cents),
                max(0.0, customer_value_cents(human.action, human.amount_cents, c)
                    - customer_value_cents(agent.action, agent.amount_cents, c)))
    return (leak, short, 0, 0)


def human_error_components(other: Resolution, reference: Resolution, c: Case,
                           p: CostParams) -> tuple[float, float, int, int]:
    """Same cost function between two people, for the human-human cost baseline."""
    as_agent = AgentDecision(other.action, other.amount_cents, other.requires_return, other.clause, 1_000_000, "")
    return error_components(as_agent, reference, c, p)


def total_cost(components: np.ndarray, p: CostParams) -> np.ndarray:
    return components @ p.weights()


def bootstrap_component_means(components: np.ndarray, b: int, seed_parts: tuple[str, ...],
                              chunk: int = 256) -> np.ndarray:
    """Percentile-bootstrap draws of the per-case mean of each component, shape (b, 4)."""
    comps = np.asarray(components, dtype=float)
    n = comps.shape[0]
    if n == 0:
        raise ValueError("cannot bootstrap an empty sample")
    rng = np.random.default_rng(int_hash(*seed_parts))
    out = np.empty((b, comps.shape[1]))
    for start in range(0, b, chunk):
        size = min(chunk, b - start)
        idx = rng.integers(0, n, size=(size, n))
        out[start:start + size] = comps[idx].mean(axis=1)
    return out


@dataclass(frozen=True, slots=True)
class CostGate:
    mean: float  # point estimate, cents per case
    ucb: float  # 97.5th percentile of the bootstrap mean
    baseline_mean: float  # human-human mean, cents per case
    threshold: float
    passed: bool


def cost_gate(agent_components: np.ndarray, hh_mean_cents: float, p: CostParams, b: int,
              seed_parts: tuple[str, ...], eps_abs_cents: float = 50.0, rho: float = 0.25) -> CostGate:
    draws = bootstrap_component_means(agent_components, b, seed_parts) @ p.weights()
    mean = float(total_cost(np.asarray(agent_components, dtype=float), p).mean())
    ucb = float(np.quantile(draws, 0.975))
    threshold = hh_mean_cents + max(eps_abs_cents, rho * hh_mean_cents)
    return CostGate(mean, ucb, hh_mean_cents, threshold, ucb <= threshold)
