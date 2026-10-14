"""Spike S1 against the configured TENURE_LLM_* endpoint (marker llm).

With TENURE_LLM_API_KEY=mock_key (decision D-llm-mock) these run against the in-process mock from
tests/conftest.py, which exercises the client path but does NOT verify S1. Any other key hits the
live endpoint.
"""
import asyncio
from pathlib import Path

import httpx
import pytest

from tenure.config import endpoint, load
from tenure.llm import LLMClient

ROOT = Path(__file__).resolve().parents[2]

pytestmark = [pytest.mark.llm, pytest.mark.gate_g00]


def test_models_endpoint_lists_configured_model(llm_endpoint):
    with httpx.Client(base_url=llm_endpoint.base_url.rstrip("/"), transport=llm_endpoint.transport, timeout=30,
                      headers={"Authorization": f"Bearer {llm_endpoint.api_key}"}) as client:
        resp = client.get("/models")
    assert resp.status_code == 200, f"GET /models returned HTTP {resp.status_code}"
    ids = [m["id"] for m in resp.json()["data"]]
    assert llm_endpoint.model in ids, f"configured model not served; endpoint lists {ids}"


def test_one_completion_nonempty(llm_endpoint):
    profile, key = endpoint(load(ROOT / "tenure.yaml"))
    assert profile.model == llm_endpoint.model and key == llm_endpoint.api_key

    async def go():
        client = LLMClient(profile, key, transport=llm_endpoint.transport)
        try:
            return await client.complete([{"role": "user", "content": "Responda apenas: ok"}])
        finally:
            await client.aclose()

    out = asyncio.run(go())
    assert out.error is None, f"completion failed with {out.error}"
    assert out.text and out.text.strip()
    assert out.completion_tokens > 0
