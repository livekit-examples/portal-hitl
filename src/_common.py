"""Shared plumbing: env loading, LiveKit tokens, and the tick pacer.

`portal.yaml` and the `.env` files live one directory above this file, at the repo root.
"""
from __future__ import annotations

import asyncio
import datetime
import os
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncIterator, Callable, Optional

os.environ.setdefault("RUST_LOG", "warn,livekit_portal::sync_buffer=error,libwebrtc=error")

from dotenv import load_dotenv
from livekit import api
from livekit.protocol.room import RoomConfiguration

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "portal.yaml"

ROBOT_IDENTITY = "robot"
TELEOPERATOR_IDENTITY = "teleoperator"
RECORDER_IDENTITY = "recorder"


def load_env() -> None:
    """`.env` then `.env.local` (which overrides), from the repo root and cwd."""
    for directory in (ROOT, pathlib.Path.cwd()):
        for name, override in ((".env", False), (".env.local", True)):
            if (path := directory / name).exists():
                load_dotenv(path, override=override)


def required_env(name: str) -> str:
    if not (value := os.environ.get(name)):
        raise RuntimeError(f"{name} must be set (see .env.example)")
    return value


def env_str(name: str, default: str) -> str:
    return os.environ.get(name) or default


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name) or default)


def env_camera_id(name: str, default: str) -> int | str:
    """Camera identifier: an int index on macOS, a /dev/videoN string on Linux."""
    raw = env_str(name, default)
    return int(raw) if raw.isdigit() else raw


def mint_token(identity: str, room: str, ttl_hours: int = 6) -> str:
    """Mint a LiveKit JWT scoped to `room` with low-latency playout bounds.

    `can_update_own_metadata` is required so Portal can publish its
    `lk.portal.role` attribute on connect, which peer discovery reads.
    """
    grants = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=True,
        can_subscribe=True,
        can_update_own_metadata=True,
    )
    return (
        api.AccessToken(required_env("LIVEKIT_API_KEY"), required_env("LIVEKIT_API_SECRET"))
        .with_identity(identity)
        .with_grants(grants)
        .with_room_config(RoomConfiguration(name=room, min_playout_delay=0, max_playout_delay=1))
        .with_ttl(datetime.timedelta(hours=ttl_hours))
        .to_jwt()
    )


async def pace(fps: int) -> AsyncIterator[int]:
    """Yield a tick index at `fps`, as `async for tick in pace(30)`.

    A tick that runs over budget snaps the schedule forward instead of
    accumulating drift. The `sleep(0)` on that path is load-bearing: `async
    for` is not itself a suspension point, so a consistently overrunning tick
    body would turn this into a busy-loop that never services the Portal
    callbacks it depends on -- the arm stops following while the loop still
    looks healthy. See tutorial/07-running-the-rig.md#troubleshooting.
    """
    interval = 1.0 / fps
    next_tick = time.perf_counter()
    tick = 0
    while True:
        yield tick
        tick += 1
        next_tick += interval
        now = time.perf_counter()
        if next_tick > now:
            await asyncio.sleep(next_tick - now)
        else:
            next_tick = now
            await asyncio.sleep(0)


class HardwareThread:
    """Runs one device's blocking calls on a single dedicated thread.

    Both tick bodies are synchronous device I/O, which on the event loop is
    what pushes a tick past its interval; awaiting it here leaves the loop free
    for the whole duration.

    One worker, not a pool. The device's calls share its serial bus and must
    never overlap -- `send_action` interleaved with `get_observation` corrupts
    both -- and awaiting each call in turn on a single thread is what
    guarantees that, independently of how the tick body is ordered later.
    """

    def __init__(self, name: str) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)

    async def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    def close(self) -> None:
        """Wait for the in-flight call before letting the caller disconnect.
        Cancelling `run()` abandons the await, not the thread, and
        disconnecting a device mid-transaction is how you corrupt a CAN adapter
        on ctrl-c."""
        self._pool.shutdown(wait=True)


class TickBudget:
    """Rolling tick-cost accounting behind the robot's 5s diagnostic line.

    `device_ms` is time inside the driver calls, `tick_ms` the whole body. They
    separate the two ways to miss the budget -- a slow CAN bus versus slow
    publishing -- which need different fixes.
    """

    def __init__(self, fps: int) -> None:
        self.interval_ms = 1000.0 / fps
        self.reset()

    def reset(self) -> None:
        self._ticks = 0
        self._over = 0
        self._device_sum = self._device_max = 0.0
        self._tick_sum = self._tick_max = 0.0
        self._t0 = time.perf_counter()

    def record(self, device_ms: float, tick_ms: float) -> None:
        self._ticks += 1
        if tick_ms > self.interval_ms:
            self._over += 1
        self._device_sum += device_ms
        self._device_max = max(self._device_max, device_ms)
        self._tick_sum += tick_ms
        self._tick_max = max(self._tick_max, tick_ms)

    def report(self) -> Optional[str]:
        elapsed = time.perf_counter() - self._t0
        if not self._ticks or elapsed <= 0:
            return None
        line = (f"{self._ticks / elapsed:.1f} fps of {1000.0 / self.interval_ms:.0f} "
                f"requested; device {self._device_sum / self._ticks:.0f}ms "
                f"(worst {self._device_max:.0f}ms), "
                f"tick {self._tick_sum / self._ticks:.0f}ms (worst {self._tick_max:.0f}ms) "
                f"of {self.interval_ms:.0f}ms budget; {self._over}/{self._ticks} over")
        self.reset()
        return line
