"""TENURE command line: doctor, ingest, smoke (RUNBOOK T00). Later modules add their commands here."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import typer
from rich.console import Console
from rich.table import Table

from .canonical import fx
from .config import config_sha, endpoint, ingest_config, load, load_dotenv
from .llm import Completion, EndpointProfile, LLMClient
from .olist import build_cases, load_tables

app = typer.Typer(no_args_is_help=True, add_completion=False, help="TENURE: earned autonomy for refund agents.")
console = Console()

MOCK_API_KEY = "mock_key"  # decision D-llm-mock: never sent over the network
RAW_FILES = (
    "olist_customers_dataset.csv", "olist_geolocation_dataset.csv", "olist_order_items_dataset.csv",
    "olist_order_payments_dataset.csv", "olist_order_reviews_dataset.csv", "olist_orders_dataset.csv",
    "olist_products_dataset.csv", "olist_sellers_dataset.csv", "product_category_name_translation.csv",
)
ConfigOpt = typer.Option(Path("tenure.yaml"), "--config", "-c", help="Path to tenure.yaml")


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
    r = s1_check(prof, key, n)
    console.print(s1_state_line(r))
    if not r["pass"]:
        raise typer.Exit(1)
