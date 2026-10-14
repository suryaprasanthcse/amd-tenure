#!/usr/bin/env python3
"""Spike S1 (master §24): does the endpoint serve a Gemma instruction model over an OpenAI-compatible API?

Usage: uv run python scripts/spikes/s1_models.py [--n 1]
Prints one STATE-ready line. Exit 0 on PASS, 1 on FAIL, 2 when only the mock key is configured (D-llm-mock).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tenure.cli import MOCK_API_KEY, s1_check, s1_state_line
from tenure.config import endpoint, load, load_dotenv

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1)
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")
    prof, key = endpoint(load(ROOT / "tenure.yaml"))
    if key == MOCK_API_KEY:
        print("S1: PENDING → mock key configured (D-llm-mock); set real TENURE_LLM_* values in .env")
        return 2
    result = s1_check(prof, key, args.n)
    print(s1_state_line(result))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
