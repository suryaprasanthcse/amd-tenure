"""Policy oracle: the executable twin of policy/refund_rules_v1.md (master §12.1).

The synthetic team (humans.py) decides with this oracle plus human noise. The agent never sees
this code; it sees only the markdown policy. tests/unit/test_policy_doc_sync.py keeps the two in step.
"""
from __future__ import annotations

from dataclasses import dataclass

from .domain import Action, Case, CaseType, Resolution

CLAUSES: tuple[str, ...] = (
    "E1", "E2", "E3", "E4", "E5", "E6",
    "F1", "F2", "F3", "F4", "F5",
    "P1", "P2", "P3",
    "R1", "R2",
    "D1", "D2", "D3",
    "I1", "I2", "I3",
    "N1", "N2", "N3",
)


@dataclass(frozen=True, slots=True)
class PolicyParams:
    version: str = "v1"
    high_value_cents: int = 50_000  # E3: order total above EUR 500
    late_full_days: int = 5  # F1, F3: more than 5 days late
    window_days: int = 30  # F4, F5, P3, D1, D3: 30-day window after delivery
    minor_delay_mode: str = "freight"  # P1: "freight" (v1) or "fixed" (v2 drift scenario)
    minor_delay_fixed_cents: int = 500
    minor_delay_fallback_bp: int = 500  # P1: 5% of items when no freight was charged
    wrong_variant_bp: int = 2_000  # P2: 20% of items
    quality_bp: int = 1_000  # P3: 10% of items


POLICY_V1 = PolicyParams()
POLICY_V2 = PolicyParams(version="v2", minor_delay_mode="fixed")


def bp_of(cents: int, bp: int) -> int:
    """Basis points of a non-negative cent amount, rounded half-up to the cent."""
    if cents < 0 or bp < 0:
        raise ValueError("bp_of takes non-negative inputs")
    return (cents * bp + 5_000) // 10_000


def _r(action: Action, clause: str, amount: int | None = None, ret: bool = False) -> Resolution:
    return Resolution(action, amount, ret, clause)


def _partial(c: Case, cents: int, clause: str) -> Resolution:
    return _r(Action.REFUND_PARTIAL, clause, min(max(cents, 1), c.order_total_cents))


def resolve(c: Case, p: PolicyParams = POLICY_V1) -> Resolution:
    t = c.case_type
    # Escalations are checked first, in order (policy §1).
    if c.fraud_flag:
        return _r(Action.ESCALATE, "E1")
    if c.legal_threat:
        return _r(Action.ESCALATE, "E2")
    if c.order_total_cents > p.high_value_cents and t is not CaseType.POSITIVE:
        return _r(Action.ESCALATE, "E3")
    if t is CaseType.BILLING:
        return _r(Action.ESCALATE, "E4")
    if t is CaseType.NOT_RECEIVED and c.delivered:
        return _r(Action.ESCALATE, "E5")
    if t is CaseType.OTHER:
        return _r(Action.ESCALATE, "E6")
    # No-compensation categories.
    if t is CaseType.POSITIVE:
        return _r(Action.NO_ACTION, "N1")
    if t is CaseType.SELLER_SERVICE:
        return _r(Action.NO_ACTION, "N2")
    if t is CaseType.VAGUE_COMPLAINT:
        return _r(Action.REQUEST_INFO, "I2")
    # Orders that have not been delivered.
    if t in (CaseType.DELAY, CaseType.NOT_RECEIVED) and not c.delivered:
        if c.days_past_estimate > p.late_full_days:
            return _r(Action.REFUND_FULL, "F3")
        return _r(Action.NO_ACTION, "N3")
    if not c.delivered:
        return _r(Action.REQUEST_INFO, "I3")
    # Delivered orders.
    if t is CaseType.DELAY:
        late = c.days_late if c.days_late is not None else 0
        if late > p.late_full_days:
            return _r(Action.REFUND_FULL, "F1")
        if late >= 1:
            if p.minor_delay_mode == "fixed":
                return _partial(c, p.minor_delay_fixed_cents, "P1")
            if c.freight_total_cents > 0:
                return _partial(c, c.freight_total_cents, "P1")
            return _partial(c, bp_of(c.items_total_cents, p.minor_delay_fallback_bp), "P1")
        return _r(Action.DENY, "D2")
    if t is CaseType.DAMAGED:
        return _r(Action.REFUND_FULL, "F2") if c.photo_evidence else _r(Action.REQUEST_INFO, "I1")
    if t is CaseType.WRONG_ITEM:
        return _r(Action.RESHIP, "R1", ret=True)
    if t is CaseType.MISSING_ITEM:
        return _r(Action.RESHIP, "R2")
    if t is CaseType.WRONG_VARIANT:
        return _partial(c, bp_of(c.items_total_cents, p.wrong_variant_bp), "P2")
    assert c.days_since_delivery is not None
    within = c.days_since_delivery <= p.window_days
    if t is CaseType.DEFECTIVE:
        return _r(Action.REFUND_FULL, "F4", ret=True) if within else _r(Action.DENY, "D3")
    if t is CaseType.QUALITY:
        return _partial(c, bp_of(c.items_total_cents, p.quality_bp), "P3") if within else _r(Action.DENY, "D3")
    if t is CaseType.REMORSE:
        return _r(Action.REFUND_FULL, "F5", ret=True) if within else _r(Action.DENY, "D1")
    raise ValueError(f"unhandled case type {t}")
