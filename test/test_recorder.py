"""Recorder test: does the archive line the three streams up?

    uv run python -m test.test_recorder

Runs all three peers over a real Portal link -- the robot as a subprocess, a
fake teleoperator, and the real recorder with nothing stubbed out -- then reads
the `.rrd` back with the reader `livekit-portal convert` uses.

The decisive check is alignment. Each row pairs an action with the state and
frame at exactly the timestamp it answered, and the robot tick in that state
must equal the tick the operator decoded out of the frame. A mismatch means the
action is paired with an observation that did not produce it, the exact failure
the split exists to prevent. It holds with no tolerance because the operator's
`in_reply_to_ts_us` and the tick it reports having seen come from the SAME
frame, and the robot published that frame under the same timestamp as the state
for that tick.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

from livekit.portal.convert import EpisodeRule

from test._harness import (
    FPS,
    SEEN_ACTION_FIELD,
    TICK_WRAP,
    Checks,
    RobotProcess,
    aligned_rows,
    archive_rows,
    fresh_room,
    pairing_mismatches,
    tick_from_frame,
)
from test.fake_peers import FakeOperator, FakeRecorder

WARMUP_TICKS = 45      # ~1.5s before the mark, so every stream is live
EPISODE_TICKS = 120    # ~4s inside the episode
TASK = "recorder selftest"


async def main() -> int:
    c = Checks("recorder archive")
    room = fresh_room("recorder")
    rrd_dir = Path(tempfile.mkdtemp(prefix="portal-test-rrd-"))
    c.info("room", room)

    url = os.environ["LIVEKIT_URL"]
    recorder = FakeRecorder(room, url, rrd_dir=rrd_dir)
    operator = FakeOperator(room, url, fps=FPS)

    with RobotProcess(room, ticks=WARMUP_TICKS + EPISODE_TICKS + FPS * 2) as robot:
        await recorder.connect()
        await operator.connect()
        await asyncio.sleep(1.0)
        c.check(await operator.claim(), "operator claims the gate")

        await operator.run(WARMUP_TICKS)
        c.check(operator.anchored_view() is not None,
                "operator has a frame to anchor against")

        heard = await operator.send_keypoint("recording", task_description=TASK)
        c.check(recorder.identity in heard, "the recorder received the keypoint", str(heard))
        await operator.run(EPISODE_TICKS)
        await operator.send_keypoint("idle", task_description=TASK)

        recording = recorder.portal.metrics().observer
        await operator.close()
        await recorder.close()
    c.info("robot process", str(robot.summary))
    c.info("recorder", str(recording))

    c.check(recording.frames_written > 0, "the recorder wrote frames",
            f"{recording.frames_written} written")
    c.check(recording.frames_dropped == 0, "no frame outran the writer",
            f"{recording.frames_dropped} dropped")

    path = Path(recorder.rrd_path)
    c.check(path.exists(), "rrd written", f"{path.stat().st_size / 1e6:.1f} MB")
    archive, rows = archive_rows(path)

    episodes = [(e.task, e.end_us > e.start_us) for e in archive.episodes(EpisodeRule())]
    c.check(episodes == [(TASK, True)], "one episode, bounded by the two keypoints", str(episodes))
    c.check({kp.sender for kp in archive.keypoints} == {operator.identity},
            "each keypoint names the operator that drew it")

    paired = aligned_rows(rows)
    c.check(len(paired) >= EPISODE_TICKS * 0.5, "actions paired with the observation they answered",
            f"{len(paired)} of {operator.actions_sent} sent")
    mismatches = pairing_mismatches(rows)
    c.check(not mismatches, "every action is paired with the observation that produced it",
            f"{len(mismatches)} mismatched, first={mismatches[:3]}" if mismatches else "exact")

    # The frame in the row must be the one the operator flew on, not a
    # neighbour: decode the tick back out of the archived pixels.
    off = [row for row in rows if row.action[SEEN_ACTION_FIELD] >= 0
           for frame in row.frames.values()
           if (tick := tick_from_frame(frame)) is None
           or round(tick) % TICK_WRAP != row.action[SEEN_ACTION_FIELD]]
    c.check(not off, "every archived frame decodes to the tick the operator saw",
            f"{len(off)} off" if off else "exact")

    if os.environ.get("PORTAL_TEST_KEEP_RRD"):
        c.info("archive kept at", str(path))
    else:
        shutil.rmtree(rrd_dir, ignore_errors=True)
    return c.exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
