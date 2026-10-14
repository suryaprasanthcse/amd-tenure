"""TENURE command line: doctor, ingest, smoke (T00); intake, decide-sample, spike s2|s3 (T01)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import statistics
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from .canonical import fx
from .config import TenureConfig, config_sha, endpoint, ingest_config, load, load_dotenv
from .domain import Case, CaseType
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
from .prompt import case_block, messages, prompt_sha, static_block
from .ratelimit import RateLimitedTransport, shared_limiter
from .schema import DECISION_SCHEMA, Parsed, parse_decision

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
