"""Gold-set harness (master §15, decision D-intake): sampling, labelling, translation cache, raw agreement.

The 300 gold cases are drawn from the D-intake gold pool (1,500 Gemma-labelled live cases), stratified by
case_type. A person labels them with `tenure gold label`; `tenure gold eval` reports raw agreement until
T04 adds Wilson-cc intervals.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd

from .domain import Action, CaseType, Resolution, unit_hash
from .intake import case_from_record
from .prompt import case_block
from .schema import AgentDecision, agrees, eur_to_cents

GOLD_N = 300
LABEL_FIELDS: tuple[str, ...] = ("case_id", "labeler", "action", "amount_cents", "note", "utc")
_ACTIONS = list(Action)


def gold_quotas(sizes: Mapping[str, int], n: int = GOLD_N) -> dict[str, int]:
    """Per-type quotas: ⌊n/14⌋ each (capped by availability); the remainder and any shortfall go to types
    with spare cases in proportion to their size (largest remainder, ties by size then name)."""
    sizes = {t: int(s) for t, s in sizes.items()}
    quota = {t: min(n // len(CaseType), s) for t, s in sizes.items()}
    left = n - sum(quota.values())
    while left > 0:
        open_types = [t for t in sizes if quota[t] < sizes[t]]
        if not open_types:
            break
        total = sum(sizes[t] for t in open_types)
        shares = {t: left * sizes[t] / total for t in open_types}
        add = {t: min(int(shares[t]), sizes[t] - quota[t]) for t in open_types}
        rest = left - sum(add.values())
        order = sorted(open_types, key=lambda t: (-(shares[t] - int(shares[t])), -sizes[t], t))
        for t in order:
            if rest == 0:
                break
            if quota[t] + add[t] < sizes[t]:
                add[t] += 1
                rest -= 1
        if not any(add.values()):
            break
        for t in open_types:
            quota[t] += add[t]
        left = n - sum(quota.values())
    return quota


def sample_gold(pool: pd.DataFrame, seed: int, n: int = GOLD_N) -> pd.DataFrame:
    """Stratified gold sample from a typed pool; within a type the smallest unit_hash(seed, "gold", case_id)."""
    if (pool["case_type"] == "").any() or pool["case_type"].isna().any():
        raise ValueError("the gold pool must carry intake case types (run intake first)")
    if not pool["case_id"].is_unique:
        raise ValueError("the gold pool has duplicate case_ids")
    quota = gold_quotas(pool["case_type"].value_counts().to_dict(), n)
    chosen: list[str] = []
    for t in sorted(quota):
        ids = sorted(pool.loc[pool["case_type"] == t, "case_id"], key=lambda c: (unit_hash(str(seed), "gold", c), c))
        chosen += ids[:quota[t]]
    out = pool[pool["case_id"].isin(chosen)].assign(split="gold")
    return out.sort_values("case_id", kind="stable").reset_index(drop=True)


def live_stream(cases: pd.DataFrame, gold: pd.DataFrame) -> pd.DataFrame:
    """The live split without the gold cases (master §15: gold cases leave the live stream)."""
    live = cases[(cases["split"] == "live") & ~cases["case_id"].isin(gold["case_id"])]
    return live.reset_index(drop=True)


def read_labels(path: Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


class TranslationCache:
    """English translations of customer messages, produced once per case and kept in a jsonl file."""

    def __init__(self, path: Path, translate: Callable[[str], str]):
        self.path = Path(path)
        self._translate = translate
        self._cache = {r["case_id"]: r["text_en"] for r in read_labels(self.path)}

    def __call__(self, rec: Mapping[str, Any]) -> str:
        cid = str(rec["case_id"])
        if cid not in self._cache:
            text = self._translate(str(rec.get("message") or ""))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps({"case_id": cid, "text_en": text}, ensure_ascii=False) + "\n")
            self._cache[cid] = text
        return self._cache[cid]


def _ask_action(ask: Callable[[str], str], show: Callable[[str], None]) -> Action | None:
    menu = "  ".join(f"{i}={a.value}" for i, a in enumerate(_ACTIONS, 1))
    while True:
        raw = ask(f"action [{menu}] or q: ").strip()
        if raw.lower() == "q":
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(_ACTIONS):
            return _ACTIONS[int(raw) - 1]
        try:
            return Action(raw.upper())
        except ValueError:
            show(f"not an action: {raw!r}")


def _ask_amount(ask: Callable[[str], str], show: Callable[[str], None], total_cents: int) -> int:
    while True:
        raw = ask(f"amount in EUR (0.01 – {total_cents // 100}.{total_cents % 100:02d}): ").strip()
        try:
            cents = eur_to_cents(float(Decimal(raw)))
        except (InvalidOperation, ValueError):
            show(f"not an amount: {raw!r}")
            continue
        if 1 <= cents <= total_cents:
            return cents
        show("the amount must be at least 0.01 and at most the order total")


def label_session(rows: Iterable[Mapping[str, Any]], path: Path, labeler: str, ask: Callable[[str], str],
                  show: Callable[[str], None], translate: Callable[[Mapping[str, Any]], str],
                  clock: Callable[[], str]) -> int:
    """Label gold cases in order, appending one jsonl line per case; resumes after the last labelled case.
    Returns how many cases were labelled in this session; 'q' stops early."""
    rows = list(rows)
    done = {r["case_id"] for r in read_labels(path)}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    count = 0
    for pos, rec in enumerate(rows, 1):
        if rec["case_id"] in done:
            continue
        c = case_from_record(rec)
        show(f"--- gold case {pos}/{len(rows)} ---")
        show(case_block(c))
        show(f"EN: {translate(rec)}")
        action = _ask_action(ask, show)
        if action is None:
            break
        amount = _ask_amount(ask, show, c.order_total_cents) if action is Action.REFUND_PARTIAL else None
        note = ask("note (optional): ").strip()
        line = dict(zip(LABEL_FIELDS, (c.case_id, labeler, action.value, amount, note, clock())))
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        count += 1
    return count


def raw_agreement(gold: Mapping[str, Resolution], other: Mapping[str, Resolution],
                  types: Mapping[str, str]) -> dict[str, Any]:
    """Agreement (master §2.2) of `other` decisions with the gold labels, overall and per case type."""
    by_type: dict[str, list[int]] = {}
    for cid in sorted(gold):
        if cid not in other:
            continue
        o = other[cid]
        hit = agrees(AgentDecision(o.action, o.amount_cents, o.requires_return, o.clause, 0, ""), gold[cid])
        cell = by_type.setdefault(types[cid], [0, 0])
        cell[0] += hit
        cell[1] += 1
    total = (sum(v[0] for v in by_type.values()), sum(v[1] for v in by_type.values()))
    return {"overall": total, "by_type": {t: (v[0], v[1]) for t, v in sorted(by_type.items())}}
