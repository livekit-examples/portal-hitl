"""Turn session archives into a LeRobot v3 dataset.

    uv run rrd-converter data/sessions                       # a folder
    uv run rrd-converter a.rrd b.rrd --repo-id local/cubes   # or named files
    uv run rrd-converter data/sessions --dry-run             # look first

The recorder writes one continuous `.rrd` per session, marked with keypoints
rather than cut into episodes. Slicing it is deliberately a separate, offline
job: it decodes every frame and re-encodes video, and none of that belongs in a
loop with an arm on the end of it. What comes out is whatever the installed
lerobot writes -- `CODEBASE_VERSION` is `v3.0` -- via
`LeRobotDataset.create`/`resume`.

Actions come off `action_time` (see `reader.py`), and a span closed with
`discard: true` is skipped unless `--keep-discarded` says otherwise.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from pathlib import Path

from portal_rebot.common import CONFIG_PATH, env_int, load_env
from portal_rebot.convert.reader import Session, decode, read_session

ROBOT_TYPE = "rebot_b601_follower"
# `LeRobotDataset.resume` needs a finished `create`; this marker is what an
# aborted run leaves behind without.
_TASKS_MARKER = Path("meta") / "tasks.parquet"

logger = logging.getLogger(__name__)


def quiet_third_parties() -> None:
    """Silence the libraries that greet you on the way in: the lerobot dataset
    stack prints about 120 lines before the first episode, between a torchcodec
    traceback for every FFmpeg version it probed (a warning -- lerobot falls
    back to pyav, which works) and libav's per-frame colourspace grumbling.
    Only while `LOG_LEVEL` is at its default, so `LOG_LEVEL=INFO uv run
    rrd-converter ...` brings the lot back.

    The SVT-AV1 banner survives: the encoder prints it rather than logging it,
    so there is no handler to turn down.
    """
    if os.environ.get("LOG_LEVEL"):
        return
    logging.getLogger("lerobot.utils.import_utils").setLevel(logging.ERROR)
    try:
        import av.logging

        av.logging.set_level(av.logging.ERROR)
    except Exception:
        pass


def dataset_exists(root: Path) -> bool:
    return (root / _TASKS_MARKER).exists()


def archives(paths: list[str]) -> list[Path]:
    """Every `.rrd` named, or contained in a directory named, in file order.
    Sorted, because the archive names carry a timestamp."""
    found: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            found.extend(sorted(path.glob("*.rrd")))
        elif path.suffix == ".rrd":
            found.append(path)
        else:
            print(f"[convert] skipping {path}: not a .rrd or a directory")
    return found


def describe(session: Session, keep_discarded: bool) -> str:
    kept = [e for e in session.episodes if keep_discarded or not e.discarded]
    discarded = len(session.episodes) - len(kept)
    parts = [f"{len(kept)} episode{'' if len(kept) == 1 else 's'}"]
    if discarded:
        parts.append(f"{discarded} discarded")
    if session.unclosed:
        parts.append("1 never closed (dropped)")
    parts.append(f"cameras={session.cameras or 'none'}")
    return ", ".join(parts)


def schema_conflicts(sessions: list[Session]) -> list[str]:
    """How each archive differs from the first, if it does.

    One dataset has one feature set, built from the first archive. Without this
    check a second archive with an extra camera or a renamed joint converts
    anyway -- its column silently dropped, or written under the wrong name --
    and the dataset looks fine while training on the wrong thing.
    """
    first = sessions[0]
    conflicts = []
    for session in sessions[1:]:
        for label, mine, theirs in (
            ("cameras", first.cameras, session.cameras),
            ("state fields", first.state_fields, session.state_fields),
            ("action fields", first.action_fields, session.action_fields),
        ):
            if mine != theirs:
                conflicts.append(
                    f"  {session.path.name}: {label} are {theirs}, "
                    f"but {first.path.name} has {mine}")
    return conflicts


def build_features(session: Session, height: int, width: int) -> dict:
    from lerobot.utils.constants import ACTION, OBS_STR
    from lerobot.utils.feature_utils import (
        combine_feature_dicts,
        hw_to_dataset_features,
    )

    observation: dict = {name: float for name in session.state_fields}
    observation.update({camera: (height, width, 3) for camera in session.cameras})
    return combine_feature_dicts(
        hw_to_dataset_features(observation, OBS_STR, use_video=True),
        hw_to_dataset_features({name: float for name in session.action_fields},
                               ACTION, use_video=True),
    )


def convert(sessions: list[Session], args) -> int:
    """Write every kept episode into one dataset. Returns rows written."""
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    from lerobot.utils.constants import ACTION, OBS_STR
    from lerobot.utils.feature_utils import build_dataset_frame

    root = Path(args.root).expanduser()
    # The frame geometry is not in the archive's metadata, only in its frames,
    # so it comes off the first one that decodes.
    probe = _first_frame(sessions, args.max_stale_ms * 1000)
    if probe is None:
        print("[convert] no episode in any archive yielded a decodable frame")
        return 0
    height, width = probe.shape[:2]
    print(f"[convert] frames are {width}x{height}")

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    if dataset_exists(root):
        print(f"[convert] appending to {root}")
        dataset = LeRobotDataset.resume(repo_id=args.repo_id, root=root,
                                        image_writer_threads=4, streaming_encoding=True)
    else:
        if root.exists() and any(root.iterdir()):
            # `create` refuses a non-empty root, and an aborted run leaves one
            # without the tasks marker.
            print(f"[convert] wiping incomplete dataset at {root}")
            shutil.rmtree(root)
        print(f"[convert] creating {root}")
        dataset = LeRobotDataset.create(
            repo_id=args.repo_id, fps=args.fps,
            features=build_features(sessions[0], height, width), root=root,
            robot_type=ROBOT_TYPE, use_videos=True,
            image_writer_threads=4, streaming_encoding=True)

    written = saved = skipped = 0
    try:
        for session in sessions:
            for number, episode in enumerate(session.episodes, start=1):
                if episode.discarded and not args.keep_discarded:
                    continue
                rows = 0
                for state, frames, action in session.rows(episode, args.max_stale_ms * 1000):
                    observation = dict(state)
                    try:
                        for camera, blob in frames.items():
                            observation[camera] = decode(blob)
                    except ValueError:
                        skipped += 1
                        continue
                    dataset.add_frame({
                        **build_dataset_frame(dataset.meta.features, observation, prefix=OBS_STR),
                        **build_dataset_frame(dataset.meta.features, action, prefix=ACTION),
                        "task": episode.task,
                    })
                    rows += 1
                if not rows:
                    print(f"[convert]   {session.path.name} #{number}: no usable rows, skipped")
                    continue
                dataset.save_episode()
                written += rows
                saved += 1
                print(f"[convert]   {session.path.name} #{number}: {rows} rows"
                      f"  task={episode.task!r}")
    finally:
        dataset.finalize()
    if skipped:
        print(f"[convert] {skipped} rows dropped on an undecodable frame")
    print(f"[convert] {saved} episodes, {written} rows -> {root}")
    return written


def _first_frame(sessions: list[Session], max_stale_us: int):
    for session in sessions:
        for episode in session.episodes:
            for _, frames, _ in session.rows(episode, max_stale_us):
                for blob in frames.values():
                    try:
                        return decode(blob)
                    except ValueError:
                        continue
    return None


def parse_args(argv: list[str]):
    parser = argparse.ArgumentParser(
        prog="rrd-converter",
        description="Slice recorder session archives into a LeRobot v3 dataset.")
    parser.add_argument("paths", nargs="+", help=".rrd files, or directories holding them")
    parser.add_argument("--repo-id", default="local/portal-rebot",
                        help="dataset id; a bare name is read as local/<name>")
    parser.add_argument("--root", default=None,
                        help="where to write it (default: data/<repo-id>)")
    parser.add_argument("--fps", type=int, default=None,
                        help="dataset fps (default: portal.yaml's, or PORTAL_FPS)")
    parser.add_argument("--max-stale-ms", type=int, default=200,
                        help="how old an observation may be to still pair with an action "
                             "(default: 200)")
    parser.add_argument("--keep-discarded", action="store_true",
                        help="include takes the operator marked discarded")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what each archive holds and write nothing")
    args = parser.parse_args(argv)
    if "/" not in args.repo_id:
        args.repo_id = f"local/{args.repo_id}"
    if args.root is None:
        args.root = Path("data") / args.repo_id
    return args


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING").upper())
    quiet_third_parties()
    load_env()
    args = parse_args(sys.argv[1:] if argv is None else argv)

    paths = archives(args.paths)
    if not paths:
        print("[convert] nothing to convert")
        return 1

    state_order, action_order, config_fps = _wire_contract()
    if args.fps is None:
        args.fps = env_int("PORTAL_FPS", config_fps)

    sessions = []
    for path in paths:
        session = read_session(path, state_order, action_order)
        print(f"[convert] {path.name}: {describe(session, args.keep_discarded)}")
        if session.episodes:
            sessions.append(session)

    if not sessions:
        print("[convert] no episodes found -- was anything marked with `r`?")
        return 1

    if conflicts := schema_conflicts(sessions):
        print("[convert] these archives do not share a schema, so they cannot go "
              "into one dataset:")
        for line in conflicts:
            print(f"[convert] {line}")
        print("[convert] convert them separately, naming the paths for each group")
        return 1
    if args.dry_run:
        total = sum(len([e for e in s.episodes if args.keep_discarded or not e.discarded])
                    for s in sessions)
        print(f"[convert] dry run: {total} episodes across {len(sessions)} archives")
        return 0
    return 0 if convert(sessions, args) else 1


def _wire_contract() -> tuple[list[str], list[str], int]:
    """`portal.yaml`'s field order and fps, so the vectors match the wire
    contract. A field the archive has and the contract does not still converts,
    sorted after these -- an archive outlives a schema change."""
    try:
        from livekit.portal import OperatorConfig

        cfg = OperatorConfig.from_yaml_file(CONFIG_PATH, "convert")
    except Exception as exc:
        logger.warning("could not read %s (%s); ordering fields alphabetically",
                       CONFIG_PATH, exc)
        return [], [], 30
    return ([f.name for f in cfg.state_schema],
            [f.name for f in cfg.action_schema], cfg.fps)


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run()
