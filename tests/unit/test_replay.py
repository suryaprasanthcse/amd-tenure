"""ReplayEngine (R.13): concurrent decisions, strict input-order commits, bounded reorder buffer (I6)."""
import asyncio
import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tenure.replay import ReplayEngine

pytestmark = [pytest.mark.unit, pytest.mark.gate_g03]


# Verbatim from master Appendix T (tests/unit/test_reference.py).
def test_replay_commits_in_order_under_random_latency():
    rng = random.Random(9)
    delays = [rng.random() / 200 for _ in range(500)]
    seen = []

    async def decide(i):
        await asyncio.sleep(delays[i])
        return i * 2

    def commit(idx, case, out):
        seen.append((idx, case, out))

    eng = ReplayEngine(decide, commit, concurrency=32, max_ahead=64)
    n = asyncio.run(eng.run(list(range(500))))
    assert n == 500 and seen == [(i, i, 2 * i) for i in range(500)]


@given(st.lists(st.integers(min_value=0, max_value=3), min_size=0, max_size=40),
       st.integers(min_value=1, max_value=8), st.integers(min_value=0, max_value=8))
def test_replay_order_invariant_property(delays_ms, concurrency, extra):
    seen = []

    async def decide(i):
        await asyncio.sleep(delays_ms[i] / 1000)
        return -i

    eng = ReplayEngine(decide, lambda idx, case, out: seen.append((idx, case, out)),
                       concurrency=concurrency, max_ahead=concurrency + extra)
    assert asyncio.run(eng.run(list(range(len(delays_ms))))) == len(delays_ms)
    assert seen == [(i, i, -i) for i in range(len(delays_ms))]


def test_replay_bounds_buffer():
    rng = random.Random(3)
    delays = [rng.random() / 500 for _ in range(400)]
    delays[0] = 0.05  # the head case is slow, so later cases pile up behind it
    started = committed = peak = 0

    async def decide(i):
        nonlocal started, peak
        started += 1
        peak = max(peak, started - committed)
        await asyncio.sleep(delays[i])
        return i

    def commit(idx, case, out):
        nonlocal committed
        assert started - committed <= 24
        committed += 1

    eng = ReplayEngine(decide, commit, concurrency=16, max_ahead=24)
    assert asyncio.run(eng.run(list(range(400)))) == 400
    assert peak == 24  # the buffer fills to max_ahead and never beyond


def test_replay_empty_and_single():
    seen = []

    async def decide(c):
        return c.upper()

    def commit(idx, case, out):
        seen.append((idx, case, out))

    eng = ReplayEngine(decide, commit, concurrency=4, max_ahead=8)
    assert asyncio.run(eng.run([])) == 0 and seen == [] and eng.committed == 0
    eng = ReplayEngine(decide, commit, concurrency=4, max_ahead=8)
    assert asyncio.run(eng.run(["only"])) == 1 and seen == [(0, "only", "ONLY")]


def test_replay_rejects_bad_bounds():
    async def decide(c):
        return c

    with pytest.raises(ValueError):
        ReplayEngine(decide, lambda *a: None, concurrency=0, max_ahead=8)
    with pytest.raises(ValueError):
        ReplayEngine(decide, lambda *a: None, concurrency=8, max_ahead=4)
