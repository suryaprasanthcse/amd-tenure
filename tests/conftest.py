"""Shared test setup: hypothesis profile, .env loading and the LLM endpoint fixture (RUNBOOK §3.3)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from hypothesis import settings

ROOT = Path(__file__).resolve().parents[1]
MOCK_API_KEY = "mock_key"  # decision D-llm-mock: this key routes llm tests to the in-process mock endpoint

settings.register_profile("ci", max_examples=200, derandomize=True, deadline=2000)
settings.load_profile("ci")


def _load_env_file(path: Path) -> None:
    """Gates run with .env loaded (RUNBOOK §3.2). Variables already set in the environment win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file(ROOT / ".env")


@dataclass(frozen=True)
class LLMEndpoint:
    base_url: str
    api_key: str
    model: str
    transport: httpx.MockTransport | None  # None means the live endpoint

    @property
    def mock(self) -> bool:
        return self.transport is not None


def _schema_instance(schema: dict) -> object:
    """The simplest value that satisfies a JSON schema (first enum value, minimum, null when allowed)."""
    if "anyOf" in schema:
        options = schema["anyOf"]
        return None if {"type": "null"} in options else _schema_instance(options[0])
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if kind == "object":
        return {k: _schema_instance(v) for k, v in schema.get("properties", {}).items()}
    if kind == "number" or kind == "integer":
        return schema.get("minimum", 0)
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    return "mock"


def _request_schema(body: dict) -> dict | None:
    """The structured-output schema of a request in any of the master §13.2 modes, else None (mode none)."""
    rf = body.get("response_format") or {}
    if rf.get("type") == "json_schema":
        return rf["json_schema"]["schema"]
    if isinstance(body.get("structured_outputs"), dict):
        return body["structured_outputs"].get("json")
    return body.get("guided_json")


def mock_openai_transport(model: str, api_key: str) -> httpx.MockTransport:
    """A minimal OpenAI-compatible endpoint: GET /models and POST /chat/completions.

    A constrained request (master §13.2) gets the simplest schema-valid JSON object; an unconstrained one
    gets free text. That exercises the request and parse path only: it says nothing about Gemma's answers.
    """

    def handler(req: httpx.Request) -> httpx.Response:
        if req.headers.get("authorization") != f"Bearer {api_key}":
            return httpx.Response(401, json={"error": {"message": "bad key"}})
        if req.method == "GET" and req.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": model, "object": "model"}]})
        if req.method == "POST" and req.url.path.endswith("/chat/completions"):
            body = json.loads(req.content)
            if body.get("model") != model:
                return httpx.Response(404, json={"error": {"message": "unknown model"}})
            schema = _request_schema(body)
            content = "Olá! Pedido recebido." if schema is None else json.dumps(_schema_instance(schema))
            return httpx.Response(200, json={
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5}})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture(scope="session")
def llm_endpoint() -> LLMEndpoint:
    """The configured TENURE_LLM_* endpoint. A missing variable fails the test; it never skips."""
    names = ("TENURE_LLM_BASE_URL", "TENURE_LLM_API_KEY", "TENURE_LLM_MODEL")
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        pytest.fail(f"llm tests need {', '.join(missing)} (set them in .env)")
    base, key, model = (os.environ[n] for n in names)
    transport = mock_openai_transport(model, key) if key == MOCK_API_KEY else None
    return LLMEndpoint(base, key, model, transport)
