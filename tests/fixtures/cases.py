"""Random case builder shared by the unit tests (verbatim `make_case` from master Appendix T)."""
from __future__ import annotations

import random
from datetime import date, timedelta

from tenure.domain import Case, CaseType, value_band


def make_case(rng: random.Random, i: int, case_type: CaseType | None = None) -> Case:
    t = case_type or rng.choice(list(CaseType))
    delivered = rng.random() < 0.9
    items = rng.randint(500, 60_000)
    freight = rng.choice([0, rng.randint(200, 4_000)])
    total = items + freight
    return Case(
        case_id=f"c{i:06d}", contact_date=date(2018, 1, 1) + timedelta(days=rng.randint(0, 180)), case_type=t,
        order_status="delivered" if delivered else rng.choice(["shipped", "processing", "canceled"]),
        days_late=rng.randint(-15, 25) if delivered else None,
        days_since_delivery=rng.randint(0, 60) if delivered else None,
        days_past_estimate=rng.randint(-10, 30), items_total_cents=items, freight_total_cents=freight,
        order_total_cents=total, value_band=value_band(total), photo_evidence=rng.random() < 0.6,
        fraud_flag=rng.random() < 0.01, legal_threat=rng.random() < 0.01, repeat_customer=rng.random() < 0.05,
        message="produto chegou com atraso")
