"""Case objects as cases.parquet-shaped rows (the columns case_from_record reads, plus split), for T04 tests."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import fields

import pandas as pd

from tenure.domain import Case

CASE_FIELDS = tuple(f.name for f in fields(Case))


def cases_frame(cases: Iterable[Case], split: str) -> pd.DataFrame:
    rows = []
    for c in cases:
        row = {name: getattr(c, name) for name in CASE_FIELDS}
        row["case_type"] = c.case_type.value
        row["contact_date"] = c.contact_date.isoformat()
        row["split"] = split
        rows.append(row)
    return pd.DataFrame(rows, columns=[*CASE_FIELDS, "split"])
