"""Ledger CLI: verify, export with the governance report and head.json, and gold-label import
(master §19.4, §19.5, §19.1 GOLD_LABEL and EXPORT; RUNBOOK T06)."""
import hashlib
import json

import pytest
from typer.testing import CliRunner

from tenure.cli import app
from tenure.domain import Level
from tenure.ledger import Ledger
from tenure.runs import append_ladder_events
from tests.fixtures.ladder_fx import BAD, HUMAN, cell_case, drive_to, make_ladder
from tests.fixtures.replay_fake import ROOT

pytestmark = [pytest.mark.integration, pytest.mark.gate_g06]


def invoke(env, *args):
    return CliRunner().invoke(app, [*args, "--config", str(ROOT / "tenure.yaml")], env=env)


def entries(led):
    return [(seq, kind, json.loads(p)) for seq, kind, p in
            led.conn.execute("SELECT seq, kind, payload FROM ledger ORDER BY seq")]


@pytest.fixture
def ledger_env(tmp_path):
    """A ledger holding promotions to L2 for two cells, a CUSUM signal with its demotion, and two overrides."""
    path = tmp_path / "ledger.db"
    led = Ledger(path)
    a, b = cell_case(i=0), cell_case(band="V2", i=1)
    lad = make_ladder([a.cell, b.cell])
    events = drive_to(lad, a, Level.L2) + drive_to(lad, b, Level.L2)
    for _ in range(500):
        ev = lad.observe(a, BAD, HUMAN)
        events += ev
        if ev:
            break
    events += lad.override(b.cell, "demote", "alice", "seasonal review", Level.L1)
    events += lad.override(b.cell, "set_ceiling", "bob", "regulator request", Level.L1)
    append_ladder_events(led, events)
    led.close()
    return {"path": path, "env": {"TENURE_LEDGER_DB": str(path), "TENURE_GOLD_DIR": str(tmp_path / "gold")},
            "tmp": tmp_path}


def test_governance_report(ledger_env):
    led = Ledger(ledger_env["path"])
    rows = entries(led)
    head = led.head()
    led.close()
    kinds = [k for _, k, _ in rows]
    assert kinds.count("LEVEL_CHANGE") >= 5 and kinds.count("CUSUM_SIGNAL") == 1 and kinds.count("OVERRIDE") == 2

    ok = invoke(ledger_env["env"], "ledger", "verify")
    assert ok.exit_code == 0, ok.output
    assert "OK" in ok.output and head.hash in ok.output

    out = ledger_env["tmp"] / "exports" / "demo"
    res = invoke(ledger_env["env"], "ledger", "export", "--out", str(out))
    assert res.exit_code == 0, res.output
    assert json.loads((out / "head.json").read_text(encoding="utf-8")) == {"seq": head.seq, "hash": head.hash}
    exported = (out / "ledger.jsonl").read_bytes()
    assert len(exported.decode("utf-8").splitlines()) == head.seq
    report = (out / "governance_report.md").read_text(encoding="utf-8")
    assert f"Verification: OK · {head.seq} entries · head seq {head.seq} · {head.hash}" in report
    lines = report.splitlines()
    for seq, kind, p in rows:
        row = next((line for line in lines if line.startswith(f"| {seq} |")), None)
        assert row is not None, (seq, kind)
        if kind == "LEVEL_CHANGE":
            assert f"{p['from']} → {p['to']}" in row and p["reason"] in row
            for key, value in p["evidence"].items():
                if key != "blockers":
                    assert f"{key}={value}" in row, (key, row)
        elif kind == "CUSUM_SIGNAL":
            assert p["s"] in row and p["h"] in row and str(p["outcomes_monitored"]) in row
        elif kind == "OVERRIDE":
            assert p["actor"] in row and p["reason"] in row and p["op"] in row
    assert "DELAY\\|V1" in report and "| DELAY|V1 |" not in report  # cell ids escape the table pipe

    led = Ledger(ledger_env["path"])
    last_seq, last_kind, export = entries(led)[-1]
    assert last_kind == "EXPORT" and last_seq == head.seq + 1
    assert export == {"path_sha": hashlib.sha256(exported).hexdigest(), "head_seq": head.seq, "head_hash": head.hash}
    assert led.verify().ok

    led.conn.execute("DROP TRIGGER ledger_no_update")
    led.conn.execute("UPDATE ledger SET actor='mallory' WHERE seq=3")
    led.close()
    bad = invoke(ledger_env["env"], "ledger", "verify")
    assert bad.exit_code == 1 and "HASH" in bad.output and "seq 3" in bad.output
    res = invoke(ledger_env["env"], "ledger", "export", "--out", str(ledger_env["tmp"] / "exports" / "bad"))
    assert res.exit_code == 1
    report = (ledger_env["tmp"] / "exports" / "bad" / "governance_report.md").read_text(encoding="utf-8")
    assert "Verification: FAILED at seq 3 (HASH)" in report


def test_gold_import(ledger_env):
    gold = ledger_env["tmp"] / "gold"
    gold.mkdir()
    labels = [{"case_id": "g1", "labeler": "surya", "action": "DENY", "amount_cents": None, "note": "", "utc": "t1"},
              {"case_id": "g2", "labeler": "surya", "action": "REFUND_PARTIAL", "amount_cents": 500,
               "note": "freight only", "utc": "t2"}]
    (gold / "gold_labels.jsonl").write_text("".join(json.dumps(x) + "\n" for x in labels), encoding="utf-8")
    first = invoke(ledger_env["env"], "gold", "import-ledger")
    assert first.exit_code == 0, first.output
    again = invoke(ledger_env["env"], "gold", "import-ledger")
    assert again.exit_code == 0, again.output
    led = Ledger(ledger_env["path"])
    golds = [(p, actor) for (p, actor) in led.conn.execute("SELECT payload, actor FROM ledger WHERE kind='GOLD_LABEL'")]
    assert len(golds) == 2  # idempotent
    payloads = [json.loads(p) for p, _ in golds]
    assert payloads[1] == {"case_id": "g2", "labeler": "surya", "action": "REFUND_PARTIAL", "amount_cents": 500,
                           "note_sha": hashlib.sha256(b"freight only").hexdigest()}
    assert all(actor == "surya" for _, actor in golds) and led.verify().ok
    labels.append({"case_id": "g3", "labeler": "ana", "action": "ESCALATE", "amount_cents": None, "note": "x",
                   "utc": "t3"})
    (gold / "gold_labels.jsonl").write_text("".join(json.dumps(x) + "\n" for x in labels), encoding="utf-8")
    assert invoke(ledger_env["env"], "gold", "import-ledger").exit_code == 0
    assert led.conn.execute("SELECT count(*) FROM ledger WHERE kind='GOLD_LABEL'").fetchone()[0] == 3
