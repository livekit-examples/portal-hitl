"""RRD archive test: does the recorder's file read back aligned?

    uv run python -m test.test_rrd      (no LiveKit server needed)

The archive IS the recording, so the checks are about what survives to disk:
the three streams landing on one `robot_time` row each, the images kept as
timeline-indexed frames rather than one static thumbnail, and the keypoint
marks that say which spans are demonstrations and what task they were.

The `Archive` is fed directly here, with the transport out of the loop, so a
failure is the writer's and not the network's. `test_recorder.py` is the same
file produced over a real link.

Two things this pins down. `static` data is timeless -- rerun keeps one value
per entity, latest wins -- so logging frames with `static=True` yields a
recording holding exactly ONE image per camera however long the session ran;
only the `StateConfiguration` lane colours belong there. And `StateChange`
opens a phase wherever it lands, so the bands must appear once per transition,
not once per keypoint.

Reading an `.rrd` back needs `datafusion` (rerun dropped `rerun.dataframe` in
0.34); it is in the `dev` dependency group, which `uv sync` installs.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import tempfile
from dataclasses import dataclass
from pathlib import Path

from portal_rebot.recorder.archive import Archive
from test._harness import (
    SEEN_ACTION_ENTITY,
    Checks,
    bands,
    column,
    frame_for_tick,
    pairing_mismatches,
    read_archive,
    tick_from_frame,
)

CAMERAS = ["arm_camera", "overhead_camera"]
TASK = "Grab the black cube"
TICKS = 60
FPS = 30
# Smaller than the rig's 640x480: this test is about what reaches the file, and
# a quarter-size frame keeps the JPEG encoder from being what it measures. The
# bar still resolves -- `frame_for_tick` steps 3px per tick, so 60 ticks need
# 180 columns.
FRAME_W, FRAME_H = 320, 240
BASE_US = 1_700_000_000_000_000

# The operator answers the frame from two ticks ago: the picture is always
# behind the wire. Alignment must survive that, since it is the normal case.
VIEW_LAG_TICKS = 2


@dataclass
class FakeState:
    values: dict
    timestamp_us: int


@dataclass
class FakeFrame:
    data: bytes
    width: int
    height: int
    timestamp_us: int


@dataclass
class FakeAction:
    values: dict
    timestamp_us: int
    in_reply_to_ts_us: int | None


class FakeMetrics:
    class rtt: rtt_us_last = 2500
    class sync: states_dropped = 3
    class transport: state_jitter_us = 400; action_jitter_us = 500


def ts_for(tick: int) -> int:
    return BASE_US + tick * (1_000_000 // FPS)


def main() -> int:
    c = Checks("rrd archive")
    tmp = Path(tempfile.mkdtemp(prefix="portal-rrd-"))
    try:
        archive = Archive("selftest", CAMERAS, rrd_dir=tmp, jpeg_quality=90)
        c.check(archive.rrd_path is not None, "archive path allocated", str(archive.rrd_path))

        # An action before any frame has arrived: it cannot be placed on the
        # robot's clock, and must be counted rather than filed at a guess.
        archive.log_action(FakeAction({"elbow_flex.pos": 0.0}, ts_for(0), None))

        for tick in range(TICKS):
            time.sleep(1.0 / FPS)
            archive.log_state(
                FakeState({"shoulder_pan.pos": float(tick)}, ts_for(tick)))
            image = frame_for_tick(tick, size=(FRAME_H, FRAME_W))
            for cam in CAMERAS:
                archive.log_frame(cam, FakeFrame(
                    image.tobytes(), FRAME_W, FRAME_H, ts_for(tick)))

            seen = max(0, tick - VIEW_LAG_TICKS)
            archive.log_action(FakeAction(
                {"elbow_flex.pos": float(seen)}, ts_for(tick) + 15_000, ts_for(seen)))

            if tick == 20:
                archive.keypoint({"state": "recording", "task": TASK,
                                  "at_ts_us": ts_for(tick)})
            if tick == 50:
                archive.keypoint({"state": "idle", "task": TASK,
                                  "at_ts_us": ts_for(tick), "discard": True})
            if tick % 20 == 0:
                archive.log_metrics(FakeMetrics)
        archive.close()

        c.check(archive.counts["unanchored"] == 1, "the action with no anchor was counted, not placed",
                str(archive.counts))
        c.check(archive.counts["impossible_lag"] == 0,
                "no action is dated before the frame it answered")

        # The alarm itself, in a throwaway archive: an action logged here would
        # be a 61st, and the collision check below counts them.
        alarm = Archive("alarm", CAMERAS, rrd_dir=tmp, jpeg_quality=90)
        alarm.log_state(FakeState({"shoulder_pan.pos": 0.0}, ts_for(5)))
        alarm.log_action(FakeAction({"elbow_flex.pos": 0.0}, ts_for(0), ts_for(5)))
        alarm.close()
        c.check(alarm.counts["impossible_lag"] == 1,
                "an action dated BEFORE its own anchor is caught -- it cannot have "
                "happened, so it can only mean the clock estimate is wrong",
                str(alarm.counts["impossible_lag"]))
        c.check(archive.poll_errors() == [], "archive raised no errors")

        path = archive.rrd_path
        c.check(path.exists(), "rrd written", f"{path.stat().st_size / 1e3:.0f} KB")
        table = read_archive(path)

        # Static data is timeless -- one value per entity, latest wins -- so it
        # is right for the lane colours and for the field order, and wrong for
        # anything that changes. Frames especially: logged static, the archive
        # would hold ONE image per camera however long the session ran.
        statics = [name for name in read_archive(path, index=None).column_names
                   if name.startswith("/")
                   and "StateConfiguration" not in name
                   and not name.startswith("/schema:")]
        c.check(not statics, "only the lane colours and the field order are static",
                f"static: {statics}" if statics else "everything else timeline-indexed")

        c.check(table.num_rows >= TICKS, "one row per robot tick",
                f"{table.num_rows} rows for {TICKS} ticks")
        c.check(not pairing_mismatches(table),
                "every action is filed at the observation that produced it",
                str(pairing_mismatches(table)[:3]))
        # The collision that costs 25-28% of actions without `action_time`:
        # ticks 0..VIEW_LAG_TICKS all answer frame 0, so they share one
        # `robot_time` and rerun keeps only the last. Read on the operator's
        # own clock and every action must still be there.
        by_action = read_archive(path, index="action_time")
        acted = [v for v in column(by_action, SEEN_ACTION_ENTITY) if v is not None]
        c.check(len(acted) == TICKS, "no action is lost to an anchor collision",
                f"{len(acted)} on action_time vs {TICKS} sent, "
                f"{len([v for v in column(table, SEEN_ACTION_ENTITY) if v is not None])} "
                f"distinct anchors on robot_time")
        c.check("robot_time" in by_action.column_names,
                "an action_time row still names the observation it answered")

        lag = [v for v in column(table, "/latency/see_to_act_ms") if v is not None]
        # One per DISTINCT anchor, not one per action: ticks 0..VIEW_LAG_TICKS
        # all answer frame 0, and the last VIEW_LAG_TICKS rows are answered by
        # actions this run never sent.
        c.check(len(lag) == TICKS - VIEW_LAG_TICKS, "see-to-act series archived",
                f"{len(lag)} samples for {TICKS} ticks")
        c.check(len([v for v in column(table, "/metrics/rtt_last_ms") if v is not None]) >= 3,
                "metrics series archived")

        for cam in CAMERAS:
            blobs = [v for v in column(table, f"/observation/{cam}", "EncodedImage:blob")
                     if v is not None]
            c.check(len(blobs) >= TICKS, f"{cam}: every frame archived, not just the last",
                    f"{len(blobs)} frames")

        # The image on a row must be the one the action on that row answered.
        images = decode_column(table, CAMERAS[0])
        seen = column(table, "/action/elbow_flex.pos")
        wrong = [(i, t, s) for i, (t, s) in enumerate(zip(images, seen))
                 if t is not None and s is not None and abs(t - s) > 0.5]
        c.check(not wrong, "the frame on each row is the one its action answered",
                f"{len(wrong)} off, first={wrong[:3]}" if wrong else "exact")

        c.check(bands(table, "state/recording") == ["idle", "recording", "idle"],
                "the recording band opens and closes once",
                str(bands(table, "state/recording")))
        c.check(bands(table, "state/task") == [TASK], "task archived once, not per keypoint",
                str(bands(table, "state/task")))

        marks = [json.loads(v) for v in column(table, "/keypoints", "TextLog:text")
                 if v is not None]
        c.check(len(marks) == 2, "both keypoints archived", str(len(marks)))
        c.check(marks and marks[-1].get("discard") is True,
                "a key this file does not interpret still reaches the archive",
                str(marks[-1] if marks else None))
        return c.exit_code
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def decode_column(table, camera: str) -> list:
    """The tick each archived frame carries, read back out of the JPEG."""
    import cv2
    import numpy as np

    out = []
    for blob in column(table, f"/observation/{camera}", "EncodedImage:blob"):
        if blob is None:
            out.append(None)
            continue
        image = cv2.imdecode(np.frombuffer(bytes(blob), np.uint8), cv2.IMREAD_COLOR)
        out.append(None if image is None else tick_from_frame(image, ref_width=FRAME_W))
    return out


if __name__ == "__main__":
    sys.exit(main())
