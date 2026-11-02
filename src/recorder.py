"""Recorder side: a Portal observer that writes the session's `.rrd`.

Run with `uv run recorder`. It subscribes to state, camera frames, the actions
of whichever operator drives, and every keypoint, and writes them with Portal's
`RrdSink`. There is nothing to press and no window to look at. An observer
cannot drive, so this process cannot move the arm however it is started.

The archive layout is Portal's: see its docs/03-data-recording.md.
"""
from __future__ import annotations

import asyncio

from _common import CONFIG_PATH, RECORDER_IDENTITY, connect_to_robot, env_int, env_str, load_env, required_env
from livekit.portal import Observer, ObserverConfig
from livekit.portal.recording import RrdSink


def status(obs: Observer) -> str:
    m = obs.metrics()
    return (f"{m.transport.states_received} states, "
            f"{sum(m.transport.frames_received.values())} frames, "
            f"{m.transport.actions_received} actions; "
            f"{m.observer.frames_written} frames written, "
            f"{m.observer.frames_dropped} dropped by the writer")


async def main() -> None:
    load_env()

    url = required_env("LIVEKIT_URL")
    room = required_env("LIVEKIT_ROOM")
    obs = Observer(ObserverConfig.from_yaml_file(CONFIG_PATH, room))
    obs.on_keypoint(lambda kp: print(f"[recorder] {kp.type} {kp.payload} from {kp.sender}"))

    print(f"[recorder] connecting to {url} as '{RECORDER_IDENTITY}' in room '{room}' ...")
    # connect() returns after the first sync with the robot's clock, so every row
    # is stamped on it. Start the robot first: without it, connect() raises
    # TimeSyncTimeout after 10 s.
    await connect_to_robot(obs, url, RECORDER_IDENTITY, room)

    sink = RrdSink(env_str("PORTAL_DATA_RRD_DIR", "data/sessions"),
                   jpeg_quality=env_int("PORTAL_DATA_RRD_QUALITY", 95))
    obs.record_to(sink)
    print(f"[recorder] recording to {sink.path}; ctrl-c to stop")

    try:
        while True:
            await asyncio.sleep(5.0)
            print(f"[recorder] {status(obs)}")
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[recorder] stopping ...")
    finally:
        try:
            await obs.disconnect()  # writes what is queued, then closes the file
        finally:
            obs.close()
            print(f"[recorder] wrote {sink.path}")


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
