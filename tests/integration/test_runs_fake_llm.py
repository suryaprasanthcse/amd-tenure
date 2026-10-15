"""ReplayRun end to end against a FakeLLM transport (master §16, I5, I6, I7)."""
import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest

from tenure.domain import Action
from tenure.runs import AGENT_LABELS, HUMAN_LABELS, ReplayRun
from tenure.store import OutcomeStore
from tests.fixtures.replay_fake import FAKE_ENV, ROOT, FakeClock, FakeLLM, config, derived_cases, primary_humans

pytestmark = [pytest.mark.integration, pytest.mark.gate_g03]


def make_run(tmp_path: Path, name: str, cases, fake: FakeLLM, store: str = "outcomes.db", cls=ReplayRun, **kw):
    return cls(config(), "token_factory", "replay", root=ROOT, env=FAKE_ENV, cases=cases,
               humans=primary_humans(cases), store=OutcomeStore(tmp_path / store), run_dir=tmp_path / "runs" / name,
               transport=fake.transport(), concurrency=8, tick_k=kw.pop("tick_k", 50), **kw)


def records(run: ReplayRun) -> list[dict]:
    return [json.loads(line) for line in run.decisions_path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def cases(tmp_path):
    return derived_cases(tmp_path, 200)


def test_run_replay_deterministic(tmp_path, cases):
    fake = FakeLLM(cases)
    first = make_run(tmp_path, "R-a", cases, fake)
    asyncio.run(first.run())
    assert fake.total_calls == 200 and first.committed == 200
    order = [r["case_id"] for r in records(first)]
    assert order == [c.case_id for c in sorted(cases, key=lambda c: (c.contact_date, c.case_id))]  # I6
    second = make_run(tmp_path, "R-b", cases, fake)
    summary = asyncio.run(second.run())
    assert fake.total_calls == 200  # the second run is served entirely from the outcome cache
    assert second.snapshot() == first.snapshot()
    again = make_run(tmp_path, "R-a", cases, fake)  # restarting a finished run is a no-op
    assert asyncio.run(again.run())["snapshot_sha"] == summary["snapshot_sha"]
    assert again.resumed_from == 200 and fake.total_calls == 200
    assert second.metrics()["store_hits"] == 200 and second.metrics()["endpoint_calls"] == 0
    manifest = json.loads((tmp_path / "runs" / "R-b" / "manifest.json").read_text(encoding="utf-8"))
    for key in ("run_id", "mode", "split", "profile", "model", "adapter_sha", "policy_sha", "prompt_sha",
                "schema_sha", "decoding", "run_key", "config_sha", "cases", "seed", "tick_k"):
        assert key in manifest, key
    assert manifest["run_id"] == "R-b" and manifest["run_key"] == first.run_key == second.run_key
    assert manifest["cases"] == 200 and manifest["finished"]["committed"] == 200


def test_resume_after_crash(tmp_path, cases):
    class Crashing(ReplayRun):
        def commit(self, i, case, outcome):
            if self.committed == 120:
                raise RuntimeError("injected crash")
            super().commit(i, case, outcome)

    fake = FakeLLM(cases)
    crashed = make_run(tmp_path, "R-c", cases, fake, cls=Crashing)
    with pytest.raises(RuntimeError, match="injected crash"):
        asyncio.run(crashed.run())
    calls_before = fake.total_calls
    assert calls_before >= 120
    resumed = make_run(tmp_path, "R-c", cases, fake)
    asyncio.run(resumed.run())
    assert resumed.committed == 200
    assert 0 < resumed.resumed_from <= 120 and resumed.resumed_from % 50 == 0
    assert fake.total_calls - calls_before <= 200 - 120  # committed cases come back from the cache
    ids = [r["case_id"] for r in records(resumed)]
    assert len(ids) == 200 and len(set(ids)) == 200  # no duplicate outcomes
    n_rows = resumed.store.conn.execute("SELECT COUNT(*) FROM outcomes WHERE run_key = ?",
                                        (resumed.run_key,)).fetchone()[0]
    assert n_rows == 200
    reference = make_run(tmp_path, "R-ref", cases, FakeLLM(cases), store="reference.db")
    asyncio.run(reference.run())
    assert resumed.snapshot() == reference.snapshot()


def test_invalid_counts_as_disagreement(tmp_path, cases):
    bad = frozenset(c.case_id for c in cases[:20])
    run = make_run(tmp_path, "R-i", cases, FakeLLM(cases, malformed=bad))
    asyncio.run(run.run())
    recs = {r["case_id"]: r for r in records(run)}
    for cid in bad:  # I5: never ignored, always a disagreement
        assert recs[cid]["valid"] is False and recs[cid]["agree"] is False
        assert recs[cid]["decision"] is None and recs[cid]["error"] in ("PARSE_ERROR", "SCHEMA_ERROR")
    snap = run.snapshot()
    assert snap["valid"] == 200 - len(bad) and sum(snap["errors"].values()) >= len(bad)
    invalid_row = AGENT_LABELS.index("INVALID")
    assert sum(sum(cell["matrix"][invalid_row]) for cell in snap["cells"].values()) == len(bad)
    assert snap["agree"] == sum(r["agree"] for r in recs.values())


def test_transport_errors_not_cached(tmp_path, cases):
    failing = frozenset(c.case_id for c in cases[:10])
    first = make_run(tmp_path, "R-t1", cases, FakeLLM(cases, fail=failing))
    asyncio.run(first.run())
    recs = {r["case_id"]: r for r in records(first)}
    assert all(recs[c]["error"] == "HTTP_400" and not recs[c]["valid"] and not recs[c]["agree"] for c in failing)
    stored = {cid for (cid,) in first.store.conn.execute("SELECT case_id FROM outcomes WHERE run_key = ?",
                                                          (first.run_key,))}
    assert len(stored) == 190 and not stored & failing  # I7
    healthy = FakeLLM(cases)
    second = make_run(tmp_path, "R-t2", cases, healthy)
    asyncio.run(second.run())
    assert set(healthy.calls) == failing and healthy.total_calls == 10
    assert all(r["valid"] for r in records(second) if r["case_id"] in failing)


def test_confusion_matrix_sums(tmp_path, cases):
    run = make_run(tmp_path, "R-m", cases, FakeLLM(cases))
    asyncio.run(run.run())
    snap = run.snapshot()
    assert snap["agent_labels"] == list(AGENT_LABELS) and snap["human_labels"] == list(HUMAN_LABELS)
    assert len(AGENT_LABELS) == 8 and len(HUMAN_LABELS) == 7 and AGENT_LABELS[:7] == HUMAN_LABELS
    per_cell = Counter(c.cell for c in cases)
    assert set(snap["cells"]) == set(per_cell)
    recs = records(run)
    for cell, s in snap["cells"].items():
        m = s["matrix"]
        assert len(m) == 8 and all(len(row) == 7 for row in m)
        assert sum(map(sum, m)) == per_cell[cell] == s["n"]
        diagonal = sum(m[i][i] for i in range(7))
        # The diagonal is "same action"; agreement also needs a partial amount within tolerance (§2.2).
        assert diagonal == s["x"] + s["amount_off"]
        assert s["x"] == sum(r["agree"] for r in recs if r["cell"] == cell)
        off = [r for r in recs if r["cell"] == cell and r["valid"] and not r["agree"]
               and r["decision"]["action"] == r["human_action"]]
        assert s["amount_off"] == len(off) and all(r["human_action"] == Action.REFUND_PARTIAL.value for r in off)
    assert sum(s["x"] for s in snap["cells"].values()) == snap["agree"]
    assert snap["committed"] == sum(per_cell.values()) == 200


def test_metrics(tmp_path, cases):
    run = make_run(tmp_path, "R-x", cases, FakeLLM(cases))
    asyncio.run(run.run())
    m = run.metrics(elapsed_s=50.0)
    assert m["cases"] == 200 and m["cases_per_s"] == pytest.approx(4.0) and m["cases_per_min"] == pytest.approx(240)
    assert m["prompt_tps"] == pytest.approx(200 * 3_000 / 50) and m["completion_tps"] == pytest.approx(200 * 50 / 50)
    assert m["cached_ratio"] == pytest.approx(2_900 / 3_000)
    dates = sorted(c.contact_date for c in cases)
    span_s = (dates[-1] - dates[0]).days * 86_400
    assert m["sim_days"] == (dates[-1] - dates[0]).days and m["compression"] == pytest.approx(span_s / 50.0)
    assert m["error_rate"] == 0 and m["errors"] == {} and m["attempts"] == {"1": 200}
    assert m["endpoint_calls"] == 200 and m["store_hits"] == 0
    assert isinstance(m["p50_ms"], int) and m["p95_ms"] >= m["p50_ms"]
    clock = FakeClock()
    timed = make_run(tmp_path, "R-y", cases, FakeLLM(cases), clock=clock)
    clock.step = 0.001
    asyncio.run(timed.run())
    assert timed.metrics()["wall_s"] > 0  # the injected clock drives wall time
