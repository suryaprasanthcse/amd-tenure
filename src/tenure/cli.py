"""TENURE command line: doctor, ingest, smoke (T00); intake, decide-sample, spike s2|s3 (T01); humans, gold (T02);
replay (T03); evaluate (T04); ladder sensitivity, live (T05); ledger verify|export|import-run, gold import-ledger
(T06)."""
from __future__ import annotations

import os

# Small dense solves (the 401-state Brook-Evans chain, R.8) run ~10x slower on a multi-threaded BLAS than on one
# thread (measured 38 vs 3.4 ms per ARL on 8 cores); pin before numpy loads. An explicit setting still wins.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

import asyncio
import hashlib
import json
import platform
import re
import statistics
import sys
import time
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import numpy as np
import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from .baselines import TRAIN_REPLAY, cell_baselines, pair_table
from .canonical import fx
from .config import TenureConfig, config_sha, endpoint, ingest_config, load, load_dotenv
from .cost import CostParams, cost_gate, error_components
from .domain import Action, Case, CaseType, Level, Resolution
from .gold import TranslationCache, agreement_intervals, label_session, raw_agreement, read_labels, sample_gold
from .humans import PolicySchedule, label_case
from .intake import (
    case_from_record,
    classify_keywords,
    classify_llm,
    intake_prompt_sha,
    keyword_agreement,
    keyword_case_type,
    keyword_rules_sha,
    keywords_rest_blockers,
    load_examples,
    merge_into_cases,
    run_intake,
    subset_cases,
)
from .llm import STRUCTURED_MODES, Completion, EndpointProfile, LLMClient
from .olist import build_cases, load_tables
from .policy import POLICY_V1, POLICY_V2
from .prompt import case_block, messages, prompt_sha, static_block
from .ratelimit import RateLimitedTransport, shared_limiter
from .schema import DECISION_SCHEMA, AgentDecision, Parsed, parse_decision
from .stats import Z95, non_inferiority, wilson_cc_interval

app = typer.Typer(no_args_is_help=True, add_completion=False, help="TENURE: earned autonomy for refund agents.")
console = Console()

MOCK_API_KEY = "mock_key"  # decision D-llm-mock: never sent over the network
RAW_FILES = (
    "olist_customers_dataset.csv", "olist_geolocation_dataset.csv", "olist_order_items_dataset.csv",
    "olist_order_payments_dataset.csv", "olist_order_reviews_dataset.csv", "olist_orders_dataset.csv",
    "olist_products_dataset.csv", "olist_sellers_dataset.csv", "product_category_name_translation.csv",
)
ConfigOpt = typer.Option(Path("tenure.yaml"), "--config", "-c", help="Path to tenure.yaml")
SampleOutOpt = typer.Option(Path("tests/fixtures/llm/decide_sample.jsonl"), "--out", help="Fixture file to write")


def _setup(config: Path):
    root = config.resolve().parent
    load_dotenv(root / ".env")
    return root, load(config)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def s1_check(profile: EndpointProfile, api_key: str, n: int = 1,
             transport: httpx.BaseTransport | None = None,
             async_transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Spike S1 (master §24): GET {base}/models, then n chat completions through LLMClient."""
    with httpx.Client(base_url=profile.base_url.rstrip("/"), transport=transport, timeout=profile.timeout_s,
                      headers={"Authorization": f"Bearer {api_key}"}) as client:
        try:
            resp = client.get("/models")
            status, headers = resp.status_code, resp.headers
        except httpx.HTTPError as e:
            status, headers, resp = 0, httpx.Headers(), None
            console.print(f"[red]GET /models failed: {type(e).__name__}[/red]")
    ids: list[str] = []
    if resp is not None and status == 200:
        try:
            ids = [str(m.get("id")) for m in resp.json().get("data", [])]
        except (ValueError, AttributeError):
            ids = []

    async def run() -> list[Completion]:
        llm = LLMClient(profile, api_key, transport=async_transport)
        try:
            return [await llm.complete([{"role": "user", "content": "Responda apenas: ok"}]) for _ in range(n)]
        finally:
            await llm.aclose()

    comps = asyncio.run(run())
    ok = [c for c in comps if c.error is None and c.text and c.text.strip()]
    listed = profile.model in ids
    gemma = "gemma" in profile.model.lower()
    return {"pass": status == 200 and listed and gemma and len(ok) == n, "host": urlparse(profile.base_url).hostname,
            "model": profile.model, "gemma": gemma, "models_status": status, "model_listed": listed,
            "models_served": ids, "completions_ok": len(ok), "n": n,
            "errors": sorted({c.error for c in comps if c.error}), "latency_ms": [c.latency_ms for c in comps],
            "rate_limit_headers": {k: v for k, v in headers.items() if "ratelimit" in k.lower()}}


def s1_state_line(r: dict[str, Any]) -> str:
    rl = ", ".join(f"{k}={v}" for k, v in sorted(r["rate_limit_headers"].items())) or "none reported"
    return (f"S1: {'PASS' if r['pass'] else 'FAIL'} → host={r['host']}, model={r['model']}, "
            f"models_status={r['models_status']}, listed={r['model_listed']}, gemma={r['gemma']}, "
            f"completions={r['completions_ok']}/{r['n']}, rate_limit_headers: {rl}")


@app.command()
def doctor(config: Path = ConfigOpt) -> None:
    """Check python, lock file, data files, env vars (names only) and the endpoint."""
    root, cfg = _setup(config)
    rows: list[tuple[str, bool, str]] = []
    rows.append(("python 3.12", sys.version_info[:2] == (3, 12), platform.python_version()))
    rows.append(("uv.lock", (root / "uv.lock").exists(), str(root / "uv.lock")))
    raw = root / cfg.paths.raw
    missing = [f for f in RAW_FILES if not (raw / f).is_file()]
    rows.append(("Olist files (9)", not missing, "all present" if not missing else f"missing: {', '.join(missing)}"))
    p = cfg.inference.profiles[cfg.inference.default_profile]
    for var in (p.base_url_env, p.api_key_env, p.model_env):
        rows.append((var, bool(os.environ.get(var)), "set" if os.environ.get(var) else "MISSING"))
    try:
        prof, key = endpoint(cfg)
    except ValueError as e:
        rows.append(("endpoint", False, str(e)))
    else:
        if key == MOCK_API_KEY:
            rows.append(("endpoint", True, "MOCK key (D-llm-mock): S1 not verified"))
        else:
            try:
                r = httpx.get(prof.base_url.rstrip("/") + "/models", timeout=10,
                              headers={"Authorization": f"Bearer {key}"})
                rows.append(("endpoint", r.status_code == 200, f"GET /models → HTTP {r.status_code}"))
            except httpx.HTTPError as e:
                rows.append(("endpoint", False, type(e).__name__))
    table = Table("check", "ok", "detail")
    for name, ok, detail in rows:
        table.add_row(name, "[green]✓[/green]" if ok else "[red]✗[/red]", detail)
    console.print(table)
    console.print(f"config_sha {config_sha(cfg)}")
    if not all(ok for _, ok, _ in rows):
        raise typer.Exit(1)


@app.command()
def ingest(config: Path = ConfigOpt) -> None:
    """Build data/derived/cases.parquet and ingest_manifest.json from the Olist CSVs."""
    root, cfg = _setup(config)
    raw, derived = root / cfg.paths.raw, root / cfg.paths.derived
    df = build_cases(load_tables(raw), ingest_config(cfg))
    derived.mkdir(parents=True, exist_ok=True)
    df.to_parquet(derived / "cases.parquet", index=False)
    counts = {s: {b: int(n) for b, n in sorted(g["value_band"].value_counts().items())}
              for s, g in sorted(df.groupby("split"))}
    band_n = df["value_band"].value_counts()
    split_n = df["split"].value_counts()
    delivered = (df["order_status"] == "delivered") & df["days_since_delivery"].notna()
    manifest = {
        "created_utc": utc_now(),
        "config_sha": config_sha(cfg),
        "brl_per_eur": str(cfg.currency.brl_per_eur),
        "brl_per_eur_status": "ASSUMPTION (D-fx)",
        "rows": int(len(df)),
        "split_counts": {s: int(split_n[s]) for s in sorted(split_n.index)},
        "band_counts": {b: int(band_n[b]) for b in sorted(band_n.index)},
        "band_shares": {b: fx(band_n[b] / len(df), 4) for b in sorted(band_n.index)},
        "counts_by_split_band": counts,
        "delivered_share": fx(float(delivered.mean()), 4),
        "contact_date_range": [str(df["contact_date"].min()), str(df["contact_date"].max())],
        "raw_files_sha256": {p.name: _sha256_file(p) for p in sorted(raw.glob("*.csv"))},
    }
    (derived / "ingest_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    table = Table("split", "V1", "V2", "V3", "V4", "total")
    for s, c in counts.items():
        table.add_row(s, *(str(c.get(b, 0)) for b in ("V1", "V2", "V3", "V4")), str(sum(c.values())))
    console.print(table)
    console.print(f"{manifest['rows']} cases · band shares {manifest['band_shares']} · "
                  f"delivered {manifest['delivered_share']} → {derived / 'cases.parquet'}")


@app.command()
def smoke(n: int = typer.Option(3, "--n", min=1), config: Path = ConfigOpt) -> None:
    """Spike S1 against the configured endpoint: list models and run n completions."""
    _, cfg = _setup(config)
    prof, key = endpoint(cfg)
    if key == MOCK_API_KEY:
        console.print("S1: PENDING → TENURE_LLM_API_KEY is the mock key (D-llm-mock); set real TENURE_LLM_* values")
        raise typer.Exit(2)
    t = limited_transport(cfg, prof)
    r = s1_check(prof, key, n, transport=t, async_transport=t)
    console.print(s1_state_line(r))
    if not r["pass"]:
        raise typer.Exit(1)


# --- T01: intake, decisions and spikes S2/S3 ------------------------------------------------------------------

REPAIR_SUFFIX = "\nReturn only the JSON object."  # master §12.6: the single repair retry in structured_mode none


def _live_endpoint(cfg: TenureConfig, what: str) -> tuple[EndpointProfile, str]:
    """The configured endpoint; exit 2 on the mock key, because these commands record model output."""
    prof, key = endpoint(cfg)
    if key == MOCK_API_KEY:
        console.print(f"{what}: PENDING → TENURE_LLM_API_KEY is the mock key (D-llm-mock); "
                      "set real TENURE_LLM_* values")
        raise typer.Exit(2)
    return prof, key


def _profile_cfg(cfg: TenureConfig):
    return cfg.inference.profiles[cfg.inference.default_profile]


def limited_transport(cfg: TenureConfig, prof: EndpointProfile) -> RateLimitedTransport | None:
    """The profile's request-rate limit (D-rpm) as an httpx transport, shared per endpoint; None if unlimited."""
    rpm = cfg.inference.profiles[prof.name].rpm
    return None if rpm is None else RateLimitedTransport(shared_limiter(prof.base_url, rpm))


def decider_static(root: Path, cfg: TenureConfig) -> str:
    return static_block((root / cfg.policy.file).read_text(encoding="utf-8"), DECISION_SCHEMA,
                        load_examples(root / cfg.policy.examples))


def sample_cases(root: Path, cfg: TenureConfig, split: str, n: int) -> list[Case]:
    """The first n cases of a split by case_id, typed by frozen intake where present, else by keywords."""
    derived = root / cfg.paths.derived
    df = pd.read_parquet(derived / "cases.parquet")
    if (derived / "intake.parquet").exists():
        df = merge_into_cases(df, pd.read_parquet(derived / "intake.parquet"))
    df = df[df["split"] == split].sort_values("case_id").head(n)
    out = []
    for r in df.to_dict("records"):
        t = CaseType(r["case_type"]) if r["case_type"] else keyword_case_type(
            r["message"], None if pd.isna(r["review_score"]) else int(r["review_score"]))
        out.append(case_from_record(r, t))
    return out


async def decide_many(prof: EndpointProfile, key: str, static: str, cases: list[Case], system_mode: str,
                      concurrency: int, transport: httpx.AsyncBaseTransport | None = None,
                      ) -> list[tuple[Completion, Parsed, bool]]:
    """Decide cases concurrently: (completion, parsed, repaired). Mode none gets one repair retry (§12.6)."""
    client = LLMClient(prof, key, transport=transport)
    sem = asyncio.Semaphore(concurrency)

    async def one(c: Case) -> tuple[Completion, Parsed, bool]:
        msgs = messages(static, case_block(c), system_mode)
        async with sem:
            comp = await client.complete(msgs, DECISION_SCHEMA)
        parsed = parse_decision(comp.text or "", c.order_total_cents)
        if prof.structured_mode == "none" and comp.error is None and parsed.error in ("PARSE_ERROR", "SCHEMA_ERROR"):
            msgs = msgs[:-1] + [{"role": "user", "content": msgs[-1]["content"] + REPAIR_SUFFIX}]
            async with sem:
                comp = await client.complete(msgs, DECISION_SCHEMA)
            return comp, parse_decision(comp.text or "", c.order_total_cents), True
        return comp, parsed, False

    try:
        return list(await asyncio.gather(*(one(c) for c in cases)))
    finally:
        await client.aclose()


def run_metrics(results: list[tuple[Completion, Parsed, bool]], wall_s: float) -> dict[str, Any]:
    """Master §13.5 metrics for one batch of decisions."""
    comps = [c for c, _, _ in results]
    n = len(comps)
    prompt = sum(c.prompt_tokens for c in comps)
    cached = [c.cached_tokens for c in comps if c.cached_tokens is not None]
    lat = sorted(c.latency_ms for c in comps)
    errors: dict[str, int] = {}
    for c, p, _ in results:
        code = c.error or p.error
        if code:
            errors[code] = errors.get(code, 0) + 1
    per_s = wall_s if wall_s > 0 else None
    return {
        "n": n, "wall_s": round(wall_s, 2), "cases_per_min": round(60 * n / per_s, 1) if per_s else None,
        "prompt_tok_s": round(prompt / per_s, 1) if per_s else None,
        "completion_tok_s": round(sum(c.completion_tokens for c in comps) / per_s, 1) if per_s else None,
        "cached_ratio": fx(sum(cached) / prompt, 4) if cached and prompt else None,
        "p50_ms": lat[n // 2] if n else None, "p95_ms": lat[min(n - 1, int(0.95 * n))] if n else None,
        "valid_rate": fx(sum(p.valid for _, p, _ in results) / n, 4) if n else "0",
        "repair_rate": fx(sum(r for _, _, r in results) / n, 4) if n else "0",
        "error_rate": fx(sum(c.error is not None for c in comps) / n, 4) if n else "0",
        "errors": dict(sorted(errors.items())), "attempts_max": max((c.attempts for c in comps), default=0),
    }


def set_profile_modes(yaml_path: Path, profile: str, structured_mode: str, system_mode: str,
                      concurrency: int | None = None) -> None:
    """Rewrite one profile's structured_mode / system_mode (S3) and concurrency (S2) in tenure.yaml, keeping comments."""
    text = yaml_path.read_text(encoding="utf-8")
    m = re.search(rf"^([ \t]+){re.escape(profile)}:\n((?:\1[ \t]+.*\n)+)", text, re.MULTILINE)
    if m is None:
        raise ValueError(f"profile {profile} not found in {yaml_path}")
    block = re.sub(r"(structured_mode:[ \t]*)\S+", rf"\g<1>{structured_mode}", m.group(2), count=1)
    block = re.sub(r"(system_mode:[ \t]*)\S+", rf"\g<1>{system_mode}", block, count=1)
    if concurrency is not None:
        block = re.sub(r"(concurrency:[ \t]*)\d+", rf"\g<1>{concurrency}", block, count=1)
    yaml_path.write_text(text[:m.start(2)] + block + text[m.end(2):], encoding="utf-8")
    load(yaml_path)  # still valid


def s3_run(root: Path, cfg: TenureConfig, prof: EndpointProfile, key: str, n: int = 100,
           transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Spike S3: modes in master §13.2 order × both system modes; the first pair reaching 99% valid wins."""
    static, cases = decider_static(root, cfg), sample_cases(root, cfg, "replay", n)
    trials: list[dict[str, Any]] = []
    chosen = None
    for mode in STRUCTURED_MODES:
        for system_mode in ("system", "fold"):
            start = time.perf_counter()
            res = asyncio.run(decide_many(replace(prof, structured_mode=mode), key, static, cases, system_mode,
                                          _profile_cfg(cfg).concurrency, transport))
            m = run_metrics(res, time.perf_counter() - start) | {"mode": mode, "system_mode": system_mode}
            trials.append(m)
            console.print(f"S3 {mode}/{system_mode}: valid {m['valid_rate']} errors {m['errors']}")
            if chosen is None and float(m["valid_rate"]) >= 0.99:
                chosen = (mode, system_mode)
        if chosen is not None:
            break
    return {"pass": chosen is not None, "chosen": chosen or ("none", "system"), "n": len(cases),
            "prompt_sha": prompt_sha(static), "trials": trials}


def s2_run(root: Path, cfg: TenureConfig, prof: EndpointProfile, key: str, n: int = 500,
           levels: tuple[int, ...] = (16, 32, 64, 128),
           transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """Spike S2: throughput and prefix-cache evidence over a concurrency sweep (master §24)."""
    static = decider_static(root, cfg)
    system_mode = _profile_cfg(cfg).system_mode
    cases = sample_cases(root, cfg, "replay", n)
    # Cold vs warm latency on one case: the cache evidence when usage carries no cached_tokens (§12.2).
    probe = asyncio.run(decide_many(prof, key, static, cases[:1] * 3, system_mode, 1, transport))
    sweep = []
    for conc in levels:
        start = time.perf_counter()
        res = asyncio.run(decide_many(prof, key, static, cases, system_mode, conc, transport))
        m = run_metrics(res, time.perf_counter() - start) | {"concurrency": conc}
        sweep.append(m)
        console.print(f"S2 c={conc}: {m['cases_per_min']} cases/min, cached {m['cached_ratio']}, "
                      f"p50 {m['p50_ms']} ms, p95 {m['p95_ms']} ms, error rate {m['error_rate']}")
    ok = [m for m in sweep if float(m["error_rate"]) < 0.01]
    best = max(ok, key=lambda m: m["cases_per_min"] or 0) if ok else None
    cold = probe[0][0].latency_ms
    warm = statistics.median(c.latency_ms for c, _, _ in probe[1:])
    cache_ok = any(m["cached_ratio"] is not None and float(m["cached_ratio"]) > 0.8 for m in sweep) or \
        warm < 0.8 * cold
    return {"pass": best is not None and (best["cases_per_min"] or 0) >= 750 and cache_ok,
            "best_concurrency": best["concurrency"] if best else None, "cold_ms": cold, "warm_ms": warm,
            "cache_evidence": cache_ok, "prompt_sha": prompt_sha(static), "sweep": sweep}


def _spike_record(root: Path, name: str, prof: EndpointProfile, result: dict[str, Any]) -> Path:
    out = root / "var" / "spikes" / f"{name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    rec = {"spike": name, "created_utc": utc_now(), "host": urlparse(prof.base_url).hostname, "model": prof.model,
           **result}
    out.write_text(json.dumps(rec, indent=2, default=str) + "\n", encoding="utf-8")
    return out


@app.command()
def intake(subset: str = typer.Option("", "--subset", help="Gemma on one D-intake subset: gold-pool or slice"),
           keywords_rest: bool = typer.Option(False, "--keywords-rest",
                                              help="Keyword labels for every unlabelled case (no network)"),
           all_cases: bool = typer.Option(False, "--all", help="Gemma on every case (refused on rate-limited profiles)"),
           batch_size: int = typer.Option(500, "--batch-size", min=1), config: Path = ConfigOpt) -> None:
    """Frozen intake (master §11, D-intake): Gemma with keyword fallback → intake.parquet, merged into cases.parquet."""
    if sum((bool(subset), keywords_rest, all_cases)) != 1:
        console.print("pass exactly one of --subset gold-pool|slice, --keywords-rest, --all")
        raise typer.Exit(2)
    root, cfg = _setup(config)
    derived = root / cfg.paths.derived
    out = derived / "intake.parquet"
    cases = pd.read_parquet(derived / "cases.parquet")
    def progress(done: int, total: int) -> None:
        console.print(f"intake {done}/{total}")


    if keywords_rest:
        blockers = keywords_rest_blockers(cases, pd.read_parquet(out) if out.exists() else None, cfg.seed)
        if blockers:
            console.print(f"--keywords-rest refused: Gemma subsets still have unlabelled cases {blockers}")
            raise typer.Exit(1)
        frozen = asyncio.run(run_intake(cases, out, classify_keywords, "keywords", keyword_rules_sha(), batch_size,
                                        progress))
    else:
        prof, key = _live_endpoint(cfg, "intake")
        if all_cases and _profile_cfg(cfg).rpm is not None:
            console.print(f"--all refused: profile {prof.name} is rate-limited ({_profile_cfg(cfg).rpm} rpm); "
                          "use --subset (D-intake)")
            raise typer.Exit(2)
        try:
            todo = cases if all_cases else subset_cases(cases, subset, cfg.seed)
        except ValueError as e:
            console.print(str(e))
            raise typer.Exit(2) from e
        system_mode, conc = _profile_cfg(cfg).system_mode, _profile_cfg(cfg).concurrency

        async def go() -> pd.DataFrame:
            client = LLMClient(prof, key, transport=limited_transport(cfg, prof))
            try:
                return await run_intake(todo, out, lambda df: classify_llm(client, df, conc, system_mode), prof.model,
                                        intake_prompt_sha(), batch_size, progress)
            finally:
                await client.aclose()

        frozen = asyncio.run(go())
    merge_into_cases(cases, frozen).to_parquet(derived / "cases.parquet", index=False)
    console.print(f"{len(frozen)} labelled · sources {dict(frozen['source'].value_counts())}")
    console.print(f"case types {dict(frozen['case_type'].value_counts().sort_index())}")
    agree = keyword_agreement(cases, frozen)
    console.print("gemma–keyword agreement " + ", ".join(f"{t} {a}/{b}" for t, (a, b) in agree.items()))


@app.command("decide-sample")
def decide_sample(n: int = typer.Option(30, "--n", min=1), split: str = typer.Option("replay", "--split"),
                  out: Path = SampleOutOpt,
                  config: Path = ConfigOpt) -> None:
    """Decide n cases live and record the raw completions as offline test fixtures."""
    root, cfg = _setup(config)
    prof, key = _live_endpoint(cfg, "decide-sample")
    static = decider_static(root, cfg)
    cases = sample_cases(root, cfg, split, n)
    res = asyncio.run(decide_many(prof, key, static, cases, _profile_cfg(cfg).system_mode,
                                  _profile_cfg(cfg).concurrency, limited_transport(cfg, prof)))
    target = out if out.is_absolute() else root / out
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        for c, (comp, parsed, repaired) in zip(cases, res):
            f.write(json.dumps({"case_id": c.case_id, "case_type": c.case_type.value,
                                "order_total_cents": c.order_total_cents, "model": prof.model,
                                "structured_mode": prof.structured_mode, "prompt_sha": prompt_sha(static),
                                "text": comp.text, "error": comp.error, "parse_error": parsed.error,
                                "repaired": repaired}, ensure_ascii=False) + "\n")
    console.print(f"{sum(p.valid for _, p, _ in res)}/{len(res)} valid → {target}")


@app.command()
def spike(name: str = typer.Argument(..., help="s2 or s3"), n: int = typer.Option(0, "--n", min=0),
          write: bool = typer.Option(True, "--write/--no-write", help="Write the result to tenure.yaml"),
          levels: str = typer.Option("16,32,64,128", "--levels", help="S2 concurrency sweep, comma-separated"),
          config: Path = ConfigOpt) -> None:
    """Run spike S2 (throughput, prefix cache) or S3 (structured-output mode); prints a STATE-ready line."""
    if name not in ("s2", "s3"):
        console.print("spike must be s2 or s3")
        raise typer.Exit(2)
    root, cfg = _setup(config)
    prof, key = _live_endpoint(cfg, name.upper())
    if name == "s3":
        r = s3_run(root, cfg, prof, key, n or 100, transport=limited_transport(cfg, prof))
        mode, system_mode = r["chosen"]
        if write:
            set_profile_modes(config, cfg.inference.default_profile, mode, system_mode)
        path = _spike_record(root, "S3", prof, r)
        console.print(f"S3: {'PASS' if r['pass'] else 'FAIL → none + repair (propose D-mode)'} → "
                      f"structured_mode={mode}, system_mode={system_mode}, model={prof.model}, "
                      f"valid={[(t['mode'], t['system_mode'], t['valid_rate']) for t in r['trials']]}, "
                      f"record {path.relative_to(root)}")
    else:
        r = s2_run(root, cfg, prof, key, n or 500, tuple(int(x) for x in levels.split(",")),
                   transport=limited_transport(cfg, prof))
        if write and r["best_concurrency"]:
            p = _profile_cfg(cfg)
            set_profile_modes(config, cfg.inference.default_profile, p.structured_mode, p.system_mode,
                              r["best_concurrency"])
        path = _spike_record(root, "S2", prof, r)
        best = next((m for m in r["sweep"] if m["concurrency"] == r["best_concurrency"]), {})
        console.print(f"S2: {'PASS' if r['pass'] else 'FAIL'} → model={prof.model}, "
                      f"concurrency={r['best_concurrency']}, {best.get('cases_per_min')} cases/min, "
                      f"cached_ratio={best.get('cached_ratio')}, cold/warm {r['cold_ms']}/{r['warm_ms']} ms, "
                      f"p50/p95 {best.get('p50_ms')}/{best.get('p95_ms')} ms, error_rate={best.get('error_rate')}, "
                      f"record {path.relative_to(root)}")
    if not r["pass"]:
        raise typer.Exit(1)


# --- T02: synthetic team and gold-set harness -----------------------------------------------------------------

HUMANS_COLUMNS: tuple[str, ...] = (
    "case_id", "primary_hid", "primary_action", "primary_amount_cents", "primary_requires_return", "primary_clause",
    "secondary_hid", "secondary_action", "secondary_amount_cents", "secondary_requires_return", "secondary_clause",
)
TRANSLATE_PROMPT = ("Translate this customer message about an online order from Brazilian Portuguese into English. "
                    "Reply with the translation only.")


def humans_table(cases: pd.DataFrame, schedule: PolicySchedule, seed: int, double_rate: float = 0.15) -> pd.DataFrame:
    """Team decisions per case (master §14.3 columns), in case_id order; secondary_* null when not double-coded."""
    if (cases["case_type"] == "").any() or cases["case_type"].isna().any():
        raise ValueError("every case needs an intake case_type before the team can decide it")
    rows = []
    for rec in cases.sort_values("case_id", kind="stable").to_dict("records"):
        lab = label_case(case_from_record(rec), schedule, seed, double_rate=double_rate)
        s = lab.secondary
        rows.append((lab.case_id, lab.primary_hid, lab.primary.action.value, lab.primary.amount_cents,
                     lab.primary.requires_return, lab.primary.clause, lab.secondary_hid,
                     None if s is None else s.action.value, None if s is None else s.amount_cents,
                     None if s is None else s.requires_return, None if s is None else s.clause))
    df = pd.DataFrame(rows, columns=list(HUMANS_COLUMNS))
    for col in ("primary_amount_cents", "secondary_amount_cents"):
        df[col] = df[col].astype("Int64")
    df["secondary_requires_return"] = df["secondary_requires_return"].astype("boolean")
    return df


def _typed_cases(root: Path, cfg: TenureConfig) -> pd.DataFrame:
    derived = root / cfg.paths.derived
    cases = pd.read_parquet(derived / "cases.parquet")
    if (derived / "intake.parquet").exists():
        cases = merge_into_cases(cases, pd.read_parquet(derived / "intake.parquet"))
    return cases


@app.command()
def humans(config: Path = ConfigOpt) -> None:
    """Synthetic team decisions → humans.parquet (all splits, Policy v1) and humans_drift.parquet (live, D-POLICY)."""
    root, cfg = _setup(config)
    derived = root / cfg.paths.derived
    cases = _typed_cases(root, cfg)
    cases = cases[cases["split"] != "excluded"]
    rate = cfg.synthetic.double_rate
    base = humans_table(cases, PolicySchedule(), cfg.seed, rate)
    base.to_parquet(derived / "humans.parquet", index=False)
    drift = PolicySchedule(((date.min, POLICY_V1), (cfg.policy.drift_date, POLICY_V2)))
    live = humans_table(cases[cases["split"] == "live"], drift, cfg.seed, rate)
    live.to_parquet(derived / "humans_drift.parquet", index=False)
    doubled = base["secondary_hid"].notna()
    console.print(f"humans.parquet {len(base)} cases, double-coded {doubled.mean():.4f}; "
                  f"humans_drift.parquet {len(live)} live cases (v2 from {cfg.policy.drift_date})")


gold_app = typer.Typer(no_args_is_help=True, help="Gold set: sample, label, eval (master §15).")
app.add_typer(gold_app, name="gold")


def _gold_dir(root: Path, cfg: TenureConfig) -> Path:
    return root / cfg.paths.gold


@gold_app.command("sample")
def gold_sample(config: Path = ConfigOpt) -> None:
    """Draw the 300 gold cases from the D-intake gold pool, stratified by case type."""
    root, cfg = _setup(config)
    cases = _typed_cases(root, cfg)
    pool = subset_cases(cases, "gold-pool", cfg.seed)
    gold = sample_gold(pool, cfg.seed)
    out = _gold_dir(root, cfg)
    out.mkdir(parents=True, exist_ok=True)
    gold.to_parquet(out / "gold_cases.parquet", index=False)
    console.print(f"{len(gold)} gold cases → {out / 'gold_cases.parquet'}")
    console.print(f"by type {dict(gold['case_type'].value_counts().sort_index())}")


@gold_app.command("label")
def gold_label(labeler: str = typer.Option(..., "--labeler", help="Your name, stored with each label"),
               config: Path = ConfigOpt) -> None:
    """Label the gold cases (resumable): case file, Portuguese message, cached English translation."""
    root, cfg = _setup(config)
    gdir = _gold_dir(root, cfg)
    rows = pd.read_parquet(gdir / "gold_cases.parquet").to_dict("records")
    prof, key = _live_endpoint(cfg, "gold label")
    system_mode = _profile_cfg(cfg).system_mode

    def translate(text: str) -> str:
        async def go() -> Completion:
            client = LLMClient(prof, key, transport=limited_transport(cfg, prof))  # one per call: one event loop each
            try:
                return await client.complete(messages(TRANSLATE_PROMPT, text, system_mode))  # unconstrained text
            finally:
                await client.aclose()

        comp = asyncio.run(go())
        return comp.text.strip() if comp.text else f"(translation unavailable: {comp.error})"

    cache = TranslationCache(gdir / "translations.jsonl", translate)
    n = label_session(rows, gdir / "gold_labels.jsonl", labeler, input, console.print, cache, utc_now)
    console.print(f"{n} labelled this session; {len(read_labels(gdir / 'gold_labels.jsonl'))}/{len(rows)} in total")


@gold_app.command("eval")
def gold_eval(config: Path = ConfigOpt) -> None:
    """Agreement of the synthetic primary decisions with the gold labels, with Wilson-cc intervals (master §3)."""
    root, cfg = _setup(config)
    gdir = _gold_dir(root, cfg)
    gold_cases = pd.read_parquet(gdir / "gold_cases.parquet")
    labels = {r["case_id"]: Resolution(Action(r["action"]), r["amount_cents"], False, "")
              for r in read_labels(gdir / "gold_labels.jsonl")}
    hum = pd.read_parquet(root / cfg.paths.derived / "humans.parquet")
    hum = hum[hum["case_id"].isin(labels)]
    primary = {r["case_id"]: Resolution(Action(r["primary_action"]),
                                        None if pd.isna(r["primary_amount_cents"]) else int(r["primary_amount_cents"]),
                                        bool(r["primary_requires_return"]), r["primary_clause"])
               for r in hum.to_dict("records")}
    types = dict(zip(gold_cases["case_id"], gold_cases["case_type"]))
    r = agreement_intervals(raw_agreement(labels, primary, types))

    def line(v: dict[str, Any]) -> str:
        if not v["n"]:
            return "0/0"
        return f"{v['x']}/{v['n']} = {v['p_hat']:.3f}, Wilson-cc 95% [{v['L_cc']:.4f}, {v['U_cc']:.4f}]"

    console.print(f"synthetic primary vs gold: {line(r['overall'])}")
    for t, v in r["by_type"].items():
        console.print(f"  {t}: {line(v)}")


# --- T03: replay ------------------------------------------------------------------------------------------------

REPLAY_SELECTION = {"subset": "slice", "decision": "D-replay",
                    "definition": "the 3,000 replay cases with the smallest unit_hash(seed, 'slice', case_id)"}


def next_run_id(runs_dir: Path, today: date) -> str:
    prefix = f"R-{today:%Y%m%d}-"
    taken = [p.name for p in runs_dir.glob(prefix + "*")] if runs_dir.exists() else []
    nums = [int(n[len(prefix):]) for n in taken if n[len(prefix):].isdigit()]
    return f"{prefix}{max(nums, default=0) + 1:02d}"


@app.command()
def replay(split: str = typer.Option("replay", "--split"),
           profile: str = typer.Option("", "--profile", help="Inference profile (default: tenure.yaml default)"),
           record: Path | None = typer.Option(None, "--record", help="Run directory var/runs/<run_id>; reuse to resume"),
           all_cases: bool = typer.Option(False, "--all", help="Every case of the split, not the D-replay slice"),
           limit: int = typer.Option(0, "--limit", min=0, help="Only the first N cases in case order"),
           adapter: str = typer.Option("", "--adapter", help="LoRA adapter model name served by vLLM"),
           config: Path = ConfigOpt) -> None:
    """Replay a split through the Gemma decider in case order, with events, cache and resumption (master §16)."""
    from .events import EventBus
    from .gpu import GpuPoller
    from .runs import ReplayRun, load_primary_humans, load_replay_cases
    from .store import OutcomeStore

    root, cfg = _setup(config)
    name = profile or cfg.inference.default_profile
    prof, key = endpoint(cfg, profile=name)
    if key == MOCK_API_KEY:
        console.print("replay: PENDING → the API key is the mock key (D-llm-mock); set real TENURE_LLM_* values")
        raise typer.Exit(2)
    subset = None if all_cases or split != "replay" else "slice"
    cases = load_replay_cases(root, cfg, split, subset=subset)
    if limit:
        cases = cases[:limit]
    selection = dict(REPLAY_SELECTION) if subset else {"subset": None, "split": split}
    if limit:
        selection["first_n"] = limit
    humans = load_primary_humans(root / cfg.paths.derived / "humans.parquet", [c.case_id for c in cases])
    runs_dir = root / cfg.paths.runs
    run_dir = record if record is not None else runs_dir / next_run_id(runs_dir, datetime.now(timezone.utc).date())
    outcomes = root / cfg.paths.outcomes_db
    outcomes.parent.mkdir(parents=True, exist_ok=True)
    bus = EventBus()
    total = len(cases)

    def show(event: str, data: dict[str, Any]) -> None:
        if event == "run":
            console.print(f"{data['run_id']} {data['status']} · {data['model']} · {data['profile']}")
        elif event == "progress" and (data["committed"] % 25 == 0 or data["committed"] == total):
            cpm = None if data["cases_per_s"] is None else round(60 * data["cases_per_s"], 1)
            console.print(f"  {data['committed']}/{total} · sim {data['sim_date']} · {cpm} cases/min · "
                          f"p50 {data['p50_ms']} ms · errors {data['errors']}")

    bus.add_sink(show)
    gpu = GpuPoller(bus) if name == "vllm_droplet" else None
    led, _ = _ledger(root, cfg)
    run = ReplayRun(cfg, name, split, adapter or None, root=root, cases=cases, humans=humans,
                    store=OutcomeStore(outcomes), run_dir=run_dir, transport=limited_transport(cfg, prof), bus=bus,
                    gpu=gpu, selection=selection, ledger=led)
    console.print(f"replay {run.run_id}: {total} cases ({selection.get('subset') or split}), run_key {run.run_key}, "
                  f"concurrency {run.concurrency}, rpm {cfg.inference.profiles[name].rpm}")
    try:
        summary = asyncio.run(run.run())
    finally:
        led.close()
    m = summary["metrics"]
    console.print(f"{run.run_id}: committed {summary['committed']} (resumed from {summary['resumed_from']}), "
                  f"wall {m['wall_s']} s, {m['cases_per_min']} cases/min, compression {m['compression']}, "
                  f"cached_ratio {m['cached_ratio']}, valid {m['valid_rate']}, agree {m['agree_rate']}, "
                  f"errors {m['errors']}")
    console.print(f"record: {run_dir}")


# --- T04: statistical evaluation --------------------------------------------------------------------------------

NOT_EVALUATED = ("DWELL", "MONITOR_POWER")  # ladder-state blockers: T05 (master §7.2)


def _agent_from_payload(d: dict[str, Any] | None) -> AgentDecision | None:
    if d is None:
        return None
    return AgentDecision(Action(d["action"]), d["amount_cents"], bool(d["requires_return"]), d["clause"],
                         int(d["confidence_ppm"]), "")


def _interval(x: int, n: int) -> dict[str, Any]:
    lo, hi = wilson_cc_interval(x, n)
    return {"n": n, "x": x, "p_hat": x / n, "L_cc": lo, "U_cc": hi}


def evaluate_run(run_dir: Path, cfg: TenureConfig, cases: pd.DataFrame, humans: pd.DataFrame,
                 sources: dict[str, str] | None = None) -> dict[str, Any]:
    """Per-cell evidence for a replay run (master §3–§5): agreement with its Wilson-cc bounds, the human–human
    baseline and scope, the NI threshold and blockers per level, and the cost mean, UCB and gate.

    Invalid outputs stay in n as disagreements (I5). A run stopped before "finished" is evaluated on its
    committed records. The report holds no timestamps, so equal inputs give equal bytes."""
    run_id = run_dir.name
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    raw = (run_dir / "decisions.jsonl").read_bytes()
    recs = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    if not recs:
        raise ValueError(f"{run_dir / 'decisions.jsonl'} holds no committed records")
    foreign = [r["case_id"] for r in recs if r["run_id"] != run_id]
    if foreign:
        raise ValueError(f"{len(foreign)} records belong to another run (first: {foreign[0]})")
    ids = {r["case_id"] for r in recs}
    rows = cases[cases["case_id"].isin(ids)]
    by_id = {str(rec["case_id"]): case_from_record(rec) for rec in rows.to_dict("records")}
    hum = humans[humans["case_id"].isin(ids)]
    primary = {str(r["case_id"]): Resolution(Action(r["primary_action"]),
                                             None if pd.isna(r["primary_amount_cents"]) else int(r["primary_amount_cents"]),
                                             bool(r["primary_requires_return"]), str(r["primary_clause"]))
               for r in hum.to_dict("records")}
    for r in recs:
        cid = r["case_id"]
        if cid not in by_id or cid not in primary:
            raise ValueError(f"case {cid} of run {run_id} is missing from cases.parquet or humans.parquet")
        h = primary[cid]
        if h.action.value != r["human_action"] or h.amount_cents != r["human_amount_cents"]:
            raise ValueError(f"humans.parquet no longer matches run {run_id} at case {cid}")

    params = CostParams(**cfg.cost.model_dump())
    table = pair_table(cases, humans, params, TRAIN_REPLAY, cfg.agreement.tol_abs_cents, cfg.agreement.tol_rel_bp)
    lad = cfg.ladder
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in recs:
        grouped.setdefault(r["cell"], []).append(r)

    cells: dict[str, Any] = {}
    for cell in sorted(grouped):
        rs = grouped[cell]
        n, x, valid = len(rs), sum(bool(r["agree"]) for r in rs), sum(bool(r["valid"]) for r in rs)
        comps = np.asarray([error_components(_agent_from_payload(r["decision"]), primary[r["case_id"]],
                                             by_id[r["case_id"]], params) for r in rs], dtype=float)
        base, cbase = cell_baselines(table, cell, lad.baseline_n_min)
        cg = cost_gate(comps, cbase.mean_cents, params, lad.bootstrap_b, (str(cfg.seed), cell, run_id, str(n)),
                       lad.cost_eps_abs_cents, lad.cost_rho)
        ceiling = lad.ceilings.get(cell.split("|", 1)[0], "L3")
        levels: dict[str, Any] = {}
        for name, g in sorted(lad.gates.items()):
            ni = non_inferiority(x, n, base, g.delta)
            ni_ok = ni.passed_strict if lad.ni_mode == "strict" else ni.passed
            blockers = []
            if Level[name] > Level[ceiling]:
                blockers.append("CEILING")
            if n < g.n_min:
                blockers.append("N_MIN")
            if valid / n < g.min_valid_rate:
                blockers.append("VALID_RATE")
            if not ni_ok:
                blockers.append("NON_INFERIORITY")
            if g.cost_gate and not cg.passed:
                blockers.append("COST")
            levels[name] = {"n_min": g.n_min, "delta": g.delta, "min_valid_rate": g.min_valid_rate,
                            "threshold": ni.threshold, "ni_passed": ni.passed, "diff_lower": ni.diff_lower,
                            "ni_passed_strict": ni.passed_strict,
                            "cost_threshold_cents": cg.threshold if g.cost_gate else None,
                            "cost_passed": cg.passed if g.cost_gate else None, "blockers": blockers}
        cells[cell] = {**_interval(x, n), "valid": valid, "valid_rate": valid / n, "baseline": base.rate,
                       "baseline_x": base.x, "baseline_n": base.n, "scope": base.scope, "ceiling": ceiling,
                       "cost_mean_cents": cg.mean, "cost_ucb_cents": cg.ucb,
                       "cost_components_mean": [float(v) for v in comps.mean(axis=0)],
                       "cost_baseline_cents": cbase.mean_cents, "cost_baseline_n": cbase.n, "cost_scope": cbase.scope,
                       "levels": levels}

    summary: dict[str, Any] = {}
    for name in sorted(lad.gates):
        blocked: dict[str, int] = {}
        for c in cells.values():
            for b in c["levels"][name]["blockers"]:
                blocked[b] = blocked.get(b, 0) + 1
        summary[name] = {
            "ni_pass": sorted(k for k, c in cells.items() if not set(c["levels"][name]["blockers"]) - {"COST"}),
            "pass": sorted(k for k, c in cells.items() if not c["levels"][name]["blockers"]),
            "blocked": dict(sorted(blocked.items()))}

    by_source: dict[str, Any] = {}
    if sources is not None:
        tally: dict[str, list[int]] = {}
        for r in recs:
            t = tally.setdefault(sources.get(r["case_id"], "unknown"), [0, 0])
            t[0] += int(bool(r["agree"]))
            t[1] += 1
        by_source = {k: _interval(v[0], v[1]) for k, v in sorted(tally.items())}

    x_all = sum(bool(r["agree"]) for r in recs)
    return {
        "run_id": run_id, "run_key": manifest.get("run_key"), "split": manifest.get("split"),
        "model": manifest.get("model"), "selection": manifest.get("selection"),
        "records": len(recs), "planned_cases": manifest.get("cases"), "complete": "finished" in manifest,
        "first_case_date": min(c.contact_date for c in by_id.values()).isoformat(),
        "last_case_date": max(c.contact_date for c in by_id.values()).isoformat(),
        "decisions_sha256": hashlib.sha256(raw).hexdigest(), "config_sha": config_sha(cfg),
        "params": {"z": Z95, "baseline_n_min": lad.baseline_n_min, "ni_mode": lad.ni_mode,
                   "bootstrap_b": lad.bootstrap_b, "cost_eps_abs_cents": lad.cost_eps_abs_cents,
                   "cost_rho": lad.cost_rho, "cost": cfg.cost.model_dump(),
                   "gates": {k: v.model_dump() for k, v in sorted(lad.gates.items())},
                   "ceilings": dict(sorted(lad.ceilings.items())), "not_evaluated": list(NOT_EVALUATED)},
        "baseline_pairs": {"splits": list(TRAIN_REPLAY), "pairs": table.pairs, "agree": table.agree,
                           "cells": len(table.agreement)},
        "overall": {**_interval(x_all, len(recs)), "valid": sum(bool(r["valid"]) for r in recs)},
        "by_intake_source": by_source, "cells": cells, "summary": summary,
    }


@app.command()
def evaluate(run: str = typer.Option(..., "--run", help="Run id: the directory var/runs/<run_id>"),
             config: Path = ConfigOpt) -> None:
    """Per-cell Wilson-cc, non-inferiority and cost evidence for a replay run → var/runs/<id>/evaluation.json."""
    root, cfg = _setup(config)
    run_dir = root / cfg.paths.runs / run
    if not (run_dir / "decisions.jsonl").exists() or not (run_dir / "manifest.json").exists():
        console.print(f"evaluate: no run at {run_dir} (needs manifest.json and decisions.jsonl)")
        raise typer.Exit(1)
    derived = root / cfg.paths.derived
    cases = _typed_cases(root, cfg)
    humans_df = pd.read_parquet(derived / "humans.parquet")
    sources = None
    if (derived / "intake.parquet").exists():
        intake_df = pd.read_parquet(derived / "intake.parquet", columns=["case_id", "source"])
        sources = dict(zip(intake_df["case_id"].astype(str), intake_df["source"].astype(str)))
    report = evaluate_run(run_dir, cfg, cases, humans_df, sources)
    out = run_dir / "evaluation.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
                   encoding="utf-8", newline="\n")
    tmp.replace(out)

    o = report["overall"]
    console.print(f"{report['run_id']}: {o['x']}/{o['n']} agree = {o['p_hat']:.3f} "
                  f"[{o['L_cc']:.4f}, {o['U_cc']:.4f}], valid {o['valid']}/{o['n']}, "
                  f"{'complete' if report['complete'] else 'stopped before finished'}")
    table = Table("cell", "n", "x", "p_hat", "L_cc", "baseline (scope)", "L1", "L2", "L3", "cost mean/UCB ¢")
    for cell, c in report["cells"].items():
        marks = [", ".join(c["levels"][lv]["blockers"]) or "pass" for lv in ("L1", "L2", "L3")]
        table.add_row(cell, str(c["n"]), str(c["x"]), f"{c['p_hat']:.3f}", f"{c['L_cc']:.3f}",
                      f"{c['baseline']:.3f} ({c['scope']})", *marks,
                      f"{c['cost_mean_cents']:.0f}/{c['cost_ucb_cents']:.0f}")
    console.print(table)
    for lv, s in report["summary"].items():
        console.print(f"{lv}: NI pass {len(s['ni_pass'])}/{len(report['cells'])} cells {s['ni_pass']} · "
                      f"blocked {s['blocked']}")
    console.print(f"evaluation: {out}")


# --- T05: ladder sensitivity and the go-live simulation ----------------------------------------------------------------

ladder_app = typer.Typer(no_args_is_help=True, help="Autonomy ladder: replay a run through it (master §7).")
app.add_typer(ladder_app, name="ladder")


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n",
                   encoding="utf-8", newline="\n")
    tmp.replace(path)


def _run_dir(root: Path, cfg: TenureConfig, run: str, what: str) -> Path:
    run_dir = root / cfg.paths.runs / run
    if not (run_dir / "decisions.jsonl").exists():
        console.print(f"{what}: no run at {run_dir} (needs decisions.jsonl)")
        raise typer.Exit(1)
    return run_dir


@ladder_app.command("sensitivity")
def ladder_sensitivity(run: str = typer.Option(..., "--run", help="Run id: the directory var/runs/<run_id>"),
                       tick_k: int = typer.Option(0, "--tick-k", min=0, help="Tick every k records (0: tenure.yaml)"),
                       config: Path = ConfigOpt) -> None:
    """Replay a run's records through the ladder: cells at ≥ L2 / ≥ L3 for D_max ∈ {30, 60, 90, ∞} and three
    margin variants → sensitivity.md, sensitivity.json, and ladder_state.json (levels under the defaults)."""
    from .runs import sensitivity, sensitivity_markdown

    root, cfg = _setup(config)
    run_dir = _run_dir(root, cfg, run, "ladder sensitivity")
    cases = _typed_cases(root, cfg)
    humans_df = pd.read_parquet(root / cfg.paths.derived / "humans.parquet")
    report, state = sensitivity(run_dir, cfg, cases, humans_df, tick_k or None)
    _write_json(run_dir / "sensitivity.json", report)
    _write_json(run_dir / "ladder_state.json", state)
    md = sensitivity_markdown(report, state)
    (run_dir / "sensitivity.md").write_text(md, encoding="utf-8", newline="\n")
    console.print(md)
    console.print(f"sensitivity: {run_dir / 'sensitivity.md'} · ladder state: {run_dir / 'ladder_state.json'}")


def next_live_id(runs_dir: Path, today: date) -> str:
    prefix = f"L-{today:%Y%m%d}-"
    taken = [p.name for p in runs_dir.glob(prefix + "*")] if runs_dir.exists() else []
    nums = [int(n[len(prefix):]) for n in taken if n[len(prefix):].isdigit()]
    return f"{prefix}{max(nums, default=0) + 1:02d}"


@app.command()
def live(from_run: str = typer.Option(..., "--from-run", help="Replay run whose final ladder state seeds go-live"),
         drift: str = typer.Option("policy", "--drift", help="policy (D-POLICY, humans_drift.parquet) or none"),
         agent: str = typer.Option("mock", "--agent",
                                   help="mock (policy oracle with 2% errors; offline) or cache (cached completions)"),
         policy_update: str = typer.Option("", "--policy-update", help="Date the operator moves the agent to v2"),
         record: Path | None = typer.Option(None, "--record", help="Output directory (default var/runs/L-…)"),
         ledger: bool = typer.Option(False, "--ledger/--no-ledger", help="Persist ladder events to the ledger"),
         config: Path = ConfigOpt) -> None:
    """Go-live simulation over the live split (master §17) from a replay run's ladder; makes no endpoint call."""
    from .events import EventBus, Recorder
    from .runs import Drift, LiveRun, PolicyUpdate, cached_agent, ladder_from_run, load_primary_humans, mock_agent
    from .store import OutcomeStore

    if drift not in ("policy", "none") or agent not in ("mock", "cache"):
        console.print("live: --drift is policy|none and --agent is mock|cache")
        raise typer.Exit(2)
    root, cfg = _setup(config)
    seed_dir = _run_dir(root, cfg, from_run, "live")
    derived = root / cfg.paths.derived
    cases_df = _typed_cases(root, cfg)
    humans_df = pd.read_parquet(derived / "humans.parquet")
    ladder, _ = ladder_from_run(seed_dir, cfg, cases_df, humans_df)
    live_rows = cases_df[cases_df["split"] == "live"]
    cases = sorted((case_from_record(r) for r in live_rows.to_dict("records")),
                   key=lambda c: (c.contact_date, c.case_id))
    humans_file = "humans_drift.parquet" if drift == "policy" else "humans.parquet"
    humans = load_primary_humans(derived / humans_file, [c.case_id for c in cases])
    seed_manifest_path = seed_dir / "manifest.json"
    seed_manifest = json.loads(seed_manifest_path.read_text(encoding="utf-8")) if seed_manifest_path.exists() else {}
    if agent == "mock":
        decide, agent_label = mock_agent(cfg.seed), "mock-oracle-v1"
    else:
        key = seed_manifest.get("run_key")
        if not key:
            console.print(f"live: {seed_manifest_path} names no run_key for --agent cache")
            raise typer.Exit(1)
        decide, agent_label = cached_agent(OutcomeStore(root / cfg.paths.outcomes_db), key), f"cache:{key}"
    scenario = None
    if drift == "policy":
        scenario = Drift("D-POLICY", cfg.policy.drift_date, {"policy_file": cfg.policy.drift_file,
                                                             "changed_clauses": ["P1"]})
    update = None
    if policy_update:
        if agent != "mock":
            console.print("live: --policy-update needs --agent mock (no v2 completions are cached)")
            raise typer.Exit(2)
        update = PolicyUpdate(date.fromisoformat(policy_update), ("P1",), _sha256_file(root / cfg.policy.drift_file),
                              "v2", mock_agent(cfg.seed, POLICY_V2))
    runs_dir = root / cfg.paths.runs
    out = record if record is not None else runs_dir / next_live_id(runs_dir, datetime.now(timezone.utc).date())
    out.mkdir(parents=True, exist_ok=True)
    note = ("offline under D-offline-gates, no endpoint call: a mock agent's figures describe the ladder mechanics, "
            "not Gemma") if agent == "mock" else "cached completions only, no endpoint call"
    manifest = {"run_id": out.name, "mode": "live", "seed_run": from_run, "seed_run_key": seed_manifest.get("run_key"),
                "drift": "D-POLICY" if drift == "policy" else "D-NONE", "agent": agent_label,
                "humans": humans_file, "policy_update": policy_update or None, "config_sha": config_sha(cfg),
                "seed": cfg.seed, "cases": len(cases),
                "first_date": cases[0].contact_date.isoformat() if cases else None,
                "last_date": cases[-1].contact_date.isoformat() if cases else None, "note": note}
    _write_json(out / "manifest.json", manifest)
    bus = EventBus()
    recorder = Recorder(out / "events.jsonl")
    bus.add_sink(recorder)
    led = _ledger(root, cfg)[0] if ledger else None
    try:
        run = LiveRun(cfg, cases=cases, humans=humans, agent=decide, ladder=ladder, drift=scenario,
                      policy_update=update, run_dir=out, bus=bus, run_id=out.name, ledger=led)
        summary = run.run()
    finally:
        bus.remove_sink(recorder)
        recorder.close()
        if led is not None:
            led.close()
    summary.update(seed_run=from_run, agent=agent_label, humans=humans_file, drift_scenario=manifest["drift"])
    _write_json(out / "summary.json", summary)
    console.print(f"{out.name}: {summary['cases']} live cases over {summary['days']} days · agent {agent_label} · "
                  f"{manifest['drift']} · observed {summary['observed']}, autonomous {summary['autonomous']}, "
                  f"audited {summary['audited']}")
    above = sum(v != "L0" for v in summary["levels_at_start"].values())
    console.print(f"cells above L0 at go-live: {above} · promotions {len(summary['promotions'])} · "
                  f"demotions {len(summary['demotions'])} · global alarm {summary['global_alarm']}")
    for d in summary["demotions"]:
        console.print(f"  demoted {d['cell']} {d['from']}->{d['to']} on {d['date']} after "
                      f"{d['outcomes_monitored']} monitored outcomes")
    console.print(f"record: {out}")


# --- T06: governance ledger ----------------------------------------------------------------------------------------

ledger_app = typer.Typer(no_args_is_help=True, help="Hash-chained governance ledger: verify, export (master §19).")
app.add_typer(ledger_app, name="ledger")


def _ledger(root: Path, cfg: TenureConfig):
    from .ledger import Ledger

    path = root / cfg.paths.ledger_db
    path.parent.mkdir(parents=True, exist_ok=True)
    return Ledger(path), path


@ledger_app.command("verify")
def ledger_verify(config: Path = ConfigOpt) -> None:
    """Check the whole chain offline: gapless seq, prev_hash, payload digest, canonical payload, hash, head."""
    from .governance import verification_line

    root, cfg = _setup(config)
    led, path = _ledger(root, cfg)
    try:
        rep = led.verify()
    finally:
        led.close()
    typer.echo(f"ledger {path}: {verification_line(rep)}")
    if not rep.ok:
        raise typer.Exit(1)


@ledger_app.command("export")
def ledger_export(out: Path = typer.Option(..., "--out", help="Directory, e.g. exports/<run>/"),
                  run: str = typer.Option("", "--run", help="Replay run whose final ladder state the report lists"),
                  config: Path = ConfigOpt) -> None:
    """ledger.jsonl, head.json and governance_report.md; a verified export is itself recorded (EXPORT)."""
    from .governance import export, verification_line
    from .runs import build_ladder, cell_summary, ladder_inputs, primary_resolutions, rebuild_ladder

    root, cfg = _setup(config)
    led, _ = _ledger(root, cfg)
    try:
        final = None
        if run:
            cases = _typed_cases(root, cfg)
            humans_df = pd.read_parquet(root / cfg.paths.derived / "humans.parquet")
            ladder = build_ladder(cfg, ladder_inputs(cfg, cases, humans_df))
            by_id = {str(r["case_id"]): case_from_record(r) for r in cases.to_dict("records")}
            rebuild_ladder(led, run, by_id, primary_resolutions(humans_df), ladder, cfg.ladder.tick_k)
            final = {cell: cell_summary(ladder, cell) for cell in sorted(ladder.cells)}
        label = f"ladder rebuilt from run {run}'s DECISION entries" if run else ""
        rep = export(led, out, final, final_label=label)
    finally:
        led.close()
    typer.echo(verification_line(rep))
    typer.echo(f"export: {out / 'ledger.jsonl'} · {out / 'head.json'} · {out / 'governance_report.md'}")
    if not rep.ok:
        raise typer.Exit(1)


@ledger_app.command("import-run")
def ledger_import_run(run: str = typer.Option(..., "--run", help="Recorded run id under var/runs"),
                      config: Path = ConfigOpt) -> None:
    """Write a run recorded before the ledger existed (its decisions.jsonl) to the ledger; no endpoint call."""
    from .runs import import_run_records

    root, cfg = _setup(config)
    run_dir = _run_dir(root, cfg, run, "ledger import-run")
    cases = _typed_cases(root, cfg)
    humans_df = pd.read_parquet(root / cfg.paths.derived / "humans.parquet")
    led, path = _ledger(root, cfg)
    try:
        head = import_run_records(led, run_dir, cfg, cases, humans_df)
        rep = led.verify()
    finally:
        led.close()
    typer.echo(f"{run} → {path}: head seq {head.seq} · {head.hash} · verify {'OK' if rep.ok else rep.reason}")


@gold_app.command("import-ledger")
def gold_import_ledger(config: Path = ConfigOpt) -> None:
    """gold_labels.jsonl → GOLD_LABEL entries {case_id, labeler, action, amount_cents, note_sha}; idempotent."""
    root, cfg = _setup(config)
    labels = read_labels(_gold_dir(root, cfg) / "gold_labels.jsonl")
    led, path = _ledger(root, cfg)
    try:
        seen = {(json.loads(p)["case_id"], actor) for p, actor in
                led.conn.execute("SELECT payload, actor FROM ledger WHERE kind = 'GOLD_LABEL'")}
        new = []
        for x in labels:
            key = (x["case_id"], x["labeler"])
            if key in seen:
                continue
            seen.add(key)
            note = x.get("note") or ""
            new.append(("GOLD_LABEL", x["labeler"], {
                "case_id": x["case_id"], "labeler": x["labeler"], "action": x["action"],
                "amount_cents": None if x.get("amount_cents") is None else int(x["amount_cents"]),
                "note_sha": hashlib.sha256(note.encode("utf-8")).hexdigest()}))
        if new:
            led.append_many(new)
    finally:
        led.close()
    typer.echo(f"gold import-ledger: {len(new)} new GOLD_LABEL entries ({len(labels)} labels) → {path}")
