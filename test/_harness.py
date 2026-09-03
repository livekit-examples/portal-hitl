"""Shared rig for the test scripts: synthetic payloads, the robot subprocess, asserts.

Every robot tick stamps its own index into both the state and the frame, and
the operator stamps the tick it decoded out of the frame it was looking at
into the action it sends back. The recorder files that action at the frame's
own capture timestamp, so in the archive all three land on ONE `robot_time`
row, and a correctly aligned row satisfies, with no tolerance:

    /observation/shoulder_pan.pos == /action/elbow_flex.pos   (mod TICK_WRAP)

The modulo is the bar wrapping: state carries the raw tick, the frame can only
carry TICK_WRAP of them before the bar runs off the right edge.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import numpy as np

from test import ROOT
from portal_rebot.common import CONFIG_PATH

FPS = 30

TICK_STATE_IDX = 0    # shoulder_pan.pos carries the robot's tick
SEEN_ACTION_IDX = 2   # elbow_flex.pos carries the tick the operator saw

TICK_STATE_ENTITY = "/observation/shoulder_pan.pos"
SEEN_ACTION_ENTITY = "/action/elbow_flex.pos"

FRAME_W, FRAME_H = 640, 480

# A tick is encoded as the COLUMN POSITION of a white bar, not as a brightness.
# A flat grey level cannot tell a real frame from a half-initialised one -- a
# startup frame of uniform grey decodes as a perfectly plausible tick.
BAR_STEP, BAR_WIDTH = 3, 12
TICK_WRAP = 200

WEBRTC_CODECS = {"h264", "vp8", "vp9", "av1", "h265"}


def fresh_room(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def config_yaml(codec: str | None = None) -> str:
    """`portal.yaml` verbatim, or with every video codec swapped out.

    `PORTAL_TEST_CODEC=mjpeg` moves video onto the data channel, which
    isolates a WebRTC media problem from a Portal one.
    """
    text = CONFIG_PATH.read_text()
    codec = codec or os.environ.get("PORTAL_TEST_CODEC") or ""
    if not codec:
        return text
    text = re.sub(r"codec:\s*\w+", f"codec: {codec}", text)
    if codec.lower() not in WEBRTC_CODECS:
        # The loader rejects these keys outright on a non-WebRTC codec.
        for key in ("max_bitrate_kbps", "simulcast", "screencast"):
            text = re.sub(rf",\s*{key}:\s*\w+", "", text)
    return text


def state_for_tick(tick: int, fields: list[str]) -> dict[str, float]:
    return {name: float(tick + i * 1000) for i, name in enumerate(fields)}


def frame_for_tick(tick: int, step: int = BAR_STEP, width: int = BAR_WIDTH,
                   size: tuple[int, int] = (FRAME_H, FRAME_W)) -> np.ndarray:
    frame = np.zeros((*size, 3), np.uint8)
    x = tick * step
    frame[:, x:x + width] = 255
    return frame


def tick_from_frame(frame, step: int = BAR_STEP, width: int = BAR_WIDTH,
                    ref_width: int = FRAME_W) -> float | None:
    """Recover the tick a frame was built from, or None if no bar resolves.

    Fractional, so a caller can measure a sub-tick offset. Robust to codec
    luma shifts and to partially decoded frames: a real frame has one bright,
    narrow column band, and anything else returns None rather than a
    plausible-looking lie.
    """
    a = np.asarray(frame)
    if a.ndim == 3 and a.shape[0] == 3:
        a = np.transpose(a, (1, 2, 0))
    if a.dtype != np.uint8:
        a = a * 255.0 if a.max() <= 1.0 else a
    # Normalise by the frame's ACTUAL width: the transport may deliver a
    # different resolution than we published (WebRTC rescales), and a bar read
    # in delivered-pixel coordinates would decode as a proportional -- and very
    # convincing -- temporal lag.
    scale = a.shape[1] / ref_width
    cols = a.mean(axis=(0, 2)).astype(np.float64)
    peak = float(cols.max())
    if peak < 60:
        return None
    lit = np.where(cols > peak * 0.5)[0]
    if lit.size == 0 or lit.size > width * 3 * max(scale, 1.0):
        return None
    return (float(lit.mean()) / scale - (width - 1) / 2.0) / step


def read_archive(path, index: str | None = "robot_time") -> "pyarrow.Table":
    """Read a session `.rrd` back, one row per `robot_time`.

    Needs `datafusion`, which is why it is in the `dev` dependency group:
    rerun dropped the lighter `rerun.dataframe` reader in 0.34, so the only
    way back in is a local server over the file.
    """
    import rerun as rr

    with rr.server.Server(datasets={"session": [str(path)]}) as server:
        client = server.client()
        dataset = client.get_dataset("session")
        return dataset.reader(index=index).to_arrow_table()


def column(table, entity: str, component: str | None = None) -> list:
    """One entity's values in `robot_time` order, `None` where it logged
    nothing on that row. Column names carry the archetype and component
    (`/action/x:Scalars:scalars`), so entities are matched by prefix.

    Scalars come back wrapped in a list -- one row may hold several -- and this
    unwraps the single-value case every entity here logs.
    """
    prefix = f"/{entity.lstrip('/')}:{component or ''}"
    hits = [name for name in table.column_names if name.startswith(prefix)]
    if not hits:
        return []
    return [v[0] if isinstance(v, list) and len(v) == 1 else v
            for v in table.column(hits[0]).to_pylist()]


def bands(table, entity: str) -> list[str]:
    """A state lane's values in order, one per transition."""
    return [v for v in column(table, entity, "StateChange") if v is not None]


def aligned_rows(table) -> list[tuple[int, int]]:
    """(robot tick, tick the operator saw) for every row carrying both.

    Rows with only one of the two are the honest gaps -- a state whose frame
    was dropped, an action logged before the first frame resolved -- and say
    nothing about alignment either way.
    """
    states = column(table, TICK_STATE_ENTITY)
    seen = column(table, SEEN_ACTION_ENTITY)
    return [(int(round(s)), int(round(a)))
            for s, a in zip(states, seen)
            if s is not None and a is not None and a >= 0]


def pairing_mismatches(table) -> list[tuple[int, int]]:
    """Rows whose action is filed against an observation that did not produce
    it. The check the whole split exists to make possible."""
    return [(state, seen) for state, seen in aligned_rows(table)
            if state % TICK_WRAP != seen]


class RobotProcess:
    """Runs `test.peer_robot` as a subprocess for the duration of a test.

    Publisher and subscriber must not share a process -- see the module
    docstring of `test/peer_robot.py`. `summary` holds the child's counters
    after exit.
    """

    def __init__(self, room: str, ticks: int = 600, impairment=None, codec: str | None = None):
        self.room, self.ticks = room, ticks
        self.impairment = impairment
        self.codec = codec or os.environ.get("PORTAL_TEST_CODEC") or ""
        self.proc: subprocess.Popen | None = None
        self.summary: dict = {}

    def __enter__(self) -> "RobotProcess":
        env = dict(os.environ, TEST_ROOM=self.room, TEST_TICKS=str(self.ticks))
        if self.impairment is not None:
            env["TEST_IMPAIRMENT"] = json.dumps(dataclasses.asdict(self.impairment))
        if self.codec:
            env["PORTAL_TEST_CODEC"] = self.codec
        env.setdefault("RUST_LOG", "error")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "test.peer_robot"],
            cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        for line in self.proc.stdout:
            if line.strip() == "READY":
                return self
        raise RuntimeError("robot process exited before signalling READY")

    def __exit__(self, *exc) -> None:
        if self.proc is None:
            return
        self.proc.terminate()          # peer_robot cancels and prints SUMMARY
        try:
            out, _ = self.proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, _ = self.proc.communicate()
        for line in (out or "").splitlines():
            if line.startswith("SUMMARY "):
                self.summary = json.loads(line[len("SUMMARY "):])


class Checks:
    """Collects pass/fail lines so one run reports everything, not just the
    first failure. `exit_code` prints the summary and is what the script returns."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.failures = 0
        self.checks = 0
        print(f"\n=== {title} ===")

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        self.checks += 1
        self.failures += not ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        return ok

    def info(self, label: str, detail: str = "") -> None:
        print(f"   ---  {label}" + (f"  {detail}" if detail else ""))

    @property
    def exit_code(self) -> int:
        print(f"--- {self.title}: {self.checks - self.failures}/{self.checks} passed"
              + ("" if not self.failures else f", {self.failures} FAILED"))
        return 1 if self.failures else 0
