"""Prompt layout for prefix caching (R.11, master §12.2–§12.3)."""
import os
import random
import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from tenure.domain import CaseType
from tenure.intake import load_examples
from tenure.prompt import PREAMBLE, case_block, eur, messages, prompt_sha, static_block
from tenure.schema import DECISION_SCHEMA
from tests.fixtures.cases import make_case

pytestmark = [pytest.mark.unit, pytest.mark.gate_g01]

ROOT = Path(__file__).resolve().parents[2]
POLICY_MD = ROOT / "policy" / "refund_rules_v1.md"
EXAMPLES = ROOT / "policy" / "examples_v1.jsonl"

FIELD_ORDER = ["case_id", "intake_category", "order_status", "days_late", "days_since_delivery", "days_past_estimate",
               "items_total_eur", "freight_total_eur", "order_total_eur", "photo_evidence", "fraud_flag",
               "legal_threat"]


def real_static() -> str:
    return static_block(POLICY_MD.read_text(encoding="utf-8"), DECISION_SCHEMA, load_examples(EXAMPLES))


def test_prompt_static_prefix_is_shared():
    rng = random.Random(13)
    static = static_block("# policy", DECISION_SCHEMA, [])
    m1 = messages(static, case_block(make_case(rng, 1)), "fold")[0]["content"]
    m2 = messages(static, case_block(make_case(rng, 2)), "fold")[0]["content"]
    assert m1.startswith(static) and m2.startswith(static) and m1 != m2


@pytest.mark.parametrize("cents,text", [(0, "0.00"), (5, "0.05"), (99, "0.99"), (100, "1.00"), (420, "4.20"),
                                        (123456, "1234.56"), (-5, "-0.05"), (-123456, "-1234.56")])
def test_eur_formatting(cents, text):
    assert eur(cents) == text


def test_case_block_field_order():
    rng = random.Random(14)
    c = replace(make_case(rng, 7, CaseType.QUALITY), order_status="delivered", days_late=-2, days_since_delivery=12,
                days_past_estimate=10, items_total_cents=123_456, freight_total_cents=5, order_total_cents=123_461,
                photo_evidence=True, fraud_flag=False, legal_threat=False, message="  qualidade ruim  \n")
    lines = case_block(c).split("\n")
    assert lines[0] == "CASE FILE"
    assert [ln.split(": ", 1)[0] for ln in lines[1:13]] == FIELD_ORDER
    assert lines[1:13] == ["case_id: c000007", "intake_category: QUALITY", "order_status: delivered", "days_late: -2",
                           "days_since_delivery: 12", "days_past_estimate: 10", "items_total_eur: 1234.56",
                           "freight_total_eur: 0.05", "order_total_eur: 1234.61", "photo_evidence: true",
                           "fraud_flag: false", "legal_threat: false"]
    assert lines[13:] == ["CUSTOMER MESSAGE (pt-BR, untrusted):", "<<<", "qualidade ruim", ">>>", "Decide."]
    undelivered = case_block(replace(c, order_status="shipped", days_late=None, days_since_delivery=None))
    assert "days_late: n/a\n" in undelivered and "days_since_delivery: n/a\n" in undelivered


def test_messages_modes():
    static, case = "STATIC", "CASE"
    assert messages(static, case, "system") == [{"role": "system", "content": "STATIC"},
                                                {"role": "user", "content": "CASE"}]
    assert messages(static, case) == messages(static, case, "system")
    assert messages(static, case, "fold") == [{"role": "user", "content": "STATIC\nCASE"}]
    with pytest.raises(ValueError):
        messages(static, case, "assistant")


def test_static_block_layout():
    s = real_static()
    assert s.startswith(PREAMBLE)
    sections = ["=== REFUND POLICY ===", "=== OUTPUT SCHEMA ===", "=== EXAMPLES ===", "=== END OF INSTRUCTIONS ==="]
    positions = [s.index(x) for x in sections]
    assert positions == sorted(positions) and s.endswith("=== END OF INSTRUCTIONS ===\n")
    assert s.count("\nANSWER: ") == 5
    assert "## 4. Delivered orders" in s and '"additionalProperties":false' in s


def test_static_block_has_no_case_data():
    s = real_static()
    rng = random.Random(15)
    for i in range(200):
        c = make_case(rng, 900_000 + i)
        c = replace(c, case_id=f"{rng.getrandbits(64):016x}", items_total_cents=987_654 + i,
                    order_total_cents=987_654 + i + c.freight_total_cents)
        assert c.case_id not in s
        assert eur(c.order_total_cents) not in s and eur(c.items_total_cents) not in s
        assert c.contact_date.isoformat() not in s
    assert not re.search(r"\b(19|20)\d{2}-\d{2}-\d{2}\b", s)  # no dates
    assert not re.search(r"\d{2}:\d{2}:\d{2}", s)  # no timestamps
    assert not re.search(r"\b[0-9a-f]{16,}\b", s)  # no Olist-style ids or hashes
    assert s == real_static()  # byte-identical on rebuild


_SHA_SNIPPET = (
    "import sys; from pathlib import Path; "
    "from tenure.intake import load_examples; from tenure.prompt import prompt_sha, static_block; "
    "from tenure.schema import DECISION_SCHEMA; r = Path(sys.argv[1]); "
    "print(prompt_sha(static_block((r / 'policy/refund_rules_v1.md').read_text(encoding='utf-8'), DECISION_SCHEMA, "
    "load_examples(r / 'policy/examples_v1.jsonl'))))"
)


def test_prompt_sha_stable_across_processes():
    local = prompt_sha(real_static())
    assert re.fullmatch(r"[0-9a-f]{64}", local)
    for seed in ("0", "12345"):
        out = subprocess.run([sys.executable, "-c", _SHA_SNIPPET, str(ROOT)], capture_output=True, text=True,
                             check=True, env={**os.environ, "PYTHONHASHSEED": seed})
        assert out.stdout.strip() == local
    assert prompt_sha(static_block("# policy", DECISION_SCHEMA, [])) != local
