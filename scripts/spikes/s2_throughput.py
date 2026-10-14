#!/usr/bin/env python3
"""Spike S2 (master §24): replay throughput over a concurrency sweep, and is the policy prefix cached?

Usage: uv run python scripts/spikes/s2_throughput.py [--n 500] [--no-write]
Same as `uv run tenure spike s2`: writes var/spikes/S2.json and the result into tenure.yaml, and prints
one STATE-ready line. Exit 0 on PASS, 1 on FAIL, 2 when only the mock key is configured (D-llm-mock).
"""
from __future__ import annotations

import sys
from pathlib import Path

from typer.testing import CliRunner

from tenure.cli import app

ROOT = Path(__file__).resolve().parents[2]

if __name__ == "__main__":
    result = CliRunner().invoke(app, ["spike", "s2", "--config", str(ROOT / "tenure.yaml"), *sys.argv[1:]])
    print(result.output, end="")
    sys.exit(result.exit_code)
