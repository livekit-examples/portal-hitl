"""Recorder side: a Portal operator that only listens, and writes the `.rrd`.

Run with `uv run recorder`. It subscribes to state, camera frames and the
executed actions and starts writing the moment each stream arrives -- there is
nothing to press and no window to look at.

`cfg.set_action_subscription(True)` is the operator-side opt-in to receiving
the actions the *teleoperator* sends; without it `on_action` is never called on
an operator at all. And `set_active_operator` is never called here, so this
process cannot move the arm however it is started.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from livekit.portal import Operator, OperatorConfig, RpcInvocationData, RpcError

from portal_rebot.common import (
    CONFIG_PATH,
    RECORDER_IDENTITY,
    all_video_tracks,
    env_int,
    env_str,
    load_env,
    mint_token,
    required_env,
)
from portal_rebot.keypoints import METHOD as KEYPOINT_METHOD
from portal_rebot.recorder.archive import Archive

logger = logging.getLogger(__name__)


def keypoint_handler(archive: Archive):
    """Turn one `add_keypoint` invocation into a mark on the timeline.

    A payload that cannot be marked comes back as an RPC error: the operator
    pressed a key and needs to know it did not land.
    """
    def handle(data: RpcInvocationData) -> str:
        try:
            payload = json.loads(data.payload or "{}")
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")
            payload.setdefault("by", data.caller_identity)
            index = archive.keypoint(payload)
        except ValueError as exc:
            raise RpcError.Error(code=1400, message=str(exc), data=None) from exc
        print(f"[recorder] keypoint {index}: {json.dumps(payload, sort_keys=True)}")
        return json.dumps({"keypoint": index})
    return handle


def status(archive: Archive) -> str:
    counts = archive.counts
    line = (f"{counts['states']} states, {counts['frames']} frames, "
            f"{counts['actions']} actions, {counts['keypoints']} keypoints")
    if (lag := archive.see_to_act_ms) is not None:
        line += f"; see-to-act {lag:.0f}ms"
    if counts["impossible_lag"]:
        line += (f"; {counts['impossible_lag']} actions dated BEFORE the frame "
                 f"they answered -- the operator's clock estimate is wrong")
    if counts["unanchored"]:
        line += f"; {counts['unanchored']} actions unanchored"
    if counts["dropped_frames"]:
        line += f"; {counts['dropped_frames']} frames dropped by the writer"
    return line


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    load_env()

    url = required_env("LIVEKIT_URL")
    room = required_env("LIVEKIT_ROOM")
    cfg = OperatorConfig.from_yaml_file(CONFIG_PATH, room)
    cfg.set_action_subscription(True)
    cameras = all_video_tracks(cfg)

    archive = Archive(
        room,
        cameras,
        rrd_dir=env_str("PORTAL_DATA_RRD_DIR", "data/sessions"),
        jpeg_quality=env_int("PORTAL_DATA_RRD_QUALITY", 95),
    )
    op = Operator(cfg)
    op.on_state(archive.log_state)
    for cam in cameras:
        op.on_video_frame(cam, archive.log_frame)
    op.on_action(archive.log_action)
    op.register_rpc_method(KEYPOINT_METHOD, keypoint_handler(archive))

    print(f"[recorder] connecting to {url} as '{RECORDER_IDENTITY}' in room '{room}' ...")
    await op.connect(url, mint_token(RECORDER_IDENTITY, room))
    print(f"[recorder] recording to {archive.rrd_path}; cameras={cameras}; ctrl-c to stop")

    try:
        tick = 0
        while True:
            await asyncio.sleep(1.0)
            tick += 1
            if (metrics := op.metrics()) is not None:
                archive.log_metrics(metrics)
            if tick % 5 == 0:
                print(f"[recorder] {status(archive)}")
            for error in archive.poll_errors():
                print(f"[recorder] archive error: {error}")
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[recorder] stopping ...")
    finally:
        try:
            await op.disconnect()
        finally:
            op.close()
            # After the disconnect, so no callback can enqueue a frame the
            # writer has already been told to drain.
            archive.close()
            print(f"[recorder] {status(archive)}")
            print(f"[recorder] wrote {archive.rrd_path}")


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
