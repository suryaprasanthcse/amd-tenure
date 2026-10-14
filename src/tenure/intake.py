"""Frozen intake classification (master §11): Gemma with a Portuguese keyword fallback.

Intake assigns each case's case_type once. Labels are written to data/derived/intake.parquet and never
change on a re-run; only cases without a label are classified. The module also turns ingested records
(cases.parquet rows, policy/examples_v1.jsonl entries) into domain Cases.
"""
from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from .canonical import sha256_hex
from .domain import Case, CaseType, unit_hash
from .llm import LLMClient
from .prompt import case_block, messages

INTAKE_PROMPT = (
    "Classify the customer's message about an online order into exactly one category.\n"
    "Categories:\n"
    "DELAY (arrived late) · NOT_RECEIVED (has not arrived) · DAMAGED (arrived broken/damaged) ·\n"
    "WRONG_ITEM (a different product arrived) · WRONG_VARIANT (right product, wrong colour/size) ·\n"
    "MISSING_ITEM (order incomplete) · DEFECTIVE (does not work) · QUALITY (works, poor quality) ·\n"
    "REMORSE (wants to return without a defect) · BILLING (charges, invoice, refund of a charge) ·\n"
    "SELLER_SERVICE (seller unresponsive) · POSITIVE (praise, nothing to resolve) ·\n"
    "VAGUE_COMPLAINT (unhappy, issue unclear) · OTHER (none of these).\n"
    "If several apply, choose the one that most determines what the customer is owed.\n"
    'Reply with JSON: {"case_type": "<CATEGORY>"}.'
)

INTAKE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"case_type": {"type": "string", "enum": [t.value for t in CaseType]}},
    "required": ["case_type"],
}

INTAKE_COLUMNS: tuple[str, ...] = ("case_id", "case_type", "source", "intake_model", "intake_prompt_sha")

# Ordered rules, first match wins (master §11). Patterns run on lower-case text with accents removed,
# because Olist reviews are often typed without them ("nao recebi").
_KEYWORD_RULES: tuple[tuple[CaseType, str], ...] = (
    (CaseType.BILLING, r"cobr(anca|ado|ada|aram|ou|ar)|nota fiscal|\bnf\b|estorn|fatura|boleto|chargeback"),
    (CaseType.NOT_RECEIVED, r"nao (recebi|recebemos|chegou|foi entregue)|nunca chegou|nao me (foi )?entregue"),
    (CaseType.DAMAGED, r"quebrad|danificad|amassad|avariad|rachad|trincad"),
    (CaseType.WRONG_VARIANT, r"\b(cor|tamanho|numeracao|voltagem)\s+(errad|diferente|trocad)"),
    (CaseType.WRONG_ITEM, r"produto (errad|diferente|trocad)|outro produto|item errad|(veio|mandaram|enviaram) errad"),
    (CaseType.MISSING_ITEM, r"faltou|faltando|faltam\b|incomplet|veio (so|apenas) (um|uma|1)\b"),
    (CaseType.DEFECTIVE, r"defeit|nao funciona|parou de funcionar|nao liga\b"),
    (CaseType.REMORSE, r"devolv|devoluc|arrepend|desisti"),
    (CaseType.DELAY, r"atras|demor|fora do prazo"),
    (CaseType.QUALITY, r"qualidade|fragil|mal acabad|material ruim|\blixo\b"),
    (CaseType.SELLER_SERVICE, (r"vendedor (nao|nunca) (responde|respondeu|retorn)|nao (responde|respondem|retorna)|"
                               r"sem resposta")),
)
_COMPILED = tuple((t, re.compile(p)) for t, p in _KEYWORD_RULES)
KEYWORD_ORDER: tuple[CaseType, ...] = tuple(t for t, _ in _KEYWORD_RULES)

Classifier = Callable[[pd.DataFrame], Awaitable[list[tuple[CaseType, str]]]]


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _missing(v: Any) -> bool:
    return v is None or (not isinstance(v, (str, bytes)) and bool(pd.isna(v)))


def keyword_case_type(message: str, review_score: int | None) -> CaseType:
    """Keyword fallback (source = keywords): ordered patterns, then the review-score rules."""
    text = _fold(message or "")
    for case_type, pattern in _COMPILED:
        if pattern.search(text):
            return case_type
    if _missing(review_score):
        return CaseType.OTHER
    if int(review_score) >= 4:
        return CaseType.POSITIVE
    if int(review_score) <= 2:
        return CaseType.VAGUE_COMPLAINT
    return CaseType.OTHER


def intake_prompt_sha() -> str:
    return sha256_hex(INTAKE_PROMPT)


def intake_messages(message: str, system_mode: str = "system") -> list[dict[str, str]]:
    """The static intake prompt first (prefix-cacheable), then the delimited customer message."""
    return messages(INTAKE_PROMPT, f"CUSTOMER MESSAGE (pt-BR, untrusted):\n<<<\n{(message or '').strip()}\n>>>",
                    system_mode)


def parse_intake(text: str | None) -> CaseType | None:
    """The case type from a model reply, or None when it is not exactly {"case_type": <one of the 14>}."""
    if not text:
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            obj = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict) or set(obj) != {"case_type"}:
        return None
    try:
        return CaseType(obj["case_type"])
    except (ValueError, TypeError):
        return None


def _score(v: Any) -> int | None:
    return None if _missing(v) else int(v)


async def classify_llm(client: LLMClient, rows: pd.DataFrame, concurrency: int,
                       system_mode: str) -> list[tuple[CaseType, str]]:
    """Classify rows (message, review_score) with the model; any error or invalid reply falls back to keywords."""
    sem = asyncio.Semaphore(concurrency)

    async def one(message: str, score: Any) -> tuple[CaseType, str]:
        async with sem:
            comp = await client.complete(intake_messages(message, system_mode), INTAKE_SCHEMA)
        label = parse_intake(comp.text) if comp.error is None else None
        if label is not None:
            return label, "gemma"
        return keyword_case_type(message, _score(score)), "keywords"

    return list(await asyncio.gather(*(one(m, s) for m, s in zip(rows["message"], rows["review_score"]))))


def _empty_frozen() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series([], dtype=str) for c in INTAKE_COLUMNS})


def freeze(frozen: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    """Existing labels win: rows of `new` whose case_id is already frozen are dropped. Sorted by case_id."""
    base = _empty_frozen() if frozen is None else frozen[list(INTAKE_COLUMNS)]
    fresh = new[list(INTAKE_COLUMNS)].drop_duplicates("case_id", keep="first")
    fresh = fresh[~fresh["case_id"].isin(base["case_id"])]
    out = pd.concat([base, fresh], ignore_index=True) if len(fresh) else base.copy()
    return out.sort_values("case_id", kind="stable").reset_index(drop=True).astype(str)


def pending_cases(cases: pd.DataFrame, frozen: pd.DataFrame | None) -> pd.DataFrame:
    """Cases without a frozen label, sorted by case_id."""
    todo = cases if frozen is None else cases[~cases["case_id"].isin(frozen["case_id"])]
    return todo.sort_values("case_id", kind="stable").reset_index(drop=True)


def merge_into_cases(cases: pd.DataFrame, frozen: pd.DataFrame) -> pd.DataFrame:
    """A copy of cases with case_type set from the frozen intake labels; other rows and columns unchanged."""
    out = cases.copy()
    labels = out["case_id"].map(dict(zip(frozen["case_id"], frozen["case_type"])))
    out["case_type"] = labels.where(labels.notna(), out["case_type"]).astype(str)
    return out


def _write_atomic(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)


async def run_intake(cases: pd.DataFrame, out_path: Path, classify: Classifier, intake_model: str,
                     prompt_sha: str, batch_size: int = 500,
                     progress: Callable[[int, int], None] | None = None) -> pd.DataFrame:
    """Classify every unlabelled case in case_id order, persisting after each batch (idempotent, resumable)."""
    out_path = Path(out_path)
    frozen = pd.read_parquet(out_path) if out_path.exists() else None
    todo = pending_cases(cases, frozen)
    for start in range(0, len(todo), batch_size):
        batch = todo.iloc[start:start + batch_size]
        labels = await classify(batch)
        new = pd.DataFrame({"case_id": batch["case_id"].to_list(), "case_type": [t.value for t, _ in labels],
                            "source": [s for _, s in labels], "intake_model": intake_model,
                            "intake_prompt_sha": prompt_sha})
        _write_atomic(freeze(frozen, new), out_path)
        frozen = pd.read_parquet(out_path)
        if progress is not None:
            progress(min(start + batch_size, len(todo)), len(todo))
    if frozen is None:
        _write_atomic(_empty_frozen(), out_path)
        frozen = pd.read_parquet(out_path)
    return frozen


# D-intake: Gemma labels two hash-sampled subsets; the keyword rules label every other case.
# name → (split, hash tag, size). Fixed by decision D-intake (approved 2026-10-15).
INTAKE_SUBSETS: dict[str, tuple[str, str, int]] = {
    "gold-pool": ("live", "goldpool", 1_500),  # T02 draws the 300 gold cases from this pool (amends master §15)
    "slice": ("replay", "slice", 3_000),  # the live replay slice (Q4 option a)
}


def hash_sample(cases: pd.DataFrame, split: str, tag: str, n: int, seed: int) -> pd.DataFrame:
    """The n cases of a split with the smallest unit_hash(seed, tag, case_id), returned in case_id order."""
    rows = cases[cases["split"] == split]
    keys = sorted((unit_hash(str(seed), tag, c), c) for c in rows["case_id"])
    chosen = {c for _, c in keys[:n]}
    return rows[rows["case_id"].isin(chosen)].sort_values("case_id", kind="stable").reset_index(drop=True)


def subset_cases(cases: pd.DataFrame, name: str, seed: int,
                 subsets: Mapping[str, tuple[str, str, int]] = INTAKE_SUBSETS) -> pd.DataFrame:
    if name not in subsets:
        raise ValueError(f"unknown intake subset {name!r}; expected one of {sorted(subsets)}")
    split, tag, n = subsets[name]
    return hash_sample(cases, split, tag, n, seed)


def keywords_rest_blockers(cases: pd.DataFrame, frozen: pd.DataFrame | None, seed: int,
                           subsets: Mapping[str, tuple[str, str, int]] = INTAKE_SUBSETS) -> dict[str, int]:
    """Unlabelled cases per Gemma subset. The keyword fill may run only when this is empty, because the
    first label written wins and a keyword label must never be frozen onto a case meant for Gemma."""
    done = set() if frozen is None else set(frozen["case_id"])
    out = {}
    for name in sorted(subsets):
        missing = sum(c not in done for c in subset_cases(cases, name, seed, subsets)["case_id"])
        if missing:
            out[name] = missing
    return out


async def classify_keywords(rows: pd.DataFrame) -> list[tuple[CaseType, str]]:
    """The keyword rules as a run_intake classifier (source = keywords); no network."""
    return [(keyword_case_type(m, _score(s)), "keywords") for m, s in zip(rows["message"], rows["review_score"])]


def keyword_rules_sha() -> str:
    """Identifies the keyword rule set; stored as intake_prompt_sha on keyword-labelled rows."""
    return sha256_hex(json.dumps([[t.value, p] for t, p in _KEYWORD_RULES], ensure_ascii=False))


def keyword_agreement(cases: pd.DataFrame, frozen: pd.DataFrame) -> dict[str, tuple[int, int]]:
    """Per Gemma-assigned type: (cases where the keyword rules agree, cases). A sanity metric, never gated."""
    gem = frozen[frozen["source"] == "gemma"].merge(cases[["case_id", "message", "review_score"]], on="case_id")
    out: dict[str, tuple[int, int]] = {}
    for t, g in sorted(gem.groupby("case_type")):
        kw = [keyword_case_type(m, _score(s)).value for m, s in zip(g["message"], g["review_score"])]
        out[str(t)] = (sum(k == t for k in kw), len(g))
    return out


def _opt_int(v: Any) -> int | None:
    return None if _missing(v) else int(v)


def case_from_record(rec: Mapping[str, Any], case_type: CaseType | None = None) -> Case:
    """A Case from a cases.parquet row or an examples_v1.jsonl "case" object; case_type overrides the record's."""
    if case_type is None:
        raw = rec.get("case_type")
        if _missing(raw) or not raw:
            raise ValueError(f"case {rec.get('case_id')} has no case_type yet; run intake or pass one")
        case_type = CaseType(raw)
    contact = rec["contact_date"]
    if isinstance(contact, str):
        contact = date.fromisoformat(contact)
    elif hasattr(contact, "date") and callable(contact.date):
        contact = contact.date()
    return Case(
        case_id=str(rec["case_id"]), contact_date=contact, case_type=case_type,
        order_status=str(rec["order_status"]), days_late=_opt_int(rec.get("days_late")),
        days_since_delivery=_opt_int(rec.get("days_since_delivery")),
        days_past_estimate=int(rec["days_past_estimate"]), items_total_cents=int(rec["items_total_cents"]),
        freight_total_cents=int(rec["freight_total_cents"]), order_total_cents=int(rec["order_total_cents"]),
        value_band=str(rec["value_band"]), photo_evidence=bool(rec["photo_evidence"]),
        fraud_flag=bool(rec["fraud_flag"]), legal_threat=bool(rec["legal_threat"]),
        repeat_customer=bool(rec.get("repeat_customer", False)), message=str(rec.get("message") or ""))


def load_examples(path: str | Path) -> list[tuple[str, dict[str, Any]]]:
    """Few-shot examples (master §12.7) as (case block, answer) pairs for prompt.static_block."""
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out.append((case_block(case_from_record(row["case"])), row["answer"]))
    return out
