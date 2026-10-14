#!/usr/bin/env python3
"""Cumulative gate runner for RUNBOOK_TENURE.  Usage: uv run python scripts/gate.py G03

A gate runs every test marked with its own gate marker or any earlier gate's marker, so each
gate is a regression ratchet. It fails on any failure, error or skip, on forbidden patterns in
src/tenure, on coverage below threshold, and on missing TDD evidence for the module.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ORDER = ["G00", "G01", "G02", "G03", "G04", "G05", "G06", "G07", "G08"]
STRICT = {"stats", "cost", "cusum", "ladder", "ledger", "policy", "humans", "business"}  # >= 90%
EXEMPT = {"__init__", "cli", "api", "gpu", "finetune"}  # exercised by e2e and manual runs
DEFAULT_MIN, STRICT_MIN = 0.80, 0.90
FORBIDDEN = re.compile(r"\b(TODO|FIXME|XXX)\b|NotImplementedError|pytest\.skip|\bxfail\b")
PASS_ONLY = re.compile(r"def [^\n]+:\n(?:[ \t]+(?:\"\"\"|''')[^\n]*(?:\"\"\"|''')\n)?[ \t]+pass[ \t]*\n")
ROOT = Path(__file__).resolve().parents[1]


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def junit_counts(path: Path) -> dict[str, int]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    keys = ("tests", "failures", "errors", "skipped")
    return {k: sum(int(s.get(k, 0)) for s in suites) for k in keys}


def forbidden_hits() -> list[str]:
    hits = []
    for p in sorted((ROOT / "src" / "tenure").rglob("*.py")):
        text = p.read_text(encoding="utf-8")
        rel = p.relative_to(ROOT).as_posix()
        for m in FORBIDDEN.finditer(text):
            hits.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}: {m.group(0)}")
        if PASS_ONLY.search(text):
            hits.append(f"{rel}: pass-only function body")
    return hits


def tdd_evidence(module: str) -> list[str]:
    problems = []
    ev = ROOT / ".agent" / "evidence" / module
    if not any(ev.glob("red-*.txt")):
        problems.append(f"no red evidence in .agent/evidence/{module}/")
    subjects = git("log", "--reverse", "--format=%s").splitlines()
    tests = [i for i, s in enumerate(subjects) if s.startswith(f"test({module})")]
    feats = [i for i, s in enumerate(subjects) if s.startswith(f"feat({module})")]
    if feats and (not tests or tests[0] > feats[0]):
        problems.append(f"feat({module}) commit precedes any test({module}) commit")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1].upper() not in ORDER:
        print(f"usage: gate.py <{'|'.join(ORDER)}>")
        return 2
    gate = argv[1].upper()
    module = "T" + gate[1:]
    markers = " or ".join(f"gate_{g.lower()}" for g in ORDER[: ORDER.index(gate) + 1])
    out = ROOT / "var" / "gates"
    out.mkdir(parents=True, exist_ok=True)
    junit, cov = out / f"{gate}.junit.xml", out / f"{gate}.cov.json"
    cmd = [sys.executable, "-m", "pytest", "-m", markers, "--strict-markers", "-p", "no:randomly",
           f"--junitxml={junit}", "--cov=tenure", f"--cov-report=json:{cov}", "--cov-report=term-missing:skip-covered"]
    rc = subprocess.call(cmd, cwd=ROOT)
    problems: list[str] = []
    counts = junit_counts(junit) if junit.exists() else {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    if rc != 0 or counts["failures"] or counts["errors"]:
        problems.append(f"pytest rc={rc} failures={counts['failures']} errors={counts['errors']}")
    if counts["skipped"]:
        problems.append(f"{counts['skipped']} skipped (gates allow none)")
    if counts["tests"] == 0:
        problems.append("no tests selected")
    hits = forbidden_hits()
    problems += [f"forbidden: {h}" for h in hits]
    coverage: dict[str, float] = {}
    if cov.exists():
        for path, data in json.loads(cov.read_text(encoding="utf-8"))["files"].items():
            name = Path(path).stem
            pct = data["summary"]["percent_covered"] / 100.0
            coverage[name] = round(pct, 4)
            need = STRICT_MIN if name in STRICT else DEFAULT_MIN
            if name not in EXEMPT and pct < need:
                problems.append(f"coverage {name} {pct:.1%} < {need:.0%}")
    problems += tdd_evidence(module)
    status = "red" if problems else "green"
    record = {"gate": gate, "module": module, "status": status, "commit": git("rev-parse", "HEAD"),
              "utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "tests": counts,
              "coverage": coverage, "forbidden": len(hits), "problems": problems}
    (ROOT / "gates").mkdir(exist_ok=True)
    (ROOT / "gates" / f"{gate}.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"\n{gate}: {status.upper()}")
    for p in problems:
        print(f"  - {p}")
    return 0 if status == "green" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
