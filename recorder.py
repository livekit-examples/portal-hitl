"""Recorder: a Portal `Observer` that writes the session to `.rrd` archives.

It never drives. It receives the robot's raw state and video, every action
the robot executes, and the teleoperator's episode keypoints, all on the
robot's clock, and hands them to an `RrdSink`. Pairing actions with
observations happens later, when `livekit-portal convert` turns the archives
into a `LeRobotDataset` (see tutorial/04-hitl-recording.md).

Run it anywhere with the LiveKit credentials, next to robot.py and
teleoperator.py. One archive per run lands in ``PORTAL_HITL_RECORDING_DIR``
(default ``data/recordings``).
"""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib

from livekit.portal import Observer, ObserverConfig
from livekit.portal.recording import RrdSink

from _common import env_str, load_env, mint_token, required_env

IDENTITY = "recorder"
CONFIG_PATH = pathlib.Path(__file__).parent / "portal.yaml"


async def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    load_env()

    url = required_env("LIVEKIT_URL")
    room = required_env("LIVEKIT_ROOM")
    out_dir = env_str("PORTAL_HITL_RECORDING_DIR", "data/recordings")

    obs = Observer(ObserverConfig.from_yaml_file(CONFIG_PATH, room))
    obs.on_keypoint(lambda kp: print(f"[recorder] {kp.type} {kp.payload or ''} from {kp.sender}"))

    synced = asyncio.Event()
    obs.on_time_synced(synced.set)
    print(f"[recorder] connecting to {url} as '{IDENTITY}' in room '{room}' ...")
    await obs.connect(url, mint_token(IDENTITY, room))
    # Start on the robot's clock, so every row of the archive is on it too.
    print("[recorder] waiting for the robot's clock")
    await synced.wait()
    sink = RrdSink(out_dir)
    obs.record_to(sink)
    print(f"[recorder] recording to {sink.path}; ctrl-c to stop")

    try:
        while True:
            await asyncio.sleep(5)
            m = obs.metrics()
            print(
                f"[recorder] frames written={m.observer.frames_written} "
                f"dropped={m.observer.frames_dropped} states={m.transport.states_received}"
            )
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[recorder] stopping ...")
    finally:
        await obs.disconnect()  # writes what is queued, then closes the file
        obs.close()
        print(f"[recorder] saved {sink.path}")


if __name__ == "__main__":
    asyncio.run(main())
