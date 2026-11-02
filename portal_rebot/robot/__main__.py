"""Robot side: drives the B601-DM follower behind a Portal `Robot`.

Run with `uv run robot`. Each tick reads state and camera frames from the
follower, publishes them on one shared timestamp so the operator's
synchronizer can match them into a single observation, and applies whatever
action last cleared the active-operator gate.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Iterator

from livekit.portal import Action, Robot, RobotConfig

from portal_rebot.common import (
    CONFIG_PATH,
    ROBOT_IDENTITY,
    HardwareThread,
    TickBudget,
    env_int,
    load_env,
    mint_token,
    pace,
    required_env,
)
from portal_rebot.robot.follower import build_follower, split_state_frames

logger = logging.getLogger(__name__)


def diagnostics(follower, budget: TickBudget) -> Iterator[str]:
    """The 5s console lines: achieved rate, bus health, speed cap."""
    if line := budget.report():
        yield line
    bus = follower.resilient_bus
    if bus.errors or bus.reconnects:
        yield (f"motor bus: {bus.errors} errors, {bus.reconnects} reconnects"
               + (" - CURRENTLY DEGRADED" if bus.degraded else ""))
    limiter = follower.step_limiter
    if clamped := limiter.clamped:
        limiter.clamped = 0
        yield (f"speed cap: {clamped} joint commands clamped to "
               f"{limiter.max_step_deg:g} deg in 5s")


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    load_env()

    url = required_env("LIVEKIT_URL")
    room = required_env("LIVEKIT_ROOM")
    cfg = RobotConfig.from_yaml_file(CONFIG_PATH, room)
    fps = env_int("PORTAL_FPS", cfg.fps)
    tracks = list(cfg.video_tracks)

    follower = build_follower(tracks, fps)
    robot = Robot(cfg)
    hardware = HardwareThread("follower")

    latest_action: dict[str, float] = {}

    def on_action(action: Action) -> None:
        nonlocal latest_action
        latest_action = {k: float(v) for k, v in action.values.items()}

    def on_operator_left(identity: str) -> None:
        # Portal pins the active-operator pointer across a disconnect so a
        # same-identity reconnect resumes control. For a physical arm that
        # would hold the last commanded pose indefinitely, so clear it.
        nonlocal latest_action
        if identity == robot.active_operator():
            print(f"[robot] active operator '{identity}' left; releasing")
            latest_action = {}
            asyncio.create_task(robot.set_active_operator(None))

    robot.on_action(on_action)
    robot.on_operator_left(on_operator_left)
    robot.on_active_operator_changed(lambda i: print(f"[robot] active operator now: {i}"))

    follower.connect()
    print(f"[robot] connecting to {url} as '{ROBOT_IDENTITY}' in room '{room}' ...")
    await robot.connect(url, mint_token(ROBOT_IDENTITY, room))
    print(f"[robot] connected; cameras={list(tracks)}; streaming at {fps} fps; ctrl-c to stop")

    budget = TickBudget(fps)

    try:
        async for tick in pace(fps):
            ts_us = int(time.time() * 1_000_000)
            started = time.perf_counter()
            observation = await hardware.run(follower.get_observation)
            device_ms = (time.perf_counter() - started) * 1e3

            state, frames = split_state_frames(observation, tracks)
            # Empty state means the motor bus is down and `_ResilientBus`
            # handed back cameras only. Publish the video anyway -- seeing a
            # live arm that has stopped following is the difference between
            # "the link died" and "the arm did" -- but never publish a state
            # we did not read.
            if state:
                robot.send_state(state, timestamp_us=ts_us)
            for name, frame in frames.items():
                robot.send_video_frame(name, frame, timestamp_us=ts_us)
            if latest_action:
                applied = time.perf_counter()
                await hardware.run(follower.send_action, latest_action)
                device_ms += (time.perf_counter() - applied) * 1e3
            budget.record(device_ms, (time.perf_counter() - started) * 1e3)

            if tick and tick % (fps * 5) == 0:
                for line in diagnostics(follower, budget):
                    print(f"[robot] {line}")
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl-C under `asyncio.run` raises at the run_until_complete frame,
        # not here; the runner then cancels this task, so CancelledError is
        # what actually lands.
        print("\n[robot] stopping ...")
    finally:
        print("[robot] disconnecting ...")
        try:
            await robot.disconnect()
        finally:
            robot.close()
            hardware.close()
            follower.disconnect()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
