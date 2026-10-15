"""FakeLLM transport and olist_mini-derived replay cases for the T03 integration tests (master §25).

The FakeLLM is an OpenAI-compatible httpx MockTransport. It serves the completions recorded from the
live endpoint in T01 (tests/fixtures/llm/decide_sample.jsonl) by case_id, and a synthetic completion
for every other case: the policy oracle's decision, with a deterministic share of deliberate errors.
"""
from __future__ import annotations

import json
import random
import re
from collections import Counter
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import httpx

from tenure.config import load
from tenure.domain import Action, Case, CaseType, Resolution, int_hash, value_band
from tenure.humans import PolicySchedule, label_case
from tenure.intake import case_from_record
from tenure.olist import IngestConfig, build_cases, load_tables
from tenure.policy import resolve
from tests.fixtures.olist_mini.build import write_fixture

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "llm" / "decide_sample.jsonl"
CASE_ID_RE = re.compile(r"^case_id: (\S+)$", re.MULTILINE)
USAGE = {"prompt_tokens": 3_000, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 2_900}}
FAKE_ENV = {"TENURE_LLM_BASE_URL": "http://fake.local/v1", "TENURE_LLM_API_KEY": "test-key",
            "TENURE_LLM_MODEL": "gemma-fake"}
SEED = 20261018


def config():
    return load(ROOT / "tenure.yaml", env={})


def fixture_completions() -> dict[str, dict]:
    rows = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {r["case_id"]: r for r in rows}


def derived_cases(tmp_path: Path, n: int = 200) -> list[Case]:
    """n cases built from the two olist_mini cases: the first 30 take the ids, types and totals of the
    recorded completions; the rest get synthetic ids, every case type and varied totals. Three per day."""
    raw = tmp_path / "olist_mini"
    raw.mkdir(parents=True, exist_ok=True)
    write_fixture(raw)
    recs = build_cases(load_tables(raw), IngestConfig(brl_per_eur=Decimal("6.00"))).to_dict("records")
    bands = tuple(sorted(config().bands_cents.items(), key=lambda kv: kv[1]))
    recorded = list(fixture_completions().values())
    rng = random.Random(SEED)
    types = list(CaseType)
    out = []
    for i in range(n):
        base = case_from_record(recs[i % len(recs)], CaseType.DELAY)
        if i < len(recorded):
            r = recorded[i]
            cid, ctype, total = r["case_id"], CaseType(r["case_type"]), int(r["order_total_cents"])
        else:
            cid, ctype, total = f"m{i:05d}", types[i % len(types)], rng.randint(300, 20_000)
        freight = min(base.freight_total_cents, total // 4)
        out.append(replace(base, case_id=cid, case_type=ctype, contact_date=date(2018, 1, 1) + timedelta(days=i // 3),
                           items_total_cents=total - freight, freight_total_cents=freight, order_total_cents=total,
                           value_band=value_band(total, bands), photo_evidence=rng.random() < 0.6, fraud_flag=False,
                           legal_threat=rng.random() < 0.03, repeat_customer=rng.random() < 0.05))
    rng.shuffle(out)  # the run must order cases itself
    return out


def primary_humans(cases: list[Case]) -> dict[str, Resolution]:
    return {c.case_id: label_case(c, PolicySchedule(), SEED).primary for c in cases}


class FakeLLM:
    """Counts endpoint calls per case. `malformed` ids get unparseable or schema-violating text;
    `fail` ids get a non-retryable HTTP status (a transport-level failure, never cached)."""

    def __init__(self, cases: list[Case], malformed: frozenset[str] = frozenset(),
                 fail: frozenset[str] = frozenset(), fail_status: int = 400):
        self.by_id = {c.case_id: c for c in cases}
        self.recorded = fixture_completions()
        self.malformed, self.fail, self.fail_status = malformed, fail, fail_status
        self.calls: Counter[str] = Counter()

    @property
    def total_calls(self) -> int:
        return sum(self.calls.values())

    def text_for(self, c: Case) -> str:
        if c.case_id in self.malformed:
            return ("I would refund this customer." if int_hash("bad", c.case_id) % 2 else
                    '{"action": "REFUND_FULL", "confidence": 7}')
        if c.case_id in self.recorded:
            return self.recorded[c.case_id]["text"]
        r = resolve(c)
        if int_hash("fake", c.case_id) % 5 == 0:  # a deterministic share of agent errors
            r = Resolution(Action.ESCALATE if r.action is not Action.ESCALATE else Action.REQUEST_INFO, None,
                           False, "E6" if r.action is not Action.ESCALATE else "I1")
        elif r.action is Action.REFUND_PARTIAL and int_hash("amount", c.case_id) % 2 == 0:  # right action, wrong amount
            r = Resolution(r.action, min(c.order_total_cents, 3 * (r.amount_cents or 1) + 500), False, r.clause)
        obj = {"rationale": f"Clause {r.clause} applies.", "policy_clause": r.clause, "action": r.action.value,
               "amount_eur": None if r.amount_cents is None else r.amount_cents / 100,
               "requires_return": r.requires_return, "confidence": 0.9}
        return "```json\n" + json.dumps(obj) + "\n```"

    def handler(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        cid = CASE_ID_RE.findall(body["messages"][-1]["content"])[-1]  # the case block comes last
        self.calls[cid] += 1
        if cid in self.fail:
            return httpx.Response(self.fail_status, json={"error": {"message": "injected"}})
        return httpx.Response(200, json={"choices": [{"index": 0, "message": {
            "role": "assistant", "content": self.text_for(self.by_id[cid])}}], "usage": USAGE})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class FakeClock:
    """A settable monotonic clock; each read advances it by `step` seconds."""

    def __init__(self, step: float = 0.0):
        self.t, self.step = 0.0, step

    def __call__(self) -> float:
        self.t += self.step
        return self.t
