"""Replay of 500 D-replay slice cases against the configured TENURE_LLM_* endpoint (markers llm, data).

Live, the endpoint sits behind the shared 30 RPM limiter (D-rpm), so this takes about 17 minutes. The
outcome store is fresh, so every case is a real endpoint call and the throughput is measured, not cached.
"""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from tenure.config import load
from tenure.runs import ReplayRun, load_primary_humans, load_replay_cases
from tenure.store import OutcomeStore
from tests.fixtures.replay_fake import ROOT

pytestmark = [pytest.mark.llm, pytest.mark.data, pytest.mark.gate_g03]


def test_replay_500_live(llm_endpoint, tmp_path):
    cfg = load(ROOT / "tenure.yaml")
    cases = load_replay_cases(ROOT, cfg, "replay", subset="slice")
    assert len(cases) == 3_000  # D-replay slice
    cases = cases[:500]
    assert [(c.contact_date, c.case_id) for c in cases] == sorted((c.contact_date, c.case_id) for c in cases)
    humans = load_primary_humans(ROOT / cfg.paths.derived / "humans.parquet", [c.case_id for c in cases])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = ROOT / cfg.paths.runs / f"T03-live500-{stamp}"
    run = ReplayRun(cfg, cfg.inference.default_profile, "replay", root=ROOT, cases=cases, humans=humans,
                    store=OutcomeStore(tmp_path / "outcomes.db"), run_dir=run_dir, transport=llm_endpoint.transport,
                    selection={"subset": "slice", "decision": "D-replay", "first_n": 500})
    summary = asyncio.run(run.run())
    m = summary["metrics"]
    assert run.committed == 500 and m["cases"] == 500 and m["endpoint_calls"] == 500
    assert m["error_rate"] < 0.01, m["errors"]
    assert m["cases_per_s"] > 0 and m["p50_ms"] > 0
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))  # throughput recorded
    assert manifest["finished"]["metrics"]["cases_per_min"] == m["cases_per_min"]
    assert manifest["selection"]["decision"] == "D-replay"
    print(f"\nreplay-500 live: {json.dumps(m)}")
