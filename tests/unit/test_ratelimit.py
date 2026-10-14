"""Client-side request-rate limit for shared endpoints (decision D-rpm: Token Factory allows 30 RPM)."""
import asyncio
from pathlib import Path

import httpx
import pytest

from tenure.config import load
from tenure.ratelimit import RateLimitedTransport, RateLimiter, shared_limiter

pytestmark = [pytest.mark.unit, pytest.mark.gate_g01]

ROOT = Path(__file__).resolve().parents[2]


class FakeClock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_limiter_spaces_request_starts():
    clock = FakeClock()
    lim = RateLimiter(30, clock=clock)
    assert lim.interval == 2.0
    assert [lim.reserve() for _ in range(4)] == [0.0, 2.0, 4.0, 6.0]
    clock.t += 20.0  # idle time is not banked as a burst allowance
    assert [lim.reserve() for _ in range(2)] == [0.0, 2.0]


@pytest.mark.parametrize("rpm", [0, -5])
def test_limiter_rejects_non_positive_rpm(rpm):
    with pytest.raises(ValueError):
        RateLimiter(rpm)


def test_shared_limiter_is_one_per_name():
    a, b = shared_limiter("tf-test", 30), shared_limiter("tf-test", 30)
    assert a is b and shared_limiter("other-test", 30) is not a
    with pytest.raises(ValueError):
        shared_limiter("tf-test", 60)  # one host, one limit


def _ok(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"path": req.url.path})


def test_async_transport_waits_its_slot_and_reopens_after_close():
    clock, slept, made = FakeClock(), [], []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    def factory() -> httpx.MockTransport:
        made.append(1)
        return httpx.MockTransport(_ok)

    t = RateLimitedTransport(RateLimiter(30, clock=clock), async_factory=factory, sleep=fake_sleep)

    async def go() -> list[int]:
        out = []
        for _ in range(2):  # two clients in a row, as the tests and CLI create them
            async with httpx.AsyncClient(base_url="http://h/v1", transport=t) as c:
                out += [(await c.get("/models")).status_code for _ in range(3)]
        return out

    assert asyncio.run(go()) == [200] * 6
    assert slept == [0.0, 2.0, 4.0, 6.0, 8.0, 10.0] and len(made) == 2


def test_sync_transport_waits_its_slot():
    clock, slept = FakeClock(), []
    t = RateLimitedTransport(RateLimiter(60, clock=clock), sync_factory=lambda: httpx.MockTransport(_ok),
                             sync_sleep=slept.append)
    with httpx.Client(base_url="http://h/v1", transport=t) as c:
        assert [c.get("/models").status_code for _ in range(3)] == [200, 200, 200]
    assert slept == [0.0, 1.0, 2.0]


def test_token_factory_profile_declares_30_rpm():
    cfg = load(ROOT / "tenure.yaml")
    assert cfg.inference.profiles["token_factory"].rpm == 30
    assert cfg.inference.profiles["vllm_droplet"].rpm is None  # self-hosted: no shared limit
