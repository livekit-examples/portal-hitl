"""Record human → ACT → human handoffs over a local LiveKit server."""
import asyncio
import os
import tempfile
from pathlib import Path

from livekit.portal.convert import EpisodeRule, Selection, read_archive
from teleoperator import cycle_active_operator
from test._harness import FPS, Checks, RobotProcess, fresh_room
from test.fake_peers import FakeOperator, FakeRecorder


async def main():
    checks = Checks("HITL handoff")
    room = fresh_room("handoff")
    url = os.environ["LIVEKIT_URL"]
    with tempfile.TemporaryDirectory(prefix="portal-handoff-") as tmp:
        human = FakeOperator(room, url, identity="teleoperator")
        policy = FakeOperator(room, url, identity="policy-act")
        recorder = FakeRecorder(room, url, Path(tmp))
        with RobotProcess(room, ticks=FPS * 20):
            try:
                await recorder.connect()
                await human.connect()
                await policy.connect()
                await asyncio.sleep(1)
                await asyncio.gather(human.run(FPS), policy.run(FPS))
                checks.check(human.portal.active_operator() is None, "neither operator auto-claims")
                await cycle_active_operator(human.portal, human.identity)
                await asyncio.sleep(0.2)
                await human.send_keypoint("recording", task_description="HITL correction")
                for index, expected in enumerate((human.identity, policy.identity, human.identity)):
                    checks.check(human.portal.active_operator() == expected,
                                 f"gate selects {expected}")
                    # Both send so the archived sender verifies that the gate rejected the other.
                    await asyncio.gather(human.run(FPS), policy.run(FPS))
                    if index == 2:
                        break
                    await cycle_active_operator(human.portal, human.identity)
                    await asyncio.sleep(0.2)
                await human.send_keypoint("idle", task_description="HITL correction")
            finally:
                await policy.close()
                await human.close()
                await recorder.close()
        archive = read_archive(recorder.rrd_path)
        episodes = list(archive.episodes(EpisodeRule()))
        checks.check(len(episodes) == 1, "handoffs preserve a single episode")
        rows = list(archive.rows(episodes[0], Selection())) if episodes else []
        runs = []
        for row in rows:
            if not runs or row.sender != runs[-1]:
                runs.append(row.sender)
        checks.check(runs == [human.identity, policy.identity, human.identity],
                     "archive preserves human, policy, correction order", str(runs))
        checks.check(len(rows) > FPS, "archive contains actions across handoffs", str(len(rows)))
    return checks.exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
