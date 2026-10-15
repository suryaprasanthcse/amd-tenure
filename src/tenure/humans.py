"""Synthetic team: historical human decisions with realistic inconsistency (master §14)."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, replace
from datetime import date

from .domain import Action, Case, Resolution, int_hash, unit_hash
from .policy import POLICY_V1, PolicyParams, bp_of, resolve


@dataclass(frozen=True, slots=True)
class HumanProfile:
    hid: str
    slip: float  # probability of picking a confusable alternative action
    late_offset: int  # added to perceived lateness (days)
    window_offset: int  # extra days of return/defect window this person grants
    leniency: float  # probability of a goodwill upgrade for valuable or repeat customers
    rounding_cents: int  # partial refunds are rounded to this step
    amount_sigma: float  # log-normal noise on partial refund amounts


TEAM: tuple[HumanProfile, ...] = (
    HumanProfile("H1", 0.02, 0, 0, 0.05, 1, 0.00),
    HumanProfile("H2", 0.03, 1, 0, 0.10, 100, 0.03),
    HumanProfile("H3", 0.04, 0, 2, 0.15, 50, 0.05),
    HumanProfile("H4", 0.03, -1, 0, 0.05, 100, 0.02),
    HumanProfile("H5", 0.05, 0, -2, 0.20, 100, 0.06),
    HumanProfile("H6", 0.02, 0, 0, 0.25, 1, 0.00),
)

SLIPS: dict[Action, tuple[Action, ...]] = {
    Action.REFUND_FULL: (Action.REFUND_PARTIAL, Action.RESHIP),
    Action.REFUND_PARTIAL: (Action.REFUND_FULL, Action.NO_ACTION),
    Action.RESHIP: (Action.REFUND_FULL,),
    Action.REQUEST_INFO: (Action.NO_ACTION, Action.ESCALATE),
    Action.DENY: (Action.NO_ACTION, Action.REQUEST_INFO),
    Action.ESCALATE: (Action.REQUEST_INFO,),
    Action.NO_ACTION: (Action.REQUEST_INFO,),
}


@dataclass(frozen=True, slots=True)
class PolicySchedule:
    """Which policy the team applies on a given contact date (drift scenarios change it)."""

    entries: tuple[tuple[date, PolicyParams], ...] = ((date.min, POLICY_V1),)

    def at(self, d: date) -> PolicyParams:
        current = self.entries[0][1]
        for start, params in self.entries:
            if d >= start:
                current = params
        return current


@dataclass(frozen=True, slots=True)
class HumanLabel:
    case_id: str
    primary_hid: str
    primary: Resolution
    secondary_hid: str | None
    secondary: Resolution | None


def _perceive(c: Case, h: HumanProfile) -> Case:
    return replace(
        c,
        days_late=None if c.days_late is None else c.days_late + h.late_offset,
        days_past_estimate=c.days_past_estimate + h.late_offset,
        days_since_delivery=None if c.days_since_delivery is None else c.days_since_delivery - h.window_offset,
    )


def _upgrade(r: Resolution, c: Case) -> Resolution:
    if r.action is Action.DENY:
        return Resolution(Action.REFUND_PARTIAL, max(1, bp_of(c.items_total_cents, 1_000)), False, "G1")
    if r.action is Action.REQUEST_INFO and r.clause == "I1":
        return Resolution(Action.REFUND_FULL, None, False, "G1")
    if r.action is Action.REFUND_PARTIAL:
        return Resolution(Action.REFUND_FULL, None, False, "G1")
    return r


def _slip(r: Resolution, c: Case, rng: random.Random) -> Resolution:
    alt = rng.choice(SLIPS[r.action])
    amount = max(1, bp_of(c.items_total_cents, 1_000)) if alt is Action.REFUND_PARTIAL else None
    return Resolution(alt, amount, False, "S0")


def _round_to(cents: int, step: int) -> int:
    return cents if step <= 1 else ((cents + step // 2) // step) * step


def human_decision(h: HumanProfile, c: Case, schedule: PolicySchedule, seed: int) -> Resolution:
    rng = random.Random(int_hash(str(seed), c.case_id, h.hid))
    u_lenient, u_slip = rng.random(), rng.random()  # always drawn, in this order
    r = resolve(_perceive(c, h), schedule.at(c.contact_date))
    if (c.value_band in ("V3", "V4") or c.repeat_customer) and u_lenient < h.leniency:
        r = _upgrade(r, c)
    if u_slip < h.slip:
        r = _slip(r, c, rng)
    if r.action is Action.REFUND_PARTIAL:
        amount = r.amount_cents if r.amount_cents is not None else 1
        if h.amount_sigma > 0:
            amount = int(math.floor(amount * math.exp(rng.gauss(0.0, h.amount_sigma)) + 0.5))
        amount = min(max(_round_to(amount, h.rounding_cents), 1), c.order_total_cents)
        r = Resolution(r.action, amount, r.requires_return, r.clause)
    return r


def label_case(c: Case, schedule: PolicySchedule, seed: int, team: tuple[HumanProfile, ...] = TEAM,
               double_rate: float = 0.15) -> HumanLabel:
    s = str(seed)
    i = int_hash(s, c.case_id, "primary") % len(team)
    primary = human_decision(team[i], c, schedule, seed)
    if unit_hash(s, c.case_id, "double") >= double_rate:
        return HumanLabel(c.case_id, team[i].hid, primary, None, None)
    j = (i + 1 + int_hash(s, c.case_id, "secondary") % (len(team) - 1)) % len(team)
    return HumanLabel(c.case_id, team[i].hid, primary, team[j].hid, human_decision(team[j], c, schedule, seed))
