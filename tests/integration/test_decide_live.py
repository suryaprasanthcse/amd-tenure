"""Spike S3 and intake against the configured TENURE_LLM_* endpoint (marker llm, master §13.2, §24).

With TENURE_LLM_API_KEY=mock_key (decision D-llm-mock) these run against the in-process mock from
tests/conftest.py, which answers constrained requests with a schema-valid object. That exercises the
prompt, request and parse path but does NOT verify S3: only a live run does.
"""
import asyncio
from pathlib import Path

import pandas as pd
import pytest

from tenure.config import endpoint, load
from tenure.domain import CaseType
from tenure.intake import case_from_record, classify_llm, keyword_case_type, load_examples
from tenure.llm import LLMClient
from tenure.prompt import case_block, messages, static_block
from tenure.schema import DECISION_SCHEMA, parse_decision

ROOT = Path(__file__).resolve().parents[2]

pytestmark = [pytest.mark.llm, pytest.mark.gate_g01]


def replay_sample(n: int) -> pd.DataFrame:
    """The first n replay cases by case_id: a fixed, deterministic sample."""
    df = pd.read_parquet(ROOT / "data" / "derived" / "cases.parquet")
    return df[df["split"] == "replay"].sort_values("case_id").head(n).reset_index(drop=True)


def test_s3_valid_rate(llm_endpoint):
    cfg = load(ROOT / "tenure.yaml")
    profile, key = endpoint(cfg)
    system_mode = cfg.inference.profiles[cfg.inference.default_profile].system_mode
    static = static_block((ROOT / cfg.policy.file).read_text(encoding="utf-8"), DECISION_SCHEMA,
                          load_examples(ROOT / cfg.policy.examples))
    cases = [case_from_record(r, CaseType(r["case_type"]) if r["case_type"] else
                              keyword_case_type(r["message"], None if pd.isna(r["review_score"])
                                                else int(r["review_score"])))
             for r in replay_sample(100).to_dict("records")]
    assert len(cases) == 100

    async def go():
        client = LLMClient(profile, key, transport=llm_endpoint.transport)
        sem = asyncio.Semaphore(16)

        async def one(c):
            async with sem:
                return await client.complete(messages(static, case_block(c), system_mode), DECISION_SCHEMA)

        try:
            return await asyncio.gather(*(one(c) for c in cases))
        finally:
            await client.aclose()

    comps = asyncio.run(go())
    parsed = [parse_decision(comp.text or "", c.order_total_cents) for comp, c in zip(comps, cases)]
    valid = sum(p.valid for p in parsed)
    errors = sorted({comp.error or p.error for comp, p in zip(comps, parsed) if not p.valid})
    assert valid / len(cases) >= 0.99, f"{profile.structured_mode}/{system_mode}: {valid}/100 valid; {errors}"


def test_intake_live_sample(llm_endpoint):
    cfg = load(ROOT / "tenure.yaml")
    profile, key = endpoint(cfg)
    system_mode = cfg.inference.profiles[cfg.inference.default_profile].system_mode
    rows = replay_sample(50)

    async def go():
        client = LLMClient(profile, key, transport=llm_endpoint.transport)
        try:
            return await classify_llm(client, rows, concurrency=16, system_mode=system_mode)
        finally:
            await client.aclose()

    out = asyncio.run(go())
    assert len(out) == 50
    assert all(isinstance(t, CaseType) for t, _ in out)
    sources = [s for _, s in out]
    assert set(sources) <= {"gemma", "keywords"}
    assert sources.count("gemma") >= 48, f"only {sources.count('gemma')}/50 classified by the model"
