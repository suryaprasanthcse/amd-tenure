from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from tenure.config import TenureConfig, config_sha, endpoint, ingest_config, load, load_dotenv
from tenure.domain import VALUE_BANDS
from tenure.llm import STRUCTURED_MODES

pytestmark = [pytest.mark.unit, pytest.mark.gate_g00]

ROOT = Path(__file__).resolve().parents[2]
YAML = ROOT / "tenure.yaml"


def _write(tmp_path: Path, mutate) -> Path:
    data = yaml.safe_load(YAML.read_text(encoding="utf-8"))
    mutate(data)
    p = tmp_path / "tenure.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_config_loads_into_model():
    cfg = load(YAML, env={})
    assert isinstance(cfg, TenureConfig)
    assert cfg.seed == 20261018
    assert cfg.ladder.gates["L2"].n_min == 150 and cfg.ladder.gates["L3"].delta == pytest.approx(0.03)
    assert cfg.splits["replay"] == (date(2018, 1, 1), date(2018, 7, 1))
    assert cfg.policy.drift_date == date(2018, 8, 1)
    assert cfg.inference.profiles["token_factory"].structured_mode in STRUCTURED_MODES  # measured by S3 (D-q3)
    assert cfg.business.handle_minutes == (3.0, 6.0, 10.0)
    assert cfg.ladder.ceilings == {"OTHER": "L1", "BILLING": "L1"}


def test_brl_per_eur_is_decimal():
    cfg = load(YAML, env={})
    assert isinstance(cfg.currency.brl_per_eur, Decimal) and cfg.currency.brl_per_eur == Decimal("6.00")


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(unexpected=1),
    lambda d: d["ladder"].update(confirm_tick=2),
    lambda d: d["inference"]["profiles"]["token_factory"].update(api_key="sk-literal"),
    lambda d: d["inference"].update(default_profile="nope"),
    lambda d: d["ladder"]["ceilings"].update(OTHER="L9"),
    lambda d: d["inference"]["profiles"]["token_factory"].update(structured_mode="grammar"),
])
def test_unknown_or_invalid_key_rejected(tmp_path, mutate):
    with pytest.raises(ValidationError):
        load(_write(tmp_path, mutate), env={})


def test_env_overrides_apply():
    cfg = load(YAML, env={"TENURE_LEDGER_DB": "x/ledger.db", "TENURE_RAW_DIR": "elsewhere/raw",
                          "TENURE_PROFILE": "vllm_droplet"})
    assert cfg.paths.ledger_db == "x/ledger.db" and cfg.paths.raw == "elsewhere/raw"
    assert cfg.inference.default_profile == "vllm_droplet"
    assert load(YAML, env={}).paths.ledger_db == "var/ledger.db"


def test_config_sha_stable_and_sensitive(tmp_path):
    a, b = config_sha(load(YAML, env={})), config_sha(load(YAML, env={}))
    assert a == b and len(a) == 64 and int(a, 16) >= 0
    changed = load(_write(tmp_path, lambda d: d["cost"].update(churn_max=0.3)), env={})
    assert config_sha(changed) != a
    assert config_sha(load(YAML, env={"TENURE_LEDGER_DB": "x.db"})) != a


def test_ingest_config_mirrors_yaml():
    ic = ingest_config(load(YAML, env={}))
    assert ic.brl_per_eur == Decimal("6.00") and ic.seed == 20261018
    assert ic.bands == (("V1", 0), ("V2", 1500), ("V3", 4000), ("V4", 7500))  # D-band; R.1 default unchanged
    assert VALUE_BANDS[-1] == ("V4", 10_000)
    assert ic.splits[0] == ("train", date(2017, 1, 1), date(2018, 1, 1))
    assert ic.photo_rate == pytest.approx(0.60) and ic.prior_claims_for_fraud == 2


def test_endpoint_reads_env_names_only():
    cfg = load(YAML, env={})
    env = {"TENURE_LLM_BASE_URL": "http://h/v1", "TENURE_LLM_API_KEY": "k", "TENURE_LLM_MODEL": "gemma-x"}
    prof, key = endpoint(cfg, env=env)
    assert (prof.name, prof.base_url, prof.model, key) == ("token_factory", "http://h/v1", "gemma-x", "k")
    assert prof.structured_mode == cfg.inference.profiles["token_factory"].structured_mode  # yaml passes through
    assert prof.max_tokens == 256 and prof.seed == 0
    assert prof.timeout_s == cfg.inference.timeout_s and prof.max_retries == 6  # yaml passes through (D-rpm2)
    with pytest.raises(ValueError) as e:
        endpoint(cfg, env={"TENURE_LLM_API_KEY": "secret-value"})
    assert "TENURE_LLM_BASE_URL" in str(e.value) and "secret-value" not in str(e.value)
    with pytest.raises(ValueError):
        endpoint(cfg, env=env, profile="missing")


def test_load_dotenv_does_not_override(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# comment\nA=1\nB = 'two'\n\nC=\"3\"\nnot a pair\n", encoding="utf-8")
    env = {"A": "keep"}
    loaded = load_dotenv(p, env)
    assert env == {"A": "keep", "B": "two", "C": "3"} and loaded == ["B", "C"]
    assert load_dotenv(tmp_path / "absent.env", env) == []
