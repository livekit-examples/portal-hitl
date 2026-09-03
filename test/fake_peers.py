"""Synthetic peers, driven by the test scripts.

`FakeRobot` stands in for `robot/__main__.py` minus the hardware,
`FakeOperator` for `teleoperator/__main__.py` minus the leader arm, and
`FakeRecorder` for `recorder/__main__.py` -- which needs no stubbing at all,
since it has no hardware: it drives the real `Archive` and the real
`add_keypoint` handler. Each exposes `run()` so a test can drive them as tasks
and inspect their counters afterwards.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
from livekit.portal import (
    Action,
    Observation,
    Operator,
    OperatorConfig,
    Robot,
    RobotConfig,
    State,
    VideoFrameData,
    frame_bytes_to_numpy_rgb,
)

from portal_rebot.common import RECORDER_IDENTITY, all_video_tracks, mint_token, pace
from portal_rebot.keypoints import METHOD as KEYPOINT_METHOD
from portal_rebot.teleoperator.__main__ import Clock, View
from test._harness import (
    SEEN_ACTION_IDX,
    TICK_STATE_IDX,
    TICK_WRAP,
    config_yaml,
    frame_for_tick,
    state_for_tick,
    tick_from_frame,
)


def all_tracks(cfg) -> list[str]:
    """The app's own track resolution, so a codec sweep exercises what ships."""
    return all_video_tracks(cfg)


@dataclass
class Impairment:
    """Sender-side degradation, applied by `FakeRobot` as it publishes.

    Models what goes wrong on a real rig -- an irregular control loop, a camera
    that skips, a link that stalls -- at the point where it originates, so
    Portal's synchronizer sees the irregular input it would in the field. It
    does NOT model delay or loss between the peers; that is `test/netsim.py`.
    """

    jitter_ms: float = 0.0
    drop_state: float = 0.0
    drop_frame: float = 0.0
    stall_every: int = 0
    stall_ms: float = 0.0
    seed: int = 1234

    def describe(self) -> str:
        bits = []
        if self.jitter_ms:
            bits.append(f"jitter+/-{self.jitter_ms:g}ms")
        if self.drop_state:
            bits.append(f"drop_state={self.drop_state:.0%}")
        if self.drop_frame:
            bits.append(f"drop_frame={self.drop_frame:.0%}")
        if self.stall_every:
            bits.append(f"stall {self.stall_ms:g}ms every {self.stall_every} ticks")
        return ", ".join(bits) or "clean"


class FakeRobot:
    """Publishes tick-stamped state and frames; records what it receives."""

    def __init__(self, room: str, url: str, fps: int = 30, identity: str = "robot",
                 impairment: "Impairment | None" = None) -> None:
        self.cfg = RobotConfig.from_yaml_str(config_yaml(), room)
        self.room, self.url, self.fps, self.identity = room, url, fps, identity
        self.portal = Robot(self.cfg)
        self.tracks = all_tracks(self.cfg)
        self.state_fields = [f.name for f in self.cfg.state_schema]
        self.impairment = impairment or Impairment()

        self.ticks_sent = 0
        self.states_published = 0
        self.frames_published = 0
        self.actions_received: list[dict[str, float]] = []
        self.gate_changes: list[Optional[str]] = []

        self.portal.on_action(self._on_action)
        self.portal.on_active_operator_changed(self.gate_changes.append)

    def _on_action(self, action: Action) -> None:
        self.actions_received.append({k: float(v) for k, v in action.values.items()})

    async def connect(self) -> None:
        await self.portal.connect(self.url, mint_token(self.identity, self.room))

    async def run(self, ticks: int) -> None:
        """Publish `ticks` ticks. State and every frame share one timestamp,
        exactly as the real robot loop does."""
        imp = self.impairment
        rng = random.Random(imp.seed)
        async for tick in pace(self.fps):
            if tick >= ticks:
                return
            if imp.stall_every and tick and tick % imp.stall_every == 0:
                await asyncio.sleep(imp.stall_ms / 1000.0)
            if imp.jitter_ms:
                await asyncio.sleep(rng.uniform(0, imp.jitter_ms) / 1000.0)

            ts_us = int(time.time() * 1_000_000)
            if rng.random() >= imp.drop_state:
                self.portal.send_state(
                    state_for_tick(tick, self.state_fields), timestamp_us=ts_us)
                self.states_published += 1
            if rng.random() >= imp.drop_frame:
                frame = frame_for_tick(tick % TICK_WRAP)
                for name in self.tracks:
                    self.portal.send_video_frame(name, frame, timestamp_us=ts_us)
                self.frames_published += 1
            self.ticks_sent = tick + 1

    async def close(self) -> None:
        await self.portal.disconnect()
        self.portal.close()


class FakeOperator:
    """Receives the robot's streams and sends actions anchored to what it saw.

    Two things travel with every action, and they must come from the SAME
    frame or the pairing check means nothing: `in_reply_to_ts_us` (the frame's
    robot capture timestamp, which is where the recorder files the action) and
    `SEEN_ACTION_IDX` (the robot tick decoded out of that frame's bar).

    The first comes from the shipped `View`, not from a copy of it -- so every
    run of every suite exercises the real anchor. Decoding the bar is the
    test's only addition, and it is what lets the archive be checked with no
    tolerance.

    Both receive paths are subscribed: the synchronized `Observation`, which is
    what `test_pubsub` judges the transport by, and the raw `on_video_frame`
    stream the shipped operator actually flies on.
    """

    def __init__(self, room: str, url: str, fps: int = 30, identity: str = "teleoperator") -> None:
        self.cfg = OperatorConfig.from_yaml_str(config_yaml(), room)
        self.room, self.url, self.fps, self.identity = room, url, fps, identity
        self.portal = Operator(self.cfg)
        self.tracks = all_tracks(self.cfg)
        self.action_fields = [f.name for f in self.cfg.action_schema]

        self.latest_obs: Optional[Observation] = None
        self.latest_state: Optional[State] = None
        self.view = View(self.tracks)
        # The shipped estimator, not a copy: every suite run then exercises the
        # clock that stamps actions in production.
        self.clock = Clock()
        # track -> (robot capture timestamp, decoded image). The timestamps
        # duplicate what `view` holds; the images are what `view` has no use
        # for and the bar decode needs.
        self.frames: dict[str, tuple[int, np.ndarray]] = {}
        self.observations = 0
        self.states = 0
        self.actions_sent = 0
        self.unanchored = 0

        self.portal.on_observation(self._on_observation)
        self.portal.on_state(self._on_state)
        for name in self.tracks:
            self.portal.on_video_frame(name, self._on_video_frame)

    def _on_state(self, state: State) -> None:
        self.latest_state = state
        self.states += 1

    def _on_video_frame(self, name: str, frame: VideoFrameData) -> None:
        self.view.saw(name, frame)
        self.clock.saw(frame.timestamp_us)
        self.frames[name] = (
            frame.timestamp_us,
            frame_bytes_to_numpy_rgb(frame.data, frame.width, frame.height),
        )

    def _on_observation(self, obs: Observation) -> None:
        self.latest_obs = obs
        self.observations += 1

    async def connect(self) -> None:
        await self.portal.connect(self.url, mint_token(self.identity, self.room))

    async def claim(self, timeout_s: float = 5.0) -> bool:
        """Claim the action gate, retrying until the robot is reachable.

        Same reasoning as `maintain_control` in the real operator: the RPC goes
        to the robot, so it fails until the robot has joined.
        """
        me = self.portal.local_identity()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if self.portal.robot_identity() is not None:
                    await self.portal.set_active_operator(me)
                    if self.portal.active_operator() == me:
                        return True
            except Exception:
                pass
            await asyncio.sleep(0.1)
        return False

    def anchored_view(self) -> Optional[tuple[int, int]]:
        """(capture timestamp, robot tick) of the frame the operator is flying
        on. The timestamp is the shipped `View.anchor()`; the tick is decoded
        from the frame that anchor names, so the two cannot drift apart."""
        if (anchor := self.view.anchor()) is None:
            return None
        held = next((img for ts, img in self.frames.values() if ts == anchor), None)
        if held is None:
            return None
        tick = tick_from_frame(held)
        return None if tick is None else (anchor, int(round(tick)))

    def build_action(self, tick: int, seen: Optional[int]) -> dict[str, float]:
        action = {name: float(tick + i * 100) for i, name in enumerate(self.action_fields)}
        action[self.action_fields[SEEN_ACTION_IDX]] = float(-1 if seen is None else seen)
        return action

    async def send_keypoint(self, **payload) -> str:
        """The teleoperator's `add_keypoint`, addressed the same way: by name,
        because Portal routes an operator's RPCs to the robot by default."""
        view = self.anchored_view()
        payload.setdefault("at_ts_us", None if view is None else view[0])
        return await self.portal.perform_rpc(
            KEYPOINT_METHOD, json.dumps(payload), destination=RECORDER_IDENTITY)

    async def run(self, ticks: int, on_tick=None) -> None:
        async for tick in pace(self.fps):
            if tick >= ticks:
                return
            view = self.anchored_view()
            if view is None:
                self.unanchored += 1
            anchor, seen = (None, None) if view is None else view
            action = self.build_action(tick, seen)
            if metrics := self.portal.metrics():
                self.clock.rtt_us = metrics.rtt.rtt_us_mean or 0
            ts_us = self.clock.now_us()
            self.portal.send_action(action, timestamp_us=ts_us, in_reply_to_ts_us=anchor)
            self.actions_sent += 1
            if on_tick is not None:
                on_tick(action, ts_us)

    async def close(self) -> None:
        await self.portal.disconnect()
        self.portal.close()


class FakeRecorder:
    """`recorder/__main__.py` with nothing stubbed out.

    It has no hardware to fake, so this is the shipped flow verbatim: the
    action subscription opt-in, the real `Archive`, and the real
    `add_keypoint` handler. What it adds is a `run()` a test can cancel and an
    `rrd_path` to read back.
    """

    def __init__(self, room: str, url: str, rrd_dir, jpeg_quality: int = 95,
                 identity: str = RECORDER_IDENTITY) -> None:
        from portal_rebot.recorder.__main__ import keypoint_handler
        from portal_rebot.recorder.archive import Archive

        self.cfg = OperatorConfig.from_yaml_str(config_yaml(), room)
        self.cfg.set_action_subscription(True)
        self.room, self.url, self.identity = room, url, identity
        self.portal = Operator(self.cfg)
        self.tracks = all_tracks(self.cfg)
        self.archive = Archive(room, self.tracks, rrd_dir=rrd_dir, jpeg_quality=jpeg_quality)

        self.portal.on_state(self.archive.log_state)
        for name in self.tracks:
            self.portal.on_video_frame(name, self.archive.log_frame)
        self.portal.on_action(self.archive.log_action)
        self.portal.register_rpc_method(KEYPOINT_METHOD, keypoint_handler(self.archive))

    @property
    def rrd_path(self):
        return self.archive.rrd_path

    async def connect(self) -> None:
        await self.portal.connect(self.url, mint_token(self.identity, self.room))

    async def close(self) -> None:
        """Disconnect first, so no callback can enqueue a frame the archive
        writer has already been told to drain."""
        await self.portal.disconnect()
        self.portal.close()
        self.archive.close()
