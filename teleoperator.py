"""Operator side: reBot 102 leader + episode hotkeys + rerun viewer.

Hardware lives in ``utils/rebot.py``. Rerun blueprint and per-tick
log helpers live in ``utils/rerun_view.py``. The hotkey listener lives
in ``utils/hotkeys.py``.

Recording is done by ``recorder.py``, a Portal observer. This side only
marks where episodes start and end, with keypoints the recorder writes
next to everything else:

* ``recording`` (payload ``{"task_description": ...}``) opens an episode,
* ``idle`` closes it,
* ``discard`` closes it and tells the converter to drop it.

Every leader action carries ``in_reply_to_ts_us``, the robot-clock
timestamp of the newest observation on screen when the human moved. The
recorder files the action under that timestamp, so the converter pairs it
with exactly the frames the human was looking at.

``action_subscription: active`` in ``portal.yaml`` mirrors the executed
action stream back here for the rerun view, so the plot shows what the
arm actually ran, whether it came from the leader or a policy.

Hotkeys:
  c: cycle the active operator through ``[self, *remote_operators]``,
     wrapping back to self.
  r: start / end an episode.
  [: discard the in-flight episode.
  x: clean quit.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib
from typing import Optional

import numpy as np
import rerun as rr

from livekit.portal import (
    Action,
    Observation,
    Operator,
    OperatorConfig,
    frame_bytes_to_numpy_rgb,
)

from _common import env_int, env_str, load_env, mint_token, pace, required_env
from utils.hotkeys import Hotkeys
from utils.rebot import CAMERAS, build_leader
from utils.rerun_view import (
    build_blueprint,
    log_action,
    log_metrics,
    log_observation,
    log_recording,
)

IDENTITY = "teleoperator"
CONFIG_PATH = pathlib.Path(__file__).parent / "portal.yaml"

logger = logging.getLogger(__name__)


async def cycle_active_operator(op: Operator, me: str | None) -> None:
    """`c`: cycle active operator through ``[self, *others]`` and wrap
    back to self. With no remote operator connected the ring is just
    ``[self]``, so pressing `c` claims control for self — the common
    case during solo teleop before any policy has joined, when the
    active operator is still ``None`` and the robot is dropping our
    actions."""
    ring = [me, *op.operators()]
    current = op.active_operator()
    try:
        idx = ring.index(current)
    except ValueError:
        # Current active operator dropped from the ring (e.g. disconnected
        # mid-session) or was never set. Snap to self.
        idx = -1
    nxt = ring[(idx + 1) % len(ring)]
    await op.set_active_operator(nxt)
    print(f"[teleop] active operator → {nxt}")


async def mark(op: Operator, kind: str, payload: dict | None = None) -> None:
    """Send an episode keypoint, warning when no recorder will see it."""
    if not await op.send_keypoint(kind, payload):
        print(f"[teleop] '{kind}' sent, but no recorder is in the room; start recorder.py")


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    load_env()

    url = required_env("LIVEKIT_URL")
    room = required_env("LIVEKIT_ROOM")
    token = mint_token(IDENTITY, room)

    cfg = OperatorConfig.from_yaml_file(CONFIG_PATH, room)
    fps = env_int("PORTAL_FPS", 30)
    task = env_str("PORTAL_HITL_TASK", "reBot B601 manipulation")

    leader = build_leader()
    op = Operator(cfg)

    # Latest synced observation for the rerun mirror and the action anchor.
    # Written by Portal callbacks (already on the asyncio loop) and read on
    # the main tick. One-writer / one-reader.
    latest_obs: Optional[Observation] = None
    latest_state: dict = {}
    latest_frames: dict[str, np.ndarray] = {}
    latest_executed: Optional[Action] = None
    recording = False

    def on_observation(obs: Observation) -> None:
        nonlocal latest_obs, latest_state
        latest_obs = obs
        latest_state = dict(obs.state)
        for cam in CAMERAS:
            f = obs.frames.get(cam)
            if f is not None:
                latest_frames[cam] = frame_bytes_to_numpy_rgb(f.data, f.width, f.height)

    def on_action(action: Action) -> None:
        nonlocal latest_executed
        latest_executed = action

    op.on_observation(on_observation)
    op.on_action(on_action)
    op.on_active_operator_changed(
        lambda i: print(f"[teleop] active operator now: {i}")
    )
    op.on_operator_joined(lambda i: print(f"[teleop] operator joined: {i}"))
    op.on_operator_left(lambda i: print(f"[teleop] operator left: {i}"))

    leader.connect()
    print(f"[teleop] connecting to {url} as '{IDENTITY}' in room '{room}' ...")
    await op.connect(url, token)
    me = op.local_identity()

    print(f"[teleop] connected as '{me}' @ {fps} fps")
    print("[teleop] hotkeys: c=cycle operator  r=record  [=discard  x=quit")

    rr.init(f"portal-hitl-{room}", spawn=True)
    rr.send_blueprint(build_blueprint())

    hotkeys = Hotkeys()
    hotkeys.start()
    quit_requested = False

    try:
        async for tick in pace(fps):
            for key in hotkeys.pop_pressed():
                if key == "c":
                    await cycle_active_operator(op, me)
                elif key == "r":
                    if recording:
                        await mark(op, "idle")
                        print("[teleop] episode ended")
                    else:
                        await mark(op, "recording", {"task_description": task})
                        print(f"[teleop] episode recording (task={task!r})")
                    recording = not recording
                elif key == "[" and recording:
                    await mark(op, "discard")
                    recording = False
                    print("[teleop] episode discarded")
                elif key == "x":
                    quit_requested = True
            if quit_requested:
                break

            # Drive the wire. Always send, even when not active; the
            # robot's gate drops mismatched-sender actions, and keeping
            # the leader in sync means takeover from a policy is instant.
            op.send_action(
                leader.get_action(),
                in_reply_to_ts_us=latest_obs.timestamp_us if latest_obs else None,
            )

            # Mirror to rerun. Reads the latest snapshots; rerun is a
            # visualization, not the dataset, so duplicate frames at
            # tick rate don't matter.
            log_observation(latest_obs, latest_state, latest_frames)
            log_action(latest_executed, me)
            log_recording(recording)
            if tick % fps == 0 and (m := op.metrics()) is not None:
                log_metrics(m)

    except KeyboardInterrupt:
        print("\n[teleop] stopping ...")
    finally:
        hotkeys.stop()
        if recording:
            await mark(op, "idle")
        try:
            await op.disconnect()
        finally:
            op.close()
            leader.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
