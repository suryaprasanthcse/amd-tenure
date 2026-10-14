import asyncio
import json
from decimal import Decimal

import httpx
import pandas as pd
import pytest

from tenure.llm import EndpointProfile, LLMClient
from tenure.olist import FILES, IngestConfig, brl_to_eur_cents, build_cases, load_tables
from tenure.store import OutcomeStore, StoredOutcome, run_key
from tests.fixtures.olist_mini.build import write_fixture

pytestmark = [pytest.mark.integration, pytest.mark.gate_g00]

SCHEMA = {"type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"]}


def test_ingestion_fixture(tmp_path):
    write_fixture(tmp_path)
    df = build_cases(load_tables(tmp_path), IngestConfig(brl_per_eur=Decimal("6.00")))
    assert list(df["review_id"]) == ["r1", "r2"]  # duplicate dropped, empty review dropped
    a, b = df.iloc[0], df.iloc[1]
    assert a["days_late"] == 6 and a["days_since_delivery"] == 1 and a["days_past_estimate"] == 7
    assert a["items_total_cents"] == 3_333 and a["freight_total_cents"] == 667 and a["order_total_cents"] == 4_000
    assert a["value_band"] == "V3" and a["legal_threat"] and a["category"] == "bed_bath_table"
    assert pd.isna(b["days_late"]) and pd.isna(b["days_since_delivery"]) and b["days_past_estimate"] == 10
    assert b["order_total_cents"] == 998 and b["split"] == "replay" and b["category"] == "unknown"
    assert len(set(df["case_id"])) == len(df)


def test_missing_column_fails(tmp_path):
    write_fixture(tmp_path)
    pd.DataFrame([dict(order_id="o1")]).to_csv(tmp_path / FILES["payments"], index=False)
    with pytest.raises(ValueError):
        load_tables(tmp_path)


def test_brl_conversion_rounds_half_up():
    assert brl_to_eur_cents(6.03, Decimal("6.00")) == 101  # 100.5 -> 101
    assert brl_to_eur_cents(0, Decimal("6")) == 0


def _ok(content, cached=12):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                     "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                                               "prompt_tokens_details": {"cached_tokens": cached}}})


@pytest.mark.parametrize("mode,key", [("json_schema", "response_format"), ("vllm_structured", "structured_outputs"),
                                      ("vllm_guided", "guided_json"), ("none", None)])
def test_payload_modes(mode, key):
    c = LLMClient(EndpointProfile("t", "http://x/v1", "gemma", structured_mode=mode), "k")
    body = c.payload([{"role": "user", "content": "hi"}], SCHEMA)
    assert body["temperature"] == 0.0 and body["seed"] == 0
    if key:
        assert key in body
    else:
        assert not {"response_format", "structured_outputs", "guided_json"} & set(body)
    asyncio.run(c.aclose())


def test_retry_then_success_and_metrics():
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "0"})
        return _ok('{"a":1}')

    slept = []

    async def fake_sleep(s):
        slept.append(s)

    c = LLMClient(EndpointProfile("t", "http://x/v1", "gemma"), "k", transport=httpx.MockTransport(handler),
                  sleep=fake_sleep)
    out = asyncio.run(c.complete([{"role": "user", "content": "hi"}], SCHEMA))
    assert out.text == '{"a":1}' and out.attempts == 3 and out.cached_tokens == 12 and out.error is None
    assert slept == [0.0, 0.0]


def test_non_retryable_and_exhaustion():
    async def nosleep(s):
        return None

    c = LLMClient(EndpointProfile("t", "http://x/v1", "gemma"), "k",
                  transport=httpx.MockTransport(lambda r: httpx.Response(400)), sleep=nosleep)
    out = asyncio.run(c.complete([{"role": "user", "content": "hi"}]))
    assert out.error == "HTTP_400" and out.attempts == 1
    c2 = LLMClient(EndpointProfile("t", "http://x/v1", "gemma", max_retries=2), "k",
                   transport=httpx.MockTransport(lambda r: httpx.Response(503)), sleep=nosleep)
    out2 = asyncio.run(c2.complete([{"role": "user", "content": "hi"}]))
    assert out2.error == "HTTP_503" and out2.attempts == 3


def test_outcome_store(tmp_path):
    s = OutcomeStore(tmp_path / "o.db")
    k = run_key("gemma", None, "p" * 64, "s" * 64, {"temperature": 0})
    assert k == run_key("gemma", None, "p" * 64, "s" * 64, {"temperature": 0})
    assert k != run_key("gemma", "a" * 64, "p" * 64, "s" * 64, {"temperature": 0})
    s.put(k, "c1", StoredOutcome('{"x":1}', None, 10, 5, 8, 120, 1))
    s.put(k, "c2", StoredOutcome(None, "TIMEOUT", 0, 0, None, 60000, 7))
    assert s.get(k, "c1").text == '{"x":1}' and s.get(k, "c2") is None
    assert s.export(k, tmp_path / "rec.jsonl") == 1
