# 0. What is Portal?

Picture a robot arm folding laundry on its own. A learned policy is
driving it, sending an action chunk for every observation from a server
in San Francisco. Then it slips. It misreads the pile and grabs the wrong
corner of the shirt. A human operator has been watching the whole time
from a laptop in Manila, and the moment the policy gets into trouble they
take the controls, correct the motion, and hand back once things settle.
Every frame, every joint angle, and every correction is recorded and
aligned into clean training data as it happens.

That is **human-in-the-loop** (HITL): a policy and a human sharing one
robot, trading control back and forth, with everything recorded as you
go.

In this tutorial we implement a simple version of that loop with a real
arm, the
[Seeed reBot Arm B601-DM](https://wiki.seeedstudio.com/rebot_arm_b601_dm_lerobot/):
a 6-DOF arm with a gripper and two cameras. A person drives it through a
second, smaller "leader" arm they hold; a trained ACT policy
can take over at the press of a key; and a recorder saves whatever's
being driven into a dataset you can train on later.

By the end you'll have done the full loop: collect data by teleoperating,
train a policy on it, and run that policy back against the same arm,
correcting it by hand when it struggles.

## What Portal is

[Portal](https://github.com/livekit/portal) is a thin layer over
LiveKit's real-time infrastructure that lets you operate robots over the
network. It gives you three things against one robot, from anywhere in
the world: **teleoperation** (drive it by hand), **inference** (let a
policy drive it), and **recording** (save clean data while you do
either). The arm, the policy, and the person driving can each be on a
different continent, and it still works. That geography is the whole
point.

The reason you'd want it: doing this over the internet is genuinely hard.
The camera feed and the joint readings travel separately and arrive at
slightly different times, so something has to stitch them back together
into one coherent snapshot before a policy can use it. You also have to
decide who's allowed to drive at any moment. Portal handles both. You
just declare what your robot sends and what your operator receives, and
it takes care of the transport.

## A brief look into Portal

In Portal there are two roles:

- A **robot** publishes what it sees (camera frames plus joint state) and
  runs whatever action it's told to.
- An **operator** receives those observations and sends back actions. A
  human teleop is an operator; a policy is an operator. Same thing, to
  Portal.

For example, say you have an arm wired up to one computer and you want to
teleoperate it from your laptop across the room. You'd run a robot script
on the machine the arm is plugged into, and an operator script on your
laptop. Here's that whole session in two files, small enough to read in
one sitting.

`robot.py` runs on the machine the arm is plugged into:

```python
import asyncio, time
from livekit.portal import DType, Robot, RobotConfig

async def main():
    cfg = RobotConfig("session-1")
    cfg.add_video("front")                       # add more tracks for multi-camera
    cfg.add_state_typed([("j1", DType.F32), ("j2", DType.F32), ("j3", DType.F32)])
    cfg.add_action_typed([("j1", DType.F32), ("j2", DType.F32), ("j3", DType.F32)])
    cfg.set_fps(30)

    robot_portal = Robot(cfg)

    # One-shot commands. Either side can register. Either side can invoke.
    def on_home(_):
        robot.home()
        return "ok"
    robot_portal.register_rpc_method("home", on_home)

    # Actions arrive here from whichever operator currently holds control.
    # Other operators in the room are silently dropped at the gate.
    robot_portal.on_action(lambda a: robot.send_action(a.values))

    await robot_portal.connect(url, token)

    while running:
        obs = robot.get_observation()
        ts = int(time.time() * 1_000_000)
        robot_portal.send_video_frame("front", obs.image, 640, 480, timestamp_us=ts)
        robot_portal.send_state(obs.state, timestamp_us=ts)
        await asyncio.sleep(1 / 30)

asyncio.run(main())
```

`operator.py` runs wherever your policy or teleop UI lives:

```python
import asyncio
from livekit.portal import DType, Operator, OperatorConfig

async def main():
    cfg = OperatorConfig("session-1")
    cfg.add_video("front")
    cfg.add_state_typed([("j1", DType.F32), ("j2", DType.F32), ("j3", DType.F32)])
    cfg.add_action_typed([("j1", DType.F32), ("j2", DType.F32), ("j3", DType.F32)])
    cfg.set_fps(30)

    op = Operator(cfg)

    # Cameras, state, and a sender timestamp arrive fused as one tuple.
    def on_observation(obs):
        # obs.frames["front"], obs.state, obs.timestamp_us
        # Pass in_reply_to_ts_us so metrics.policy.e2e_us_* measures
        # true observation to action latency on the robot side.
        op.send_action(policy(obs), in_reply_to_ts_us=obs.timestamp_us)

    op.on_observation(on_observation)
    await op.connect(url, token)

    # Robot starts with `active_operator=None` and drops every action.
    # Claim control to be the one whose actions are accepted.
    await op.set_active_operator(op.local_identity())

    await op.perform_rpc("home")                 # imperative commands, not a loop
    print(op.metrics())                          # RTT, sync delta, jitter, drops

    while running:
        await asyncio.sleep(1)

asyncio.run(main())
```

Portal runs a single-robot, multiple-operator model. Many operators can
connect to one robot at once: a human on a leader arm, one policy,
several policies cooperating. Only one drives at any moment, and changing
who that is is the handoff between human and policy, with no restart. You
saw the gate in the scripts above: `set_active_operator` claims control,
and everyone else's actions are dropped until it's their turn.

Everything underneath that is plumbing Portal handles for you. It fuses
the camera frames and joint state into one timestamped observation,
carries actions back the other way, manages the control gate, and tracks
latency and drops. You write the two loops; Portal does the transport.

## How the rest of this tutorial is laid out

That two-file sketch teleops one operator against one robot. Our rig adds
a real schema, two cameras, a recorder, and a policy you can hand off to.
Each remaining page takes on one piece:

| Page                                                   | What it covers                                                                           |
| ------------------------------------------------------ | ---------------------------------------------------------------------------------------- |
| [01. Wire contract](01-wire-contract.md)               | The schema all three processes must agree on, and where it lives so it can't drift.      |
| [02. Robot loop](02-robot-loop.md)                     | The robot side: publish state plus frames on one timestamp, run the action that arrives. |
| [03. Operator loop](03-operator-loop.md)               | The operator side: receive observations, send actions, claim control.                    |
| [04. HITL recording](04-hitl-recording.md)             | Record whoever is driving and line each action up with the observation it answered.      |
| [05. Handoff](05-handoff.md)                           | Trade control between a human and a policy mid-session, cleanly.                         |
| [06. Plugging in a policy](06-plugging-in-policies.md) | Drop a trained ACT or Diffusion checkpoint in as just another operator.                  |

Read top to bottom, they build the rig from the contract up.

Next: [01. Wire contract](01-wire-contract.md).
