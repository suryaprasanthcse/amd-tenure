from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tenure.canonical import canonical_bytes, fx, sha256_hex
from tenure.domain import CaseType

pytestmark = [pytest.mark.unit, pytest.mark.gate_g00]


def test_sorted_keys_and_compact():
    assert canonical_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_utf8_not_escaped():
    assert canonical_bytes({"m": "não chegou é"}) == '{"m":"não chegou é"}'.encode("utf-8")


def test_floats_rejected():
    with pytest.raises(TypeError):
        canonical_bytes({"p": 0.5})
    with pytest.raises(TypeError):
        canonical_bytes([1, [2.0]])


def test_naive_datetime_rejected_and_aware_normalised():
    with pytest.raises(TypeError):
        canonical_bytes({"t": datetime(2026, 10, 15, 12, 0)})
    ist = timezone(timedelta(hours=5, minutes=30))
    out = canonical_bytes({"t": datetime(2026, 10, 15, 17, 30, tzinfo=ist), "d": date(2018, 1, 2)})
    assert out == b'{"d":"2018-01-02","t":"2026-10-15T12:00:00.000Z"}'


def test_decimal_normalised():
    assert canonical_bytes({"r": Decimal("6.00")}) == b'{"r":"6"}'
    assert canonical_bytes({"r": Decimal("0.150")}) == b'{"r":"0.15"}'
    assert canonical_bytes({"r": Decimal("1E+2")}) == b'{"r":"100"}'


def test_nested_lists_dicts_enums_and_none():
    obj = {"z": [{"y": 1, "x": [True, None]}, (3, "s")], "a": {"k": CaseType.DELAY}}
    assert canonical_bytes(obj) == b'{"a":{"k":"DELAY"},"z":[{"x":[true,null],"y":1},[3,"s"]]}'


def test_bad_keys_and_types_rejected():
    with pytest.raises(TypeError):
        canonical_bytes({1: "x"})
    with pytest.raises(TypeError):
        canonical_bytes({"s": {1, 2}})


def test_sha256_and_fx():
    assert sha256_hex("abc") == sha256_hex(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
    assert fx(0.1234567) == "0.123457" and fx(2.5, 1) == "2.5"
