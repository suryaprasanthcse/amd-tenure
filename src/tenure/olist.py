"""Olist ingestion: raw CSVs -> one row per customer contact (master §10)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from .domain import VALUE_BANDS, unit_hash, value_band

FILES: dict[str, str] = {
    "orders": "olist_orders_dataset.csv",
    "items": "olist_order_items_dataset.csv",
    "payments": "olist_order_payments_dataset.csv",
    "reviews": "olist_order_reviews_dataset.csv",
    "customers": "olist_customers_dataset.csv",
    "products": "olist_products_dataset.csv",
    "translation": "product_category_name_translation.csv",
}

REQUIRED: dict[str, tuple[str, ...]] = {
    "orders": ("order_id", "customer_id", "order_status", "order_purchase_timestamp",
               "order_delivered_customer_date", "order_estimated_delivery_date"),
    "items": ("order_id", "order_item_id", "product_id", "price", "freight_value"),
    "payments": ("order_id", "payment_value"),
    "reviews": ("review_id", "order_id", "review_score", "review_comment_title", "review_comment_message",
                "review_creation_date"),
    "customers": ("customer_id", "customer_unique_id"),
    "products": ("product_id", "product_category_name"),
    "translation": ("product_category_name", "product_category_name_english"),
}

LEGAL_RE = re.compile(
    r"\b(procon|reclame\s*aqui|advogad\w*|processar|processo\s+judicial|a[cç][aã]o\s+judicial|juizado|"
    r"pequenas\s+causas)\b", re.IGNORECASE)
PHOTO_RE = re.compile(r"\bfotos?\b|\bimage(m|ns)\b", re.IGNORECASE)


@dataclass(frozen=True)
class IngestConfig:
    brl_per_eur: Decimal  # ASSUMPTION until set from a cited reference rate (master §10.5)
    seed: int = 20261018
    photo_rate: float = 0.60  # SYNTHETIC: share of contacts that carry photo evidence
    fraud_rate: float = 0.005  # SYNTHETIC: random fraud flags on complaint-like contacts
    prior_claims_for_fraud: int = 2
    bands: tuple[tuple[str, int], ...] = VALUE_BANDS
    splits: tuple[tuple[str, date, date], ...] = field(default_factory=lambda: (
        ("train", date(2017, 1, 1), date(2018, 1, 1)),
        ("replay", date(2018, 1, 1), date(2018, 7, 1)),
        ("live", date(2018, 7, 1), date(2018, 9, 1)),
    ))


def load_tables(raw_dir: str | Path) -> dict[str, pd.DataFrame]:
    raw = Path(raw_dir)
    tables: dict[str, pd.DataFrame] = {}
    for key, name in FILES.items():
        path = raw / name
        if not path.exists():
            raise FileNotFoundError(f"missing Olist file {path}")
        df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")
        missing = [c for c in REQUIRED[key] if c not in df.columns]
        if missing:
            raise ValueError(f"{name} lacks columns {missing}")
        tables[key] = df
    return tables


def brl_to_eur_cents(brl: float, brl_per_eur: Decimal) -> int:
    cents_brl = Decimal(str(round(float(brl), 2))) * 100
    return int((cents_brl / brl_per_eur).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.replace("", np.nan), errors="coerce")


def _ts(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s.replace("", np.nan), errors="coerce")


def build_cases(t: dict[str, pd.DataFrame], cfg: IngestConfig) -> pd.DataFrame:
    rv = t["reviews"].copy()
    rv["message"] = (rv["review_comment_title"].str.strip() + " — " + rv["review_comment_message"].str.strip()
                     ).str.strip(" —")
    rv = rv[rv["message"].str.len() > 0]
    rv = rv.drop_duplicates(subset=["review_id", "order_id"], keep="first")
    rv["contact_ts"] = _ts(rv["review_creation_date"])
    rv["review_score"] = _num(rv["review_score"]).astype("Int64")

    od = t["orders"].copy()
    for col in ("order_purchase_timestamp", "order_delivered_customer_date", "order_estimated_delivery_date"):
        od[col] = _ts(od[col])

    it = t["items"].copy()
    it["price"] = _num(it["price"])
    it["freight_value"] = _num(it["freight_value"])
    tr = dict(zip(t["translation"]["product_category_name"], t["translation"]["product_category_name_english"]))
    pr = t["products"][["product_id", "product_category_name"]].copy()
    pr["category"] = pr["product_category_name"].map(tr).fillna(pr["product_category_name"]).replace("", "unknown")
    it = it.merge(pr[["product_id", "category"]], on="product_id", how="left")
    agg = it.groupby("order_id").agg(items_brl=("price", "sum"), freight_brl=("freight_value", "sum"),
                                     n_items=("order_item_id", "count"), n_products=("product_id", "nunique"),
                                     category=("category", lambda s: s.mode().iat[0] if len(s.mode()) else "unknown"))
    pay = t["payments"].copy()
    pay["payment_value"] = _num(pay["payment_value"])
    pay = pay.groupby("order_id").agg(paid_brl=("payment_value", "sum"))

    df = (rv.merge(od, on="order_id", how="inner").merge(agg, on="order_id", how="inner")
            .merge(pay, on="order_id", how="left").merge(t["customers"][["customer_id", "customer_unique_id"]],
                                                         on="customer_id", how="left"))
    df = df[df["contact_ts"].notna() & df["order_estimated_delivery_date"].notna()].copy()
    df["paid_brl"] = df["paid_brl"].fillna(0.0)
    df["total_brl"] = np.where(df["paid_brl"] > 0, df["paid_brl"], df["items_brl"] + df["freight_brl"])

    df["case_id"] = [hashlib.sha256(f"{r}|{o}".encode()).hexdigest()[:16]
                     for r, o in zip(df["review_id"], df["order_id"])]
    df["contact_date"] = df["contact_ts"].dt.date
    delivered = (df["order_status"] == "delivered") & df["order_delivered_customer_date"].notna()
    deliv_d = df["order_delivered_customer_date"].dt.normalize()
    est_d = df["order_estimated_delivery_date"].dt.normalize()
    contact_d = df["contact_ts"].dt.normalize()
    df["days_late"] = pd.array(np.where(delivered, (deliv_d - est_d).dt.days, np.nan), dtype="Int64")
    since = (contact_d - deliv_d).dt.days.clip(lower=0)
    df["days_since_delivery"] = pd.array(np.where(delivered, since, np.nan), dtype="Int64")
    df["days_past_estimate"] = (contact_d - est_d).dt.days.astype("Int64")

    r = cfg.brl_per_eur
    df["items_total_cents"] = [brl_to_eur_cents(v, r) for v in df["items_brl"]]
    df["freight_total_cents"] = [brl_to_eur_cents(v, r) for v in df["freight_brl"]]
    df["order_total_cents"] = [brl_to_eur_cents(v, r) for v in df["total_brl"]]
    df["value_band"] = [value_band(v, cfg.bands) for v in df["order_total_cents"]]

    df = df.sort_values(["contact_ts", "case_id"]).reset_index(drop=True)
    first_purchase = df.groupby("customer_unique_id")["order_purchase_timestamp"].transform("min")
    df["repeat_customer"] = (df["order_purchase_timestamp"] > first_purchase).fillna(False).astype(bool)

    complaint = df["review_score"].fillna(5) <= 2
    prior = np.zeros(len(df), dtype=int)
    for _, idx in df[complaint].groupby("customer_unique_id").groups.items():
        dates = df.loc[idx, "contact_ts"].to_numpy()
        for k, i in enumerate(idx):
            window = dates[:k]
            prior[df.index.get_loc(i)] = int(((dates[k] - window) <= np.timedelta64(90, "D")).sum())
    df["prior_claims_90d"] = prior
    s = str(cfg.seed)
    df["fraud_flag"] = [(p >= cfg.prior_claims_for_fraud) or (sc is not pd.NA and sc <= 3
                                                             and unit_hash(s, cid, "fraud") < cfg.fraud_rate)
                        for p, sc, cid in zip(df["prior_claims_90d"], df["review_score"], df["case_id"])]
    df["photo_evidence"] = [bool(PHOTO_RE.search(m)) or unit_hash(s, cid, "photo") < cfg.photo_rate
                            for m, cid in zip(df["message"], df["case_id"])]
    df["legal_threat"] = [bool(LEGAL_RE.search(m)) for m in df["message"]]

    df["split"] = "excluded"
    for name, start, end in cfg.splits:
        df.loc[(df["contact_date"] >= start) & (df["contact_date"] < end), "split"] = name
    df["case_type"] = ""  # filled by intake (master §11); frozen afterwards

    cols = ["case_id", "review_id", "order_id", "customer_unique_id", "contact_date", "split", "case_type",
            "order_status", "days_late", "days_since_delivery", "days_past_estimate", "items_total_cents",
            "freight_total_cents", "order_total_cents", "value_band", "photo_evidence", "fraud_flag",
            "legal_threat", "repeat_customer", "prior_claims_90d", "review_score", "category", "n_items",
            "n_products", "message"]
    return df[cols]
