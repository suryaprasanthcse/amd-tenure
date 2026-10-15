"""GPU telemetry for the vllm_droplet profile: rocm-smi (or amd-smi) polled every second (master §13.5).

Each poll publishes one `gpu` event per device (Appendix B). When neither tool is installed, as on the
Token Factory profile or a laptop, nothing is published and the absence is logged once.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from typing import Any

from .events import EventBus

log = logging.getLogger("tenure.gpu")

ROCM_SMI = ("rocm-smi", "--showuse", "--showmemuse", "--showmeminfo", "vram", "--showpower", "--json")
AMD_SMI = ("amd-smi", "metric", "--json")
GIB = 2 ** 30
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _run(cmd: Sequence[str]) -> str:
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=5, check=True).stdout


def _num(v: Any) -> float | None:
    """A number from 37, "37", "512.0 W" or {"value": 37, "unit": "%"}; None when absent or N/A."""
    if isinstance(v, dict):
        v = v.get("value")
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = _NUM.search(str(v))
    return float(m.group(0)) if m else None


def _gb(v: float | None, scale: float) -> float | None:
    return None if v is None else round(v / scale, 2)


def parse_rocm_smi(obj: Any) -> list[dict[str, Any]]:
    """`rocm-smi ... --json` → gpu events, one per card."""
    out = []
    if not isinstance(obj, dict):
        return out
    for card in sorted(k for k in obj if k.startswith("card")):
        f = obj[card]
        if not isinstance(f, dict):
            continue
        power = next((_num(v) for k, v in sorted(f.items()) if "Power (W)" in k), None)
        out.append({"util_pct": _num(f.get("GPU use (%)")),
                    "vram_used_gb": _gb(_num(f.get("VRAM Total Used Memory (B)")), GIB),
                    "vram_total_gb": _gb(_num(f.get("VRAM Total Memory (B)")), GIB),
                    "power_w": power, "device": card})
    return out


def parse_amd_smi(obj: Any) -> list[dict[str, Any]]:
    """`amd-smi metric --json` → gpu events, one per GPU (VRAM is reported in MB)."""
    items = obj.get("gpu_data", []) if isinstance(obj, dict) else obj
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        usage, mem, power = item.get("usage") or {}, item.get("mem_usage") or {}, item.get("power") or {}
        util = _num(usage.get("gfx_activity", usage.get("gfx_usage")))
        watts = _num(power.get("socket_power", power.get("average_socket_power")))
        out.append({"util_pct": util, "vram_used_gb": _gb(_num(mem.get("used_vram")), 1024),
                    "vram_total_gb": _gb(_num(mem.get("total_vram")), 1024), "power_w": watts,
                    "device": f"gpu{item.get('gpu', len(out))}"})
    return out


class GpuPoller:
    def __init__(self, bus: EventBus, which: Callable[[str], str | None] = shutil.which,
                 run: Callable[[Sequence[str]], str] = _run, interval: float = 1.0):
        self.bus = bus
        self._which = which
        self._run = run
        self.interval = interval
        self._warned = False

    def available(self) -> bool:
        return any(self._which(cmd[0]) is not None for cmd in (ROCM_SMI, AMD_SMI))

    def poll_once(self) -> list[dict[str, Any]]:
        for cmd, parse in ((ROCM_SMI, parse_rocm_smi), (AMD_SMI, parse_amd_smi)):
            if self._which(cmd[0]) is None:
                continue
            try:
                events = parse(json.loads(self._run(cmd)))
            except (OSError, subprocess.SubprocessError, ValueError) as e:
                log.debug("%s failed: %s", cmd[0], type(e).__name__)
                continue
            if events:
                return events
        if not self._warned:
            self._warned = True
            log.info("no GPU telemetry: neither rocm-smi nor amd-smi produced readings")
        return []

    async def run(self, stop: asyncio.Event) -> None:
        """Publish gpu events every `interval` seconds until `stop` is set; returns at once without tools."""
        if not self.available():
            self.poll_once()  # logs the absence once
            return
        while not stop.is_set():
            for event in await asyncio.to_thread(self.poll_once):
                self.bus.publish("gpu", event)
            try:
                await asyncio.wait_for(stop.wait(), self.interval)
            except TimeoutError:
                continue
