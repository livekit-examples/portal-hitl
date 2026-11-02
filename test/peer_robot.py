"""The robot peer as its own process. Started by `RobotProcess`.

Publisher and subscriber MUST NOT share a process. Running both in one
interpreter changes the results in two ways that look like real findings and
are not:

  * WebRTC video panics on the subscribe path
    (`video frame missing user_timestamp`), so no frames arrive at all.
  * When it does run, one process encoding two tracks while decoding two more
    is CPU-starved, and libwebrtc's default MAINTAIN_FRAMERATE degradation
    silently rescales 640x480 down to 320x240.

Separate processes are also the topology the rig ships in.

Configured by environment: TEST_ROOM (required), TEST_TICKS, TEST_IMPAIRMENT
(JSON), plus PORTAL_TEST_CODEC / LIVEKIT_*. Prints `READY` once connected,
then a final `SUMMARY <json>`.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal

from test.fake_peers import FakeRobot, Impairment


async def main() -> None:
    robot = FakeRobot(
        os.environ["TEST_ROOM"],
        os.environ["LIVEKIT_URL"],
        impairment=Impairment(**json.loads(os.environ.get("TEST_IMPAIRMENT") or "{}")),
    )
    await robot.connect()
    print("READY", flush=True)

    # The parent terminates us once its checks are done, normally before
    # `ticks` runs out. Cancel rather than die, so the summary still lands.
    task = asyncio.ensure_future(robot.run(int(os.environ.get("TEST_TICKS") or 600)))
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        print("SUMMARY " + json.dumps({
            "ticks_sent": robot.ticks_sent,
            "states_published": robot.states_published,
            "frames_published": robot.frames_published,
            "actions_received": len(robot.actions_received),
        }), flush=True)
        await robot.close()


if __name__ == "__main__":
    asyncio.run(main())
