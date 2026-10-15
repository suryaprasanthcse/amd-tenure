"""Event bus, JSONL recorder and GPU telemetry (master §13.5, §20.2, §20.3, Appendix B)."""
import asyncio
import json
import logging

import pytest

from tenure.events import EventBus, Recorder
from tenure.gpu import GpuPoller, parse_amd_smi, parse_rocm_smi
from tenure.runs import ReplayRun
from tenure.store import OutcomeStore
from tests.fixtures.replay_fake import FAKE_ENV, ROOT, FakeClock, FakeLLM, config, derived_cases, primary_humans

pytestmark = [pytest.mark.integration, pytest.mark.gate_g03]

# Master Appendix B: SSE event name → data keys ("|null" marks a nullable value, not another key).
APPENDIX_B = {
    "run": {"run_id", "mode", "status", "model", "adapter_sha", "profile", "started_at"},
    "progress": {"run_id", "committed", "total", "sim_date", "elapsed_s", "cases_per_s", "prompt_tps",
                 "completion_tps", "cached_ratio", "p50_ms", "p95_ms", "errors", "compression"},
    "cell": {"cell", "level", "ceiling", "pinned", "n", "x", "p_hat", "lower", "baseline", "baseline_scope",
             "threshold", "cost_ucb_cents", "cost_threshold_cents", "detect_days", "blockers"},
    "level": {"cell", "from", "to", "reason", "ledger_seq"},
    "cusum": {"cell", "s", "h", "monitored"},
    "gpu": {"util_pct", "vram_used_gb", "vram_total_gb", "power_w", "device"},
    "business": {"net_median_eur", "net_lo_eur", "net_hi_eur", "error_bound_eur", "autonomous_share",
                 "human_only_eur"},
    "ledger": {"seq", "hash"},
    "alarm": {"kind", "s", "h"},
    "override": None,  # {cell|case_id, op, actor, reason, ledger_seq}
}

ROCM_SAMPLE = {
    "card0": {"GPU use (%)": "37", "GPU Memory Allocated (VRAM%)": "45", "VRAM Total Memory (B)": "205822885888",
              "VRAM Total Used Memory (B)": "92600000000", "Current Socket Graphics Package Power (W)": "512.0"},
    "system": {"Driver version": "6.8.5"},
}
AMD_SAMPLE = [{"gpu": 0, "usage": {"gfx_activity": {"value": 41, "unit": "%"}},
               "power": {"socket_power": {"value": 498, "unit": "W"}},
               "mem_usage": {"total_vram": {"value": 196288, "unit": "MB"}, "used_vram": {"value": 88320, "unit": "MB"}}}]


def test_event_recorder(tmp_path):
    cases = derived_cases(tmp_path, 200)
    run = ReplayRun(config(), "token_factory", "replay", root=ROOT, env=FAKE_ENV, cases=cases,
                    humans=primary_humans(cases), store=OutcomeStore(tmp_path / "o.db"), run_dir=tmp_path / "R-e",
                    transport=FakeLLM(cases).transport(), concurrency=8, tick_k=50, clock=FakeClock(step=0.0005))
    asyncio.run(run.run())
    lines = [json.loads(x) for x in (tmp_path / "R-e" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert lines and all(set(x) == {"t_ms", "event", "data"} for x in lines)
    t = [x["t_ms"] for x in lines]
    assert all(isinstance(v, int) and v >= 0 for v in t) and t == sorted(t)  # monotone, relative to run start
    for x in lines:
        assert x["event"] in APPENDIX_B, x["event"]
        keys = APPENDIX_B[x["event"]]
        if keys is not None:
            assert set(x["data"]) == keys, (x["event"], set(x["data"]) ^ keys)
    assert lines[0]["event"] == "run" and lines[0]["data"]["status"] == "started"
    assert lines[-1]["event"] == "run" and lines[-1]["data"]["status"] == "finished"
    progress = [x["data"] for x in lines if x["event"] == "progress"]
    assert progress[-1]["committed"] == progress[-1]["total"] == 200
    assert [p["committed"] for p in progress] == sorted(p["committed"] for p in progress)
    cells = {x["data"]["cell"] for x in lines if x["event"] == "cell"}
    assert cells == {c.cell for c in cases}


def test_bus_fans_out_and_coalesces():
    clock = FakeClock()
    bus = EventBus(clock=clock)
    q = bus.subscribe()
    seen = []
    bus.add_sink(lambda name, data: seen.append((name, data)))
    assert bus.publish("progress", {"committed": 1})
    assert not bus.publish("progress", {"committed": 2})  # within 100 ms: dropped (≤ 10/s)
    assert bus.publish("progress", {"committed": 3}, force=True)
    clock.t += 0.1
    assert bus.publish("progress", {"committed": 4})
    assert bus.publish("cell", {"cell": "DELAY|V1"}) and not bus.publish("cell", {"cell": "DELAY|V1"})
    assert bus.publish("cell", {"cell": "DELAY|V2"})
    bus.tick()
    assert bus.publish("cell", {"cell": "DELAY|V1"})  # once per cell per tick
    names = [(n, d.get("committed", d.get("cell"))) for n, d in seen]
    assert names == [("progress", 1), ("progress", 3), ("progress", 4), ("cell", "DELAY|V1"), ("cell", "DELAY|V2"),
                     ("cell", "DELAY|V1")]
    got = [q.get_nowait() for _ in range(q.qsize())]
    assert got == seen
    bus.unsubscribe(q)
    bus.publish("ledger", {"seq": 1, "hash": "h"})
    assert q.empty() and seen[-1] == ("ledger", {"seq": 1, "hash": "h"})
    with pytest.raises(ValueError):
        bus.publish("nonsense", {})


def test_bus_slow_subscriber_drops_oldest():
    bus = EventBus(clock=FakeClock())
    q = bus.subscribe(maxsize=2)
    for i in range(4):
        bus.publish("ledger", {"seq": i, "hash": str(i)})
    assert [q.get_nowait()[1]["seq"] for _ in range(2)] == [2, 3]


def test_recorder_appends_monotone_after_restart(tmp_path):
    path = tmp_path / "events.jsonl"
    clock = FakeClock()
    rec = Recorder(path, clock=clock)
    clock.t += 0.5
    rec("ledger", {"seq": 1, "hash": "a"})
    rec.close()
    again = Recorder(path, clock=FakeClock())  # a resumed run: its clock starts at zero again
    again("ledger", {"seq": 2, "hash": "b"})
    again.close()
    t = [json.loads(x)["t_ms"] for x in path.read_text(encoding="utf-8").splitlines()]
    assert t == [500, 500]


def test_gpu_parsers():
    (ev,) = parse_rocm_smi(ROCM_SAMPLE)
    assert ev == {"util_pct": 37.0, "vram_used_gb": 86.24, "vram_total_gb": 191.69, "power_w": 512.0,
                  "device": "card0"}
    (ev,) = parse_amd_smi(AMD_SAMPLE)
    assert ev == {"util_pct": 41.0, "vram_used_gb": 86.25, "vram_total_gb": 191.69, "power_w": 498.0,
                  "device": "gpu0"}
    assert parse_rocm_smi({"system": {}}) == [] and parse_amd_smi([]) == []


def test_gpu_poller_emits_and_falls_back(tmp_path):
    bus = EventBus(clock=FakeClock())
    seen = []
    bus.add_sink(lambda n, d: seen.append((n, d)))
    calls = []

    def run_cmd(cmd):
        calls.append(cmd[0])
        if cmd[0] == "rocm-smi":
            raise OSError("rocm-smi crashed")
        return json.dumps(AMD_SAMPLE)

    poller = GpuPoller(bus, which=lambda name: f"/usr/bin/{name}", run=run_cmd, interval=0.01)
    assert poller.poll_once()[0]["device"] == "gpu0" and calls == ["rocm-smi", "amd-smi"]

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(poller.run(stop))
        await asyncio.sleep(0.05)
        stop.set()
        await task

    asyncio.run(go())
    assert seen and all(n == "gpu" and set(d) == APPENDIX_B["gpu"] for n, d in seen)


def test_gpu_poller_without_tools_logs_once(caplog):
    bus = EventBus(clock=FakeClock())
    seen = []
    bus.add_sink(lambda n, d: seen.append(n))
    poller = GpuPoller(bus, which=lambda name: None, interval=0.01)
    with caplog.at_level(logging.INFO, logger="tenure.gpu"):
        assert poller.poll_once() == [] and poller.poll_once() == []
        asyncio.run(poller.run(asyncio.Event()))  # returns at once: nothing to poll
    assert seen == [] and sum("GPU telemetry" in r.getMessage() for r in caplog.records) == 1
