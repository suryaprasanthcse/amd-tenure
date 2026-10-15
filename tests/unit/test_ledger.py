"""Hash-chained ledger: chain, tamper detection, atomic batches, export and standalone verification
(master §0.5 I8, §19, Appendix A verify reasons, R.10; Appendix T golden test verbatim)."""
import json
import sqlite3

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tenure.canonical import canonical_bytes, sha256_hex
from tenure.governance import read_jsonl, verify_jsonl
from tenure.ledger import GENESIS, KINDS, Head, Ledger, entry_hash

pytestmark = [pytest.mark.unit, pytest.mark.gate_g06]


# --- Appendix T (verbatim) ---------------------------------------------------------------------------------------

def test_ledger_chain_and_tamper(tmp_path):
    t = iter(f"2026-10-15T00:00:{i:02d}.000Z" for i in range(60))
    led = Ledger(tmp_path / "l.db", clock=lambda: next(t))
    led.append("CONFIG", "system", {"x": 1})
    led.append_many([("DECISION", "agent", {"case_id": f"c{i}", "agree": i % 2 == 0}) for i in range(10)])
    rep = led.verify()
    assert rep.ok and rep.entries == 11 and rep.head == led.head()
    with pytest.raises(sqlite3.IntegrityError):
        led.conn.execute("UPDATE ledger SET actor='x' WHERE seq=2")
    with pytest.raises(TypeError):
        led.append("CONFIG", "system", {"p": 0.5})
    led.conn.execute("DROP TRIGGER ledger_no_update")
    led.conn.execute("UPDATE ledger SET payload=? WHERE seq=5", ('{"agree":true,"case_id":"c3"}',))
    bad = led.verify()
    assert not bad.ok and bad.first_bad_seq == 5 and bad.reason == "PAYLOAD_SHA"
    assert canonical_bytes({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode()


# --- RUNBOOK T06 ---------------------------------------------------------------------------------------------------

def clock():
    t = iter(range(100_000))
    return lambda: f"2026-10-15T00:00:00.{next(t) % 1000:03d}Z"


VALUES = st.one_of(st.integers(-10**6, 10**6), st.booleans(), st.none(),
                   st.text(st.characters(min_codepoint=32, max_codepoint=126), max_size=12))
PAYLOADS = st.dictionaries(st.text("abcdefgh", min_size=1, max_size=4), VALUES, min_size=1, max_size=5)


def filled(tmp_path, name, payloads):
    led = Ledger(tmp_path / name, clock=clock())
    led.append_many([("DECISION", "agent", p) for p in payloads])
    return led


@given(payloads=st.lists(PAYLOADS, min_size=1, max_size=8), pick=st.integers(0, 10**6), pos=st.integers(0, 10**6),
       char=st.characters(min_codepoint=33, max_codepoint=126))
def test_any_flipped_payload_byte_fails_at_that_seq(tmp_path_factory, payloads, pick, pos, char):
    led = filled(tmp_path_factory.mktemp("flip"), "l.db", payloads)
    seq = 1 + pick % len(payloads)
    text = led.conn.execute("SELECT payload FROM ledger WHERE seq=?", (seq,)).fetchone()[0]
    i = pos % len(text)
    if text[i] == char:
        char = "!" if char != "!" else "#"
    led.conn.execute("DROP TRIGGER ledger_no_update")
    led.conn.execute("UPDATE ledger SET payload=? WHERE seq=?", (text[:i] + char + text[i + 1:], seq))
    rep = led.verify()
    assert not rep.ok and rep.first_bad_seq == seq and rep.reason == "PAYLOAD_SHA"
    assert rep.entries == seq - 1


@given(payloads=st.lists(PAYLOADS, min_size=1, max_size=8), pick=st.integers(0, 10**6))
def test_rehashed_payload_fails_non_canonical_or_hash(tmp_path_factory, payloads, pick):
    """A forger who also rewrites payload_sha is caught: non-canonical text → NON_CANONICAL; a different
    canonical payload → HASH (the chain hash covers the payload digest)."""
    tmp = tmp_path_factory.mktemp("rehash")
    seq = 1 + pick % len(payloads)
    for variant in ("spaced", "changed"):
        led = filled(tmp, f"{variant}.db", payloads)
        obj = json.loads(led.conn.execute("SELECT payload FROM ledger WHERE seq=?", (seq,)).fetchone()[0])
        if variant == "spaced":
            text = json.dumps(obj, sort_keys=True, indent=1, ensure_ascii=False)
        else:
            text = canonical_bytes({**obj, "zz_forged": 1}).decode("utf-8")
        led.conn.execute("DROP TRIGGER ledger_no_update")
        led.conn.execute("UPDATE ledger SET payload=?, payload_sha=? WHERE seq=?", (text, sha256_hex(text), seq))
        rep = led.verify()
        assert not rep.ok and rep.first_bad_seq == seq
        assert rep.reason == ("NON_CANONICAL" if variant == "spaced" else "HASH")


@given(payloads=st.lists(PAYLOADS, min_size=2, max_size=8), pick=st.integers(0, 10**6))
def test_deleted_row_is_a_gap(tmp_path_factory, payloads, pick):
    led = filled(tmp_path_factory.mktemp("gap"), "l.db", payloads)
    seq = 1 + pick % (len(payloads) - 1)  # any row but the last
    led.conn.execute("DROP TRIGGER ledger_no_delete")
    led.conn.execute("DELETE FROM ledger WHERE seq=?", (seq,))
    rep = led.verify()
    assert not rep.ok and rep.reason == "GAP" and rep.first_bad_seq == seq + 1


def test_deleted_last_row_is_a_head_mismatch(tmp_path):
    led = filled(tmp_path, "l.db", [{"a": i} for i in range(4)])
    led.conn.execute("DROP TRIGGER ledger_no_delete")
    led.conn.execute("DELETE FROM ledger WHERE seq=4")
    rep = led.verify()
    assert not rep.ok and rep.reason == "HEAD_MISMATCH" and rep.head == Head(3, rep.head.hash)


def test_unknown_kind_rejected_and_transaction_atomic(tmp_path):
    led = filled(tmp_path, "l.db", [{"a": 1}])
    before = led.head()
    with pytest.raises(ValueError):
        led.append_many([("DECISION", "agent", {"a": 2}), ("NOT_A_KIND", "agent", {"a": 3}),
                         ("DECISION", "agent", {"a": 4})])
    with pytest.raises(TypeError):  # a float anywhere in the batch: nothing is written either
        led.append_many([("DECISION", "agent", {"a": 5}), ("DECISION", "agent", {"p": 0.5})])
    assert led.head() == before
    assert led.conn.execute("SELECT count(*) FROM ledger").fetchone()[0] == 1
    assert led.verify().ok
    assert "NOT_A_KIND" not in KINDS and {"DECISION", "LEVEL_CHANGE", "GOLD_LABEL", "EXPORT"} <= KINDS


def test_hash_chain_formula():
    """§19.3: H_n = SHA-256(tag ‖ u64be(n) ‖ H_{n-1} ‖ lp(ts) ‖ lp(kind) ‖ lp(actor) ‖ SHA-256(payload))."""
    import hashlib
    psha = hashlib.sha256(b'{"a":1}').digest()
    lp = lambda s: len(s.encode()).to_bytes(4, "big") + s.encode()  # noqa: E731
    want = hashlib.sha256(b"TENURE-LEDGER-v1\x00" + (1).to_bytes(8, "big") + GENESIS + lp("t") + lp("CONFIG")
                          + lp("system") + psha).digest()
    assert entry_hash(GENESIS, 1, "t", "CONFIG", "system", psha) == want


def test_export_roundtrip(tmp_path):
    led = filled(tmp_path, "l.db", [{"case_id": f"c{i}", "n": i} for i in range(25)])
    led.append("LEVEL_CHANGE", "ladder", {"cell": "DELAY|V1", "from": "L0", "to": "L1", "reason": "PROMOTE",
                                          "evidence": {"lower": "0.912000", "n": 30}})
    head = led.export_jsonl(tmp_path / "ledger.jsonl")
    lines = (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
    rep = verify_jsonl(lines)
    assert rep.ok and rep.head == head == led.head() and rep.entries == 26
    assert [e["seq"] for e in read_jsonl(tmp_path / "ledger.jsonl")] == list(range(1, 27))

    rec = json.loads(lines[9])
    rec["payload"]["n"] = 99  # an edited export is caught without the database
    tampered = lines[:9] + [json.dumps(rec, ensure_ascii=False)] + lines[10:]
    bad = verify_jsonl(tampered)
    assert not bad.ok and bad.first_bad_seq == 10 and bad.reason == "PAYLOAD_SHA"
    assert verify_jsonl(lines[:5] + lines[6:]).reason == "GAP"
    assert verify_jsonl(lines, expected_head=Head(26, "0" * 64)).reason == "HEAD_MISMATCH"
    assert verify_jsonl([]).ok and verify_jsonl([]).head == Head(0, GENESIS.hex())
