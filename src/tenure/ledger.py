"""Hash-chained, append-only governance ledger on SQLite (master §19)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes

DOMAIN = b"TENURE-LEDGER-v1\x00"
GENESIS = bytes(32)
KINDS = frozenset({
    "CONFIG", "POLICY_VERSION", "MODEL_PROFILE", "ADAPTER_REGISTERED", "RUN_STARTED", "RUN_FINISHED",
    "DECISION", "LEVEL_CHANGE", "CUSUM_SIGNAL", "OVERRIDE", "CASE_OVERRIDE", "GOLD_LABEL", "DRIFT_INJECTED",
    "GLOBAL_ALARM", "EXPORT",
})

DDL = """
CREATE TABLE IF NOT EXISTS ledger (
  seq         INTEGER PRIMARY KEY CHECK (seq >= 1),
  ts_utc      TEXT    NOT NULL,
  kind        TEXT    NOT NULL,
  actor       TEXT    NOT NULL,
  payload     TEXT    NOT NULL,
  payload_sha TEXT    NOT NULL CHECK (length(payload_sha) = 64),
  prev_hash   TEXT    NOT NULL CHECK (length(prev_hash) = 64),
  hash        TEXT    NOT NULL UNIQUE CHECK (length(hash) = 64)
);
CREATE TABLE IF NOT EXISTS ledger_head (
  id   INTEGER PRIMARY KEY CHECK (id = 1),
  seq  INTEGER NOT NULL,
  hash TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_kind ON ledger(kind);
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _lp(s: str) -> bytes:
    b = s.encode("utf-8")
    return len(b).to_bytes(4, "big") + b


def entry_hash(prev: bytes, seq: int, ts: str, kind: str, actor: str, payload_sha: bytes) -> bytes:
    """H_n = SHA-256(DOMAIN || u64(n) || H_{n-1} || lp(ts) || lp(kind) || lp(actor) || SHA-256(payload))."""
    return hashlib.sha256(DOMAIN + seq.to_bytes(8, "big") + prev + _lp(ts) + _lp(kind) + _lp(actor)
                          + payload_sha).digest()


@dataclass(frozen=True, slots=True)
class Head:
    seq: int
    hash: str


@dataclass(frozen=True, slots=True)
class VerifyReport:
    ok: bool
    entries: int
    head: Head
    first_bad_seq: int | None
    reason: str | None


class Ledger:
    """Single-writer ledger. Every write runs in one IMMEDIATE transaction."""

    def __init__(self, path: str | Path, clock: Callable[[], str] = utc_now):
        self.conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(DDL)
        self.clock = clock

    def close(self) -> None:
        self.conn.close()

    def head(self) -> Head:
        row = self.conn.execute("SELECT seq, hash FROM ledger_head WHERE id = 1").fetchone()
        return Head(0, GENESIS.hex()) if row is None else Head(int(row[0]), str(row[1]))

    def append(self, kind: str, actor: str, payload: dict[str, Any]) -> Head:
        return self.append_many([(kind, actor, payload)])

    def append_many(self, entries: Iterable[tuple[str, str, dict[str, Any]]]) -> Head:
        cur = self.conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            row = cur.execute("SELECT seq, hash FROM ledger_head WHERE id = 1").fetchone()
            seq, prev = (0, GENESIS) if row is None else (int(row[0]), bytes.fromhex(row[1]))
            for kind, actor, payload in entries:
                if kind not in KINDS:
                    raise ValueError(f"unknown ledger kind {kind}")
                body = canonical_bytes(payload)
                psha = hashlib.sha256(body).digest()
                seq += 1
                ts = self.clock()
                h = entry_hash(prev, seq, ts, kind, actor, psha)
                cur.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?,?)",
                            (seq, ts, kind, actor, body.decode("utf-8"), psha.hex(), prev.hex(), h.hex()))
                prev = h
            cur.execute("INSERT INTO ledger_head(id, seq, hash) VALUES (1, ?, ?) "
                        "ON CONFLICT(id) DO UPDATE SET seq = excluded.seq, hash = excluded.hash",
                        (seq, prev.hex()))
            cur.execute("COMMIT")
        except BaseException:
            cur.execute("ROLLBACK")
            raise
        return Head(seq, prev.hex())

    def verify(self) -> VerifyReport:
        prev, expected = GENESIS, 1
        rows = self.conn.execute(
            "SELECT seq, ts_utc, kind, actor, payload, payload_sha, prev_hash, hash FROM ledger ORDER BY seq")
        for seq, ts, kind, actor, payload, psha, prev_hex, h_hex in rows:
            def bad(reason: str) -> VerifyReport:
                return VerifyReport(False, expected - 1, Head(expected - 1, prev.hex()), seq, reason)
            if seq != expected:
                return bad("GAP")
            if prev_hex != prev.hex():
                return bad("PREV_MISMATCH")
            body = payload.encode("utf-8")
            if hashlib.sha256(body).hexdigest() != psha:
                return bad("PAYLOAD_SHA")
            if canonical_bytes(json.loads(payload)) != body:
                return bad("NON_CANONICAL")
            h = entry_hash(prev, seq, ts, kind, actor, bytes.fromhex(psha))
            if h.hex() != h_hex:
                return bad("HASH")
            prev, expected = h, expected + 1
        head = self.head()
        computed = Head(expected - 1, prev.hex())
        if head != computed:
            return VerifyReport(False, expected - 1, computed, head.seq, "HEAD_MISMATCH")
        return VerifyReport(True, expected - 1, computed, None, None)

    def export_jsonl(self, path: str | Path) -> Head:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            for seq, ts, kind, actor, payload, psha, prev_hex, h_hex in self.conn.execute(
                    "SELECT seq, ts_utc, kind, actor, payload, payload_sha, prev_hash, hash FROM ledger ORDER BY seq"):
                f.write(json.dumps({"seq": seq, "ts_utc": ts, "kind": kind, "actor": actor,
                                    "payload": json.loads(payload), "payload_sha": psha,
                                    "prev_hash": prev_hex, "hash": h_hex}, ensure_ascii=False) + "\n")
        return self.head()
