"""Replay orchestration: every case through the Gemma decider, committed in case order (master §16).

decide() runs concurrently inside ReplayEngine (R.13): an outcome-cache hit, else one endpoint call whose
successful completion is cached (R.16, I7). commit() runs strictly in (contact_date, case_id) order (I6):
it compares the agent with the primary human decision (an invalid output is a disagreement, I5), updates
the per-cell confusion matrix and counters, calls the ladder hook, and buffers the DECISION record. Every
tick_k cases the buffer is flushed to decisions.jsonl (the ledger takes this over in T06) and progress and
cell events go out. A restart with the same run directory resumes after the last flushed record (§16.5).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx
import pandas as pd

from .canonical import canonical_bytes, sha256_hex
from .config import TenureConfig, _hashable, config_sha, endpoint
from .domain import Action, Case, Resolution
from .events import EventBus, Recorder
from .gpu import GpuPoller
from .intake import INTAKE_SUBSETS, case_from_record, load_examples, merge_into_cases, subset_cases
from .llm import Completion, LLMClient
from .prompt import case_block, messages, prompt_sha, static_block
from .replay import ReplayEngine
from .schema import DECISION_SCHEMA, Parsed, agrees, parse_decision
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
        # Statistics and gates arrive with T04/T05; until then they are null and nothing blocks or promotes.
        return {"cell": cell, "level": self.ladder.level(cell), "ceiling": ceiling, "pinned": False, "n": s["n"],
                "x": s["x"], "p_hat": s["x"] / s["n"] if s["n"] else None, "lower": None, "baseline": None,
                "baseline_scope": None, "threshold": None, "cost_ucb_cents": None, "cost_threshold_cents": None,
                "detect_days": None, "blockers": []}

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
