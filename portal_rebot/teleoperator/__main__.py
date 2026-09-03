"""Operator side: reBot 102 leader -> Portal actions, and a window to fly by.

Run with `uv run teleoperator`. Each tick reads one action off the leader and
sends it; nothing is recorded here. `uv run recorder` is a second operator in
the same room that writes the archive, and this process only tells it where the
boundaries are, by RPC, when a hotkey is pressed.

Every action declares the frame it answers in `in_reply_to_ts_us` -- a FRAME
timestamp, because the operator flies on the video, and it is generally a tick
or two behind the state on the wire. See docs/architecture.md#the-anchor-is-the-whole-story.

Hotkeys: `r` toggle recording, `[` end and mark the take discarded, `x` quit.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from typing import Optional

from livekit.portal import Operator, OperatorConfig, VideoFrameData, frame_bytes_to_numpy_rgb

from portal_rebot import keypoints
from portal_rebot.common import (
    CONFIG_PATH,
    RECORDER_IDENTITY,
    TELEOPERATOR_IDENTITY,
    HardwareThread,
    all_video_tracks,
    env_int,
    load_env,
    mint_token,
    pace,
    required_env,
)
from portal_rebot.teleoperator.hotkeys import Hotkeys, input_backend_warning
from portal_rebot.teleoperator.leader import build_leader
from portal_rebot.teleoperator.preview import Preview
from portal_rebot.teleoperator.session import prompt_tasks

logger = logging.getLogger(__name__)


async def maintain_control(op: Operator, me: str, period: float = 1.0) -> None:
    """Keep the robot's action gate pointed at us, forever.

    Claiming is not a one-shot on connect: `set_active_operator` is an RPC *to
    the robot*, nothing orders the two processes, the robot clears the pointer
    when we leave the room, and a robot restart resets it to None. Polling
    covers all of that.

    Nothing here may raise -- a claim that cannot land yet is the normal state
    before the robot joins -- but it is reported once per distinct situation,
    because an unclaimed gate looks exactly like a dead leader arm from the
    operator's chair.
    """
    last_report: Optional[str] = None

    def report(message: str) -> None:
        nonlocal last_report
        if message != last_report:
            print(f"[teleop] {message}")
            last_report = message

    while True:
        try:
            if (robot := op.robot_identity()) is None:
                report("no robot in the room yet; actions we send are dropped")
            elif op.active_operator() != me:
                await op.set_active_operator(me)
                last_report = None
                print(f"[teleop] claimed control as '{me}' on robot '{robot}'")
        except Exception as exc:
            report(f"could not claim control, retrying: {exc}")
        await asyncio.sleep(period)


async def send_keypoint(op: Operator, payload: dict) -> bool:
    """Mark a boundary on the recorder's timeline. True when it landed.

    Addressed to the recorder by name: Portal routes an operator's RPCs to the
    robot by default. Silent on success, loud on failure -- a keypoint that
    went nowhere means the operator is demonstrating into the void -- and never
    raising, because losing a boundary costs one demonstration while taking the
    loop down mid-motion costs the arm.
    """
    if RECORDER_IDENTITY not in op.operators():
        print(f"[teleop] NOT RECORDED: no '{RECORDER_IDENTITY}' in the room")
        return False
    try:
        await op.perform_rpc(
            keypoints.METHOD, json.dumps(payload), destination=RECORDER_IDENTITY)
        return True
    except Exception as exc:
        print(f"[teleop] NOT RECORDED: the recorder refused the keypoint: {exc}")
        return False


class Tally:
    """How many episodes have been recorded, per task.

    Counted when the recorder ACKNOWLEDGES the closing keypoint, not when the
    key is pressed: a take the recorder never heard about is not an episode.
    Discarded takes are not counted either.
    """

    def __init__(self, tasks: list[str]) -> None:
        self._tasks = tasks
        self._counts = [0] * len(tasks)

    def record(self, index: int) -> None:
        self._counts[index] += 1
        print(f"[teleop] recorded -- {self.line(index)}")

    def count(self, index: int) -> int:
        return self._counts[index]

    def line(self, index: int) -> str:
        count = self._counts[index]
        return f"{count} episode{'' if count == 1 else 's'} of {self._tasks[index]!r}"

    def summary(self) -> list[str]:
        return [f"  {n + 1}  {self.line(n)}" for n in range(len(self._tasks))]


def select_task(tasks: list[str], active: int, key: str, recording: bool,
                preview, mark, tally: "Tally") -> tuple[int, str]:
    """Handle a `1`-`9` press. Returns the (index, description) to use from now on.

    Refused while a take is in flight, so every recorded span is single-task.
    The switch is marked even though nothing is recording: the keypoint is what
    moves the recorder's `state/task` band.
    """
    index = int(key) - 1
    if not 0 <= index < len(tasks):
        print(f"[teleop] no task {key}; this session has {len(tasks)}")
        return active, tasks[active]
    if index == active:
        return active, tasks[active]
    if recording:
        print(f"[teleop] recording; press r to end the take before switching to {key}")
        return active, tasks[active]

    print(f"[teleop] task {key}: {tally.line(index)} so far")
    preview.show_task(tasks, index)
    mark(keypoints.IDLE, task=tasks[index])
    return index, tasks[index]


class Clock:
    """The robot's clock, as seen from here.

        offset = max(frame_ts - arrival) + rtt / 2

    The largest subtraction over the window is the frame that spent the least
    time in transit, and half the RTT covers what transit is left. No drift
    term and no filter: the window slides. Two guards, both from probed
    failures -- the estimate is KEPT when the window empties (a video freeze is
    no news about the clock, not news that it is zero), and a sample implying a
    jump is not believed unless it keeps coming, at which point the robot's
    clock really did move. See docs/architecture.md#the-clock.
    """

    # Long enough to catch a genuinely uncongested frame, short enough that a
    # drifting clock is followed rather than averaged away.
    WINDOW_US = 10_000_000
    # No clock moves this far between two frames. Anything that says otherwise
    # is a bad timestamp, not evidence.
    JUMP_LIMIT_US = 1_000_000
    # ...unless it keeps saying so, at which point it is the truth. ~1s at 30fps.
    RESYNC_AFTER = 30

    def __init__(self) -> None:
        self._samples: deque[tuple[int, int]] = deque()
        self._offset_us = 0
        self._last_stamp = 0
        self._rejected = 0
        self.rtt_us = 0
        self.synced = False
        self.resyncs = 0
        self.rejected_total = 0

    def saw(self, frame_ts_us: int) -> None:
        arrival_us = int(time.time() * 1_000_000)
        delta = frame_ts_us - arrival_us
        if self.synced and abs(delta + self.rtt_us // 2 - self._offset_us) > self.JUMP_LIMIT_US:
            self._rejected += 1
            self.rejected_total += 1
            if self._rejected < self.RESYNC_AFTER:
                return
            # It kept happening: the robot's clock really did move. Start over,
            # and let the stamp follow it back down -- `robot_time` jumped too,
            # so the archive has an honest discontinuity either way.
            self._resync()
        self._rejected = 0

        self._samples.append((arrival_us, delta))
        while self._samples and self._samples[0][0] < arrival_us - self.WINDOW_US:
            self._samples.popleft()
        self._offset_us = max(d for _, d in self._samples) + self.rtt_us // 2
        self.synced = True

    def _resync(self) -> None:
        self.resyncs += 1
        self._samples.clear()
        self._last_stamp = 0
        self.synced = False

    @property
    def offset_us(self) -> int:
        """Robot clock minus this one. Zero until the first frame, which just
        means trusting the two wall clocks."""
        return self._offset_us

    def now_us(self) -> int:
        """Now, on the robot's clock. Never repeats and never goes backwards:
        two actions on the same microsecond would collapse into one archive row,
        the exact loss `action_time` exists to prevent. Only a resync clears it,
        because only a resync means the clock itself moved.
        """
        stamped = int(time.time() * 1_000_000) + self._offset_us
        self._last_stamp = max(stamped, self._last_stamp + 1)
        return self._last_stamp

    def report(self) -> str:
        if not self.synced:
            return "clock unsynced"
        line = (f"clock {self._offset_us / 1e3:+.0f}ms "
                f"(rtt {self.rtt_us / 1e3:.0f}ms, {len(self._samples)} samples)")
        if self.rejected_total:
            line += f", {self.rejected_total} bad stamps"
        if self.resyncs:
            line += f", {self.resyncs} resyncs"
        return line


class View:
    """What the operator is looking at, and how far behind it is.

    `anchor()` is the timestamp every action and every keypoint is filed
    against: the OLDEST of the newest per-camera timestamps, withheld until
    every camera has produced. It must name an instant at which all of what the
    operator sees was current, and with one view still blank there is none.
    """

    def __init__(self, cameras: list[str]) -> None:
        self._cameras = cameras
        self._newest: dict[str, int] = {}
        self._received: deque[float] = deque(maxlen=64)

    def saw(self, camera: str, frame: VideoFrameData) -> None:
        self._newest[camera] = frame.timestamp_us
        self._received.append(time.perf_counter())

    def anchor(self) -> Optional[int]:
        if len(self._newest) < len(self._cameras):
            return None
        return min(self._newest.values())

    def report(self) -> str:
        """Age and rate on this machine's clock alone, so no offset between the
        two hosts leaks in."""
        if not self._received:
            return "no frames yet"
        age_ms = (time.perf_counter() - self._received[-1]) * 1e3
        span = self._received[-1] - self._received[0]
        fps = (len(self._received) - 1) / span if span > 0 else 0.0
        return f"view {age_ms:.0f}ms old, {fps:.1f} fps in"


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    load_env()

    url = required_env("LIVEKIT_URL")
    room = required_env("LIVEKIT_ROOM")
    cfg = OperatorConfig.from_yaml_file(CONFIG_PATH, room)
    fps = env_int("PORTAL_FPS", cfg.fps)
    cameras = all_video_tracks(cfg)

    tasks = prompt_tasks()
    active = 0
    task = tasks[active]
    leader = build_leader()
    op = Operator(cfg)
    hardware = HardwareThread("leader")
    preview = Preview(room, cameras)
    view = View(cameras)
    clock = Clock()

    def on_video_frame(name: str, frame: VideoFrameData) -> None:
        view.saw(name, frame)
        clock.saw(frame.timestamp_us)
        preview.show(name, frame_bytes_to_numpy_rgb(frame.data, frame.width, frame.height))

    for cam in cameras:
        op.on_video_frame(cam, on_video_frame)
    op.on_active_operator_changed(lambda i: print(f"[teleop] active operator now: {i}"))

    leader.connect()
    print(f"[teleop] connecting to {url} as '{TELEOPERATOR_IDENTITY}' in room '{room}' ...")
    await op.connect(url, mint_token(TELEOPERATOR_IDENTITY, room))
    me = op.local_identity()

    # Single-operator rig: hold the gate for the whole session. Until the
    # pointer names us the robot drops everything we send.
    claim = asyncio.create_task(maintain_control(op, me))
    marks: set[asyncio.Task] = set()

    def mark(state: str, then=None, **extra) -> None:
        """Fire and forget: a keypoint is a round trip to another peer and the
        leader arm cannot wait on it. The task is held so the loop cannot
        collect it mid-flight, `then` runs only if the keypoint landed, and
        `**extra` lands last so a caller can override `task` -- which the task
        switch does, marking the new one before `task` itself has moved.
        """
        # The clock estimate rides along so the archive records what the
        # operator believed the offset was, not just what it produced.
        payload = {"state": state, "task": task, "at_ts_us": view.anchor(),
                   "clock_offset_us": clock.offset_us, "rtt_us": clock.rtt_us, **extra}

        async def deliver() -> None:
            if await send_keypoint(op, payload) and then is not None:
                then()

        held = asyncio.create_task(deliver())
        marks.add(held)
        held.add_done_callback(marks.discard)

    print(f"[teleop] connected as '{me}' @ {fps} fps")
    for number, name in enumerate(tasks, start=1):
        print(f"[teleop]   {'>' if number == active + 1 else ' '} {number}  {name}")
    switch = f"  1-{len(tasks)}=switch task" if len(tasks) > 1 else ""
    print(f"[teleop] hotkeys: r=record  [=discard the take  x=quit{switch}")
    preview.show_task(tasks, active)

    tally = Tally(tasks)
    hotkeys = Hotkeys(global_tap=env_int("PORTAL_HOTKEYS_GLOBAL", 1) != 0)
    hotkeys.start()
    if (why := input_backend_warning()) is not None:
        where = ("press them in THIS terminal" if hotkeys.terminal_active
                 else "and there is no terminal to fall back on")
        print(f"[teleop] global hotkeys will not fire ({why}); {where}")
    elif not hotkeys.terminal_active:
        print("[teleop] no terminal to read keys from; the global tap is the only one")

    recording = False
    try:
        async for tick in pace(fps):
            stop = False
            for key in hotkeys.drain():
                if key == "x":
                    stop = True
                elif key == "r":
                    recording = not recording
                    if recording:
                        print(f"[teleop] recording {task!r}")
                        mark(keypoints.RECORDING)
                    else:
                        # Counted in the callback, so the tally only moves on a
                        # take the recorder confirmed it heard about.
                        mark(keypoints.IDLE, then=lambda n=active: tally.record(n))
                elif key.isdigit():
                    active, task = select_task(
                        tasks, active, key, recording, preview, mark, tally)
                elif key == "[":
                    # Say so rather than no-op: a silent key reads as a dead
                    # keyboard.
                    if not recording:
                        print("[teleop] nothing being recorded to discard")
                    else:
                        recording = False
                        print("[teleop] discarded")
                        mark(keypoints.IDLE, discard=True)
            if stop:
                break

            action = await hardware.run(leader.get_action)
            # Stamped on the ROBOT's clock, so the archive can say when this
            # action happened rather than only which frame it answered.
            op.send_action(
                action,
                timestamp_us=clock.now_us(),
                in_reply_to_ts_us=view.anchor(),
            )

            if tick % fps == 0 and (metrics := op.metrics()) is not None:
                clock.rtt_us = metrics.rtt.rtt_us_mean or 0
            if tick and tick % (fps * 5) == 0:
                print(f"[teleop] {view.report()}, {clock.report()}"
                      + (f", task {active + 1}" if len(tasks) > 1 else ""))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[teleop] stopping ...")
    finally:
        hotkeys.stop()
        print("[teleop] this session:")
        for line in tally.summary():
            print(f"[teleop] {line}")
        if recording:
            # The recorder is still writing; leave the band closed rather than
            # ending the archive in the middle of a take.
            await send_keypoint(op, {"state": keypoints.IDLE, "task": task,
                                     "at_ts_us": view.anchor(), "discard": True})
        preview.close()
        claim.cancel()
        try:
            await op.disconnect()
        finally:
            op.close()
            hardware.close()
            leader.disconnect()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
