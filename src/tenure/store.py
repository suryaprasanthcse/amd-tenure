"""Outcome cache: one row per (run_key, case_id). Makes replays resumable and recordable (master §16.5)."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from .canonical import canonical_bytes, sha256_hex

DDL = """
CREATE TABLE IF NOT EXISTS outcomes (
  run_key           TEXT    NOT NULL,
  case_id           TEXT    NOT NULL,
  text              TEXT,
  error             TEXT,
  prompt_tokens     INTEGER NOT NULL,
  completion_tokens INTEGER NOT NULL,
  cached_tokens     INTEGER,
  latency_ms        INTEGER NOT NULL,
  attempts          INTEGER NOT NULL,
  PRIMARY KEY (run_key, case_id)
) WITHOUT ROWID;
"""


def run_key(model: str, adapter_sha: str | None, prompt_sha: str, schema_sha: str, decoding: dict) -> str:
    """Identity of an inference configuration: equal keys reuse cached completions."""
    return sha256_hex(canonical_bytes({"model": model, "adapter_sha": adapter_sha, "prompt_sha": prompt_sha,
                                       "schema_sha": schema_sha, "decoding": decoding}))[:24]


@dataclass(frozen=True, slots=True)
class StoredOutcome:
    text: str | None
    error: str | None
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int | None
    latency_ms: int
    attempts: int


class OutcomeStore:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(DDL)

    def get(self, key: str, case_id: str) -> StoredOutcome | None:
        row = self.conn.execute("SELECT text, error, prompt_tokens, completion_tokens, cached_tokens, latency_ms, "
                                "attempts FROM outcomes WHERE run_key = ? AND case_id = ?", (key, case_id)).fetchone()
        return None if row is None else StoredOutcome(*row)

    def put(self, key: str, case_id: str, o: StoredOutcome) -> None:
        if o.error is not None:  # transport failures are retried on the next run, never cached
            return
        self.conn.execute("INSERT OR REPLACE INTO outcomes VALUES (?,?,?,?,?,?,?,?,?)",
                          (key, case_id, o.text, o.error, o.prompt_tokens, o.completion_tokens, o.cached_tokens,
                           o.latency_ms, o.attempts))

    def export(self, key: str, path: str | Path) -> int:
        n = 0
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for row in self.conn.execute("SELECT case_id, text, prompt_tokens, completion_tokens, cached_tokens, "
                                         "latency_ms FROM outcomes WHERE run_key = ? ORDER BY case_id", (key,)):
                f.write(json.dumps(dict(zip(("case_id", "text", "prompt_tokens", "completion_tokens",
                                             "cached_tokens", "latency_ms"), row)), ensure_ascii=False) + "\n")
                n += 1
        return n


def as_row(o: StoredOutcome) -> dict:
    return asdict(o)
