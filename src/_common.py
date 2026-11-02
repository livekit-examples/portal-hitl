"""Shared plumbing: env loading, LiveKit tokens, and the tick pacer.

`portal.yaml` and the `.env` files live one directory above this file, at the repo root.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import os
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncIterator, Callable, Optional

os.environ.setdefault("RUST_LOG", "warn,livekit_portal::sync_buffer=error,libwebrtc=error")

from dotenv import load_dotenv
from livekit import api
from livekit.portal import PortalError
from livekit.protocol.room import RoomConfiguration

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "portal.yaml"

ROBOT_IDENTITY = "robot"
TELEOPERATOR_IDENTITY = "teleoperator"
RECORDER_IDENTITY = "recorder"


def load_env() -> None:
    """Load `.env` then `.env.local` (which overrides) from the repo root and cwd, then set up logging."""
    for directory in (ROOT, pathlib.Path.cwd()):
        for name, override in ((".env", False), (".env.local", True)):
            if (path := directory / name).exists():
                load_dotenv(path, override=override)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())


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


async def connect_to_robot(peer: Any, url: str, identity: str, room: str) -> None:
    """Connect an operator or observer, which returns once synced to the robot's clock.

    Without a robot in the room, `connect()` raises `TimeSyncTimeout` after
    10 s. Exit with what to do instead of a traceback.
    """
    try:
        await peer.connect(url, mint_token(identity, room))
    except PortalError.TimeSyncTimeout:
        raise SystemExit(f"[{identity}] no robot answered in room '{room}' within 10 s; "
                         "start `uv run robot` first") from None


async def pace(fps: int) -> AsyncIterator[int]:
    """Yield a tick index at `fps`, as `async for tick in pace(30)`.

    A late tick moves the schedule forward instead of piling up drift. Even
    then it yields with `sleep(0)`: without that, a body that always overruns
    would starve the Portal callbacks it depends on.
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
    """Runs one device's blocking calls on one dedicated thread.

    The event loop stays free while the device works, and a single worker
    means two calls can never overlap on the device's serial bus.
    """

    def __init__(self, name: str) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)

    async def run(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn, *args)

    def close(self) -> None:
        """Wait for the call in flight, so the device is never disconnected mid-transaction."""
        self._pool.shutdown(wait=True)


class TickBudget:
    """Tick timings behind the robot's 5s diagnostic line.

    `device_ms` is time inside the driver calls and `tick_ms` the whole body, so
    a slow CAN bus and slow publishing show up separately.
    """

    def __init__(self, fps: int) -> None:
        self.fps = fps
        self.reset()

    def reset(self) -> None:
        self.device_ms: list[float] = []
        self.tick_ms: list[float] = []
        self.started = time.perf_counter()

    def record(self, device_ms: float, tick_ms: float) -> None:
        self.device_ms.append(device_ms)
        self.tick_ms.append(tick_ms)

    def report(self) -> Optional[str]:
        ticks = len(self.tick_ms)
        if not ticks:
            return None
        budget_ms = 1000.0 / self.fps
        over = sum(ms > budget_ms for ms in self.tick_ms)
        line = (f"{ticks / (time.perf_counter() - self.started):.1f} fps of {self.fps} requested; "
                f"device {sum(self.device_ms) / ticks:.0f}ms (worst {max(self.device_ms):.0f}ms), "
                f"tick {sum(self.tick_ms) / ticks:.0f}ms (worst {max(self.tick_ms):.0f}ms) "
                f"of {budget_ms:.0f}ms budget; {over}/{ticks} over")
        self.reset()
        return line
