# Human-in-the-loop with LiveKit and Rebot B601

This is a reference project on how to use
[LiveKit Portal](https://github.com/livekit/portal). In this tutorial,
we'll implement a human-in-the-loop training pipeline for the
[Seeed reBot Arm B601-DM](https://wiki.seeedstudio.com/rebot_arm_b601_dm_lerobot/),
a 6-DOF arm with a gripper and two cameras.

You teleoperate the remote arm with a reBot 102 leader arm on your desk,
and at any point you can hand control to a trained ACT policy. Everything
that's executed, whether it came from your hand or the policy, is
recorded alongside the synchronized observation and written to a
`LeRobotDataset`. The full loop is: collect data by teleoperating, train
a policy on it, then trade control back and forth while every correction
lands back in the dataset.

## The tutorial

The `tutorial/` folder builds this project from the wire contract up, one
Portal concept per page, and ends with a runbook for bringing the real
hardware online. Read it top to bottom:

| Page                                                   | What it covers                                                                           |
| ------------------------------------------------------ | ---------------------------------------------------------------------------------------- |
| [00. What is Portal?](tutorial/00-what-is-portal.md)   | The concepts in one page: robot/operator roles, synced observations, the active-operator gate, and a two-file session to anchor them. |
| [01. Wire contract](tutorial/01-wire-contract.md)      | The schema all three processes must agree on, and where it lives so it can't drift.      |
| [02. Robot loop](tutorial/02-robot-loop.md)            | The robot side: publish state plus frames on one timestamp, run the action that arrives. |
| [03. Operator loop](tutorial/03-operator-loop.md)      | The operator side: receive observations, send actions, claim control.                    |
| [04. HITL recording](tutorial/04-hitl-recording.md)    | Record whoever is driving and line each action up with the observation it answered.      |
| [05. Handoff](tutorial/05-handoff.md)                  | Trade control between a human and a policy mid-session, cleanly.                          |
| [06. Plugging in a policy](tutorial/06-plugging-in-policies.md) | Drop a trained ACT checkpoint in as just another operator.                       |
| [07. Running the rig](tutorial/07-running-the-rig.md)  | The runbook: setup, calibration, collect data, train, run inference, deploy.             |

If you just want to run it, go straight to
[07. Running the rig](tutorial/07-running-the-rig.md) for the full
how-to: prerequisites, calibration, collecting a dataset, training, and
the HITL handoff.

## Layout

```
portal-hitl/
├── portal.yaml             # shared wire contract
├── _common.py              # env loader, token minter, async pacer
├── robot.py                # Portal Robot flow, drives the B601-DM follower
├── teleoperator.py         # Portal Operator flow, reBot 102 leader + recording
├── hitl_recorder.py        # obs ring, action↔obs alignment, dataset recorder driver
├── utils/                  # hardware builders, rerun blueprint, hotkeys, DatasetRecorder
├── policies/               # ACT: inference.py / train.py / skypilot.yaml
├── scripts/                # deploy_to_robot.sh, deploy.rsyncignore
└── tutorial/               # walkthrough of every Portal pattern used here
```

## Requirements at a glance

- A LiveKit project (URL, API key, API secret) for the wire layer.
- A reBot Arm B601-DM follower (6-DOF arm + gripper, two USB cameras)
  and a reBot 102 leader arm for teleop.
- Python 3.12+, `uv`, and `livekit-portal>=0.2.3`.

Full setup, calibration, and the commands to run each process live in
[07. Running the rig](tutorial/07-running-the-rig.md).
