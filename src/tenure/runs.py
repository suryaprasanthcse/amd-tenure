"""Replay orchestration, the ladder wiring and the go-live simulation (master §7, §16, §17).

decide() runs concurrently inside ReplayEngine (R.13): an outcome-cache hit, else one endpoint call whose
successful completion is cached (R.16, I7). commit() runs strictly in (contact_date, case_id) order (I6):
it compares the agent with the primary human decision (an invalid output is a disagreement, I5), updates
the per-cell confusion matrix and counters, calls the ladder hook, and buffers the DECISION record. Every
tick_k cases the buffer is flushed to decisions.jsonl (the ledger takes this over in T06) and progress and
cell events go out. A restart with the same run directory resumes after the last flushed record (§16.5).

T05 wires the autonomy ladder (R.9) in through LadderBridge: commit → Ladder.observe, every tick_k →
Ladder.evaluate (§7.5). LiveRun streams the live split by contact date from a seeded ladder, routes each case by
its effective level, audits L2/L3 cases by unit_hash (§7.1, §17.1), injects drift (§17.2), applies the
policy-update procedure (§17.3) and runs the global drift alarm (§6.7). Ladder events are buffered in order for
the ledger (T06 persists them).
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from itertools import groupby
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

import httpx
import pandas as pd

from .baselines import TRAIN_REPLAY, cell_baselines, pair_table
from .canonical import canonical_bytes, sha256_hex
from .config import TenureConfig, _hashable, config_sha, endpoint
from .cost import CostParams
from .cusum import CusumMonitor, design
from .domain import Action, Case, CaseType, Level, Resolution, cell_id, unit_hash
from .events import EventBus, Recorder
from .gpu import GpuPoller
from .intake import INTAKE_SUBSETS, case_from_record, load_examples, merge_into_cases, subset_cases
from .ladder import Ladder, LadderConfig, LadderEvent, LevelGate
from .llm import Completion, LLMClient
from .policy import POLICY_V1, PolicyParams, resolve
from .prompt import case_block, messages, prompt_sha, static_block
from .replay import ReplayEngine
from .schema import DECISION_SCHEMA, AgentDecision, Parsed, agrees, parse_decision
from .stats import Baseline
from .store import OutcomeStore, StoredOutcome, run_key

HUMAN_LABELS: tuple[str, ...] = tuple(a.value for a in Action)
AGENT_LABELS: tuple[str, ...] = HUMAN_LABELS + ("INVALID",)  # master §16.3: 7 actions + INVALID vs 7 actions
_AGENT_ROW = {label: i for i, label in enumerate(AGENT_LABELS)}
_HUMAN_COL = {label: i for i, label in enumerate(HUMAN_LABELS)}
GENESIS = "0" * 64


@dataclass(frozen=True, slots=True)
class Outcome:
    completion: Completion
    parsed: Parsed
    from_store: bool


@dataclass(frozen=True, slots=True)
class _Stat:
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int | None
    latency_ms: int
    attempts: int
    transport_error: str | None
    parse_error: str | None
    from_store: bool


class LadderHook(Protocol):
    def observe(self, case: Case, record: Mapping[str, Any]) -> None: ...

    def evaluate(self) -> list[tuple[str, dict[str, Any]]]: ...

    def level(self, cell: str) -> str: ...


class NoLadder:
    """The ladder hook until T05: every cell stays in shadow (L0) and no level changes."""

    def observe(self, case: Case, record: Mapping[str, Any]) -> None:
        return None

    def evaluate(self) -> list[tuple[str, dict[str, Any]]]:
        return []

    def level(self, cell: str) -> str:
        return "L0"


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _pct(sorted_values: Sequence[int], q: float) -> int | None:
    n = len(sorted_values)
    return sorted_values[min(n - 1, int(q * n))] if n else None


def _new_cell() -> dict[str, Any]:
    return {"n": 0, "x": 0, "valid": 0, "amount_off": 0, "matrix": [[0] * len(HUMAN_LABELS) for _ in AGENT_LABELS]}


class ReplayRun:
    def __init__(self, config: TenureConfig, profile: str | None = None, split: str = "replay",
                 adapter: str | None = None, *, cases: Iterable[Case], humans: Mapping[str, Resolution],
                 store: OutcomeStore, run_dir: str | Path, root: str | Path = ".",
                 env: Mapping[str, str] | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 bus: EventBus | None = None, ladder: LadderHook | None = None, gpu: GpuPoller | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 concurrency: int | None = None, max_ahead: int | None = None, tick_k: int | None = None,
                 adapter_sha: str | None = None, selection: Mapping[str, Any] | None = None, record: bool = True):
        root = Path(root)
        self.cfg = config
        self.split = split
        prof, self._api_key = endpoint(config, env, profile)
        if adapter is not None:  # vLLM serves a LoRA adapter under its own model name (--lora-modules)
            prof = replace(prof, model=adapter)
        self.profile = prof
        pcfg = config.inference.profiles[prof.name]
        self.system_mode = pcfg.system_mode
        self.concurrency = concurrency or pcfg.concurrency
        self.max_ahead = max_ahead or max(64, 8 * self.concurrency)
        self.tick_k = tick_k or config.ladder.tick_k
        self.adapter = adapter
        self.adapter_sha = adapter_sha or (None if adapter is None else sha256_hex(adapter))

        policy_path = root / config.policy.file
        self.static = static_block(policy_path.read_text(encoding="utf-8"), DECISION_SCHEMA,
                                   load_examples(root / config.policy.examples))
        self.prompt_sha = prompt_sha(self.static)
        self.policy_sha = sha256_hex(policy_path.read_bytes())
        self.schema_sha = sha256_hex(canonical_bytes(DECISION_SCHEMA))
        decoding = config.inference.decoding.model_dump()
        keyed = decoding | {"structured_mode": prof.structured_mode, "system_mode": self.system_mode}
        self.run_key = run_key(prof.model, self.adapter_sha, self.prompt_sha, self.schema_sha, _hashable(keyed))

        self.cases = sorted(cases, key=lambda c: (c.contact_date, c.case_id))
        ids = [c.case_id for c in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate case_id in the replay cases")
        missing = [i for i in ids if i not in humans]
        if missing:
            raise ValueError(f"{len(missing)} cases have no human decision (first: {missing[0]})")
        self.humans = humans
        self.store = store
        self.run_dir = Path(run_dir)
        self.run_id = self.run_dir.name
        self.decisions_path = self.run_dir / "decisions.jsonl"
        self.manifest_path = self.run_dir / "manifest.json"
        self._clock, self._now = clock, now
        self.bus = bus or EventBus(clock=clock)
        self.ladder: LadderHook = ladder or NoLadder()
        self.gpu = gpu
        self._transport = transport
        self._record = record
        self.manifest: dict[str, Any] = {
            "run_id": self.run_id, "mode": "replay", "split": split, "profile": prof.name, "model": prof.model,
            "adapter": adapter, "adapter_sha": self.adapter_sha, "policy_sha": self.policy_sha,
            "prompt_sha": self.prompt_sha, "schema_sha": self.schema_sha, "decoding": decoding,
            "structured_mode": prof.structured_mode, "system_mode": self.system_mode, "run_key": self.run_key,
            "config_sha": config_sha(config), "cases": len(self.cases),
            "first_date": self.cases[0].contact_date.isoformat() if self.cases else None,
            "last_date": self.cases[-1].contact_date.isoformat() if self.cases else None,
            "seed": config.seed, "tick_k": self.tick_k, "concurrency": self.concurrency, "max_ahead": self.max_ahead,
            "rpm": pcfg.rpm, "selection": dict(selection) if selection else None,
        }
        self._reset()

    # --- state ---------------------------------------------------------------------------------------------

    def _reset(self) -> None:
        self.committed = self.valid = self.agree = 0
        self.resumed_from = 0
        self.errors: Counter[str] = Counter()
        self._cells: dict[str, dict[str, Any]] = {}
        self.decisions_sha = GENESIS
        self.pending_decisions: list[dict[str, Any]] = []  # DECISION records not yet flushed (ledger batch, T06)
        self._session: list[_Stat] = []
        self._touched: set[str] = set()
        self._first_date: date | None = None
        self._last_date: date | None = None
        self._t0: float | None = None
        self._t1: float | None = None
        self._client: LLMClient | None = None
        self._aborted = False
        self._evaluated_at = -1  # committed count of the last ladder evaluation: one look per count

    def _apply(self, rec: Mapping[str, Any]) -> None:
        """Fold one DECISION record into the counters, the cell matrix and the decisions hash chain."""
        cell = self._cells.setdefault(rec["cell"], _new_cell())
        agent = rec["decision"]["action"] if rec["decision"] is not None else "INVALID"
        cell["matrix"][_AGENT_ROW[agent]][_HUMAN_COL[rec["human_action"]]] += 1
        cell["n"] += 1
        cell["x"] += int(rec["agree"])
        cell["valid"] += int(rec["valid"])
        if rec["valid"] and not rec["agree"] and agent == rec["human_action"]:
            cell["amount_off"] += 1  # same action, partial amount outside the §2.2 tolerance
        self.committed += 1
        self.valid += int(rec["valid"])
        self.agree += int(rec["agree"])
        if rec["error"]:
            self.errors[rec["error"]] += 1
        body = {k: v for k, v in rec.items() if k != "run_id"}
        self.decisions_sha = sha256_hex(self.decisions_sha.encode("ascii") + canonical_bytes(body))
        self._touched.add(rec["cell"])

    def snapshot(self) -> dict[str, Any]:
        """Deterministic run state (no timings): equal for any two runs over the same cached completions."""
        return {"run_key": self.run_key, "split": self.split, "cases": len(self.cases), "committed": self.committed,
                "valid": self.valid, "agree": self.agree, "errors": dict(sorted(self.errors.items())),
                "agent_labels": list(AGENT_LABELS), "human_labels": list(HUMAN_LABELS),
                "cells": {k: {**v, "matrix": [list(r) for r in v["matrix"]]} for k, v in sorted(self._cells.items())},
                "decisions_sha": self.decisions_sha}

    def snapshot_sha(self) -> str:
        return sha256_hex(canonical_bytes(self.snapshot()))

    # --- decide and commit ---------------------------------------------------------------------------------

    async def decide(self, case: Case) -> Outcome:
        """Never raises: a failure comes back as a completion with an error code (an invalid outcome)."""
        hit = self.store.get(self.run_key, case.case_id)
        if hit is not None:
            comp = Completion(hit.text, hit.prompt_tokens, hit.completion_tokens, hit.cached_tokens, hit.latency_ms,
                              hit.attempts, hit.error)
            return Outcome(comp, parse_decision(comp.text or "", case.order_total_cents), True)
        if self._aborted or self._client is None:
            comp = Completion(None, 0, 0, None, 0, 0, "ABORTED")
        else:
            try:
                comp = await self._client.complete(messages(self.static, case_block(case), self.system_mode),
                                                   DECISION_SCHEMA)
            except (httpx.HTTPError, RuntimeError):
                comp = Completion(None, 0, 0, None, 0, 1, "TRANSPORT")
            self.store.put(self.run_key, case.case_id, StoredOutcome(
                comp.text, comp.error, comp.prompt_tokens, comp.completion_tokens, comp.cached_tokens,
                comp.latency_ms, comp.attempts))
        return Outcome(comp, parse_decision(comp.text or "", case.order_total_cents), False)

    def commit(self, i: int, case: Case, outcome: Outcome) -> None:
        human = self.humans[case.case_id]
        d = outcome.parsed.decision
        ok = agrees(d, human, self.cfg.agreement.tol_abs_cents, self.cfg.agreement.tol_rel_bp)
        payload = None if d is None else d.as_payload()
        rec = {"run_id": self.run_id, "case_id": case.case_id, "cell": case.cell, "decision": payload,
               "decision_sha": None if payload is None else sha256_hex(canonical_bytes(payload)),
               "valid": d is not None, "error": outcome.completion.error or outcome.parsed.error,
               "human_action": human.action.value,
               "human_amount_cents": None if human.amount_cents is None else int(human.amount_cents),
               "agree": ok, "effective_level": self.ladder.level(case.cell)}
        self._apply(rec)
        c = outcome.completion
        self._session.append(_Stat(c.prompt_tokens, c.completion_tokens, c.cached_tokens, c.latency_ms, c.attempts,
                                   c.error, None if c.error else outcome.parsed.error, outcome.from_store))
        if self._first_date is None:
            self._first_date = case.contact_date
        self._last_date = case.contact_date
        self.ladder.observe(case, rec)
        self.pending_decisions.append(rec)
        if self.committed % self.tick_k == 0:
            self._tick()
        elif self.bus.progress_due():
            self.bus.publish("progress", self._progress())

    def _flush(self) -> None:
        if not self.pending_decisions:
            return
        with open(self.decisions_path, "a", encoding="utf-8", newline="\n") as f:
            for rec in self.pending_decisions:
                f.write(json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.pending_decisions = []

    def _tick(self, all_cells: bool = False) -> None:
        self._flush()
        if self._evaluated_at != self.committed:  # the end-of-run tick never repeats a tick_k look (§4.6)
            self._evaluated_at = self.committed
            for name, data in self.ladder.evaluate():
                self.bus.publish(name, data)
        self.bus.tick()
        for cell in sorted(self._cells if all_cells else self._touched):
            self.bus.publish("cell", self._cell_event(cell))
        self._touched.clear()
        self.bus.publish("progress", self._progress(), force=True)

    def _resume(self) -> None:
        """Rebuild state from flushed DECISION records; a torn last line (crash mid-write) is dropped."""
        if not self.decisions_path.exists():
            return
        text = self.decisions_path.read_text(encoding="utf-8")
        if text and not text.endswith("\n"):
            text = text[: text.rfind("\n") + 1]
            with open(self.decisions_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
        recs = [json.loads(line) for line in text.splitlines() if line.strip()]
        for k, rec in enumerate(recs):
            if k >= len(self.cases) or rec["case_id"] != self.cases[k].case_id or rec["run_id"] != self.run_id:
                raise ValueError(f"{self.decisions_path} does not match this run's cases at record {k + 1}")
            self._apply(rec)
            self.ladder.observe(self.cases[k], rec)
            if (k + 1) % self.tick_k == 0:  # replay the ticks too, so a resumed ladder matches an unbroken run
                self.ladder.evaluate()
                self._evaluated_at = k + 1
        self.resumed_from = len(recs)
        self._touched.clear()

    # --- events and metrics --------------------------------------------------------------------------------

    def _elapsed(self) -> float:
        if self._t0 is None:
            return 0.0
        return (self._t1 if self._t1 is not None else self._clock()) - self._t0

    def metrics(self, elapsed_s: float | None = None) -> dict[str, Any]:
        """Master §13.5 metrics over the cases committed by this process, plus §16.4 time compression."""
        s = self._session
        n = len(s)
        wall = self._elapsed() if elapsed_s is None else elapsed_s
        rate = n / wall if wall > 0 else None
        prompt = sum(x.prompt_tokens for x in s)
        completion = sum(x.completion_tokens for x in s)
        reported = [x for x in s if x.cached_tokens is not None]
        cached = sum(x.cached_tokens or 0 for x in reported)
        reported_prompt = sum(x.prompt_tokens for x in reported)
        lat = sorted(x.latency_ms for x in s)
        transport = Counter(x.transport_error for x in s if x.transport_error)
        sim_days = (self._last_date - self._first_date).days if self._first_date and self._last_date else 0
        return {
            "cases": n, "wall_s": round(wall, 3), "cases_per_s": rate,
            "cases_per_min": None if rate is None else 60 * rate,
            "prompt_tokens": prompt, "completion_tokens": completion, "cached_tokens": cached,
            "prompt_tps": prompt / wall if wall > 0 else None, "completion_tps": completion / wall if wall > 0 else None,
            "cached_ratio": cached / reported_prompt if reported_prompt else None,
            "p50_ms": _pct(lat, 0.50), "p95_ms": _pct(lat, 0.95),
            "attempts": {str(k): v for k, v in sorted(Counter(x.attempts for x in s).items())},
            "errors": dict(sorted(transport.items())),
            "parse_errors": dict(sorted(Counter(x.parse_error for x in s if x.parse_error).items())),
            "error_rate": sum(transport.values()) / n if n else 0.0,
            "valid_rate": self.valid / self.committed if self.committed else None,
            "agree_rate": self.agree / self.committed if self.committed else None,
            "endpoint_calls": sum(not x.from_store for x in s), "store_hits": sum(x.from_store for x in s),
            "first_date": None if self._first_date is None else self._first_date.isoformat(),
            "last_date": None if self._last_date is None else self._last_date.isoformat(),
            "sim_days": sim_days, "compression": sim_days * 86_400 / wall if wall > 0 else None,
        }

    def _progress(self) -> dict[str, Any]:
        m = self.metrics()
        return {"run_id": self.run_id, "committed": self.committed, "total": len(self.cases),
                "sim_date": m["last_date"], "elapsed_s": m["wall_s"], "cases_per_s": m["cases_per_s"],
                "prompt_tps": m["prompt_tps"], "completion_tps": m["completion_tps"], "cached_ratio": m["cached_ratio"],
                "p50_ms": m["p50_ms"], "p95_ms": m["p95_ms"], "errors": sum(m["errors"].values()),
                "compression": m["compression"]}

    def _cell_event(self, cell: str) -> dict[str, Any]:
        s = self._cells[cell]
        ceiling = self.cfg.ladder.ceilings.get(cell.split("|")[0], "L3")
        # Without a ladder (NoLadder) statistics and gates are null and nothing blocks or promotes.
        out = {"cell": cell, "level": self.ladder.level(cell), "ceiling": ceiling, "pinned": False, "n": s["n"],
               "x": s["x"], "p_hat": s["x"] / s["n"] if s["n"] else None, "lower": None, "baseline": None,
               "baseline_scope": None, "threshold": None, "cost_ucb_cents": None, "cost_threshold_cents": None,
               "detect_days": None, "blockers": []}
        info = getattr(self.ladder, "cell_info", None)
        if info is not None:  # the ladder's evidence window, which a CUSUM reset may have emptied
            out.update(info(cell))
        return out

    def _run_event(self, status: str, started_at: str) -> dict[str, Any]:
        return {"run_id": self.run_id, "mode": "replay", "status": status, "model": self.profile.model,
                "adapter_sha": self.adapter_sha, "profile": self.profile.name, "started_at": started_at}

    def _write_manifest(self) -> None:
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.manifest_path)

    def _start_manifest(self, started_at: str) -> None:
        if self.manifest_path.exists():
            old = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if old.get("run_key") != self.run_key or old.get("cases") != len(self.cases):
                raise ValueError(f"run {self.run_id} was started with another configuration or case list")
            self.manifest["attempts"] = list(old.get("attempts", [])) + [started_at]
        else:
            self.manifest["attempts"] = [started_at]
        self._write_manifest()

    # --- the run -------------------------------------------------------------------------------------------

    async def run(self) -> dict[str, Any]:
        self._reset()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._resume()
        started_at = _utc(self._now())
        self._start_manifest(started_at)
        recorder = Recorder(self.run_dir / "events.jsonl", clock=self._clock) if self._record else None
        if recorder is not None:
            self.bus.add_sink(recorder)
        self._t0 = self._clock()
        self.bus.publish("run", self._run_event("started", started_at))
        self._client = LLMClient(self.profile, self._api_key, transport=self._transport)
        stop = asyncio.Event()
        gpu_task = asyncio.create_task(self.gpu.run(stop)) if self.gpu is not None else None
        try:
            engine = ReplayEngine(self.decide, self.commit, self.concurrency, self.max_ahead)
            await engine.run(self.cases[self.committed:])
        except BaseException:
            self._aborted = True
            self.bus.publish("run", self._run_event("failed", started_at))
            raise
        else:
            self._t1 = self._clock()
            self._tick(all_cells=True)
            metrics = self.metrics()
            snapshot, sha = self.snapshot(), self.snapshot_sha()
            (self.run_dir / "snapshot.json").write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
            self.manifest["finished"] = {"finished_at": _utc(self._now()), "committed": self.committed,
                                         "resumed_from": self.resumed_from, "valid": self.valid, "agree": self.agree,
                                         "wall_s": metrics["wall_s"], "metrics": metrics, "snapshot_sha": sha}
            self._write_manifest()
            self.bus.publish("run", self._run_event("finished", started_at))
            return {"run_id": self.run_id, "committed": self.committed, "resumed_from": self.resumed_from,
                    "snapshot": snapshot, "snapshot_sha": sha, "metrics": metrics}
        finally:
            stop.set()
            if gpu_task is not None:
                await gpu_task
            await self._client.aclose()
            if recorder is not None:
                self.bus.remove_sink(recorder)
                recorder.close()



def load_replay_cases(root: str | Path, cfg: TenureConfig, split: str, subset: str | None = None) -> list[Case]:
    """Typed cases of a split in (contact_date, case_id) order; `subset` names a D-intake subset (D-replay: slice)."""
    derived = Path(root) / cfg.paths.derived
    df = pd.read_parquet(derived / "cases.parquet")
    if (derived / "intake.parquet").exists():
        df = merge_into_cases(df, pd.read_parquet(derived / "intake.parquet"))
    if subset is not None:
        if INTAKE_SUBSETS.get(subset, (None,))[0] != split:
            raise ValueError(f"subset {subset!r} is not a subset of split {split!r}")
        df = subset_cases(df, subset, cfg.seed)
    else:
        df = df[df["split"] == split]
    if df["case_type"].isna().any() or (df["case_type"] == "").any():
        raise ValueError(f"split {split} has cases without an intake case_type")
    return sorted((case_from_record(r) for r in df.to_dict("records")), key=lambda c: (c.contact_date, c.case_id))


def load_primary_humans(path: str | Path, case_ids: Iterable[str] | None = None) -> dict[str, Resolution]:
    """The primary synthetic-team decision per case from humans.parquet (master §14.3)."""
    cols = ["case_id", "primary_action", "primary_amount_cents", "primary_requires_return", "primary_clause"]
    df = pd.read_parquet(path, columns=cols)
    if case_ids is not None:
        df = df[df["case_id"].isin(set(case_ids))]
    return {str(cid): Resolution(Action(a), None if pd.isna(amt) else int(amt), bool(ret), str(clause))
            for cid, a, amt, ret, clause in df.itertuples(index=False, name=None)}


# --- T05: ladder wiring ----------------------------------------------------------------------------------------------

# Policy clauses each case type can resolve to (master §1.1); §17.3 caps the cells whose type uses a changed clause.
CASE_TYPE_CLAUSES: Mapping[CaseType, frozenset[str]] = MappingProxyType({
    CaseType.DELAY: frozenset({"F1", "P1", "D2", "F3", "N3"}),
    CaseType.NOT_RECEIVED: frozenset({"E5", "F3", "N3"}),
    CaseType.DAMAGED: frozenset({"F2", "I1"}),
    CaseType.WRONG_ITEM: frozenset({"R1"}),
    CaseType.WRONG_VARIANT: frozenset({"P2"}),
    CaseType.MISSING_ITEM: frozenset({"R2"}),
    CaseType.DEFECTIVE: frozenset({"F4", "D3"}),
    CaseType.QUALITY: frozenset({"P3", "D3"}),
    CaseType.REMORSE: frozenset({"F5", "D1"}),
    CaseType.BILLING: frozenset({"E4"}),
    CaseType.SELLER_SERVICE: frozenset({"N2"}),
    CaseType.POSITIVE: frozenset({"N1"}),
    CaseType.VAGUE_COMPLAINT: frozenset({"I2"}),
    CaseType.OTHER: frozenset({"E6"}),
})


def ladder_config(cfg: TenureConfig, *, max_detect_days: float | None = None,
                  deltas: Mapping[Level, float] | None = None) -> LadderConfig:
    """R.9 LadderConfig from tenure.yaml, optionally with another D_max or other margins (sensitivity table)."""
    lc = cfg.ladder
    gates = {}
    for name, g in sorted(lc.gates.items()):
        level = Level[name]
        delta = deltas.get(level, g.delta) if deltas is not None else g.delta
        gates[level] = LevelGate(g.n_min, delta, g.min_valid_rate, g.cost_gate, g.monitor_gate, g.dwell)
    return LadderConfig(MappingProxyType(gates), lc.arl0_target,
                        lc.max_detect_days if max_detect_days is None else max_detect_days, lc.cost_eps_abs_cents,
                        lc.cost_rho, lc.bootstrap_b, cfg.seed, lc.high_value_cents, lc.confirm_ticks)


@dataclass(frozen=True, slots=True)
class LadderInputs:
    baselines: dict[str, Baseline]  # cell -> human-human agreement, pooled as in §4.2
    hh_cost_means: dict[str, float]  # cell -> the team's mean pair cost in cents (§5.5)
    volumes: dict[str, float]  # cell -> historical cases per day over the replay window (§6.6)


def all_cells(cfg: TenureConfig) -> list[str]:
    bands = [name for name, _ in sorted(cfg.bands_cents.items(), key=lambda kv: kv[1])]
    return sorted(cell_id(t, b) for t in CaseType for b in bands)


def ladder_inputs(cfg: TenureConfig, cases: pd.DataFrame, humans: pd.DataFrame) -> LadderInputs:
    """Baselines from the double-coded train + replay pairs, and per-cell volumes from the replay window.

    `cases` holds typed cases.parquet rows with split; `humans` holds humans.parquet rows."""
    params = CostParams(**cfg.cost.model_dump())
    table = pair_table(cases, humans, params, TRAIN_REPLAY, cfg.agreement.tol_abs_cents, cfg.agreement.tol_rel_bp)
    start, end = cfg.splits["replay"]
    days = max(1, (end - start).days)
    replay = cases[cases["split"] == "replay"]
    counts = Counter(f"{t}|{b}" for t, b in zip(replay["case_type"].astype(str), replay["value_band"].astype(str)))
    baselines, costs, volumes = {}, {}, {}
    for cell in all_cells(cfg):
        b, cb = cell_baselines(table, cell, cfg.ladder.baseline_n_min)
        baselines[cell], costs[cell], volumes[cell] = b, cb.mean_cents, counts.get(cell, 0) / days
    return LadderInputs(baselines, costs, volumes)


def build_ladder(cfg: TenureConfig, inputs: LadderInputs, lcfg: LadderConfig | None = None) -> Ladder:
    ceilings = {k: Level[v] for k, v in sorted(cfg.ladder.ceilings.items())}
    return Ladder(lcfg or ladder_config(cfg), CostParams(**cfg.cost.model_dump()), inputs.baselines,
                  inputs.hh_cost_means, inputs.volumes, ceilings)


def agent_from_payload(d: Mapping[str, Any] | None) -> AgentDecision | None:
    if d is None:
        return None
    return AgentDecision(Action(d["action"]), d["amount_cents"], bool(d["requires_return"]), d["clause"],
                         int(d["confidence_ppm"]), "")


def bus_event(e: LadderEvent) -> tuple[str, dict[str, Any]] | None:
    """The dashboard view of a ladder event (Appendix B); the full payload stays in the ledger buffer."""
    p = e.payload
    if e.kind == "LEVEL_CHANGE":
        return "level", {"cell": p["cell"], "from": p["from"], "to": p["to"], "reason": p["reason"],
                         "ledger_seq": None}
    if e.kind == "CUSUM_SIGNAL":
        return "cusum", {"cell": p["cell"], "s": float(p["s"]), "h": float(p["h"]),
                         "monitored": p["outcomes_monitored"]}
    if e.kind == "OVERRIDE":
        return "override", {"cell": p["cell"], "op": p["op"], "actor": p["actor"], "reason": p["reason"],
                            "ledger_seq": None}
    if e.kind == "GLOBAL_ALARM":
        return "alarm", {"kind": "GLOBAL_ALARM", "s": float(p["s"]), "h": float(p["h"])}
    return None


def _finite(v: float | None) -> float | None:
    return v if v is not None and math.isfinite(v) else None


def cell_summary(ladder: Ladder, cell: str) -> dict[str, Any]:
    """A cell's level, evidence window and current blockers (the `cell` event fields of Appendix B)."""
    st = ladder.state(cell)
    a = ladder.assess(cell)
    return {"level": st.level.name, "ceiling": st.ceiling.name, "pinned": st.pinned, "n": st.n, "x": st.x,
            "p_hat": st.x / st.n if st.n else None, "valid": st.n_valid, "window_id": st.window_id,
            "lower": None if a.ni is None else a.ni.lower, "baseline": None if a.ni is None else a.ni.baseline,
            "baseline_scope": None if a.ni is None else a.ni.baseline_scope,
            "threshold": None if a.ni is None else a.ni.threshold,
            "cost_ucb_cents": None if a.cost is None else a.cost.ucb,
            "cost_threshold_cents": None if a.cost is None else a.cost.threshold,
            "detect_days": _finite(a.detect_days), "blockers": list(a.blockers)}


class LadderBridge:
    """The LadderHook for ReplayRun: DECISION records → Ladder.observe; ticks → Ladder.evaluate."""

    def __init__(self, ladder: Ladder, humans: Mapping[str, Resolution]):
        self.ladder = ladder
        self.humans = humans
        self.events: list[LadderEvent] = []  # every ladder event in order: the ledger buffer (T06 persists it)
        self._pending: list[LadderEvent] = []

    def _add(self, events: list[LadderEvent]) -> None:
        self.events += events
        self._pending += events

    def observe(self, case: Case, record: Mapping[str, Any]) -> None:
        self._add(self.ladder.observe(case, agent_from_payload(record["decision"]), self.humans[case.case_id]))

    def evaluate(self) -> list[tuple[str, dict[str, Any]]]:
        self._add(self.ladder.evaluate())
        out = [b for b in (bus_event(e) for e in self._pending) if b is not None]
        self._pending = []
        return out

    def level(self, cell: str) -> str:
        return self.ladder.state(cell).level.name

    def cell_info(self, cell: str) -> dict[str, Any]:
        return cell_summary(self.ladder, cell)


def replay_ladder(ladder: Ladder, cases: Sequence[Case], records: Sequence[Mapping[str, Any]],
                  humans: Mapping[str, Resolution], tick_k: int) -> list[LadderEvent]:
    """Fold committed DECISION records through the ladder as ReplayRun does: a tick every tick_k records and a
    final tick unless the last record was a tick. Needs no endpoint: the records hold the agent's decisions."""
    bridge = LadderBridge(ladder, humans)
    for k, (c, rec) in enumerate(zip(cases, records, strict=True), 1):
        if c.case_id != rec["case_id"]:
            raise ValueError(f"record {k} is case {rec['case_id']}, expected {c.case_id}")
        bridge.observe(c, rec)
        if k % tick_k == 0:
            bridge.evaluate()
    if records and len(records) % tick_k:
        bridge.evaluate()
    return bridge.events


def read_records(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "decisions.jsonl"
    recs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not recs:
        raise ValueError(f"{path} holds no committed records")
    foreign = [r["case_id"] for r in recs if r["run_id"] != run_dir.name]
    if foreign:
        raise ValueError(f"{len(foreign)} records belong to another run (first: {foreign[0]})")
    return recs


def primary_resolutions(humans: pd.DataFrame, case_ids: Iterable[str] | None = None) -> dict[str, Resolution]:
    df = humans if case_ids is None else humans[humans["case_id"].isin(set(case_ids))]
    out = {}
    for r in df.to_dict("records"):
        amount = r["primary_amount_cents"]
        out[str(r["case_id"])] = Resolution(Action(r["primary_action"]), None if pd.isna(amount) else int(amount),
                                            bool(r["primary_requires_return"]), str(r["primary_clause"]))
    return out


def ladder_from_run(run_dir: str | Path, cfg: TenureConfig, cases: pd.DataFrame, humans: pd.DataFrame,
                    lcfg: LadderConfig | None = None, tick_k: int | None = None,
                    inputs: LadderInputs | None = None) -> tuple[Ladder, list[LadderEvent]]:
    """The ladder after replaying a run's committed records (the base replay with the ladder, §16 + §7)."""
    run_dir = Path(run_dir)
    recs = read_records(run_dir)
    ids = [r["case_id"] for r in recs]
    rows = cases[cases["case_id"].isin(set(ids))]
    by_id = {str(r["case_id"]): case_from_record(r) for r in rows.to_dict("records")}
    primary = primary_resolutions(humans, ids)
    missing = [i for i in ids if i not in by_id or i not in primary]
    if missing:
        raise ValueError(f"{len(missing)} cases of {run_dir.name} are missing from cases or humans "
                         f"(first: {missing[0]})")
    ladder = build_ladder(cfg, inputs or ladder_inputs(cfg, cases, humans), lcfg)
    events = replay_ladder(ladder, [by_id[i] for i in ids], recs, primary, tick_k or cfg.ladder.tick_k)
    return ladder, events


SENSITIVITY_DMAX: tuple[float, ...] = (30.0, 60.0, 90.0, math.inf)
LOOSER_DELTAS: tuple[tuple[float, float], ...] = ((0.075, 0.05), (0.10, 0.075))  # (L2, L3) margins after the default


def _days(v: float) -> int | str:
    return "inf" if math.isinf(v) else int(v)


def ladder_state(ladder: Ladder, events: Sequence[LadderEvent]) -> dict[str, Any]:
    return {"cells": {cell: cell_summary(ladder, cell) for cell in sorted(ladder.cells)},
            "events": [{"kind": e.kind, "payload": e.payload} for e in events]}


def sensitivity(run_dir: str | Path, cfg: TenureConfig, cases: pd.DataFrame, humans: pd.DataFrame,
                tick_k: int | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Cells at ≥ L2 and ≥ L3 after replaying a run, for D_max ∈ {30, 60, 90, ∞} and three margin variants
    (RUNBOOK T05; informs D-gates and D-dmax). Returns (report, ladder state under the tenure.yaml defaults)."""
    run_dir = Path(run_dir)
    inputs = ladder_inputs(cfg, cases, humans)
    g = cfg.ladder.gates
    rows = []
    for d2, d3 in ((g["L2"].delta, g["L3"].delta), *LOOSER_DELTAS):
        for dmax in SENSITIVITY_DMAX:
            lcfg = ladder_config(cfg, max_detect_days=dmax, deltas={Level.L2: d2, Level.L3: d3})
            lad, events = ladder_from_run(run_dir, cfg, cases, humans, lcfg, tick_k, inputs)
            rows.append({"delta_l2": d2, "delta_l3": d3, "max_detect_days": _days(dmax),
                         "ge_l1": sorted(c for c, s in lad.cells.items() if s.level >= Level.L1),
                         "ge_l2": sorted(c for c, s in lad.cells.items() if s.level >= Level.L2),
                         "ge_l3": sorted(c for c, s in lad.cells.items() if s.level >= Level.L3),
                         "promotions": sum(e.kind == "LEVEL_CHANGE" and e.payload["reason"] == "PROMOTE"
                                           for e in events),
                         "cusum_signals": sum(e.kind == "CUSUM_SIGNAL" for e in events)})
    lad, events = ladder_from_run(run_dir, cfg, cases, humans, None, tick_k, inputs)
    head = {"run_id": run_dir.name, "records": len(read_records(run_dir)), "tick_k": tick_k or cfg.ladder.tick_k,
            "config_sha": config_sha(cfg)}
    report = {**head, "defaults": {"delta_l2": g["L2"].delta, "delta_l3": g["L3"].delta,
                                   "max_detect_days": _days(cfg.ladder.max_detect_days)}, "rows": rows}
    return report, {**head, **ladder_state(lad, events)}


def sensitivity_markdown(report: Mapping[str, Any], state: Mapping[str, Any]) -> str:
    d = report["defaults"]
    lines = [f"# Ladder sensitivity · {report['run_id']}", "",
             (f"{report['records']} committed records replayed through the ladder, a tick every {report['tick_k']}. "
              f"Defaults (tenure.yaml): δ L2 {d['delta_l2']} / L3 {d['delta_l3']}, D_max {d['max_detect_days']} "
              f"days (decisions D-gates, D-dmax)."), "",
             "| δ L2 / L3 | D_max (days) | cells ≥ L2 | cells ≥ L3 |", "|---|---|---|---|"]
    for r in report["rows"]:
        lines.append(f"| {r['delta_l2']:.3f} / {r['delta_l3']:.3f} | {r['max_detect_days']} | {len(r['ge_l2'])} | "
                     f"{len(r['ge_l3'])} |")
    lines += ["", "Cells at ≥ L2 per variant:", ""]
    for r in report["rows"]:
        lines.append(f"- δ {r['delta_l2']:.3f} / {r['delta_l3']:.3f}, D_max {r['max_detect_days']}: "
                     f"{', '.join(r['ge_l2']) or 'none'}")
    lines += ["", "Levels under the defaults:", "", "| cell | level | n | x | blockers |", "|---|---|---|---|---|"]
    for cell, v in state["cells"].items():
        lines.append(f"| {cell.replace('|', chr(92) + '|')} | {v['level']} | {v['n']} | {v['x']} | {', '.join(v['blockers']) or '–'} |")
    return "\n".join(lines) + "\n"


# --- T05: policy update, global alarm, live simulation ----------------------------------------------------------------

def apply_policy_update(ladder: Ladder, changed_clauses: Iterable[str], policy_sha: str,
                        policy_id: str) -> list[LadderEvent]:
    """Master §17.3: every cell whose case type uses a changed clause drops to min(level, L1) and its evidence
    window is reset, so re-qualification uses only labels gathered under the new policy."""
    changed = sorted(set(changed_clauses))
    events = [LadderEvent("POLICY_VERSION", {"policy_id": policy_id, "policy_sha": policy_sha,
                                             "changed_clauses": changed})]
    touched = {t.value for t, clauses in CASE_TYPE_CLAUSES.items() if clauses & set(changed)}
    for cell in sorted(ladder.cells):
        if cell.split("|", 1)[0] not in touched:
            continue
        st = ladder.cells[cell]
        prev = st.level
        st.level = min(st.level, Level.L1)
        st.agree.clear()
        st.valid.clear()
        st.comps.clear()
        st.window_id += 1
        st.level_entry_n = 0
        st.pass_streak = 0
        st.monitor = None
        events.append(LadderEvent("LEVEL_CHANGE", {"cell": cell, "from": prev.name, "to": st.level.name,
                                                   "reason": "POLICY_CHANGE",
                                                   "evidence": {"window_reset": True, "changed_clauses": changed}}))
    return events


@dataclass(slots=True)
class GlobalAlarm:
    """Master §6.7: a pooled CUSUM over every audited outcome in L2/L3 cells; it pages and freezes promotions."""

    monitor: CusumMonitor

    @classmethod
    def arm(cls, ladder: Ladder, arl0_target: float, shift: float) -> GlobalAlarm | None:
        """p0 is the pooled disagreement rate of the L2/L3 cells' evidence windows; None while there are none."""
        cells = [st for st in ladder.cells.values() if st.level >= Level.L2]
        n = sum(st.n for st in cells)
        if n == 0:
            return None
        p0 = round(min(0.5 - shift, max(0.01, 1.0 - sum(st.x for st in cells) / n)), 3)
        return cls(CusumMonitor(design(p0, round(p0 + shift, 3), arl0_target)))

    def update(self, disagree: bool) -> bool:
        return self.monitor.update(disagree)


Agent = Callable[[Case], AgentDecision | None]


@dataclass(frozen=True, slots=True)
class Drift:
    """A drift injection (§17.2), announced at `at`. D-POLICY lives in the human labels (humans_drift.parquet);
    D-MODEL switches the agent to `agent` from `at`."""

    scenario: str
    at: date
    detail: Mapping[str, Any] = field(default_factory=dict)
    agent: Agent | None = None


@dataclass(frozen=True, slots=True)
class PolicyUpdate:
    """The operator moves the agent to a new policy at `at` (§17.3)."""

    at: date
    changed_clauses: tuple[str, ...]
    policy_sha: str
    policy_id: str
    agent: Agent


def mock_agent(seed: int, policy: PolicyParams = POLICY_V1, error_share: float = 0.02) -> Agent:
    """A stand-in decider for offline runs (D-offline-gates): the policy oracle with a deterministic share of
    errors. Its numbers describe the ladder mechanics, never Gemma."""
    def decide(c: Case) -> AgentDecision:
        r = resolve(c, policy)
        if unit_hash(str(seed), c.case_id, "mock-agent") < error_share:
            r = (Resolution(Action.REQUEST_INFO, None, False, "I1") if r.action is Action.ESCALATE
                 else Resolution(Action.ESCALATE, None, False, "E6"))
        return AgentDecision(r.action, r.amount_cents, r.requires_return, r.clause, 900_000, "")
    return decide


def cached_agent(store: OutcomeStore, key: str) -> Agent:
    """The agent's cached completions for a run_key (R.16); a case without one is an invalid output (I5)."""
    def decide(c: Case) -> AgentDecision | None:
        hit = store.get(key, c.case_id)
        if hit is None or hit.text is None:
            return None
        return parse_decision(hit.text, c.order_total_cents).decision
    return decide


class LiveRun:
    """Go-live simulation (§17): the live split by contact date from a seeded ladder. L0/L1 cases are decided by a
    person and always observed; L2/L3 cases by the agent, observed only when audited (unit_hash < a_ℓ). A tick runs
    every simulated day and after every CUSUM signal; the global alarm freezes promotions, never levels."""

    def __init__(self, config: TenureConfig, *, cases: Iterable[Case], humans: Mapping[str, Resolution], agent: Agent,
                 ladder: Ladder, drift: Drift | None = None, policy_update: PolicyUpdate | None = None,
                 run_dir: str | Path | None = None, bus: EventBus | None = None, global_alarm: bool = True,
                 agent_policy: str = "v1", run_id: str = "live"):
        self.cfg = config
        self.cases = sorted(cases, key=lambda c: (c.contact_date, c.case_id))
        missing = [c.case_id for c in self.cases if c.case_id not in humans]
        if missing:
            raise ValueError(f"{len(missing)} live cases have no human decision (first: {missing[0]})")
        self.humans = humans
        self.ladder = ladder
        self.drift = drift
        self.policy_update = policy_update
        self.run_dir = None if run_dir is None else Path(run_dir)
        self.bus = bus or EventBus()
        self.alarm_enabled = global_alarm
        self.run_id = run_id
        self._agent = agent
        self._agent_policy = agent_policy
        self.routes: list[dict[str, Any]] = []
        self.events: list[LadderEvent] = []  # ledger buffer, in order (T06 persists it)
        self.alarm: GlobalAlarm | None = None
        self.frozen = False
        self._alarm_info: dict[str, Any] | None = None
        self._demotions: list[dict[str, Any]] = []
        self._promotions: list[dict[str, Any]] = []
        self._touched: set[str] = set()

    def _record(self, events: list[LadderEvent], day: date, case_id: str | None) -> None:
        signal: Mapping[str, Any] | None = None
        for e in events:
            self.events.append(e)
            p = e.payload
            if e.kind == "CUSUM_SIGNAL":
                signal = p
            elif e.kind == "LEVEL_CHANGE" and p["reason"] in ("CUSUM", "PROMOTE"):
                row = {"cell": p["cell"], "from": p["from"], "to": p["to"], "reason": p["reason"],
                       "date": day.isoformat(), "case_id": case_id}
                if p["reason"] == "CUSUM":
                    row["outcomes_monitored"] = None if signal is None else signal["outcomes_monitored"]
                    self._demotions.append(row)
                else:
                    self._promotions.append(row)
            b = bus_event(e)
            if b is not None:
                self.bus.publish(*b)

    def _tick(self, day: date) -> None:
        if not self.frozen:
            self._record(self.ladder.evaluate(), day, None)
        self.bus.tick()
        for cell in sorted(self._touched):
            self.bus.publish("cell", {"cell": cell, **cell_summary(self.ladder, cell)})
        self._touched.clear()

    def _arm(self) -> None:
        if self.alarm_enabled and self.alarm is None and self._alarm_info is None:
            ga = self.cfg.ladder.global_alarm
            self.alarm = GlobalAlarm.arm(self.ladder, ga.arl0_target, ga.shift)

    def _route(self, c: Case, day: date) -> None:
        level = self.ladder.state(c.cell).level
        eff = self.ladder.effective_level(c)
        agent = self._agent(c)
        human = self.humans[c.case_id]
        if eff >= Level.L2:
            audited = unit_hash(str(self.cfg.seed), c.case_id, "audit") < self.cfg.ladder.audit_rate[eff.name]
            decided_by, observed = "agent", audited
        else:
            audited, decided_by, observed = False, "human", True
        ok = agrees(agent, human, self.cfg.agreement.tol_abs_cents, self.cfg.agreement.tol_rel_bp) if observed else None
        self.routes.append({"case_id": c.case_id, "date": day.isoformat(), "cell": c.cell, "level": level.name,
                            "effective_level": eff.name, "decided_by": decided_by, "audited": audited,
                            "observed": observed, "agree": ok, "agent_policy": self._agent_policy})
        if not observed:
            return  # an unaudited autonomous case has no label: it leaves the cell's evidence alone
        if audited:
            self._arm()
        events = self.ladder.observe(c, agent, human)
        self._touched.add(c.cell)
        self._record(events, day, c.case_id)
        if audited and self.alarm is not None and self._alarm_info is None and self.alarm.update(not ok):
            m = self.alarm.monitor
            alarm = LadderEvent("GLOBAL_ALARM", {"s": f"{m.s:.4f}", "h": f"{m.d.h:.4f}", "outcomes_monitored": m.n})
            self._record([alarm], day, c.case_id)
            self.frozen = True
            self._alarm_info = {"date": day.isoformat(), "case_id": c.case_id, **alarm.payload, "p0": m.d.p0,
                                "p1": m.d.p1}
        if any(e.kind == "CUSUM_SIGNAL" for e in events):
            self._tick(day)

    def run(self) -> dict[str, Any]:
        self._arm()
        at_start = {cell: st.level.name for cell, st in sorted(self.ladder.cells.items())}
        injected = updated = False
        days = 0
        for day, group in groupby(self.cases, key=lambda c: c.contact_date):
            if self.drift is not None and not injected and day >= self.drift.at:
                self._record([LadderEvent("DRIFT_INJECTED", {"scenario": self.drift.scenario,
                                                             "at_date": self.drift.at.isoformat(),
                                                             "detail": dict(self.drift.detail)})], day, None)
                if self.drift.agent is not None:
                    self._agent = self.drift.agent
                injected = True
            pu = self.policy_update
            if pu is not None and not updated and day >= pu.at:
                self._record(apply_policy_update(self.ladder, pu.changed_clauses, pu.policy_sha, pu.policy_id),
                             day, None)
                self._agent, self._agent_policy = pu.agent, pu.policy_id
                updated = True
            for c in group:
                self._route(c, day)
            self._tick(day)
            days += 1
            self.bus.publish("progress", {"run_id": self.run_id, "committed": len(self.routes),
                                          "total": len(self.cases), "sim_date": day.isoformat(), "elapsed_s": None,
                                          "cases_per_s": None, "prompt_tps": None, "completion_tps": None,
                                          "cached_ratio": None, "p50_ms": None, "p95_ms": None, "errors": 0,
                                          "compression": None}, force=True)
        r = self.routes
        observed = [x for x in r if x["observed"]]
        pu = self.policy_update
        summary = {
            "run_id": self.run_id, "cases": len(r), "days": days,
            "first_date": r[0]["date"] if r else None, "last_date": r[-1]["date"] if r else None,
            "observed": len(observed), "audited": sum(x["audited"] for x in r),
            "autonomous": sum(x["decided_by"] == "agent" for x in r),
            "observed_agree": sum(bool(x["agree"]) for x in observed),
            "levels_at_start": at_start,
            "levels": {cell: st.level.name for cell, st in sorted(self.ladder.cells.items())},
            "promotions": self._promotions, "demotions": self._demotions, "global_alarm": self._alarm_info,
            "drift": None if self.drift is None else {"scenario": self.drift.scenario,
                                                      "at_date": self.drift.at.isoformat()},
            "policy_update": None if pu is None else {"date": pu.at.isoformat(), "policy_id": pu.policy_id,
                                                      "changed_clauses": sorted(pu.changed_clauses)},
            "event_counts": dict(sorted(Counter(e.kind for e in self.events).items())),
        }
        if self.run_dir is not None:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            with open(self.run_dir / "routes.jsonl", "w", encoding="utf-8", newline="\n") as f:
                for x in r:
                    f.write(json.dumps(x, sort_keys=True) + "\n")
            with open(self.run_dir / "ladder_events.jsonl", "w", encoding="utf-8", newline="\n") as f:
                for e in self.events:
                    f.write(json.dumps({"kind": e.kind, "payload": e.payload}, sort_keys=True) + "\n")
        return summary
