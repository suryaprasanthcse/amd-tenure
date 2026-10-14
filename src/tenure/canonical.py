"""Canonical JSON for hashing (master §0.4, §19.3).

Rules: UTF-8, keys sorted, no whitespace, no floats (callers encode probabilities as fixed-point
strings and money as integer cents), datetimes as ISO 8601 UTC with a trailing Z.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


def _norm(v: Any) -> Any:
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return int(v)
    if isinstance(v, float):
        raise TypeError("floats are not allowed in hashed payloads; use fixed-point strings or cents")
    if isinstance(v, Enum):
        return _norm(v.value)
    if isinstance(v, Decimal):
        return format(v.normalize(), "f")
    if isinstance(v, datetime):
        if v.tzinfo is None:
            raise TypeError("naive datetime in hashed payload")
        return v.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if not isinstance(k, str):
                raise TypeError("object keys must be strings")
            out[k] = _norm(x)
        return out
    if isinstance(v, (list, tuple)):
        return [_norm(x) for x in v]
    raise TypeError(f"unsupported type in hashed payload: {type(v).__name__}")


def canonical_bytes(obj: Any) -> bytes:
    return json.dumps(_norm(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def sha256_hex(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def fx(p: float, places: int = 6) -> str:
    """Fixed-point string for a probability or rate in a hashed payload."""
    return f"{p:.{places}f}"
