"""Recorder test: does the archive line the three streams up?

    uv run python -m test.test_recorder

Runs all three peers over a real Portal link -- the robot as a subprocess, a
fake teleoperator, and the real recorder with nothing stubbed out -- then reads
the `.rrd` back.

The decisive check is alignment. Every archive row is one `robot_time`, and on
a row carrying both, the robot tick in the state must equal the tick the
operator decoded out of the frame it was answering. A mismatch means the action
is filed against an observation that did not produce it, the exact failure the
split exists to prevent. It holds with no tolerance because the operator's
`in_reply_to_ts_us` and the tick it reports having seen come from the SAME
frame, and the robot published that frame under the same timestamp as the state
for that tick.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from test._harness import (
    FPS,
    SEEN_ACTION_ENTITY,
    TICK_STATE_ENTITY,
    Checks,
    RobotProcess,
    aligned_rows,
    bands,
    column,
    fresh_room,
    pairing_mismatches,
    read_archive,
)
from test.fake_peers import FakeOperator, FakeRecorder

WARMUP_TICKS = 45      # ~1.5s before the mark, so every stream is live
EPISODE_TICKS = 120    # ~4s inside the recording band
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
        # The recorder first: `add_keypoint` is addressed to it by name, and an
        # operator that marks a boundary before it has joined gets an error.
        await recorder.connect()
        await operator.connect()
        await asyncio.sleep(1.0)
        c.check(await operator.claim(), "operator claims the gate")

        await operator.run(WARMUP_TICKS)
        c.check(operator.anchored_view() is not None,
                "operator has a frame to anchor against")

        reply = await operator.send_keypoint(state="recording", task=TASK)
        c.check(json.loads(reply).get("keypoint") == 1, "recording keypoint acknowledged", reply)
        await operator.run(EPISODE_TICKS)
        await operator.send_keypoint(state="idle", task=TASK)

        counts = dict(recorder.archive.counts)
        errors = recorder.archive.poll_errors()
        await operator.close()
        await recorder.close()
    c.info("robot process", str(robot.summary))
    c.info("archive counts", str(counts))
    c.info("see-to-act", f"{recorder.archive.see_to_act_ms:.0f}ms"
           if recorder.archive.see_to_act_ms is not None else "n/a")

    c.check(errors == [], "archive raised no errors", "; ".join(errors) or "none")
    c.check(counts["actions"] > 0, "recorder saw the operator's actions",
            f"{counts['actions']} of {operator.actions_sent} sent")
    c.check(counts["dropped_frames"] == 0, "no frame outran the archive writer",
            f"{counts['dropped_frames']} dropped")
    c.check(counts["unanchored"] <= operator.unanchored,
            "unanchored actions are exactly the ones sent before the first frame",
            f"archive={counts['unanchored']} operator={operator.unanchored}")

    path = recorder.rrd_path
    c.check(path.exists(), "rrd written", f"{path.stat().st_size / 1e6:.1f} MB")
    table = read_archive(path)
    c.info("archive rows", f"{table.num_rows} robot_time rows")

    rows = aligned_rows(table)
    c.check(len(rows) >= EPISODE_TICKS * 0.5, "rows carrying both a state and an action",
            f"{len(rows)} of {table.num_rows}")
    mismatches = pairing_mismatches(table)
    c.check(not mismatches, "every action is filed at the observation that produced it",
            f"{len(mismatches)} mismatched, first={mismatches[:3]}" if mismatches else "exact")

    c.check(len([v for v in column(table, TICK_STATE_ENTITY) if v is not None]) > 0,
            "state series archived")
    c.check(len([v for v in column(table, SEEN_ACTION_ENTITY) if v is not None]) > 0,
            "action series archived")

    # Images belong in the archive now -- it is the record, not a sidecar --
    # and they must be timeline-indexed, not static, or the file holds exactly
    # one frame per camera however long the session ran.
    for cam in operator.tracks:
        images = [v for v in column(table, f"/observation/{cam}", "EncodedImage:blob")
                  if v is not None]
        c.check(len(images) >= EPISODE_TICKS * 0.5, f"{cam}: frames archived over time",
                f"{len(images)} frames")

    c.check(bands(table, "state/recording") == ["idle", "recording", "idle"],
            "the recording band opens and closes once",
            str(bands(table, "state/recording")))
    c.check(bands(table, "state/task") == [TASK], "task archived once, not per keypoint",
            str(bands(table, "state/task")))
    marks = [v for v in column(table, "/keypoints", "TextLog:text") if v is not None]
    c.check(len(marks) == 2, "both keypoints archived verbatim", f"{marks}")
    c.check(all(json.loads(m).get("by") == operator.identity for m in marks),
            "each keypoint names the operator that drew it")

    if os.environ.get("PORTAL_TEST_KEEP_RRD"):
        c.info("archive kept at", str(path))
    else:
        shutil.rmtree(rrd_dir, ignore_errors=True)
    return c.exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
