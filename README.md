# portal-rebot

Collect teleoperation data from a [Seeed reBot Arm
B601-DM](https://wiki.seeedstudio.com/rebot_arm_b601_dm_lerobot/) with
[LiveKit Portal](https://github.com/livekit/portal). A reBot 102 leader arm sends
actions to the follower over a LiveKit room. A separate observer records state,
video, actions, and keypoints to a Rerun `.rrd` archive.

Start with [Quick start](#quick-start) to run the three processes, or follow
[Operations](docs/operations.md) for hardware setup and calibration. The sections
below explain how recording and dataset conversion work. For Portal's general
workflow, see [Arbitration and data recording](https://docs.livekit.io/robotics/integrations/portal/arbitration-recording/).

## The recording format

This project separates collection from dataset construction. During a session,
episode boundaries, task labels, and alignment choices may still change. Writing
a training dataset directly would couple those decisions to the control loop.
Keeping a continuous archive lets you inspect the session and rebuild the dataset
later.

Each recorder run writes a continuous `.rrd` archive under
`data/sessions/<room>-<timestamp>.rrd`. Keypoints mark episode boundaries without
interrupting recording. Use `uv run livekit-portal convert` to build a LeRobot v3
dataset from completed archives. You can change task labels, episode selection,
or alignment settings and rebuild without collecting the session again.

## One room, three participants

A LiveKit room with three kinds of participants, one per Portal role. A
**robot** publishes observations and applies actions, an **operator** receives
observations and sends actions back — a human on a leader arm and a policy are
the same thing to it — and an **observer** receives everything and drives
nothing.

| Process | Portal role | What it does |
|---|---|---|
| `uv run robot` | Robot | reads the follower at 30 Hz, publishes 7 joint positions + the wrist camera, applies the action that last cleared the gate |
| `uv run teleoperator` | Operator | reads the reBot 102 leader arm, publishes one action per tick, shows the newest frame, marks episode boundaries |
| `uv run recorder` | Observer | receives state, frames, actions and keypoints and writes them all to one `.rrd` |

The **robot** publishes its observations — camera streams over WebRTC media,
joint state over a data channel. Because the SFU fans this out, every other
participant receives the same streams without the robot having to know or care
who is listening. Every video frame and every state message carries a
timestamp from the robot's local clock, and that timestamp travels with the
data to everyone in the room. One `ts_us` is stamped across the state and every
frame of a tick, which is what makes them pairable at all.

The **teleoperator** subscribes to video and state, and publishes actions back
out to the room. Only one participant may drive: Portal's robot starts with
`active_operator = None` and drops every action until someone claims the gate.
The teleoperator claims it in a loop rather than once on connect, because
nothing orders the two processes and a robot restart resets the pointer.

The **recorder** joins as an observer and hands every stream it sees to
Portal's `RrdSink`, which writes the `.rrd`.

The wire contract all three load is [`portal.yaml`](portal.yaml) — fps, the
video tracks and codec, and the state and action schemas. Peers that disagree
drop each other's packets with a fingerprint warning, so the schema is one
file, not three copies in Python.

## Why the recorder is its own participant

If recording lives on the robot, it has to be integrated with each embodiment.
If it lives on the operator side, it has to be integrated with each control
interface. Either way the recorder becomes something you re-plumb every time
the setup changes, and it is coupled to a piece of software that already has a
real-time job to do.

The observer subscribes to the room's streams independently of the hardware and
control interface. You can restart the teleoperator while the recorder continues
writing the same archive.

This separation provides two properties:

- **A disk stall is never a stall in the loop driving the arm.** Portal
  writes on its own thread behind a bounded queue; when the disk falls behind
  it drops video frames and counts them, and never drops state, actions or
  keypoints.
- **The recorder cannot move the arm.** An observer has no `send_action`, so
  it cannot drive however it is started or misconfigured.

## Action timestamps

An operator responds to a delayed observation. The frame on screen can be older
than the robot's current state, so the observation timestamp and the action's
send timestamp answer different questions. Recording both preserves the
information needed to align observations and actions later.

Every action carries both timestamps:

```python
op.send_action(action, in_reply_to_ts_us=view.anchor())   # the frame they were looking at
# timestamp_us defaults to now_us(): when the operator acted, on the robot's clock
```

`in_reply_to_ts_us` is the anchor: the oldest timestamp among the latest frames
received from each declared camera, withheld until every camera has produced one.
With the single wrist camera, it is simply that camera's latest frame timestamp.
With multiple cameras, it follows the slowest view; it does not guarantee that
all displayed frames were captured at the same instant. It is a frame
timestamp and not a state timestamp because the operator flies on the video,
which is generally a tick or two behind the state on the wire. Nothing
estimated ever touches this field; it is the one column no clock model can be
wrong about.

`timestamp_us` is when the operator acted, **on the robot's clock**. Portal
syncs every peer to the robot and stamps actions with its `now_us()`, which
never repeats and never goes backwards. The recording keeps the metrics as
streams too — RTT, the clock offset and its uncertainty, what was received —
so if the alignment should shift later, the numbers to redo it are in the file
instead of in a session you have to throw away.

## The archive has two timelines

The archive uses two timelines on the robot's clock:

- `robot_time` stores state and frames at their capture timestamps. An action
  uses its `in_reply_to_ts_us` anchor, or its send timestamp if it has no anchor.
  State and frames with the same timestamp line up with that action. Missing
  samples remain missing.
- `action_time` stores each action at its send timestamp. This preserves actions
  that answer the same frame, which happens when the operator ticks faster than
  new frames arrive.

Querying only `robot_time` can collapse several actions into one value per
entity. In the historical 240-tick tests, 25–28% of actions shared anchors under
jitter, camera drops, or stalls. Reading actions from `action_time` preserves
those separate responses. See [Measured baselines](test/README.md#measured-baselines).

Episode boundaries are not a recording mode. A hotkey on the teleoperator sends
a keypoint, filed at the anchor it belongs at:

```python
await op.send_keypoint("recording", {"task_description": "Pick up the cube"},
                       timestamp_us=view.anchor())
```

`recording` opens a take, `idle` closes it, `discard` throws it away, and the
archive keeps every keypoint verbatim. It also records its own field **order**:
Rerun entities are independent, so without it nothing rebuilding a state vector
knows which joint was third, and an alphabetical guess produces a dataset that
looks entirely normal with its joints permuted. All of this is Portal's
`RrdSink`; see its *Data recording* guide for the full layout.

## Converting is a separate, offline job

```bash
uv run livekit-portal convert data/sessions --output lerobotv3 --repo-id local/cubes
```

Raw stays raw. The LeRobot v3 dataset is a build artifact: if the labeling
changes, or the alignment changes, or a different subset of streams is wanted,
rebuild it from the raw sessions instead of going back out to the lab. Slicing
decodes every frame and re-encodes video, which is not work that belongs in a
loop with an arm on the end of it.

The converter applies the following rules:

- **Reads actions off `action_time`**, for the reason above, and pairs each
  with the observation it answered.
- **Takes observations as-of, not exact** — the newest state and frame at or
  before each action's anchor, bounded by `--max-stale-ms` (200 by default).
  Set `--max-stale-ms 0` to require exact timestamps. A row is skipped if any
  required state or camera frame is missing or older than the limit.
- **Cuts episodes at keypoints**, skipping discarded and unclosed takes.
  If no `recording` keypoint exists, it treats the archive as one episode.
- **Refuses archives that do not share a schema**, naming what differs. One
  dataset uses one consistent feature set.

## Quick start

Use Python 3.12 and `uv`. See [Requirements](docs/operations.md#requirements).
Run each peer in a separate terminal on its designated host, with matching
`portal.yaml` files and LiveKit room settings.

```bash
cp .env.example .env      # LIVEKIT_*, serial/CAN ports, camera devices
uv sync

uv run robot           # on the B601-DM host
uv run recorder        # wherever the disk is; records after robot clock sync
uv run teleoperator    # on the operator's machine, with the leader arm

uv run livekit-portal convert data/sessions --output lerobotv3   # afterwards
```

The teleoperator asks what this session is collecting — up to nine task
descriptions, because the hotkeys *are* the digits — then hands over the keys:
`r` toggle recording, `[` discard the take, `1`-`9` switch task, `x` quit. A
take boundary is a keypoint; the teleoperator itself writes nothing to disk.

Both loops print a diagnostic line every 5 seconds — achieved fps against
requested, device time versus tick time, motor-bus errors and reconnects, view
age, and the clock offset — because a rig that is quietly missing its budget
looks exactly like a rig that is fine.

## Nothing hardware-shaped may raise into a tick loop

A device fault reconnects; it never ends the run. That property is load-bearing
and easy to break:

- **`_ResilientCamera`** serves the last good frame across a USB hiccup and
  reconnects on a background thread. It also reports `is_connected` True while
  holding a buffered frame, because the driver guards its *motor* I/O on
  `all(cam.is_connected)`.
- **`_ResilientBus`** swallows, counts and reports CAN failures, rebuilding the
  bus after 5 consecutive ones — inline, on the calling thread, because every
  follower call funnels through one worker thread precisely so two can never
  overlap on the serial port.
- **`_StepLimiter`** advances the commanded *target* by at most
  `B601_MAX_STEP_DEG` (5° default) per command, and re-seeds it from the
  present pose at startup, after a bus rebuild, and whenever commands pause —
  so a cold start, a handover and a recovery are all ramps instead of lunges.
  It ramps the target and deliberately does **not** clamp the tracking error:
  the B601 runs impedance control, where torque *is* the position error, so
  clamping it caps force and freezes the gravity-loaded joints.

Everything device-shaped lives in `robot/follower.py` and
`teleoperator/leader.py`, so the two `__main__.py` files read as Portal flow
alone.

## Testing without a robot

```bash
livekit-server --dev &                    # for the three transport suites

uv run python -m test.test_pubsub         # transport, gate, frame/state sync
uv run python -m test.test_recorder       # three peers, archive round-trip, alignment
uv run python -m test.test_degraded       # the same under sender-side degradation
uv run python -m test.test_resilience     # bus faults + speed cap     (no server)
uv run python -m test.test_teleop         # anchor, tasks, hotkeys     (no server)
```

No hardware. Synthetic peers stamp each tick's index into the state *and* into
the frame as the column position of a white bar, and the fake operator decodes
the bar and stamps back what it saw — so reading the archive back, a correctly
aligned row satisfies `state["shoulder_pan.pos"] == action["elbow_flex.pos"]`
exactly, with zero tolerance. `test/netsim.py` shapes the real sockets with
pf + dummynet (`lan`, `wifi`, `wan`, `bad-wan`, `satellite`) when the publisher-
side degradation in `test_degraded` is not enough.

## Docs

| Page | What it covers |
|---|---|
| [Architecture](docs/architecture.md) | Portal's roles, sync and the control gate; the wire contract; both tick loops; the anchor; why the archive has two timelines; the hardware wrappers |
| [Configuration](docs/configuration.md) | Every `portal.yaml` key and environment variable, and the codec trade-off table |
| [Operations](docs/operations.md) | Setup, calibration, running the rig, hotkeys, the 5s lines, reading an archive, converting, deploy, known issues |

If you just want to run it, start with [Operations](docs/operations.md).

## Layout

```
portal-rebot/
├── portal.yaml             # the wire contract all three peers load
├── portal_rebot/
│   ├── common.py           # env loading, LiveKit tokens, identities, tick pacer
│   ├── robot/
│   │   ├── __main__.py     # Portal Robot flow             -> `uv run robot`
│   │   └── follower.py     # B601-DM hardware: CAN motors, resilient cameras
│   ├── teleoperator/
│   │   ├── __main__.py     # Portal Operator flow          -> `uv run teleoperator`
│   │   ├── leader.py       # reBot 102 leader arm
│   │   ├── hotkeys.py      # keyboard listener, drained by the tick loop
│   │   ├── session.py      # startup questions: the session's task list
│   │   └── preview.py      # the window: one static image per camera
│   └── recorder/
│       └── __main__.py     # Portal Observer + RrdSink     -> `uv run recorder`
├── scripts/                # deploy_to_robot.sh, deploy.rsyncignore
├── test/                   # hardware-free validation vs a local LiveKit server
└── docs/                   # architecture, configuration, operations
```

## Requirements at a glance

- A LiveKit server for the wire layer — URL, API key, API secret. A local
  `livekit-server --dev` is enough; none of this needs LiveKit Cloud.
- A reBot Arm B601-DM follower (6-DOF arm + gripper, wrist camera) and a reBot
  102 leader arm for teleop.
- Somewhere with disk for the recorder; it needs no hardware.
- Python 3.12 and `uv`. Dependencies are `livekit-portal[rerun,lerobot]>=0.3.0rc1`
  and `lerobot[rebot,hardware]>=0.6.1`, both from PyPI.

Full setup, calibration, and the commands to run each process are in
[Operations](docs/operations.md).
