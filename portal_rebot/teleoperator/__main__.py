"""Operator side: reBot 102 leader -> Portal actions, and a window to fly by.

Run with `uv run teleoperator`. Each tick reads one action off the leader and
sends it; nothing is recorded here. `uv run recorder` is an observer in the
same room that writes the archive, and this process only marks where the
takes are, with keypoints, when a hotkey is pressed.

Every action declares the frame it answers in `in_reply_to_ts_us` -- a FRAME
timestamp, because the operator flies on the video, and it is generally a tick
or two behind the state on the wire. See docs/architecture.md#the-anchor-is-the-whole-story.

Hotkeys: `r` toggle recording, `[` end and mark the take discarded, `x` quit.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from typing import Optional

from livekit.portal import Operator, OperatorConfig, VideoFrameData, frame_bytes_to_numpy_rgb

from portal_rebot.common import (
    CONFIG_PATH,
    TELEOPERATOR_IDENTITY,
    HardwareThread,
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


# The keypoint types `livekit-portal convert` cuts episodes at by default.
RECORDING, IDLE, DISCARD = "recording", "idle", "discard"


async def send_keypoint(op: Operator, kind: str, payload: dict, at_ts_us: Optional[int]) -> bool:
    """Mark a boundary on the recorder's timeline. True when an observer got it.

    Filed at the frame the operator is looking at, so the boundary lands where
    they saw it. Loud on failure -- a keypoint that went nowhere means the
    operator is demonstrating into the void -- and never raising, because
    losing a boundary costs one demonstration while taking the loop down
    mid-motion costs the arm.
    """
    try:
        if await op.send_keypoint(kind, payload, timestamp_us=at_ts_us):
            return True
        print("[teleop] NOT RECORDED: no recorder in the room")
    except Exception as exc:
        print(f"[teleop] NOT RECORDED: the keypoint did not send: {exc}")
    return False


class Tally:
    """How many episodes have been recorded, per task.

    Counted when a recorder RECEIVED the closing keypoint, not when the key is
    pressed: a take the recorder never heard about is not an episode.
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
                preview, tally: "Tally") -> tuple[int, str]:
    """Handle a `1`-`9` press. Returns the (index, description) to use from now on.

    Refused while a take is in flight, so every recorded span is single-task.
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
    return index, tasks[index]


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


def clock_report(op: Operator) -> str:
    sync = op.metrics().time_sync
    if not sync.synced:
        return "clock unsynced"
    line = f"clock {sync.offset_us / 1e3:+.0f}ms"
    if sync.uncertainty_us is not None:
        line += f" (±{sync.uncertainty_us / 1e3:.0f}ms)"
    return line


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    load_env()

    url = required_env("LIVEKIT_URL")
    room = required_env("LIVEKIT_ROOM")
    cfg = OperatorConfig.from_yaml_file(CONFIG_PATH, room)
    fps = env_int("PORTAL_FPS", cfg.fps)
    cameras = list(cfg.video_tracks)

    tasks = prompt_tasks()
    active = 0
    task = tasks[active]
    leader = build_leader()
    op = Operator(cfg)
    hardware = HardwareThread("leader")
    preview = Preview(room, cameras)
    view = View(cameras)

    def on_video_frame(name: str, frame: VideoFrameData) -> None:
        view.saw(name, frame)
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

    def mark(kind: str, then=None) -> None:
        """Fire and forget: the leader arm cannot wait on the send. The task is
        held so the loop cannot collect it mid-flight, and `then` runs only if
        a recorder got the keypoint."""
        payload = {"task_description": task}
        at_ts_us = view.anchor()

        async def deliver() -> None:
            if await send_keypoint(op, kind, payload, at_ts_us) and then is not None:
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
                        mark(RECORDING)
                    else:
                        # Counted in the callback, so the tally only moves on a
                        # take the recorder confirmed it heard about.
                        mark(IDLE, then=lambda n=active: tally.record(n))
                elif key.isdigit():
                    active, task = select_task(
                        tasks, active, key, recording, preview, tally)
                elif key == "[":
                    # Say so rather than no-op: a silent key reads as a dead
                    # keyboard.
                    if not recording:
                        print("[teleop] nothing being recorded to discard")
                    else:
                        recording = False
                        print("[teleop] discarded")
                        mark(DISCARD)
            if stop:
                break

            action = await hardware.run(leader.get_action)
            # Portal stamps it on the robot's clock; the anchor says which
            # frame it answered.
            op.send_action(action, in_reply_to_ts_us=view.anchor())

            if tick and tick % (fps * 5) == 0:
                print(f"[teleop] {view.report()}, {clock_report(op)}"
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
            await send_keypoint(op, DISCARD, {"task_description": task}, view.anchor())
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
