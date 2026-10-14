"""Ingestion on the real Olist files in data/raw (marker data). A missing file fails; it never skips."""
from pathlib import Path

import pandas as pd
import pytest

from tenure.config import ingest_config, load
from tenure.olist import build_cases, load_tables

pytestmark = [pytest.mark.data, pytest.mark.gate_g00]

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = (
    "olist_customers_dataset.csv", "olist_geolocation_dataset.csv", "olist_order_items_dataset.csv",
    "olist_order_payments_dataset.csv", "olist_order_reviews_dataset.csv", "olist_orders_dataset.csv",
    "olist_products_dataset.csv", "olist_sellers_dataset.csv", "product_category_name_translation.csv",
)


@pytest.fixture(scope="module")
def cfg():
    return load(ROOT / "tenure.yaml")


@pytest.fixture(scope="module")
def raw_dir(cfg) -> Path:
    return ROOT / cfg.paths.raw


@pytest.fixture(scope="module")
def cases(cfg, raw_dir) -> pd.DataFrame:
    return build_cases(load_tables(raw_dir), ingest_config(cfg))


def test_real_files_present(raw_dir):
    missing = [f for f in EXPECTED if not (raw_dir / f).is_file()]
    assert not missing, f"missing Olist files in {raw_dir}: {missing} (run scripts/fetch_olist.ps1)"
    tables = load_tables(raw_dir)
    assert all(len(t) > 0 for t in tables.values())


def test_real_cases_shape(cases):
    assert len(cases) >= 20_000
    assert (cases["message"].str.len() > 0).all()
    assert cases["case_id"].is_unique
    for split in ("train", "replay", "live"):
        assert (cases["split"] == split).sum() > 0, split
    delivered = (cases["order_status"] == "delivered") & cases["days_since_delivery"].notna()
    assert (cases["days_late"].isna() == ~delivered).all()
    for col in ("items_total_cents", "freight_total_cents", "order_total_cents"):
        assert (cases[col] >= 0).all(), col


def test_real_band_shares_at_least_5pct(cases):
    shares = cases["value_band"].value_counts(normalize=True).reindex(["V1", "V2", "V3", "V4"], fill_value=0.0)
    low = {b: round(float(s), 4) for b, s in shares.items() if s < 0.05}
    assert not low, f"STOP: raise D-band. Bands below 5%: {low}; all shares: {shares.round(4).to_dict()}"
