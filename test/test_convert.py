"""Converter test: does an archive slice back into a LeRobot v3 dataset?

    uv run python -m test.test_convert      (no LiveKit server, no hardware)

Builds an archive the way the recorder does -- two takes, one of them
discarded, with the tick index stamped into the state, the frame and the
action -- runs the real converter over it, and reads the dataset back row by
row.

The check that matters is the same one the rest of the suite turns on: a row
whose action was filed against an observation that did not produce it. It
survives the round trip through rerun AND through lerobot, or the conversion
is not worth having.

The other one is loss. The archive holds an action per operator tick, several
of which answer the same frame whenever the video stutters; a converter that
read `robot_time` would quietly drop those. Here the frames deliberately
arrive at half the action rate, so a reader that collapses them loses half the
dataset and this notices.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path

from portal_rebot.convert.__main__ import archives, convert, schema_conflicts
from portal_rebot.convert.reader import _ordered, read_session
from portal_rebot.recorder.archive import Archive
from test._harness import (
    FRAME_H,
    FRAME_W,
    SEEN_ACTION_IDX,
    TICK_STATE_IDX,
    TICK_WRAP,
    Checks,
    frame_for_tick,
    tick_from_frame,
)

FPS = 30
TICKS = 60
FRAME_EVERY = 2           # video at half the action rate: the collapse trap
BASE_US = 1_700_000_000_000_000
TASKS = ["Pick up the cube", "Put it down"]
# Deliberately NOT alphabetical, so "the archive records its own order" is a
# claim the checks can actually distinguish from sorted().
STATE = ["shoulder_pan.pos", "shoulder_lift.pos", "elbow_flex.pos", "wrist_flex.pos",
         "wrist_yaw.pos", "wrist_roll.pos", "gripper.pos"]
ACTION = list(STATE)


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


def ts(tick: int) -> int:
    return BASE_US + tick * (1_000_000 // FPS)


def write_archive(directory: Path) -> Path:
    """One session: take 1 kept, take 2 discarded, and idle time around both."""
    archive = Archive("convert-selftest", ["arm_camera"], rrd_dir=directory, jpeg_quality=90)
    last_frame_tick = None
    for tick in range(TICKS):
        state = {name: float(tick + i * 1000) for i, name in enumerate(STATE)}
        archive.log_state(FakeState(state, ts(tick)))
        if tick % FRAME_EVERY == 0:
            image = frame_for_tick(tick % TICK_WRAP)
            archive.log_frame("arm_camera", FakeFrame(
                image.tobytes(), FRAME_W, FRAME_H, ts(tick)))
            last_frame_tick = tick
        if last_frame_tick is None:
            continue
        # Anchored to the newest frame, and reporting the tick decoded from it
        # -- exactly what the operator does.
        action = {name: float(tick + i * 100) for i, name in enumerate(ACTION)}
        action[ACTION[SEEN_ACTION_IDX]] = float(last_frame_tick % TICK_WRAP)
        archive.log_action(FakeAction(action, ts(tick) + 15_000, ts(last_frame_tick)))

        if tick == 10:
            archive.keypoint({"state": "recording", "task": TASKS[0], "at_ts_us": ts(tick)})
        if tick == 30:
            archive.keypoint({"state": "idle", "task": TASKS[0], "at_ts_us": ts(tick)})
        if tick == 40:
            archive.keypoint({"state": "recording", "task": TASKS[1], "at_ts_us": ts(tick)})
        if tick == 55:
            archive.keypoint({"state": "idle", "task": TASKS[1], "at_ts_us": ts(tick),
                              "discard": True})
    archive.close()
    return archive.rrd_path


def check_lerobot_consumes_it(c: Checks, dataset, camera: str) -> None:
    """The dataset has to be usable by lerobot, not merely well-formed.

    The decisive check is the image. It comes back out of an AV1 **video
    decoder**, not out of the JPEG the converter fed in, so a frame that
    lerobot's encoder dropped, duplicated or reordered would leave every row
    looking correct while its image no longer matched its action. Decoding the
    tick bar back out of the dataset's own video is the only way to know the
    alignment survived that round trip.
    """
    import numpy as np
    from torch.utils.data import DataLoader

    key = f"observation.images.{camera}"
    row = dataset[0]
    image = row[key]
    c.check(tuple(image.shape) == (3, FRAME_H, FRAME_W),
            "images come back CHW at the published resolution", str(tuple(image.shape)))
    c.check(0.0 <= float(image.min()) and float(image.max()) <= 1.0,
            "and normalised, which is what a policy expects")

    mismatched, undecodable = [], 0
    for i in range(dataset.num_frames):
        current = dataset[i]
        seen = int(round(float(current["action"][SEEN_ACTION_IDX])))
        tick = tick_from_frame(np.asarray(current[key]))
        if tick is None:
            undecodable += 1
        elif abs(tick - seen) > 0.6:
            mismatched.append((i, seen, round(tick, 1)))
    c.check(undecodable == 0, "every frame in the dataset's video decodes",
            f"{undecodable} undecodable")
    c.check(not mismatched,
            "the image in each row is still the frame its action answered, "
            "after lerobot re-encoded the video",
            f"{len(mismatched)} off, first={mismatched[:3]}" if mismatched
            else f"exact over {dataset.num_frames} rows")

    batch = next(iter(DataLoader(dataset, batch_size=4, shuffle=False, num_workers=0)))
    c.check(tuple(batch[key].shape) == (4, 3, FRAME_H, FRAME_W),
            "it batches through a DataLoader", str(tuple(batch[key].shape)))
    c.check("task" in row and row["task"] == TASKS[0],
            "and every row carries its task string", str(row.get("task")))

    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    windowed = LeRobotDataset(dataset.repo_id, root=dataset.root,
                              delta_timestamps={"action": [0.0, 1 / FPS, 2 / FPS]})
    c.check(tuple(windowed[0]["action"].shape) == (3, len(STATE)),
            "and serves the action windows a policy trains on",
            str(tuple(windowed[0]["action"].shape)))


def check_order_is_never_guessed(c: Checks) -> None:
    """An archive with no recorded order and no supplied one must FAIL.

    Sorting the names would build a dataset that looks entirely normal with
    its joints permuted. Nothing downstream can detect that, and a policy
    trained on it is quietly wrong -- so it has to be an error, not a default.
    """
    found = set(STATE)
    c.check(_ordered(found, STATE, Path("x.rrd"), "state") == STATE,
            "a known order is used as given")
    try:
        _ordered(found, [], Path("x.rrd"), "state")
        c.check(False, "an unknown field order raises rather than guessing")
    except ValueError as exc:
        c.check("permuted" in str(exc),
                "an unknown field order raises rather than guessing",
                "and the message says why")
    mixed = _ordered({"a.pos", *STATE}, STATE, Path("x.rrd"), "state")
    c.check(mixed == STATE + ["a.pos"],
            "a field the order does not mention is appended, not reordered around",
            str(mixed[-2:]))


def main() -> int:
    c = Checks("rrd -> LeRobot v3")
    tmp = Path(tempfile.mkdtemp(prefix="portal-convert-"))
    try:
        path = write_archive(tmp / "sessions")
        c.check(path.exists(), "archive written", f"{path.stat().st_size / 1e3:.0f} KB")
        c.check(archives([str(tmp / "sessions")]) == [path],
                "a directory resolves to the archives inside it")
        check_order_is_never_guessed(c)

        # No preferred order supplied on purpose: the archive records its own,
        # so a dataset can be built from it on a machine that has never seen
        # this repo's portal.yaml.
        session = read_session(path)
        c.check(session.state_fields == STATE,
                "the archive records its own field order and hands it back",
                str(session.state_fields))
        c.check(session.action_fields == ACTION, "actions too", str(session.action_fields))
        c.check(sorted(STATE) != STATE,
                "and the order is not merely alphabetical, so that check means something",
                f"wire={STATE[:2]}... sorted={sorted(STATE)[:2]}...")
        c.check(session.cameras == ["arm_camera"], "the camera is discovered from the archive")
        c.check(len(session.episodes) == 2, "both takes are found",
                f"{len(session.episodes)}")
        c.check([e.task for e in session.episodes] == TASKS,
                "each take carries the task it was collected under",
                str([e.task for e in session.episodes]))
        c.check([e.discarded for e in session.episodes] == [False, True],
                "and whether the operator threw it away")

        kept = session.episodes[0]
        rows = list(session.rows(kept, max_stale_us=200_000))
        # 20 ticks in the span, an action on every one of them. Half answer a
        # frame that is one tick stale, which is the point.
        c.check(len(rows) >= 18, "an action per tick survives, not one per frame",
                f"{len(rows)} rows over {kept.end_us - kept.start_us} us")

        args = Namespace(repo_id="local/convert-selftest", root=tmp / "dataset", fps=FPS,
                         max_stale_ms=200, keep_discarded=False, dry_run=False)
        written = convert([session], args)
        c.check(written == len(rows), "every readable row reaches the dataset",
                f"{written} written, {len(rows)} read")

        from lerobot.datasets.lerobot_dataset import LeRobotDataset

        dataset = LeRobotDataset("local/convert-selftest", root=tmp / "dataset")
        c.check(dataset.num_episodes == 1, "the discarded take is not in the dataset",
                f"num_episodes={dataset.num_episodes}")
        c.check(dataset.num_frames == written, "frame count matches what was written",
                f"{dataset.num_frames}")
        c.check(dataset.meta.info.codebase_version == "v3.0",
                "written in the v3 layout", str(dataset.meta.info.codebase_version))

        mismatched = []
        for i in range(dataset.num_frames):
            row = dataset[i]
            state_tick = int(round(float(row["observation.state"][TICK_STATE_IDX])))
            seen = int(round(float(row["action"][SEEN_ACTION_IDX])))
            if state_tick % TICK_WRAP != seen:
                mismatched.append((i, state_tick, seen))
        c.check(not mismatched,
                "every dataset row pairs its action with the observation it answered",
                f"{len(mismatched)} mismatched, first={mismatched[:3]}"
                if mismatched else f"exact over {dataset.num_frames} rows")

        c.check(f"observation.images.{session.cameras[0]}" in dataset.meta.features,
                "the camera is a video feature")
        check_lerobot_consumes_it(c, dataset, session.cameras[0])

        # A dataset has ONE feature set, taken from the first archive. A second
        # archive that disagrees would have its extra column silently dropped,
        # or a same-shaped one written under the wrong name -- a dataset that
        # looks fine and trains on the wrong thing.
        c.check(schema_conflicts([session]) == [], "one archive never conflicts with itself")
        c.check(schema_conflicts([session, session]) == [],
                "two identical archives are compatible")
        odd = read_session(path, STATE, ACTION)
        odd.cameras = ["arm_camera", "overhead_camera"]
        conflicts = schema_conflicts([session, odd])
        c.check(len(conflicts) == 1 and "cameras" in conflicts[0],
                "an archive with an extra camera is caught, not silently dropped",
                str(conflicts))
        odd.cameras = list(session.cameras)
        odd.state_fields = STATE[:-1]
        c.check(any("state fields" in line for line in schema_conflicts([session, odd])),
                "so is one with a different state schema")
        return c.exit_code
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
