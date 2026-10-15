"""A small synthetic TENURE world for the T05 CLI tests: cases.parquet (train, replay, live), humans.parquet,
humans_drift.parquet (D-POLICY) and one replay run directory whose decisions.jsonl comes from a mock agent
(the v1 policy oracle with a deterministic share of errors and invalid outputs). No LLM is involved."""
from __future__ import annotations

import json
import random
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from tenure.canonical import canonical_bytes, sha256_hex
from tenure.cli import humans_table
from tenure.domain import Action, Case, CaseType, Resolution, unit_hash
from tenure.humans import PolicySchedule
from tenure.policy import POLICY_V1, POLICY_V2
from tenure.schema import AgentDecision, agrees
from tests.fixtures.cases import make_case
from tests.fixtures.frames import cases_frame
from tests.fixtures.ladder_fx import oracle
from tests.fixtures.replay_fake import SEED

RUN_ID = "R-world"
# (case type, band, band range in cents, share of the replay cases)
MIX = ((CaseType.POSITIVE, "V1", (300, 1_499), 0.60), (CaseType.DELAY, "V2", (1_500, 3_999), 0.20),
       (CaseType.QUALITY, "V2", (1_500, 3_999), 0.10), (CaseType.BILLING, "V1", (300, 1_499), 0.10))


def world_cases(prefix: str, n: int, start: date, days: int, seed: int) -> list[Case]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        u, acc = rng.random(), 0.0
        for ctype, band, (lo, hi), share in MIX:
            acc += share
            if u < acc:
                break
        c = make_case(rng, i, ctype)
        total = rng.randint(lo, hi)
        freight = min(c.freight_total_cents, total // 3)
        out.append(replace(c, case_id=f"{prefix}{i:05d}", contact_date=start + timedelta(days=i * days // n),
                           value_band=band, order_total_cents=total, items_total_cents=total - freight,
                           freight_total_cents=freight))
    return out


def mock_decision(c: Case) -> dict | None:
    """The fake replay agent: oracle v1, 2% deterministic errors, 1 in 400 invalid."""
    if unit_hash("world-invalid", c.case_id) < 1 / 400:
        return None
    a = oracle(c)
    if unit_hash("world-error", c.case_id) < 0.02:
        a = replace(a, action=Action.ESCALATE if a.action is not Action.ESCALATE else Action.REQUEST_INFO,
                    amount_cents=None, clause="E6" if a.action is not Action.ESCALATE else "I1")
    return a.as_payload()


def build_world(tmp: Path) -> dict:
    train = world_cases("t", 2_000, date(2017, 1, 1), 360, 1)
    replay = world_cases("r", 3_000, date(2018, 1, 1), 181, 2)
    live = world_cases("l", 1_200, date(2018, 7, 1), 62, 3)
    frame = pd.concat([cases_frame(train, "train"), cases_frame(replay, "replay"), cases_frame(live, "live")],
                      ignore_index=True)
    derived = tmp / "derived"
    derived.mkdir(parents=True)
    frame.to_parquet(derived / "cases.parquet", index=False)
    humans = humans_table(frame, PolicySchedule(), SEED)
    humans.to_parquet(derived / "humans.parquet", index=False)
    drift = PolicySchedule(((date.min, POLICY_V1), (date(2018, 8, 1), POLICY_V2)))
    humans_table(frame[frame["split"] == "live"], drift, SEED).to_parquet(derived / "humans_drift.parquet",
                                                                           index=False)
    primary = {r["case_id"]: Resolution(Action(r["primary_action"]),
                                        None if pd.isna(r["primary_amount_cents"]) else int(r["primary_amount_cents"]),
                                        bool(r["primary_requires_return"]), str(r["primary_clause"]))
               for r in humans.to_dict("records")}
    runs = tmp / "runs"
    run_dir = runs / RUN_ID
    run_dir.mkdir(parents=True)
    ordered = sorted(replay, key=lambda c: (c.contact_date, c.case_id))
    lines = []
    for c in ordered:
        d = mock_decision(c)
        h = primary[c.case_id]
        agent = None if d is None else AgentDecision(Action(d["action"]), d["amount_cents"], d["requires_return"],
                                                     d["clause"], d["confidence_ppm"], "")
        rec = {"run_id": RUN_ID, "case_id": c.case_id, "cell": c.cell, "decision": d,
               "decision_sha": None if d is None else sha256_hex(canonical_bytes(d)), "valid": d is not None,
               "error": None if d is not None else "PARSE_ERROR", "human_action": h.action.value,
               "human_amount_cents": h.amount_cents, "agree": agrees(agent, h), "effective_level": "L0"}
        lines.append(json.dumps(rec, sort_keys=True))
    (run_dir / "decisions.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest = {"run_id": RUN_ID, "mode": "replay", "split": "replay", "model": "mock", "run_key": "k" * 24,
                "cases": len(ordered), "selection": None, "finished": {"committed": len(ordered)}}
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    env = {"TENURE_DERIVED_DIR": str(derived), "TENURE_RUNS_DIR": str(runs),
           "TENURE_OUTCOMES_DB": str(tmp / "outcomes.db")}
    return {"frame": frame, "humans": humans, "replay": ordered, "live": live, "runs": runs, "run_dir": run_dir,
            "env": env, "derived": derived}
