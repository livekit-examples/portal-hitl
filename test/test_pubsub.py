"""Transport test: does the wire contract carry what we publish?

    uv run python -m test.test_pubsub

Checks, in order: both peers discover each other; the action gate drops actions
until an operator claims it; observations arrive at rate with sane state; each
observation's frame carries the same tick as its state.
"""
from __future__ import annotations

import asyncio
import os
import sys

from test._harness import (
    FPS,
    FRAME_H,
    FRAME_W,
    TICK_STATE_IDX,
    TICK_WRAP,
    Checks,
    RobotProcess,
    fresh_room,
    tick_from_frame,
)
from test.fake_peers import FakeOperator

WARMUP_TICKS = 30      # ~1s: let the media path come up before we judge it
STREAM_TICKS = 150     # ~5s of steady streaming


async def main() -> int:
    c = Checks("pub/sub transport")
    url = os.environ["LIVEKIT_URL"]
    room = fresh_room("pubsub")
    c.info("room", room)
    c.info("codec", os.environ.get("PORTAL_TEST_CODEC") or "from portal.yaml (h264)")

    total_ticks = WARMUP_TICKS + STREAM_TICKS + FPS
    with RobotProcess(room, ticks=total_ticks) as robot:
        operator = FakeOperator(room, url, fps=FPS)
        await operator.connect()
        await asyncio.sleep(1.0)  # peer discovery

        c.check(operator.portal.robot_identity() is not None,
                "operator discovers the robot", str(operator.portal.robot_identity()))

        await operator.run(WARMUP_TICKS)      # gate closed: these must be dropped
        await asyncio.sleep(0.5)
        c.check(await operator.claim(), "operator claims the gate",
                str(operator.portal.active_operator()))

        baseline_obs = operator.observations
        await operator.run(STREAM_TICKS)
        await asyncio.sleep(0.5)

        received = operator.observations - baseline_obs
        c.check(received > 0, "operator receives observations", f"{received} in ~5s")
        rate = received / (STREAM_TICKS / FPS)
        c.check(rate > FPS * 0.8, f"observation rate within 20% of {FPS}fps", f"{rate:.1f} fps")

        obs = operator.latest_obs
        if c.check(obs is not None, "an observation is available for inspection"):
            for cam, f in obs.frames.items():
                c.check((f.width, f.height) == (FRAME_W, FRAME_H),
                        f"{cam}: delivered at the resolution we published",
                        f"published {FRAME_W}x{FRAME_H}, received {f.width}x{f.height}")

            values = list(obs.state.values())
            c.check(len(values) == len(operator.cfg.state_schema),
                    "state field count matches the schema", f"{len(values)} fields")
            state_tick = int(values[TICK_STATE_IDX])
            c.check(0 <= state_tick <= total_ticks, "state tick is in the published range",
                    f"tick={state_tick} of {total_ticks} published")

            for cam in operator.tracks:
                held = operator.frames.get(cam)
                if not c.check(held is not None, f"{cam}: frame received"):
                    continue
                frame_tick = tick_from_frame(held[1])
                if not c.check(frame_tick is not None, f"{cam}: frame decodes to a tick"):
                    continue
                delta = (state_tick % TICK_WRAP) - frame_tick
                c.check(abs(delta) <= 4, f"{cam}: frame is within 4 ticks of its state",
                        f"state={state_tick % TICK_WRAP} frame={frame_tick:.1f} delta={delta:+.1f}")

        m = operator.portal.metrics()
        c.info("rtt last", f"{(m.rtt.rtt_us_last or 0)/1000:.1f} ms")
        c.info("sync delta p50", f"{(m.sync.match_delta_us_p50 or 0)/1000:.1f} ms")
        c.info("stale observations", str(m.sync.stale_observations_emitted))
        c.info("state jitter", f"{m.transport.state_jitter_us/1000:.1f} ms")
        c.check(m.sync.states_dropped < received * 0.2,
                "fewer than 20% of states dropped by the synchronizer",
                f"{m.sync.states_dropped} dropped vs {received} emitted")

        await operator.close()

    # The gate, from the robot's own counters: WARMUP_TICKS actions were sent
    # before claiming and STREAM_TICKS after, so a working gate lands close to
    # STREAM_TICKS -- never the full total.
    got = robot.summary.get("actions_received", 0)
    sent = WARMUP_TICKS + STREAM_TICKS
    c.info("robot process", str(robot.summary))
    c.check(got > STREAM_TICKS * 0.8, "robot received the actions sent while the gate was ours",
            f"{got} received of {STREAM_TICKS} gated")
    c.check(got < sent, "gate withheld the actions sent before the claim",
            f"{got} received of {sent} total sent ({sent - got} dropped at the gate)")
    return c.exit_code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
