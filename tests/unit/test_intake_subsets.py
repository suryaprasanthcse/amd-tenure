"""D-intake: Gemma on two hash-sampled subsets (replay slice, gold pool), keywords for every other case."""
import asyncio

import pandas as pd
import pytest

from tenure.domain import CaseType, unit_hash
from tenure.intake import (INTAKE_SUBSETS, classify_keywords, freeze, hash_sample, keyword_case_type,
                           keyword_rules_sha, keywords_rest_blockers, run_intake, subset_cases)

pytestmark = [pytest.mark.unit, pytest.mark.gate_g01]

SEED = 20261018


def _cases(n_replay: int = 40, n_live: int = 30) -> pd.DataFrame:
    ids = [f"r{i:04d}" for i in range(n_replay)] + [f"l{i:04d}" for i in range(n_live)] + ["t0000", "t0001"]
    split = ["replay"] * n_replay + ["live"] * n_live + ["train", "train"]
    msgs = ["chegou quebrado", "atrasou", "ótimo", "não gostei"]
    return pd.DataFrame({"case_id": ids, "split": split, "message": [msgs[i % 4] for i in range(len(ids))],
                         "review_score": pd.array([(i % 5) + 1 for i in range(len(ids))], dtype="Int64")})


def test_subset_definitions_match_d_intake():
    assert INTAKE_SUBSETS == {"gold-pool": ("live", "goldpool", 1_500), "slice": ("replay", "slice", 3_000)}


def test_hash_sample_takes_the_smallest_hashes_of_one_split():
    cases = _cases()
    got = hash_sample(cases, "replay", "slice", 10, SEED)
    replay = cases[cases["split"] == "replay"]
    want = sorted(replay["case_id"], key=lambda c: (unit_hash(str(SEED), "slice", c), c))[:10]
    assert list(got["case_id"]) == sorted(want)  # returned in case_id order
    assert set(got["split"]) == {"replay"}
    assert list(hash_sample(cases.iloc[::-1], "replay", "slice", 10, SEED)["case_id"]) == list(got["case_id"])
    assert list(hash_sample(cases, "replay", "other-tag", 10, SEED)["case_id"]) != list(got["case_id"])
    assert len(hash_sample(cases, "live", "goldpool", 500, SEED)) == 30  # n larger than the split → whole split


def test_subset_cases_uses_the_named_definition():
    cases = _cases()
    small = {"gold-pool": ("live", "goldpool", 5), "slice": ("replay", "slice", 7)}
    g = subset_cases(cases, "gold-pool", SEED, small)
    assert list(g["case_id"]) == list(hash_sample(cases, "live", "goldpool", 5, SEED)["case_id"])
    assert len(subset_cases(cases, "slice", SEED, small)) == 7
    with pytest.raises(ValueError):
        subset_cases(cases, "everything", SEED, small)


def test_classify_keywords_labels_every_row_locally():
    cases = _cases(4, 0)
    out = asyncio.run(classify_keywords(cases))
    assert out == [(keyword_case_type(m, int(s)), "keywords") for m, s in zip(cases["message"], cases["review_score"])]
    assert out[0] == (CaseType.DAMAGED, "keywords")
    sha = keyword_rules_sha()
    assert len(sha) == 64 and sha == keyword_rules_sha()


def test_keywords_rest_is_blocked_until_both_subsets_are_labelled(tmp_path):
    cases = _cases()
    small = {"gold-pool": ("live", "goldpool", 5), "slice": ("replay", "slice", 7)}
    assert keywords_rest_blockers(cases, None, SEED, small) == {"gold-pool": 5, "slice": 7}

    def labelled(df: pd.DataFrame, source: str = "gemma") -> pd.DataFrame:
        return pd.DataFrame({"case_id": df["case_id"], "case_type": "DELAY", "source": source,
                             "intake_model": "g", "intake_prompt_sha": "s"})

    gold = subset_cases(cases, "gold-pool", SEED, small)
    frozen = freeze(None, labelled(gold))
    assert keywords_rest_blockers(cases, frozen, SEED, small) == {"slice": 7}
    sl = subset_cases(cases, "slice", SEED, small)
    frozen = freeze(frozen, labelled(sl.iloc[:6]))
    assert keywords_rest_blockers(cases, frozen, SEED, small) == {"slice": 1}
    frozen = freeze(frozen, labelled(sl.iloc[6:], source="keywords"))  # a Gemma fallback still counts as labelled
    assert keywords_rest_blockers(cases, frozen, SEED, small) == {}


def test_keyword_fill_never_overwrites_gemma_labels(tmp_path):
    out = tmp_path / "intake.parquet"
    cases = _cases()
    small = {"gold-pool": ("live", "goldpool", 5), "slice": ("replay", "slice", 7)}
    subset = pd.concat([subset_cases(cases, "gold-pool", SEED, small), subset_cases(cases, "slice", SEED, small)])

    async def gemma(df: pd.DataFrame) -> list[tuple[CaseType, str]]:
        return [(CaseType.OTHER, "gemma")] * len(df)

    asyncio.run(run_intake(subset, out, gemma, "Gemma-x", "p-sha"))
    done = asyncio.run(run_intake(cases, out, classify_keywords, "keywords", keyword_rules_sha()))
    assert len(done) == len(cases)
    g = done[done["case_id"].isin(subset["case_id"])]
    assert set(g["source"]) == {"gemma"} and set(g["case_type"]) == {"OTHER"} and set(g["intake_model"]) == {"Gemma-x"}
    k = done[~done["case_id"].isin(subset["case_id"])]
    assert set(k["source"]) == {"keywords"} and set(k["intake_prompt_sha"]) == {keyword_rules_sha()}
