"""Agent decision schema, parsing, normalisation and the agreement predicate (master §2, §12.4–§12.6)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .domain import Action, Resolution
from .policy import CLAUSES, bp_of

ACTIONS: tuple[str, ...] = tuple(a.value for a in Action)

# Hand-written so property order (rationale first) and keywords stay portable across
# structured-output backends (xgrammar, outlines, OpenAI-style json_schema).
DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "rationale": {"type": "string", "maxLength": 300},
        "policy_clause": {"type": "string", "enum": list(CLAUSES)},
        "action": {"type": "string", "enum": list(ACTIONS)},
        "amount_eur": {"anyOf": [{"type": "number", "minimum": 0}, {"type": "null"}]},
        "requires_return": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["rationale", "policy_clause", "action", "amount_eur", "requires_return", "confidence"],
}


class _DecisionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)
    rationale: str = Field(max_length=300)
    policy_clause: str
    action: Action
    amount_eur: float | None = Field(default=None, ge=0)
    requires_return: bool
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True, slots=True)
class AgentDecision:
    action: Action
    amount_cents: int | None
    requires_return: bool
    clause: str
    confidence_ppm: int
    rationale: str

    def as_payload(self) -> dict[str, Any]:
        return {"action": self.action.value, "amount_cents": self.amount_cents,
                "requires_return": self.requires_return, "clause": self.clause,
                "confidence_ppm": self.confidence_ppm}


@dataclass(frozen=True, slots=True)
class Parsed:
    decision: AgentDecision | None
    error: str | None  # PARSE_ERROR | SCHEMA_ERROR | UNKNOWN_CLAUSE | AMOUNT_MISSING | AMOUNT_OUT_OF_RANGE

    @property
    def valid(self) -> bool:
        return self.decision is not None


def eur_to_cents(x: float) -> int:
    return int((Decimal(repr(x)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _extract_json(text: str) -> Any | None:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def parse_decision(text: str, order_total_cents: int) -> Parsed:
    obj = _extract_json(text)
    if not isinstance(obj, dict):
        return Parsed(None, "PARSE_ERROR")
    try:
        m = _DecisionModel.model_validate(obj)
    except ValidationError:
        return Parsed(None, "SCHEMA_ERROR")
    if m.policy_clause not in CLAUSES:
        return Parsed(None, "UNKNOWN_CLAUSE")
    amount: int | None = None
    if m.action is Action.REFUND_PARTIAL:
        if m.amount_eur is None:
            return Parsed(None, "AMOUNT_MISSING")
        amount = eur_to_cents(m.amount_eur)
        if amount <= 0 or amount > order_total_cents:
            return Parsed(None, "AMOUNT_OUT_OF_RANGE")
    return Parsed(AgentDecision(m.action, amount, m.requires_return, m.policy_clause,
                                int(round(m.confidence * 1_000_000)), m.rationale), None)


def agrees(agent: AgentDecision | None, human: Resolution, tol_abs_cents: int = 50,
           tol_rel_bp: int = 1_000) -> bool:
    """Same action and, for partial refunds, an amount within max(EUR 0.50, 10%) of the human's."""
    if agent is None or agent.action is not human.action:
        return False
    if agent.action is not Action.REFUND_PARTIAL:
        return True
    assert agent.amount_cents is not None and human.amount_cents is not None
    tol = max(tol_abs_cents, bp_of(human.amount_cents, tol_rel_bp))
    return abs(agent.amount_cents - human.amount_cents) <= tol
