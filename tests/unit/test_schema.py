"""Decision schema, parsing, normalisation and agreement (R.5, master §2, §12.4–§12.6, Appendix A)."""
import json

import pytest

from tenure.domain import Action, Resolution
from tenure.policy import CLAUSES
from tenure.schema import ACTIONS, DECISION_SCHEMA, AgentDecision, agrees, eur_to_cents, parse_decision

pytestmark = [pytest.mark.unit, pytest.mark.gate_g01]

OK = {"rationale": "5 days late", "policy_clause": "P1", "action": "REFUND_PARTIAL", "amount_eur": 10.0,
      "requires_return": False, "confidence": 0.9}


def test_parse_and_agree():
    ok = json.dumps(OK)
    p = parse_decision("```json\n" + ok + "\n```", 5_000)
    assert p.valid and p.decision.amount_cents == 1_000
    assert agrees(p.decision, Resolution(Action.REFUND_PARTIAL, 1_090, False, "P1"))
    assert not agrees(p.decision, Resolution(Action.REFUND_PARTIAL, 1_200, False, "P1"))
    assert parse_decision("nope", 5_000).error == "PARSE_ERROR"
    bad = json.loads(ok) | {"policy_clause": "Z9"}
    assert parse_decision(json.dumps(bad), 5_000).error == "UNKNOWN_CLAUSE"
    over = json.loads(ok) | {"amount_eur": 60.0}
    assert parse_decision(json.dumps(over), 5_000).error == "AMOUNT_OUT_OF_RANGE"
    extra = json.loads(ok) | {"extra": 1}
    assert parse_decision(json.dumps(extra), 5_000).error == "SCHEMA_ERROR"
    assert set(DECISION_SCHEMA["required"]) == set(DECISION_SCHEMA["properties"])


def test_schema_shape_matches_master_12_4():
    assert list(DECISION_SCHEMA["properties"]) == ["rationale", "policy_clause", "action", "amount_eur",
                                                   "requires_return", "confidence"]  # rationale first
    assert DECISION_SCHEMA["additionalProperties"] is False
    assert DECISION_SCHEMA["properties"]["policy_clause"]["enum"] == list(CLAUSES)
    assert DECISION_SCHEMA["properties"]["action"]["enum"] == list(ACTIONS) == [a.value for a in Action]
    assert DECISION_SCHEMA["properties"]["rationale"]["maxLength"] == 300
    json.dumps(DECISION_SCHEMA)  # serialisable as a request field


@pytest.mark.parametrize("text", ["", "{", "[1, 2]", "null", "```json\n```", "{\"a\": 1", "42"])
def test_parse_error(text):
    p = parse_decision(text, 5_000)
    assert not p.valid and p.error == "PARSE_ERROR" and p.decision is None


@pytest.mark.parametrize("change", [
    {"confidence": 1.5}, {"confidence": -0.1}, {"rationale": "x" * 301}, {"action": "REFUND_SOME"},
    {"requires_return": "maybe"}, {"amount_eur": -1.0}, {"amount_eur": "ten"},
])
def test_schema_error(change):
    assert parse_decision(json.dumps(OK | change), 5_000).error == "SCHEMA_ERROR"


@pytest.mark.parametrize("missing", ["rationale", "policy_clause", "action", "requires_return", "confidence"])
def test_missing_required_key_is_schema_error(missing):
    obj = {k: v for k, v in OK.items() if k != missing}
    assert parse_decision(json.dumps(obj), 5_000).error == "SCHEMA_ERROR"


def test_amount_rules():
    assert parse_decision(json.dumps(OK | {"amount_eur": None}), 5_000).error == "AMOUNT_MISSING"
    assert parse_decision(json.dumps(OK | {"amount_eur": 0.0}), 5_000).error == "AMOUNT_OUT_OF_RANGE"
    assert parse_decision(json.dumps(OK | {"amount_eur": 0.004}), 5_000).error == "AMOUNT_OUT_OF_RANGE"
    assert parse_decision(json.dumps(OK | {"amount_eur": 50.0}), 5_000).decision.amount_cents == 5_000
    assert parse_decision(json.dumps(OK | {"amount_eur": 50.01}), 5_000).error == "AMOUNT_OUT_OF_RANGE"
    assert parse_decision(json.dumps(OK | {"amount_eur": 0.01}), 5_000).decision.amount_cents == 1


def test_non_partial_amount_is_discarded():
    p = parse_decision(json.dumps(OK | {"action": "REFUND_FULL", "policy_clause": "F1", "amount_eur": 999.0}), 5_000)
    assert p.valid and p.decision.amount_cents is None and p.decision.action is Action.REFUND_FULL


def test_decision_fields_and_payload():
    p = parse_decision("Sure! " + json.dumps(OK | {"confidence": 0.1234567}) + " Done.", 5_000)
    d = p.decision
    assert d == AgentDecision(Action.REFUND_PARTIAL, 1_000, False, "P1", 123_457, "5 days late")
    assert d.as_payload() == {"action": "REFUND_PARTIAL", "amount_cents": 1_000, "requires_return": False,
                              "clause": "P1", "confidence_ppm": 123_457}
    assert p.error is None


@pytest.mark.parametrize("eur,cents", [(0.0, 0), (0.005, 1), (0.015, 2), (4.2, 420), (1.005, 101), (10.0, 1_000),
                                       (1234.565, 123_457), (0.1 + 0.2, 30)])
def test_eur_to_cents_half_up(eur, cents):
    assert eur_to_cents(eur) == cents


def test_agreement_tolerance():
    h = Resolution(Action.REFUND_PARTIAL, 1_000, False, "P1")

    def a(cents):
        return AgentDecision(Action.REFUND_PARTIAL, cents, False, "P1", 0, "")

    assert agrees(a(1_100), h) and agrees(a(900), h) and not agrees(a(1_101), h)  # 10% of 1000 = 100
    small = Resolution(Action.REFUND_PARTIAL, 200, False, "P1")
    assert agrees(a(250), small) and agrees(a(150), small) and not agrees(a(251), small)  # floor of 50 cents
    assert agrees(AgentDecision(Action.DENY, None, True, "D3", 0, ""), Resolution(Action.DENY, None, False, "D2"))
    assert not agrees(AgentDecision(Action.DENY, None, False, "D2", 0, ""), Resolution(Action.ESCALATE, None, False,
                                                                                         "E1"))
    assert not agrees(None, h)
    assert agrees(a(1_120), h, tol_abs_cents=50, tol_rel_bp=1_200) and not agrees(a(1_120), h)
