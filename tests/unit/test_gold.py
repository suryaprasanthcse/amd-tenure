"""Gold-set harness (master §15, D-intake): stratified sampling from the gold pool, labelling, translation, eval."""
import json
from collections import Counter

import pandas as pd
import pytest

from tenure.domain import Action, CaseType, Resolution
from tenure.gold import (GOLD_N, LABEL_FIELDS, TranslationCache, gold_quotas, label_session, live_stream,
                         raw_agreement, read_labels, sample_gold)

pytestmark = [pytest.mark.unit, pytest.mark.gate_g02]

SEED = 20261018
TYPES = [t.value for t in CaseType]


def _pool(sizes: dict[str, int]) -> pd.DataFrame:
    rows = []
    for t, n in sizes.items():
        rows += [{"case_id": f"{t.lower()}-{i:04d}", "case_type": t, "split": "live", "message": f"m {t} {i}"}
                 for i in range(n)]
    return pd.DataFrame(rows)


def test_quotas_even_split_when_every_type_is_large():
    q = gold_quotas({t: 200 - i for i, t in enumerate(TYPES)}, 300)
    assert sum(q.values()) == 300 == GOLD_N
    assert all(v >= 21 for v in q.values())  # ⌊300/14⌋ = 21 each
    extra = {t: v - 21 for t, v in q.items() if v > 21}
    assert sum(extra.values()) == 6 and set(extra) <= set(TYPES[:6])  # remainder goes to the largest types


def test_quotas_redistribute_shortfall_by_size():
    sizes = {t: 500 for t in TYPES}
    sizes["WRONG_VARIANT"], sizes["BILLING"], sizes["POSITIVE"] = 4, 0, 900
    q = gold_quotas(sizes, 300)
    assert q["WRONG_VARIANT"] == 4 and q.get("BILLING", 0) == 0
    assert sum(q.values()) == 300 and all(q[t] <= sizes[t] for t in q)
    assert q["POSITIVE"] > q["DELAY"] >= 21  # the shortfall goes preferentially to the larger types
    assert gold_quotas({"DELAY": 10, "OTHER": 5}, 300) == {"DELAY": 10, "OTHER": 5}  # pool smaller than n


def test_gold_sampling():
    pool = _pool({t: 60 + 10 * i for i, t in enumerate(TYPES)})
    g = sample_gold(pool, SEED)
    assert len(g) == 300 and g["case_id"].is_unique and set(g["split"]) == {"gold"}
    assert dict(Counter(g["case_type"])) == {t: n for t, n in gold_quotas(Counter(pool["case_type"]), 300).items()
                                             if n}
    assert sample_gold(pool.iloc[::-1], SEED).equals(g)  # deterministic, input-order independent
    assert not sample_gold(pool, SEED + 1).equals(g)
    assert list(g["case_id"]) == sorted(g["case_id"])
    with pytest.raises(ValueError):
        sample_gold(pool.assign(case_type=""), SEED)  # pool must carry intake labels


def test_gold_cases_are_excluded_from_the_live_stream():
    cases = pd.concat([_pool({"DELAY": 50, "OTHER": 50}),
                       _pool({"POSITIVE": 20}).assign(split="replay")], ignore_index=True)
    gold = sample_gold(cases[cases["split"] == "live"], SEED, n=30)
    live = live_stream(cases, gold)
    assert len(live) == 70 and not set(live["case_id"]) & set(gold["case_id"])
    assert set(live["split"]) == {"live"}


def _case_rows(n: int) -> list[dict]:
    from tests.unit.test_intake import RECORD
    return [RECORD | {"case_id": f"g{i:03d}", "case_type": "DELAY", "message": f"atrasou {i}"} for i in range(n)]


def test_gold_label_cli_roundtrip(tmp_path):
    path = tmp_path / "gold_labels.jsonl"
    shown: list[str] = []
    answers = iter(["REFUND_PARTIAL", "4.20", "late but fine", "deny", "", "7", "", "q"])
    clock = iter(f"2026-10-17T15:30:0{i}.000Z" for i in range(10))
    n = label_session(_case_rows(5), path, "surya", lambda prompt: next(answers), shown.append,
                      translate=lambda c: f"EN: {c['message']}", clock=lambda: next(clock))
    assert n == 3
    rows = read_labels(path)
    assert [r["case_id"] for r in rows] == ["g000", "g001", "g002"]
    assert all(list(r) == list(LABEL_FIELDS) for r in rows)
    assert (rows[0]["action"], rows[0]["amount_cents"], rows[0]["note"]) == ("REFUND_PARTIAL", 420, "late but fine")
    assert (rows[1]["action"], rows[1]["amount_cents"]) == ("DENY", None)
    assert rows[2]["action"] == list(Action)[6].value and rows[0]["labeler"] == "surya"
    assert any("EN: atrasou 0" in s for s in shown) and any("CASE FILE" in s for s in shown)

    answers = iter(["escalate", "", "q"])
    seen_first: list[str] = []
    n2 = label_session(_case_rows(5), path, "surya", lambda prompt: next(answers), seen_first.append,
                       translate=lambda c: "EN", clock=lambda: "2026-10-17T16:00:00.000Z")
    assert n2 == 1 and [r["case_id"] for r in read_labels(path)][-1] == "g003"  # rerun resumes at case 4
    assert any("4/5" in s for s in seen_first)


def test_label_session_rejects_bad_input_and_reasks(tmp_path):
    path = tmp_path / "gold_labels.jsonl"
    answers = iter(["SPAM", "REFUND_PARTIAL", "abc", "-1", "999999", "2.00", "", "q"])
    label_session(_case_rows(1), path, "s", lambda p: next(answers), lambda s: None, translate=lambda c: "EN",
                  clock=lambda: "t")
    assert read_labels(path)[0]["amount_cents"] == 200


def test_translation_cache_calls_once_per_case(tmp_path):
    calls = []

    def fake(text: str) -> str:
        calls.append(text)
        return text.upper()

    cache = TranslationCache(tmp_path / "translations.jsonl", fake)
    rec = {"case_id": "a1", "message": "chegou quebrado"}
    assert cache(rec) == "CHEGOU QUEBRADO" and cache(rec) == "CHEGOU QUEBRADO"
    assert TranslationCache(tmp_path / "translations.jsonl", fake)(rec) == "CHEGOU QUEBRADO"
    assert calls == ["chegou quebrado"]
    lines = (tmp_path / "translations.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["case_id"] for x in lines] == ["a1"]


def test_raw_agreement_overall_and_by_type():
    gold = {"a": Resolution(Action.DENY, None, False, ""), "b": Resolution(Action.REFUND_PARTIAL, 1_000, False, ""),
            "c": Resolution(Action.ESCALATE, None, False, ""), "d": Resolution(Action.NO_ACTION, None, False, "")}
    other = {"a": Resolution(Action.DENY, None, False, "D2"), "b": Resolution(Action.REFUND_PARTIAL, 1_090, False, "P1"),
             "c": Resolution(Action.REQUEST_INFO, None, False, "I2")}
    types = {"a": "DELAY", "b": "DELAY", "c": "OTHER", "d": "POSITIVE"}
    r = raw_agreement(gold, other, types)
    assert r["overall"] == (2, 3)  # d has no comparison decision and is not counted
    assert r["by_type"] == {"DELAY": (2, 2), "OTHER": (0, 1)}


def test_gold_sampling_rejects_duplicate_ids():
    pool = _pool({"DELAY": 5})
    with pytest.raises(ValueError):
        sample_gold(pd.concat([pool, pool]), SEED)
