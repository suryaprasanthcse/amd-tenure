from datetime import date

import pytest

from tenure.domain import Case, CaseType, cell_id, int_hash, unit_hash, value_band

pytestmark = [pytest.mark.unit, pytest.mark.gate_g00]


@pytest.mark.parametrize("cents,band", [(0, "V1"), (1499, "V1"), (1500, "V2"), (3999, "V2"), (4000, "V3"),
                                        (10000, "V4")])
def test_value_band_edges(cents, band):
    assert value_band(cents) == band


def test_value_band_negative_raises():
    with pytest.raises(ValueError):
        value_band(-1)


def test_hashes_deterministic():
    u = unit_hash("20261018", "abc", "photo")
    assert 0.0 <= u < 1.0
    assert u == unit_hash("20261018", "abc", "photo")
    assert u != unit_hash("abc", "20261018", "photo")  # part order matters
    assert int_hash("a", "b") == int_hash("a", "b") != int_hash("b", "a")
    assert 0 <= int_hash("x") < 2**64
    values = [unit_hash("s", str(i)) for i in range(2_000)]
    assert all(0.0 <= v < 1.0 for v in values) and 0.45 < sum(values) / len(values) < 0.55


def test_case_cell_and_delivered():
    c = Case("c1", date(2018, 1, 2), CaseType.DELAY, "delivered", 3, 1, 4, 1000, 200, 1200, "V1",
             False, False, False, False, "atrasado")
    assert c.cell == "DELAY|V1" == cell_id(CaseType.DELAY, "V1")
    assert c.delivered
    from dataclasses import replace
    assert not replace(c, days_since_delivery=None).delivered
