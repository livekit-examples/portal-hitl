"""Degraded-link test: what does the rig do when the robot misbehaves?

    uv run python -m test.test_degraded

Sweeps sender-side impairments and, for each, records a short span and reports
what survived:

  clean          baseline to compare against
  jitter         an irregular control loop -- exercises `slack`/`tolerance`
  camera-drops   a flaky camera -- the operator's anchor goes stale
  state-drops    a flaky CAN read -- rows lose their state half
  stalls         a link that freezes -- shows up as an old anchor, not as loss

The invariant across every profile: **degradation may cost rows or age them,
but it may never file an action against the wrong observation.** A dropped
frame moves the operator's anchor backwards, so the action lands on an older
`robot_time` -- which is still the timestamp of the frame it was actually
answering, and still the timestamp of the state published in that same robot
tick. Staleness is visible in the archive; misalignment would not be.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

from test._harness import (
    FPS,
    Checks,
    RobotProcess,
    aligned_rows,
    archive_rows,
    fresh_room,
    pairing_mismatches,
)
from test.fake_peers import FakeOperator, FakeRecorder, Impairment

WARMUP_TICKS = 45
EPISODE_TICKS = 120

PROFILES = {
    "clean":        Impairment(),
    "jitter":       Impairment(jitter_ms=20),
    "camera-drops": Impairment(drop_frame=0.30),
    "state-drops":  Impairment(drop_state=0.20),
    "stalls":       Impairment(stall_every=25, stall_ms=250),
}

COLUMNS = [("rows", "rows"), ("paired", "pair"), ("unanchored", "unanc"),
           ("states_published", "st.pub"),
           ("frames_published", "fr.pub"), ("states_dropped", "drop"),
           ("stale_emitted", "stale"), ("mismatched", "bad")]


async def run_profile(name: str, imp: Impairment) -> dict:
    room = fresh_room(f"degraded-{name}")
    rrd_dir = Path(tempfile.mkdtemp(prefix=f"portal-test-{name}-"))

    url = os.environ["LIVEKIT_URL"]
    recorder = FakeRecorder(room, url, rrd_dir=rrd_dir)
    operator = FakeOperator(room, url, fps=FPS)

    with RobotProcess(room, ticks=WARMUP_TICKS + EPISODE_TICKS + FPS * 3,
                      impairment=imp) as robot:
        await recorder.connect()
        await operator.connect()
        await asyncio.sleep(1.0)
        await operator.claim()

        await operator.run(WARMUP_TICKS)
        await operator.send_keypoint("recording", task_description=f"degraded:{name}")
        await operator.run(EPISODE_TICKS)
        await operator.send_keypoint("idle", task_description=f"degraded:{name}")

        dropped = recorder.portal.metrics().observer.frames_dropped
        metrics = operator.portal.metrics()
        path = recorder.rrd_path
        await operator.close()
        await recorder.close()

    _, rows = archive_rows(path)
    stats = dict(
        name=name,
        writer_dropped=dropped,
        unanchored=operator.unanchored,
        states_published=robot.summary.get("states_published", 0),
        frames_published=robot.summary.get("frames_published", 0),
        states_dropped=metrics.sync.states_dropped,
        stale_emitted=metrics.sync.stale_observations_emitted,
        rows=len(rows),
        paired=len(aligned_rows(rows)),
        mismatched=len(pairing_mismatches(rows)),
    )
    shutil.rmtree(rrd_dir, ignore_errors=True)
    return stats


async def main() -> int:
    c = Checks("degraded link")
    only = os.environ.get("PORTAL_TEST_PROFILE")
    results = []
    for name, imp in PROFILES.items():
        if only and name != only:
            continue
        c.info(f"running {name}", imp.describe())
        results.append(await run_profile(name, imp))
        await asyncio.sleep(0.5)

    print()
    print("  " + "profile".ljust(14) + "".join(head.rjust(8) for _, head in COLUMNS))
    for r in results:
        print("  " + r["name"].ljust(14) + "".join(str(r[key]).rjust(8) for key, _ in COLUMNS))
    print()

    for r in results:
        c.check(r["writer_dropped"] == 0, f"{r['name']}: no frame outran the writer",
                f"{r['writer_dropped']} dropped")
        c.check(r["paired"] > 0, f"{r['name']}: rows carry a state and an action",
                f"{r['paired']} of {r['rows']}")
        c.check(r["mismatched"] == 0, f"{r['name']}: every aligned row is correctly filed",
                f"{r['mismatched']} misaligned of {r['paired']}")
    return c.exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
