"""Ledger export, standalone verification and the governance report (master §19.4, §19.5).

verify_jsonl() checks an exported ledger.jsonl with no database: the same gapless sequence, prev_hash, payload
digest and chain hash as Ledger.verify (R.10), so anyone holding the export and the anchored head can re-verify.
governance_report() renders the ledger as markdown: the verification line, the run manifests, and per cell the
level timeline with every number of its evidence, the CUSUM signals, the overrides and the final blockers.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes
from .ledger import GENESIS, Head, Ledger, VerifyReport, entry_hash
from .stats import wilson_cc_interval


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def verify_jsonl(lines: Iterable[str], expected_head: Head | None = None) -> VerifyReport:
    """Verify exported ledger lines from genesis; `expected_head` is the anchored head (head.json), if any."""
    prev, expected = GENESIS, 1
    for line in lines:
        if not line.strip():
            continue
        e = json.loads(line)
        seq = int(e["seq"])

        reason = None
        if seq != expected:
            reason = "GAP"
        elif e["prev_hash"] != prev.hex():
            reason = "PREV_MISMATCH"
        elif hashlib.sha256(canonical_bytes(e["payload"])).hexdigest() != e["payload_sha"]:
            reason = "PAYLOAD_SHA"
        else:
            h = entry_hash(prev, seq, e["ts_utc"], e["kind"], e["actor"], bytes.fromhex(e["payload_sha"]))
            if h.hex() != e["hash"]:
                reason = "HASH"
        if reason is not None:
            return VerifyReport(False, expected - 1, Head(expected - 1, prev.hex()), seq, reason)
        prev, expected = h, expected + 1
    computed = Head(expected - 1, prev.hex())
    if expected_head is not None and expected_head != computed:
        return VerifyReport(False, expected - 1, computed, expected_head.seq, "HEAD_MISMATCH")
    return VerifyReport(True, expected - 1, computed, None, None)


def verification_line(rep: VerifyReport) -> str:
    if rep.ok:
        return f"Verification: OK · {rep.entries} entries · head seq {rep.head.seq} · {rep.head.hash}"
    return (f"Verification: FAILED at seq {rep.first_bad_seq} ({rep.reason}) · {rep.entries} entries verified "
            f"before it")


def _md(v: Any) -> str:
    """A table-cell value: pipes escaped (cell ids are `TYPE|BAND`), newlines flattened."""
    return str(v).replace("|", "\\|").replace("\n", " ")


def _evidence(ev: Mapping[str, Any]) -> str:
    parts = [f"{k}={_md(v)}" for k, v in ev.items() if k != "blockers"]
    if ev.get("blockers"):
        parts.append(f"blockers={_md(','.join(ev['blockers']))}")
    return " · ".join(parts) or "–"


def _num(v: Any) -> str:
    return "–" if v is None else _md(v)


def _bracket(ev: Mapping[str, Any]) -> str:
    """Two-sided Wilson-cc 95% bracket of the agent's agreement, recomputed from x and n (the ledger stores only
    the lower bound, which is checked against the recomputation)."""
    if "x" not in ev or "n" not in ev or int(ev["n"]) <= 0:
        return "–"
    lo, hi = wilson_cc_interval(int(ev["x"]), int(ev["n"]))
    if "lower" in ev and abs(float(ev["lower"]) - lo) > 1e-6:
        return f"[{ev['lower']}, {hi:.6f}] (stored lower differs from recomputed {lo:.6f})"
    return f"[{lo:.6f}, {hi:.6f}]"


def _run_of(entries: Sequence[Mapping[str, Any]]) -> dict[int, str]:
    """Entry seq -> its run: the latest RUN_STARTED at a lower seq (level changes and signals carry no run id)."""
    out: dict[int, str] = {}
    current = "–"
    for e in entries:
        if e["kind"] == "RUN_STARTED":
            current = str(e["payload"].get("run_id", "–"))
        out[int(e["seq"])] = current
    return out


def governance_report(entries: Sequence[Mapping[str, Any]], rep: VerifyReport,
                      final: Mapping[str, Mapping[str, Any]] | None = None, title: str = "Governance report",
                      final_label: str = "", manifests: Mapping[str, Mapping[str, Any]] | None = None) -> str:
    """Markdown over exported ledger entries (dicts with seq, ts_utc, kind, actor, payload). `final` holds a
    rebuilt ladder's cell summaries; `final_label` names the run they describe (the timelines cover every run).
    `manifests` maps run_id to its manifest.json, the source of the agent label when the entry carries none."""
    manifests = manifests or {}
    run_of = _run_of(entries)
    lines = [f"# {title}", "", verification_line(rep), ""]
    runs = [e for e in entries if e["kind"] in ("RUN_STARTED", "RUN_FINISHED")]
    lines += ["## Runs", "", "| seq | kind | run_id | mode | agent | model | run_key | cases / committed |",
              "|---|---|---|---|---|---|---|---|"]
    for e in runs:
        p = e["payload"]
        size = p.get("committed", p.get("cases", "–"))
        agent = p.get("agent") or manifests.get(str(p.get("run_id")), {}).get("agent") or p.get("model") or "–"
        lines.append(f"| {e['seq']} | {e['kind']} | {_md(p.get('run_id', '–'))} | {_md(p.get('mode', '–'))} | "
                     f"{_md(agent)} | {_md(p.get('model', '–'))} | {_md(p.get('run_key', '–'))} | {_md(size)} |")
    if not runs:
        lines.append("| – | – | – | – | – | – | – | – |")
    other = [e for e in entries if e["kind"] in ("CONFIG", "POLICY_VERSION", "MODEL_PROFILE", "ADAPTER_REGISTERED",
                                                 "DRIFT_INJECTED", "GLOBAL_ALARM", "CASE_OVERRIDE", "EXPORT")]
    if other:
        lines += ["", "## System entries", "", "| seq | kind | actor | detail |", "|---|---|---|---|"]
        for e in other:
            p = e["payload"]
            detail = {k: v for k, v in p.items() if k != "config"}
            lines.append(f"| {e['seq']} | {e['kind']} | {_md(e['actor'])} | "
                         f"{_md(json.dumps(detail, sort_keys=True, ensure_ascii=False))} |")
    golds = [e for e in entries if e["kind"] == "GOLD_LABEL"]
    decisions = sum(e["kind"] == "DECISION" for e in entries)
    lines += ["", f"DECISION entries: {decisions} · GOLD_LABEL entries: {len(golds)}"]

    by_cell: dict[str, list[Mapping[str, Any]]] = {}
    for e in entries:
        if e["kind"] in ("LEVEL_CHANGE", "CUSUM_SIGNAL", "OVERRIDE"):
            by_cell.setdefault(str(e["payload"]["cell"]), []).append(e)
    cells = sorted(set(by_cell) | set(final or {}))
    lines += ["", "## Cells", ""]
    if not cells:
        lines.append("No level changes, CUSUM signals or overrides.")
    for cell in cells:
        lines += [f"### {_md(cell)}", ""]
        rows = by_cell.get(cell, [])
        changes = [e for e in rows if e["kind"] == "LEVEL_CHANGE"]
        if changes:
            lines += ["Level timeline (bracket = two-sided Wilson-cc 95% interval of agreement; threshold = human "
                      "baseline − δ):", "",
                      "| seq | run_id | change | reason | evidence | 95% bracket [lower, upper] | threshold | "
                      "human baseline |", "|---|---|---|---|---|---|---|---|"]
            for e in changes:
                p = e["payload"]
                ev = p["evidence"]
                lines.append(f"| {e['seq']} | {_md(run_of[int(e['seq'])])} | {p['from']} → {p['to']} | {p['reason']} | "
                             f"{_evidence(ev)} | {_bracket(ev)} | {_num(ev.get('threshold'))} | "
                             f"{_num(ev.get('baseline'))} |")
            lines.append("")
        signals = [e for e in rows if e["kind"] == "CUSUM_SIGNAL"]
        if signals:
            lines += ["CUSUM signals:", "", "| seq | run_id | level | S | h | outcomes monitored | change point |",
                      "|---|---|---|---|---|---|---|"]
            for e in signals:
                p = e["payload"]
                lines.append(f"| {e['seq']} | {_md(run_of[int(e['seq'])])} | {p['level']} | {p['s']} | {p['h']} | "
                             f"{p['outcomes_monitored']} | {p['change_point_index']} |")
            lines.append("")
        overrides = [e for e in rows if e["kind"] == "OVERRIDE"]
        if overrides:
            lines += ["Overrides:", "", "| seq | op | actor | reason | change | pinned | ceiling |",
                      "|---|---|---|---|---|---|---|"]
            for e in overrides:
                p = e["payload"]
                lines.append(f"| {e['seq']} | {p['op']} | {_md(p['actor'])} | {_md(p['reason'])} | "
                             f"{p['from']} → {p['to']} | {p['pinned']} | {p['ceiling']} |")
            lines.append("")
        if final is not None and cell in final:
            f = final[cell]
            blockers = _md(", ".join(f.get("blockers", [])) or "none")
            label = f" ({final_label})" if final_label else ""
            lines += [f"Final{label}: {f.get('level')} · n {f.get('n')} · blockers {blockers}", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def export(ledger: Ledger, out_dir: str | Path, final: Mapping[str, Mapping[str, Any]] | None = None,
           actor: str = "system", final_label: str = "",
           manifests: Mapping[str, Mapping[str, Any]] | None = None) -> VerifyReport:
    """Write ledger.jsonl, head.json and governance_report.md; on a verified ledger, append an EXPORT entry
    {path_sha, head_seq, head_hash} for the exported head (master §19.5)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rep = ledger.verify()
    head = ledger.export_jsonl(out / "ledger.jsonl")
    (out / "head.json").write_text(json.dumps({"seq": head.seq, "hash": head.hash}) + "\n", encoding="utf-8",
                                   newline="\n")
    entries = read_jsonl(out / "ledger.jsonl")
    (out / "governance_report.md").write_text(governance_report(entries, rep, final, final_label=final_label, manifests=manifests),
                                                   encoding="utf-8", newline="\n")
    if rep.ok:
        path_sha = hashlib.sha256((out / "ledger.jsonl").read_bytes()).hexdigest()
        ledger.append("EXPORT", actor, {"path_sha": path_sha, "head_seq": head.seq, "head_hash": head.hash})
    return rep
