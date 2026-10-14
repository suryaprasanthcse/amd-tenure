"""Frozen intake classification (master §11): prompt, keyword fallback, Gemma loop, freeze and resume."""
import asyncio
import json
from datetime import date
from pathlib import Path

import httpx
import pandas as pd
import pytest

from tenure.domain import Case, CaseType
from tenure.intake import (
    INTAKE_COLUMNS,
    INTAKE_PROMPT,
    INTAKE_SCHEMA,
    KEYWORD_ORDER,
    case_from_record,
    classify_llm,
    freeze,
    intake_messages,
    intake_prompt_sha,
    keyword_agreement,
    keyword_case_type,
    load_examples,
    merge_into_cases,
    parse_intake,
    pending_cases,
    run_intake,
)
from tenure.llm import EndpointProfile, LLMClient
from tenure.prompt import case_block

pytestmark = [pytest.mark.unit, pytest.mark.gate_g01]

MASTER_PROMPT = (
    "Classify the customer's message about an online order into exactly one category.\n"
    "Categories:\n"
    "DELAY (arrived late) · NOT_RECEIVED (has not arrived) · DAMAGED (arrived broken/damaged) ·\n"
    "WRONG_ITEM (a different product arrived) · WRONG_VARIANT (right product, wrong colour/size) ·\n"
    "MISSING_ITEM (order incomplete) · DEFECTIVE (does not work) · QUALITY (works, poor quality) ·\n"
    "REMORSE (wants to return without a defect) · BILLING (charges, invoice, refund of a charge) ·\n"
    "SELLER_SERVICE (seller unresponsive) · POSITIVE (praise, nothing to resolve) ·\n"
    "VAGUE_COMPLAINT (unhappy, issue unclear) · OTHER (none of these).\n"
    "If several apply, choose the one that most determines what the customer is owed.\n"
    'Reply with JSON: {"case_type": "<CATEGORY>"}.'
)


def test_intake_prompt_and_schema():
    assert INTAKE_PROMPT.strip() == MASTER_PROMPT
    assert INTAKE_SCHEMA == {"type": "object", "additionalProperties": False,
                             "properties": {"case_type": {"type": "string", "enum": [t.value for t in CaseType]}},
                             "required": ["case_type"]}
    sha = intake_prompt_sha()
    assert len(sha) == 64 and sha == intake_prompt_sha()


def test_intake_messages_keep_static_prefix_first():
    sys_msgs = intake_messages("o produto chegou quebrado", "system")
    assert sys_msgs[0] == {"role": "system", "content": INTAKE_PROMPT}
    assert sys_msgs[1]["role"] == "user" and "o produto chegou quebrado" in sys_msgs[1]["content"]
    fold = intake_messages("o produto chegou quebrado", "fold")
    assert len(fold) == 1 and fold[0]["content"].startswith(INTAKE_PROMPT)
    assert "o produto chegou quebrado" in fold[0]["content"]
    a, b = intake_messages("mensagem um", "fold")[0]["content"], intake_messages("outra", "fold")[0]["content"]
    assert a[:len(INTAKE_PROMPT)] == b[:len(INTAKE_PROMPT)]
    with pytest.raises(ValueError):
        intake_messages("x", "assistant")


def test_keyword_order_matches_master_11():
    assert KEYWORD_ORDER == (CaseType.BILLING, CaseType.NOT_RECEIVED, CaseType.DAMAGED, CaseType.WRONG_VARIANT,
                             CaseType.WRONG_ITEM, CaseType.MISSING_ITEM, CaseType.DEFECTIVE, CaseType.REMORSE,
                             CaseType.DELAY, CaseType.QUALITY, CaseType.SELLER_SERVICE)


# One Portuguese message per keyword rule, in master §11 order (review_score 3 so no score rule can fire).
KEYWORD_MESSAGES = [
    ("fui cobrado duas vezes no cartão, quero o estorno", CaseType.BILLING),
    ("ainda não recebi meu pedido", CaseType.NOT_RECEIVED),
    ("o produto chegou quebrado", CaseType.DAMAGED),
    ("veio na cor errada, pedi azul", CaseType.WRONG_VARIANT),
    ("recebi outro produto, produto errado", CaseType.WRONG_ITEM),
    ("comprei dois e veio só um", CaseType.MISSING_ITEM),
    ("o aparelho não funciona, veio com defeito", CaseType.DEFECTIVE),
    ("me arrependi da compra, quero devolver", CaseType.REMORSE),
    ("a entrega atrasou muito, demorou demais", CaseType.DELAY),
    ("qualidade ruim, material muito frágil", CaseType.QUALITY),
    ("o vendedor não responde minhas mensagens", CaseType.SELLER_SERVICE),
]


@pytest.mark.parametrize("message,want", KEYWORD_MESSAGES)
def test_intake_keywords(message, want):
    assert keyword_case_type(message, 3) is want


def test_keyword_messages_cover_every_rule_in_order():
    assert tuple(want for _, want in KEYWORD_MESSAGES) == KEYWORD_ORDER


@pytest.mark.parametrize("message,want", [
    ("nao recebi o produto e nem resposta da empresa", CaseType.NOT_RECEIVED),  # unaccented, as in Olist
    ("NÃO FUNCIONA", CaseType.DEFECTIVE),  # case-insensitive
    ("produto veio com DEFEITO", CaseType.DEFECTIVE),
    ("tamanho errado", CaseType.WRONG_VARIANT),
    ("chegou quebrado e atrasado", CaseType.DAMAGED),  # DAMAGED precedes DELAY
    ("cobranca indevida, entrega atrasada", CaseType.BILLING),  # BILLING is first
    ("pedido incompleto", CaseType.MISSING_ITEM),
    ("produto danificado", CaseType.DAMAGED),
    ("nota fiscal não veio", CaseType.BILLING),
])
def test_keyword_variants_and_precedence(message, want):
    assert keyword_case_type(message, 3) is want


@pytest.mark.parametrize("score,want", [(5, CaseType.POSITIVE), (4, CaseType.POSITIVE), (3, CaseType.OTHER),
                                        (2, CaseType.VAGUE_COMPLAINT), (1, CaseType.VAGUE_COMPLAINT),
                                        (None, CaseType.OTHER)])
def test_intake_score_rules(score, want):
    assert keyword_case_type("ok", score) is want
    assert keyword_case_type("", score) is want


def test_keywords_beat_score_rules():
    assert keyword_case_type("chegou quebrado", 5) is CaseType.DAMAGED
    assert keyword_case_type("ótimo, recomendo", 5) is CaseType.POSITIVE


@pytest.mark.parametrize("text,want", [
    ('{"case_type": "DELAY"}', CaseType.DELAY),
    ('```json\n{"case_type": "WRONG_VARIANT"}\n```', CaseType.WRONG_VARIANT),
    ('{"case_type": "SPAM"}', None), ('{"case_type": "delay"}', None), ('{"type": "DELAY"}', None),
    ('{"case_type": "DELAY", "extra": 1}', None), ("DELAY", None), ("", None), (None, None),
])
def test_parse_intake(text, want):
    assert parse_intake(text) is want


# --- Gemma classification loop with keyword fallback ---------------------------------------------------------

def _rows(messages: list[str], scores: list[int | None] | None = None) -> pd.DataFrame:
    scores = scores or [3] * len(messages)
    return pd.DataFrame({"case_id": [f"k{i:03d}" for i in range(len(messages))], "message": messages,
                         "review_score": pd.array(scores, dtype="Int64")})


def _profile() -> EndpointProfile:
    return EndpointProfile(name="test", base_url="http://intake.test/v1", model="gemma-test", max_retries=0)


def test_classify_llm_uses_model_and_falls_back_to_keywords():
    seen_bodies = []
    in_flight = peak = 0

    async def handler(req: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        body = json.loads(req.content)
        seen_bodies.append(body)
        text = body["messages"][-1]["content"]
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        if "ERRO" in text:
            return httpx.Response(400, json={"error": {"message": "bad request"}})
        if "LIXO" in text:
            content = "não sei"
        elif "INVALIDA" in text:
            content = '{"case_type": "SPAM"}'
        else:
            content = '{"case_type": "QUALITY"}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    msgs = ["ERRO chegou quebrado", "LIXO não funciona", "INVALIDA", "bom"] + ["bom"] * 8
    rows = _rows(msgs, [3, 3, 5, 3] + [3] * 8)

    async def go():
        client = LLMClient(_profile(), "k", transport=httpx.MockTransport(handler))
        try:
            return await classify_llm(client, rows, concurrency=3, system_mode="system")
        finally:
            await client.aclose()

    out = asyncio.run(go())
    assert out[:4] == [(CaseType.DAMAGED, "keywords"), (CaseType.DEFECTIVE, "keywords"),
                       (CaseType.POSITIVE, "keywords"), (CaseType.QUALITY, "gemma")]
    assert out[4:] == [(CaseType.QUALITY, "gemma")] * 8
    assert 1 < peak <= 3
    schema = seen_bodies[0]["response_format"]["json_schema"]["schema"]
    assert schema == INTAKE_SCHEMA
    assert seen_bodies[0]["messages"][0] == {"role": "system", "content": INTAKE_PROMPT}


# --- Freeze, merge and resume ---------------------------------------------------------------------------------

def _frozen(ids: list[str], types: list[CaseType], source: str = "gemma") -> pd.DataFrame:
    return pd.DataFrame({"case_id": ids, "case_type": [t.value for t in types], "source": source,
                         "intake_model": "m1", "intake_prompt_sha": "s1"})


def test_freeze_keeps_existing_labels_and_adds_new_ones():
    first = freeze(None, _frozen(["b", "a"], [CaseType.DELAY, CaseType.POSITIVE]))
    assert list(first.columns) == list(INTAKE_COLUMNS) and list(first["case_id"]) == ["a", "b"]
    again = _frozen(["a", "c", "b"], [CaseType.OTHER, CaseType.BILLING, CaseType.OTHER], "keywords")
    second = freeze(first, again)
    assert list(second["case_id"]) == ["a", "b", "c"]
    got = dict(zip(second["case_id"], second["case_type"]))
    assert got == {"a": "POSITIVE", "b": "DELAY", "c": "BILLING"}
    assert dict(zip(second["case_id"], second["source"])) == {"a": "gemma", "b": "gemma", "c": "keywords"}
    assert freeze(second, second).equals(second)


def test_pending_cases_excludes_frozen_and_is_sorted():
    cases = pd.DataFrame({"case_id": ["z", "b", "a", "c"], "message": ["", "", "", ""]})
    frozen = _frozen(["b"], [CaseType.DELAY])
    assert list(pending_cases(cases, frozen)["case_id"]) == ["a", "c", "z"]
    assert list(pending_cases(cases, None)["case_id"]) == ["a", "b", "c", "z"]


def test_merge_into_cases_fills_case_type_only():
    cases = pd.DataFrame({"case_id": ["x", "y", "z"], "case_type": ["", "", ""], "message": ["1", "2", "3"]})
    merged = merge_into_cases(cases, _frozen(["z", "x"], [CaseType.DAMAGED, CaseType.REMORSE]))
    assert list(merged["case_id"]) == ["x", "y", "z"] and list(merged["message"]) == ["1", "2", "3"]
    assert list(merged["case_type"]) == ["REMORSE", "", "DAMAGED"]
    assert list(cases["case_type"]) == ["", "", ""]  # input not mutated


def test_intake_freeze(tmp_path):
    out = tmp_path / "intake.parquet"
    cases = _rows([f"m{i}" for i in range(10)])
    calls: list[list[str]] = []

    def labeller(case_type: CaseType):
        async def classify(df: pd.DataFrame) -> list[tuple[CaseType, str]]:
            calls.append(list(df["case_id"]))
            return [(case_type, "gemma")] * len(df)
        return classify

    first = asyncio.run(run_intake(cases, out, labeller(CaseType.DELAY), "m1", "sha1", batch_size=4))
    assert len(first) == 10 and set(first["case_type"]) == {"DELAY"}
    assert set(first["intake_model"]) == {"m1"} and set(first["intake_prompt_sha"]) == {"sha1"}
    assert pd.read_parquet(out).equals(first)
    assert calls == [["k000", "k001", "k002", "k003"], ["k004", "k005", "k006", "k007"], ["k008", "k009"]]

    calls.clear()
    more = pd.concat([cases, _rows([f"n{i}" for i in range(13)]).iloc[10:]], ignore_index=True)
    second = asyncio.run(run_intake(more, out, labeller(CaseType.OTHER), "m2", "sha2", batch_size=4))
    assert calls == [["k010", "k011", "k012"]]  # only new cases are classified
    old = second[second["case_id"].isin(first["case_id"])].reset_index(drop=True)
    assert old.equals(first)  # existing (case_id → case_type) never changes
    assert set(second.loc[~second["case_id"].isin(first["case_id"]), "case_type"]) == {"OTHER"}


def test_intake_resumes_after_a_failed_batch(tmp_path):
    out = tmp_path / "intake.parquet"
    cases = _rows([f"m{i}" for i in range(6)])
    n = 0

    async def flaky(df: pd.DataFrame) -> list[tuple[CaseType, str]]:
        nonlocal n
        n += 1
        if n == 2:
            raise RuntimeError("endpoint went away")
        return [(CaseType.DAMAGED, "gemma")] * len(df)

    with pytest.raises(RuntimeError):
        asyncio.run(run_intake(cases, out, flaky, "m1", "sha1", batch_size=3))
    assert list(pd.read_parquet(out)["case_id"]) == ["k000", "k001", "k002"]  # first batch persisted
    done = asyncio.run(run_intake(cases, out, flaky, "m1", "sha1", batch_size=3))
    assert list(done["case_id"]) == [f"k{i:03d}" for i in range(6)]


# --- Records → Case, and the few-shot examples ----------------------------------------------------------------

RECORD = {"case_id": "278ed6001ab9ab6f", "contact_date": date(2016, 10, 6), "case_type": "",
          "order_status": "delivered", "days_late": 36, "days_since_delivery": 0, "days_past_estimate": 2,
          "items_total_cents": 2250, "freight_total_cents": 142, "order_total_cents": 2391, "value_band": "V2",
          "photo_evidence": True, "fraud_flag": False, "legal_threat": False, "repeat_customer": False,
          "prior_claims_90d": 0, "review_score": 1, "message": "nao recebi o produto"}


def test_case_from_record():
    c = case_from_record(RECORD, CaseType.NOT_RECEIVED)
    assert isinstance(c, Case) and c.case_type is CaseType.NOT_RECEIVED and c.delivered
    assert (c.days_late, c.order_total_cents, c.contact_date) == (36, 2391, date(2016, 10, 6))
    c2 = case_from_record(RECORD | {"case_type": "DELAY", "contact_date": "2016-10-06", "days_late": pd.NA,
                                    "days_since_delivery": None})
    assert c2.case_type is CaseType.DELAY and c2.days_late is None and not c2.delivered
    assert c2.contact_date == date(2016, 10, 6)
    with pytest.raises(ValueError):
        case_from_record(RECORD)  # no case type yet and none given


def test_case_from_parquet_row_types():
    df = pd.DataFrame([RECORD, RECORD | {"case_id": "x", "days_late": None, "days_since_delivery": None}])
    df["days_late"] = df["days_late"].astype("Int64")
    df["days_since_delivery"] = df["days_since_delivery"].astype("Int64")
    a, b = (case_from_record(r, CaseType.DELAY) for r in df.to_dict("records"))
    assert type(a.days_late) is int and type(a.items_total_cents) is int and type(a.photo_evidence) is bool
    assert b.days_late is None and b.days_since_delivery is None
    case_block(a)  # renders without error


def test_load_examples():
    ex = load_examples(Path(__file__).resolve().parents[2] / "policy" / "examples_v1.jsonl")
    assert len(ex) == 5
    for text, answer in ex:
        assert text.startswith("CASE FILE\n") and text.endswith("Decide.")
        assert set(answer) == {"rationale", "policy_clause", "action", "amount_eur", "requires_return", "confidence"}


def test_intake_with_nothing_pending_writes_an_empty_frozen_file(tmp_path):
    out = tmp_path / "intake.parquet"

    async def never(df: pd.DataFrame) -> list[tuple[CaseType, str]]:
        raise AssertionError("no case should be classified")

    empty = asyncio.run(run_intake(_rows([]), out, never, "m1", "sha1"))
    assert len(empty) == 0 and list(empty.columns) == list(INTAKE_COLUMNS) and out.exists()


def test_keyword_agreement_counts_only_gemma_labels():
    cases = _rows(["chegou quebrado", "chegou quebrado", "atrasou", "ok"], [3, 3, 3, 5])
    frozen = pd.DataFrame({"case_id": ["k000", "k001", "k002", "k003"],
                           "case_type": ["DAMAGED", "QUALITY", "DELAY", "POSITIVE"],
                           "source": ["gemma", "gemma", "gemma", "keywords"], "intake_model": "m",
                           "intake_prompt_sha": "s"})
    assert keyword_agreement(cases, frozen) == {"DAMAGED": (1, 1), "DELAY": (1, 1), "QUALITY": (0, 1)}
