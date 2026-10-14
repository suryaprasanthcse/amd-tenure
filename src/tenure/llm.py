"""OpenAI-compatible async client for AMD Token Factory or a self-hosted vLLM on ROCm (master §13)."""
from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
STRUCTURED_MODES = ("json_schema", "vllm_structured", "vllm_guided", "none")


@dataclass(frozen=True, slots=True)
class EndpointProfile:
    name: str
    base_url: str  # e.g. https://<token-factory-host>/v1 or http://<droplet>:8000/v1
    model: str
    structured_mode: str = "json_schema"
    max_tokens: int = 256
    temperature: float = 0.0
    seed: int | None = 0
    timeout_s: float = 60.0
    max_retries: int = 6
    backoff_base_s: float = 0.5
    backoff_cap_s: float = 20.0


@dataclass(frozen=True, slots=True)
class Completion:
    text: str | None
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int | None
    latency_ms: int
    attempts: int
    error: str | None  # None | HTTP_<code> | TRANSPORT | TIMEOUT | BAD_RESPONSE


class LLMClient:
    def __init__(self, profile: EndpointProfile, api_key: str, transport: httpx.AsyncBaseTransport | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, rng: random.Random | None = None):
        if profile.structured_mode not in STRUCTURED_MODES:
            raise ValueError(f"structured_mode must be one of {STRUCTURED_MODES}")
        self.p = profile
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._http = httpx.AsyncClient(base_url=profile.base_url.rstrip("/"), transport=transport,
                                       timeout=httpx.Timeout(profile.timeout_s),
                                       headers={"Authorization": f"Bearer {api_key}"})

    async def aclose(self) -> None:
        await self._http.aclose()

    def payload(self, messages: list[dict[str, str]], schema: dict[str, Any] | None) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.p.model, "messages": messages, "max_tokens": self.p.max_tokens,
                                "temperature": self.p.temperature, "top_p": 1.0}
        if self.p.seed is not None:
            body["seed"] = self.p.seed
        if schema is not None:
            mode = self.p.structured_mode
            if mode == "json_schema":
                body["response_format"] = {"type": "json_schema",
                                           "json_schema": {"name": "tenure_decision", "schema": schema, "strict": True}}
            elif mode == "vllm_structured":
                body["structured_outputs"] = {"json": schema}
            elif mode == "vllm_guided":
                body["guided_json"] = schema
        return body

    def _delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(self.p.backoff_cap_s, max(0.0, float(retry_after)))
            except ValueError:
                pass
        return self._rng.uniform(0.0, min(self.p.backoff_cap_s, self.p.backoff_base_s * 2 ** attempt))

    async def complete(self, messages: list[dict[str, str]], schema: dict[str, Any] | None = None) -> Completion:
        body = self.payload(messages, schema)
        start = time.perf_counter()
        error = "TRANSPORT"
        for attempt in range(self.p.max_retries + 1):
            retry_after = None
            try:
                resp = await self._http.post("/chat/completions", json=body)
            except httpx.TimeoutException:
                error = "TIMEOUT"
            except httpx.TransportError:
                error = "TRANSPORT"
            else:
                if resp.status_code == 200:
                    return self._parse(resp, attempt + 1, start)
                error = f"HTTP_{resp.status_code}"
                if resp.status_code not in RETRYABLE_STATUS:
                    break
                retry_after = resp.headers.get("retry-after")
            if attempt < self.p.max_retries:
                await self._sleep(self._delay(attempt, retry_after))
        return Completion(None, 0, 0, None, int((time.perf_counter() - start) * 1000), attempt + 1, error)

    def _parse(self, resp: httpx.Response, attempts: int, start: float) -> Completion:
        latency = int((time.perf_counter() - start) * 1000)
        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
            details = usage.get("prompt_tokens_details") or {}
            return Completion(text, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)),
                              details.get("cached_tokens"), latency, attempts, None)
        except (ValueError, KeyError, IndexError, TypeError):
            return Completion(None, 0, 0, None, latency, attempts, "BAD_RESPONSE")
