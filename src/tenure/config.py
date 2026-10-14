"""tenure.yaml loader: Pydantic models mirroring master §22, env overrides and config_sha."""
from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from .canonical import canonical_bytes, sha256_hex
from .llm import STRUCTURED_MODES, EndpointProfile
from .olist import IngestConfig

LevelName = Literal["L0", "L1", "L2", "L3"]
StructuredMode = Literal["json_schema", "vllm_structured", "vllm_guided", "none"]
assert set(StructuredMode.__args__) == set(STRUCTURED_MODES)

# TENURE_* environment variables that override paths and the active profile (C8: never values from yaml).
ENV_OVERRIDES: dict[str, tuple[str, str]] = {
    "TENURE_RAW_DIR": ("paths", "raw"),
    "TENURE_DERIVED_DIR": ("paths", "derived"),
    "TENURE_GOLD_DIR": ("paths", "gold"),
    "TENURE_OUTCOMES_DB": ("paths", "outcomes_db"),
    "TENURE_LEDGER_DB": ("paths", "ledger_db"),
    "TENURE_RUNS_DIR": ("paths", "runs"),
    "TENURE_PROFILE": ("inference", "default_profile"),
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Paths(_Strict):
    raw: str
    derived: str
    gold: str
    outcomes_db: str
    ledger_db: str
    runs: str


class Currency(_Strict):
    brl_per_eur: Decimal  # ASSUMPTION unless replaced by a cited reference rate (D-fx)


class Synthetic(_Strict):
    photo_rate: float
    fraud_rate: float
    prior_claims_for_fraud: int
    double_rate: float


class PolicyFiles(_Strict):
    file: str
    examples: str
    drift_file: str
    drift_date: date


class Profile(_Strict):
    base_url_env: str
    api_key_env: str
    model_env: str
    structured_mode: StructuredMode
    system_mode: Literal["system", "fold"]
    concurrency: int
    rpm: int | None = None  # client-side request-rate limit for shared endpoints (D-rpm)


class Decoding(_Strict):
    temperature: float
    top_p: float
    seed: int
    max_tokens: int


class Inference(_Strict):
    default_profile: str
    profiles: dict[str, Profile]
    decoding: Decoding
    timeout_s: float
    max_retries: int

    @model_validator(mode="after")
    def _default_exists(self) -> Inference:
        if self.default_profile not in self.profiles:
            raise ValueError(f"default_profile {self.default_profile!r} is not one of {sorted(self.profiles)}")
        return self


class Agreement(_Strict):
    tol_abs_cents: int
    tol_rel_bp: int


class Gate(_Strict):
    n_min: int
    delta: float
    min_valid_rate: float
    cost_gate: bool
    monitor_gate: bool
    dwell: int


class GlobalAlarm(_Strict):
    arl0_target: float
    shift: float


class Ladder(_Strict):
    confirm_ticks: int
    tick_k: int
    baseline_n_min: int
    ni_mode: Literal["fixed_baseline", "strict"]
    arl0_target: float
    max_detect_days: float
    cost_eps_abs_cents: float
    cost_rho: float
    bootstrap_b: int
    high_value_cents: int
    gates: dict[Literal["L1", "L2", "L3"], Gate]
    ceilings: dict[str, LevelName]
    audit_rate: dict[Literal["L2", "L3"], float]
    global_alarm: GlobalAlarm


class Cost(_Strict):
    clv_multiple: float
    churn_max: float
    cogs_ratio: float
    salvage_ratio: float
    return_shipping_cents: int
    unesc_cents: int
    missed_cents: int


class Business(_Strict):
    handle_minutes: tuple[float, float, float]
    wage_eur_per_hour: float
    assist_saving: tuple[float, float]
    gpu_eur_per_hour: float
    draws: int


class Server(_Strict):
    host: str
    port: int
    admin_token_env: str


class TenureConfig(_Strict):
    seed: int
    paths: Paths
    currency: Currency
    bands_cents: dict[str, int]
    splits: dict[str, tuple[date, date]]
    synthetic: Synthetic
    policy: PolicyFiles
    inference: Inference
    agreement: Agreement
    ladder: Ladder
    cost: Cost
    business: Business
    server: Server


def load(path: str | Path = "tenure.yaml", env: Mapping[str, str] | None = None) -> TenureConfig:
    """Load tenure.yaml, apply TENURE_* overrides from env (default os.environ), validate strictly."""
    env = os.environ if env is None else env
    data: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    for var, (section, key) in ENV_OVERRIDES.items():
        value = env.get(var)
        if value:
            data.setdefault(section, {})[key] = value
    return TenureConfig.model_validate(data)


def _hashable(v: Any) -> Any:
    """Floats become exact decimal strings so the canonicaliser (which rejects floats) can hash them."""
    if isinstance(v, float):
        return Decimal(repr(v))
    if isinstance(v, dict):
        return {str(k): _hashable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_hashable(x) for x in v]
    return v


def config_sha(cfg: TenureConfig) -> str:
    return sha256_hex(canonical_bytes(_hashable(cfg.model_dump())))


def ingest_config(cfg: TenureConfig) -> IngestConfig:
    bands = tuple(sorted(cfg.bands_cents.items(), key=lambda kv: kv[1]))
    splits = tuple((name, start, end) for name, (start, end) in cfg.splits.items())
    return IngestConfig(brl_per_eur=cfg.currency.brl_per_eur, seed=cfg.seed, photo_rate=cfg.synthetic.photo_rate,
                        fraud_rate=cfg.synthetic.fraud_rate,
                        prior_claims_for_fraud=cfg.synthetic.prior_claims_for_fraud, bands=bands, splits=splits)


def endpoint(cfg: TenureConfig, env: Mapping[str, str] | None = None,
             profile: str | None = None) -> tuple[EndpointProfile, str]:
    """Resolve a profile's endpoint and API key from the environment. Errors name variables, never values."""
    env = os.environ if env is None else env
    name = profile or cfg.inference.default_profile
    if name not in cfg.inference.profiles:
        raise ValueError(f"unknown inference profile {name!r}")
    p = cfg.inference.profiles[name]
    names = (p.base_url_env, p.api_key_env, p.model_env)
    missing = [n for n in names if not env.get(n)]
    if missing:
        raise ValueError(f"profile {name} needs environment variables: {', '.join(missing)}")
    d = cfg.inference.decoding
    prof = EndpointProfile(name=name, base_url=env[p.base_url_env], model=env[p.model_env],
                           structured_mode=p.structured_mode, max_tokens=d.max_tokens, temperature=d.temperature,
                           seed=d.seed, timeout_s=cfg.inference.timeout_s, max_retries=cfg.inference.max_retries)
    return prof, env[p.api_key_env]


def load_dotenv(path: str | Path, env: MutableMapping[str, str] | None = None) -> list[str]:
    """Read KEY=VALUE lines into env (default os.environ) without overriding set keys. Returns keys added."""
    env = os.environ if env is None else env
    p = Path(path)
    if not p.exists():
        return []
    added = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (s.strip() for s in line.split("=", 1))
        if key in env:
            continue
        env[key] = value.strip('"').strip("'")
        added.append(key)
    return added
