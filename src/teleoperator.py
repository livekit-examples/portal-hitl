"""Operator side: reBot 102 leader -> Portal actions, and a window to fly by.

Run with `uv run src/teleoperator.py`. Each tick reads one action off the leader
and sends it. Nothing is recorded here: `src/recorder.py` writes the archive, and
this process marks where takes start and end with keypoints.

Every action names the frame it answers in `in_reply_to_ts_us`: the operator
flies on video, which is usually a tick or two behind the state.
See tutorial/03-operator-loop.md.

Hotkeys: `c` cycle control, `r` toggle recording, `[` discard the take, `x` quit,
`1`-`9` switch task.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Optional

from _common import CONFIG_PATH, TELEOPERATOR_IDENTITY, HardwareThread, env_int, load_env, mint_token, pace, required_env
from livekit.portal import Operator, OperatorConfig, VideoFrameData, frame_bytes_to_numpy_rgb
from utils.hotkeys import Hotkeys, input_backend_warning
from utils.rebot import build_leader
from utils.rerun_view import Preview
from utils.session import prompt_tasks

# The keypoint types `livekit-portal convert` cuts episodes at by default.
RECORDING, IDLE, DISCARD = "recording", "idle", "discard"


async def cycle_active_operator(op: Operator, me: str) -> None:
    """Claim control, or pass it to the next connected operator."""
    ring = [me, *sorted(op.operators())]
    current = op.active_operator()
    target = ring[(ring.index(current) + 1) % len(ring)] if current in ring else me
    try:
        await op.set_active_operator(target)
        print(f"[teleop] active operator → {target}")
    except Exception as exc:
        print(f"[teleop] could not change control: {exc}; press c to retry")


async def send_keypoint(op: Operator, kind: str, task: str, at_ts_us: Optional[int]) -> bool:
    """Mark a take boundary at the frame the operator sees. True when a recorder got it.

    Never raises: a lost boundary costs one take, a crashed loop costs the arm."""
    try:
        if await op.send_keypoint(kind, {"task_description": task}, timestamp_us=at_ts_us):
            return True
        print("[teleop] NOT RECORDED: no recorder in the room")
    except Exception as exc:
        print(f"[teleop] NOT RECORDED: the keypoint did not send: {exc}")
    return False


class Tally:
    """Episodes recorded per task, counted only when a recorder confirmed the take."""

    def __init__(self, tasks: list[str]) -> None:
        self.tasks = tasks
        self.counts = [0] * len(tasks)

    def record(self, index: int) -> None:
        self.counts[index] += 1
        print(f"[teleop] recorded -- {self.line(index)}")

    def line(self, index: int) -> str:
        count = self.counts[index]
        return f"{count} episode{'' if count == 1 else 's'} of {self.tasks[index]!r}"


def select_task(tasks: list[str], active: int, key: str, recording: bool) -> int:
    """The task index after pressing digit `key`. Refused mid-take, so each take has one task."""
    index = int(key) - 1
    if not 0 <= index < len(tasks):
        print(f"[teleop] no task {key}; this session has {len(tasks)}")
        return active
    if recording and index != active:
        print(f"[teleop] recording; press r to end the take before switching to {key}")
        return active
    return index


class View:
    """What the operator is looking at.

    `anchor()` is the timestamp every action and keypoint is filed against: the
    oldest of the newest frame per camera, so everything on screen was current
    at that instant. None until every camera has produced a frame.
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
        """Frame age and rate, on this machine's clock only."""
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
    load_env()
    url = required_env("LIVEKIT_URL")
    room = required_env("LIVEKIT_ROOM")
    cfg = OperatorConfig.from_yaml_file(CONFIG_PATH, room)
    fps = env_int("PORTAL_FPS", cfg.fps)
    cameras = list(cfg.video_tracks)

    tasks = prompt_tasks()
    active = 0
    tally = Tally(tasks)
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
    op.on_active_operator_changed(lambda identity: print(f"[teleop] active operator now: {identity}"))

    leader.connect()
    print(f"[teleop] connecting to {url} as '{TELEOPERATOR_IDENTITY}' in room '{room}' ...")
    await op.connect(url, mint_token(TELEOPERATOR_IDENTITY, room))
    me = op.local_identity()

    in_flight: set[asyncio.Task] = set()

    def mark(kind: str, on_recorded=None) -> None:
        """Send a keypoint without stalling the leader arm. `on_recorded` runs once a recorder has it."""
        async def deliver(task: str, at_ts_us: Optional[int]) -> None:
            if await send_keypoint(op, kind, task, at_ts_us) and on_recorded:
                on_recorded()

        sending = asyncio.create_task(deliver(tasks[active], view.anchor()))
        in_flight.add(sending)  # hold a reference until it finishes
        sending.add_done_callback(in_flight.discard)

    print(f"[teleop] connected as '{me}' @ {fps} fps")
    for number, name in enumerate(tasks, start=1):
        print(f"[teleop]   {'>' if number == active + 1 else ' '} {number}  {name}")
    switch = f"  1-{len(tasks)}=switch task" if len(tasks) > 1 else ""
    print(f"[teleop] hotkeys: c=cycle operator  r=record  [=discard the take  x=quit{switch}")
    preview.show_task(tasks, active)

    hotkeys = Hotkeys(global_tap=env_int("PORTAL_HOTKEYS_GLOBAL", 1) != 0)
    hotkeys.start()
    if (why := input_backend_warning()) is not None:
        where = "press them in THIS terminal" if hotkeys.terminal_active else "and there is no terminal to fall back on"
        print(f"[teleop] global hotkeys will not fire ({why}); {where}")
    elif not hotkeys.terminal_active:
        print("[teleop] no terminal to read keys from; the global tap is the only one")

    recording = False
    try:
        async for tick in pace(fps):
            keys = hotkeys.drain()
            if "x" in keys:
                break
            for key in keys:
                if key == "c":
                    await cycle_active_operator(op, me)
                elif key == "r" and not recording:
                    recording = True
                    print(f"[teleop] recording {tasks[active]!r}")
                    mark(RECORDING)
                elif key == "r":
                    recording = False
                    mark(IDLE, on_recorded=lambda index=active: tally.record(index))
                elif key == "[" and not recording:
                    print("[teleop] nothing being recorded to discard")
                elif key == "[":
                    recording = False
                    print("[teleop] discarded")
                    mark(DISCARD)
                elif key.isdigit() and (index := select_task(tasks, active, key, recording)) != active:
                    active = index
                    print(f"[teleop] task {key}: {tally.line(active)} so far")
                    preview.show_task(tasks, active)

            action = await hardware.run(leader.get_action)
            op.send_action(action, in_reply_to_ts_us=view.anchor())

            if tick and tick % (fps * 5) == 0:
                print(f"[teleop] {view.report()}, {clock_report(op)}"
                      + (f", task {active + 1}" if len(tasks) > 1 else ""))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[teleop] stopping ...")
    finally:
        hotkeys.stop()
        print("[teleop] this session:")
        for index in range(len(tasks)):
            print(f"[teleop]   {index + 1}  {tally.line(index)}")
        if recording:
            # Close the take rather than leave the archive ending mid-take.
            await send_keypoint(op, DISCARD, tasks[active], view.anchor())
        preview.close()
        try:
            await op.disconnect()
        finally:
            op.close()
            hardware.close()
            leader.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
