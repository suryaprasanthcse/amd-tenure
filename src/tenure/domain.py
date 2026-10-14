"""Core domain types shared by every TENURE module (master §1, §2)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from enum import IntEnum, StrEnum


class Action(StrEnum):
    REFUND_FULL = "REFUND_FULL"
    REFUND_PARTIAL = "REFUND_PARTIAL"
    RESHIP = "RESHIP"
    REQUEST_INFO = "REQUEST_INFO"
    DENY = "DENY"
    ESCALATE = "ESCALATE"
    NO_ACTION = "NO_ACTION"


class CaseType(StrEnum):
    DELAY = "DELAY"
    NOT_RECEIVED = "NOT_RECEIVED"
    DAMAGED = "DAMAGED"
    WRONG_ITEM = "WRONG_ITEM"
    WRONG_VARIANT = "WRONG_VARIANT"
    MISSING_ITEM = "MISSING_ITEM"
    DEFECTIVE = "DEFECTIVE"
    QUALITY = "QUALITY"
    REMORSE = "REMORSE"
    BILLING = "BILLING"
    SELLER_SERVICE = "SELLER_SERVICE"
    POSITIVE = "POSITIVE"
    VAGUE_COMPLAINT = "VAGUE_COMPLAINT"
    OTHER = "OTHER"


class Level(IntEnum):
    L0 = 0  # shadow: the human decides, the agent is only compared
    L1 = 1  # suggest: the agent proposes, the human accepts or edits
    L2 = 2  # act: the agent decides, 10% of its cases are audited
    L3 = 3  # act: the agent decides, 2% of its cases are audited


# Lower edges of the order-value bands, in EUR cents (master §1.2).
VALUE_BANDS: tuple[tuple[str, int], ...] = (("V1", 0), ("V2", 1_500), ("V3", 4_000), ("V4", 10_000))


def value_band(order_total_cents: int, bands: tuple[tuple[str, int], ...] = VALUE_BANDS) -> str:
    if order_total_cents < 0:
        raise ValueError("order total must be non-negative")
    label = bands[0][0]
    for name, lower in bands:
        if order_total_cents >= lower:
            label = name
    return label


def cell_id(case_type: CaseType, band: str) -> str:
    return f"{case_type.value}|{band}"


@dataclass(frozen=True, slots=True)
class Case:
    """The decision-relevant view of one customer contact (master §10.4)."""

    case_id: str
    contact_date: date
    case_type: CaseType
    order_status: str
    days_late: int | None  # delivered date - estimated date; None when not delivered
    days_since_delivery: int | None  # contact date - delivered date; None when not delivered
    days_past_estimate: int  # contact date - estimated date (negative = before the estimate)
    items_total_cents: int
    freight_total_cents: int
    order_total_cents: int
    value_band: str
    photo_evidence: bool
    fraud_flag: bool
    legal_threat: bool
    repeat_customer: bool
    message: str

    @property
    def cell(self) -> str:
        return cell_id(self.case_type, self.value_band)

    @property
    def delivered(self) -> bool:
        return self.days_since_delivery is not None


@dataclass(frozen=True, slots=True)
class Resolution:
    """A decision: what is done for the customer, under which clause."""

    action: Action
    amount_cents: int | None  # set only for REFUND_PARTIAL
    requires_return: bool
    clause: str


def _digest(parts: tuple[str, ...]) -> bytes:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()


def unit_hash(*parts: str) -> float:
    """Deterministic uniform number in [0, 1) derived from string parts."""
    return int.from_bytes(_digest(parts)[:8], "big") / 2**64


def int_hash(*parts: str) -> int:
    """Deterministic 64-bit integer derived from string parts."""
    return int.from_bytes(_digest(parts)[:8], "big")
