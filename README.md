# portal-rebot — data collection, before the fancy experiments

Teleoperated data collection for a [Seeed reBot Arm
B601-DM](https://wiki.seeedstudio.com/rebot_arm_b601_dm_lerobot/) over a
network, built on [LiveKit Portal](https://github.com/livekit/portal). Three
processes join one room: the arm, the person driving it, and a listen-only
recorder that writes everything to a single `.rrd`. The arm and the operator
can be on different continents; that geography is the point.

This is the source code behind the article below. It is meant to be read as
much as run, so the three `__main__.py` files stay thin and the reasoning lives
in [`docs/`](#docs).

---

Before the experiments, how the data gets collected.

This part usually gets skipped, and for a good reason: it is dirt simple in the
setup most people are working in. One robot, one operator, one machine, a
stable wifi connection. You press record and the frames land on disk. There is
nothing to write about.

It stops being simple the moment you break one of those assumptions. Say you
want to teleoperate from a different device than the one the robot is running
on. Or run several policies at the same time as a human teleoperator, to
measure how far each model's actions drift from the operator's (why you would
want that is a later article). Now there are multiple processes, multiple
clocks, and a network in the middle, and "press record" stops being a plan.

Two questions show up immediately: what format am I recording into, and what am
I doing about latency, jitter, and time sync.

## The recording format

The obvious answer is to record straight into a LeRobot dataset, and that is
the wrong answer.

A LeRobot dataset is not built for streaming writes on the fly, and you cannot
easily change labels or add subtask annotations while data is coming in. That
is not a flaw. It is a final dataset state, meant for training. It is not a
format for crude, messy, in-progress data collection, and trying to use it as
one means making decisions at record time that you do not yet have the
information to make.

So this records into `rrd`, Rerun's format. It takes arbitrary timestamped
streams, it does not care what the schema ends up looking like, and it can be
written continuously while the session runs.

The principle is not specific to robotics. Record as much as you can, keep the
raw states forever, and refine in stages — the medallion architecture that
large-scale data pipelines have used for years. The expensive thing is a
session you cannot re-derive. Everything downstream of raw is cheap by
comparison.

In this repo the raw layer is one `.rrd` per session under
`data/sessions/<room>-<timestamp>.rrd`, and the LeRobot dataset is a build
artifact produced by `uv run rrd-converter` (see
[Converting](#converting-is-a-separate-offline-job)).

## One room, three participants

A LiveKit room with three kinds of participants. Portal has two roles — a
**robot** publishes observations and applies actions, an **operator** receives
observations and sends actions back — and a human on a leader arm and a policy
are the same thing to it.

| Process | Portal role | What it does |
|---|---|---|
| `uv run robot` | Robot | reads the follower at 30 Hz, publishes 7 joint positions + the wrist camera, applies the action that last cleared the gate |
| `uv run teleoperator` | Operator | reads the reBot 102 leader arm, publishes one action per tick, shows the newest frame, marks episode boundaries |
| `uv run recorder` | Operator (listen-only) | subscribes to state, frames and actions and writes them all to one `.rrd` |

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

The **recorder** joins as a third participant, subscribes to everything, and
writes every stream it sees into an `.rrd`.

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

As a separate participant it is none of those things. Spin it up, it joins the
room, it records all traffic, and you sort out what any of it meant later.
Adding a second robot or a third policy does not change the recorder; it just
means more streams to subscribe to. Simple, works, and it cut a lot of
development time.

It also buys two properties worth stating outright:

- **A disk stall is never a stall in the loop driving the arm.** JPEG encoding
  happens on a writer thread behind a bounded queue (60 frames deep, because
  each entry pins one undecoded frame); a full queue drops frames and counts
  them rather than blocking the receive callback.
- **The recorder cannot move the arm.** `set_active_operator` is not called
  anywhere in `portal_rebot/recorder/`, so it cannot claim the gate however it
  is started or misconfigured. What it does instead is opt in to seeing
  actions, with `cfg.set_action_subscription(True)` — off by default, and the
  reason that switch exists.

## Which timestamp does an action belong to?

Here is the problem that shows up as soon as the operator is not on the same
machine as the robot.

The operator is acting on a delayed view of the world. The robot is at t+1.0s,
but the frame on the operator's screen is from t+0.8s, because of network
latency (or, if you are unlucky, a switch config in your factory that quietly
congests everything). When the operator produces an action, does it belong to
t+0.8s, the world state they actually responded to, or t+1.0s, the world state
it will land in?

That should not be answered at collection time. It is a modeling decision, it
depends on what you are training, and you may want to change your mind after
looking at the data. What has to happen at collection time is recording enough
that the question stays answerable.

So every action carries **both**:

```python
op.send_action(action,
               timestamp_us=clock.now_us(),        # when the operator acted
               in_reply_to_ts_us=view.anchor())    # the frame they were looking at
```

`in_reply_to_ts_us` is the anchor: the capture timestamp of the newest frame on
screen, withheld until every declared camera has produced one — it must name an
instant at which all of what the operator sees was current. It is a frame
timestamp and not a state timestamp because the operator flies on the video,
which is generally a tick or two behind the state on the wire. Nothing
estimated ever touches this field; it is the one column no clock model can be
wrong about.

`timestamp_us` is the operator's own clock expressed **in the robot's**. Portal
pings for RTT over a data channel, and each arriving frame gives one equation
with two unknowns:

```
frame.timestamp_us - arrival_us  =  offset - delay
offset = max(frame_ts - arrival) + rtt / 2
```

The largest subtraction over a sliding 10-second window is the frame that spent
the least time in transit, so it is closest to the true offset; half the RTT
covers what transit is left. No drift term and no filter — the window slides.
Two guards, both from probed failures rather than prudence: the estimate is
kept when the window empties (a video freeze is no news about the clock, not
news that the clock is zero), and a sample implying a jump larger than a second
is disbelieved until ~30 of them in a row say the robot's clock really did
move.

**The measurement itself goes into the recording.** `metrics/rtt_last_ms`,
`metrics/state_jitter_ms`, `metrics/action_jitter_ms` and
`metrics/states_dropped` are logged as streams, `latency/see_to_act_ms` is the
per-action gap between a frame's capture and the response to it, and every
episode boundary carries the operator's `clock_offset_us` and `rtt_us` at the
moment the key was pressed. If the alignment should shift later, the numbers to
redo it are in the file instead of in a session you have to throw away.

There is also a free consistency check on the estimate: an action cannot
precede the frame it answered, so a negative `see_to_act_ms` is not a latency
reading, it is proof the clock estimate is wrong. The recorder counts those and
prints them.

## The archive has two timelines

Everything lands on `robot_time`, the robot's own capture clock:

| Entity | `robot_time` | `action_time` |
|---|---|---|
| `observation/<joint>.pos` | `state.timestamp_us` | — |
| `observation/<camera>` | `frame.timestamp_us` | — |
| `action/<joint>.pos` | `action.in_reply_to_ts_us` | `action.timestamp_us` |
| `latency/see_to_act_ms` | the same | the same |
| `state/recording`, `state/task` | the boundary's anchor | — |
| `keypoints` | the boundary's anchor | — |

Filing an action at its anchor rather than at its own send time is what
co-locates it with the frame and the state it answered — scrub to any instant
and the picture, the joint state and the response are on one row, with no
matching pass.

The second index is not optional. A Rerun index keeps **one** value per entity,
and the operator ticks at a fixed rate while frames arrive irregularly, so
every late or dropped frame leaves two or more actions anchored to the frame
before it. Measured over a 240-tick run: 0% lost on a clean local link and
**25–28% under jitter, camera drops or stalls** — real actions the follower
executed, produced exactly while the picture was frozen, which is when the
demonstration is most interesting. So `action_time` keeps every action the
human produced, in order, none lost, each row still carrying the `robot_time`
it answered.

Both indices are on the robot's clock and they stay separate on purpose: one is
measured, one is estimated, and merging them would make the measured positions
unverifiable.

Episode boundaries are not a recording mode. A hotkey on the teleoperator fires
an `add_keypoint` RPC at the recorder, addressed by identity, carrying the
anchor it belongs at:

```json
{"state": "recording", "task": "Pick up the cube", "at_ts_us": 1788426013542930,
 "clock_offset_us": 4980112, "rtt_us": 41000}
```

`state` drives the `state/recording` band, `task` the `state/task` band, and
the whole payload is written verbatim to a `keypoints` text log — so a key this
repo does not interpret still survives into the archive. The archive also
writes its own field **order** down as a static `schema` entity: Rerun entities
are independent, so without it nothing rebuilding a state vector knows which
joint was third, and an alphabetical guess produces a dataset that looks
entirely normal with its joints permuted.

## Converting is a separate, offline job

```bash
uv run rrd-converter data/sessions --repo-id local/cubes
```

Raw stays raw. The LeRobot v3 dataset is a build artifact: if the labeling
changes, or the alignment changes, or a different subset of streams is wanted,
rebuild it from the raw sessions instead of going back out to the lab. Slicing
decodes every frame and re-encodes video, which is not work that belongs in a
loop with an arm on the end of it.

What the converter does:

- **Reads actions off `action_time`** and observations off `robot_time`, for
  the reason above.
- **Takes observations as-of, not exact** — the newest state and frame at or
  before each action's anchor, bounded by `--max-stale-ms` (200 by default).
  A tick whose state was dropped in transit still has the frame the operator
  was looking at, and a hole in a trajectory is a jump the arm never made.
- **Cuts episodes from the keypoint spans**, skipping takes closed with
  `discard: true` unless `--keep-discarded` disagrees with the operator later.
- **Refuses archives that do not share a schema**, naming what differs. One
  dataset has one feature set, and a silently dropped column trains fine and
  wrong.

## Quick start

```bash
cp .env.example .env      # LIVEKIT_*, serial/CAN ports, camera devices
uv sync

uv run robot           # on the B601-DM host
uv run recorder        # wherever the disk is; records as soon as it joins
uv run teleoperator    # on the operator's machine, with the leader arm

uv run rrd-converter data/sessions    # afterwards: archives -> LeRobot v3
```

The teleoperator asks what this session is collecting — up to nine task
descriptions, because the hotkeys *are* the digits — then hands over the keys:
`r` toggle recording, `[` discard the take, `1`-`9` switch task, `x` quit. Each
is an `add_keypoint` RPC; the teleoperator itself writes nothing to disk.

Both loops print a diagnostic line every 5 seconds — achieved fps against
requested, device time versus tick time, motor-bus errors and reconnects, view
age, and the clock estimate — because a rig that is quietly missing its budget
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
uv run python -m test.test_rrd            # the archive writer alone   (no server)
uv run python -m test.test_resilience     # bus faults + speed cap     (no server)
uv run python -m test.test_teleop         # anchor, tasks, hotkeys     (no server)
uv run python -m test.test_convert        # rrd -> LeRobot v3          (no server)
```

163 checks, no hardware. Synthetic peers stamp each tick's index into the state
*and* into the frame as the column position of a white bar, and the fake
operator decodes the bar and stamps back what it saw — so a correctly aligned
archive row satisfies `observation/shoulder_pan.pos == action/elbow_flex.pos`
exactly, with zero tolerance. `test/netsim.py` shapes the real sockets with
pf + dummynet (`lan`, `wifi`, `wan`, `bad-wan`, `satellite`) when the publisher-
side degradation in `test_degraded` is not enough.

## Docs

| Page | What it covers |
|---|---|
| [Architecture](docs/architecture.md) | Portal's roles, sync and the control gate; the wire contract; both tick loops; the anchor; how the recorder aligns three streams; the hardware wrappers |
| [Configuration](docs/configuration.md) | Every `portal.yaml` key and environment variable, and the codec trade-off table |
| [Operations](docs/operations.md) | Setup, calibration, running the rig, hotkeys, the 5s lines, reading an archive, converting, deploy, known issues |

If you just want to run it, start with [Operations](docs/operations.md).

## Layout

```
portal-rebot/
├── portal.yaml             # the wire contract all three peers load
├── portal_rebot/
│   ├── common.py           # env loading, LiveKit tokens, identities, tick pacer
│   ├── keypoints.py        # the add_keypoint payload contract
│   ├── robot/
│   │   ├── __main__.py     # Portal Robot flow             -> `uv run robot`
│   │   └── follower.py     # B601-DM hardware: CAN motors, resilient cameras
│   ├── teleoperator/
│   │   ├── __main__.py     # Portal Operator flow          -> `uv run teleoperator`
│   │   ├── leader.py       # reBot 102 leader arm
│   │   ├── hotkeys.py      # keyboard listener, drained by the tick loop
│   │   ├── session.py      # startup questions: the session's task list
│   │   └── preview.py      # the window: one static image per camera
│   ├── recorder/
│   │   ├── __main__.py     # listen-only Portal Operator   -> `uv run recorder`
│   │   └── archive.py      # the .rrd writer: two timelines, bands, writer thread
│   └── convert/
│       ├── __main__.py     # .rrd -> LeRobot v3            -> `uv run rrd-converter`
│       └── reader.py       # reads an archive back: spans, as-of observations
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
- Python 3.12 and `uv`. Dependencies are stock PyPI: `livekit-portal>=0.2.5`
  and `lerobot[training,rebot,hardware]>=0.6.1`.

Full setup, calibration, and the commands to run each process are in
[Operations](docs/operations.md).
