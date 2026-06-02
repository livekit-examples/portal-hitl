"""Operator side: reBot 102 leader + HITL recorder + rerun viewer.

Hardware lives in ``utils/rebot.py``. Rerun blueprint and per-tick
log helpers live in ``utils/rerun_view.py``. The hotkey listener lives
in ``utils/hotkeys.py``. The recording pipeline — obs ring, action↔obs
alignment, and the stale-obs guard — lives in ``HitlRecorder``
(``hitl_recorder.py``), above the ``DatasetRecorder`` in
``utils/recorder.py``.

Two YAML knobs (set in ``portal.yaml``) carry the wire-quality
contract for this side:

* ``action_subscription: true``. ``on_action`` fires for every action
  the robot's gate forwards, regardless of sender. Without it the
  recorder would only see actions we sent, not ones from a remote
  policy during HITL handoff. Each action carries ``Action.sender``
  so dataset rows can be labelled correctly.
* ``reuse_stale_frames: true``. The synchronizer reuses the last
  good frame on a track when the current state has no fresh frame
  within the sync window, instead of holding the state. State drives
  obs emission; frames are best-effort. Without this a transient
  camera drop punches holes in the recording. With it the dataset
  retains every tick, paying at most a one or two tick visual lag
  during loss.

Recording is driven by ``on_action``. One executed action gives one
dataset row, paired with the observation the operator was responding
to. The pairing uses the right clock for each operator:

* Policy actions carry ``in_reply_to_ts_us`` (a robot-clock capture
  time naming the exact obs consumed), matched against the obs capture
  timestamp — same clock, so the pairing is exact.
* Leader actions are open-loop: the human reacts to whatever obs they
  were *looking at*, i.e. the most recently *received* one. They are
  matched on the action's teleop send time vs each obs's local receive
  time (both teleop clock). Comparing the leader's teleop send time
  against the robot-clock capture timestamp instead would fold in the
  robot/teleop clock offset plus the full transport latency, inflating
  every gap and tripping the threshold even when nothing is misaligned.

The ``PORTAL_HITL_MAX_OBS_AGE_MS`` threshold (default 100ms) then
measures only how stale the operator's view actually was, dropping (and
counting) a row only when the obs stream the operator was watching has
genuinely stalled.

Hotkeys:
  c: cycle the active operator through ``[self, *remote_operators]``,
     wrapping back to self.
  r: toggle episode recording.
  [: discard the in-flight episode.
  x: clean quit.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import time
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
from hitl_recorder import HitlRecorder
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


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    load_env()

    url = required_env("LIVEKIT_URL")
    room = required_env("LIVEKIT_ROOM")
    token = mint_token(IDENTITY, room)

    cfg = OperatorConfig.from_yaml_file(CONFIG_PATH, room)
    fps = env_int("PORTAL_FPS", 30)
    max_obs_age_us = env_int("PORTAL_HITL_MAX_OBS_AGE_MS", 100) * 1000

    leader = build_leader()
    op = Operator(cfg)

    state_field_names = [f.name for f in cfg.state_schema]
    action_field_names = [f.name for f in cfg.action_schema]

    # Latest synced observation for the rerun mirror. Written by Portal
    # callbacks (already on the asyncio loop) and read on the main tick.
    # One-writer / one-reader.
    latest_obs: Optional[Observation] = None
    latest_state: dict = {}
    latest_frames: dict[str, np.ndarray] = {}
    latest_executed: Optional[Action] = None

    # The recording pipeline — obs ring buffer, action↔obs alignment, the
    # stale-obs guard, and the lazily-built dataset recorder — all live in
    # HitlRecorder. The callbacks below just feed it.
    hitl = HitlRecorder(
        fps=fps,
        state_field_names=state_field_names,
        action_field_names=action_field_names,
        cameras=CAMERAS,
        max_obs_age_us=max_obs_age_us,
        repo_id=env_str("PORTAL_HITL_DATASET_REPO_ID", "local/portal-hitl"),
        task=env_str("PORTAL_HITL_DATASET_TASK", "reBot B601 manipulation"),
        root=os.environ.get("PORTAL_HITL_DATASET_ROOT") or None,
    )

    def on_observation(obs: Observation) -> None:
        nonlocal latest_obs, latest_state
        latest_obs = obs
        latest_state = dict(obs.state)
        for cam in CAMERAS:
            f = obs.frames.get(cam)
            if f is not None:
                latest_frames[cam] = frame_bytes_to_numpy_rgb(f.data, f.width, f.height)
        hitl.observe(obs)

    def on_action(action: Action) -> None:
        # The action stream drives the recorder. One action = one row,
        # paired with the obs the operator was responding to.
        nonlocal latest_executed
        latest_executed = action
        hitl.record(action)

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
            # Hotkeys.
            for key in hotkeys.pop_pressed():
                if key == "c":
                    await cycle_active_operator(op, me)
                elif key == "r":
                    if not hitl.is_ready:
                        print("[teleop] waiting for first observation before recording")
                    elif hitl.is_recording:
                        hitl.end_episode()
                        print(f"[teleop] episode {hitl.episode_count - 1} saving in background")
                    else:
                        hitl.start_episode()
                        print(f"[teleop] episode {hitl.episode_count} recording")
                elif key == "[":
                    if hitl.is_ready:
                        hitl.discard_episode()
                        print("[teleop] episode discarded")
                elif key == "x":
                    quit_requested = True
            if quit_requested:
                break

            # Drive the wire. Always send, even when not active; the
            # robot's gate drops mismatched-sender actions, and keeping
            # the leader in sync means takeover from a policy is instant.
            op.send_action(leader.get_action(), timestamp_us=int(time.time() * 1_000_000))

            # Lazy-build the recorder once we know the wire camera
            # resolution (the YAML doesn't pin width/height).
            if not hitl.is_ready and len(latest_frames) >= len(CAMERAS):
                if hitl.ensure_recorder(latest_frames[CAMERAS[0]]):
                    print(f"[teleop] recorder ready (episodes so far={hitl.episode_count})")

            # Mirror to rerun. Reads the latest snapshots; rerun is a
            # visualization, not the dataset, so duplicate frames at
            # tick rate don't matter.
            log_observation(latest_obs, latest_state, latest_frames)
            log_action(latest_executed, me)
            log_recording(hitl)
            if tick % fps == 0 and (m := op.metrics()) is not None:
                log_metrics(m)

            # Periodic skip surface: every 5s, report and reset.
            if tick % (fps * 5) == 0 and hitl.skipped_frames > 0:
                n = hitl.drain_skips()
                print(f"[teleop] skipped {n} frames in last 5s (obs older than {max_obs_age_us // 1000}ms)")

    except KeyboardInterrupt:
        print("\n[teleop] stopping ...")
    finally:
        hotkeys.stop()
        try:
            hitl.finalize()
        except Exception:
            logger.exception("recorder.finalize failed")
        try:
            await op.disconnect()
        finally:
            op.close()
            leader.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
