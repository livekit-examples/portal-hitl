# Architecture

Three processes, one wire contract. `uv run robot` drives the arm; `uv run
teleoperator` drives the human's leader arm and sends actions; `uv run
recorder` sits in the same room, listens to all three streams, and writes
the session archive. LiveKit Portal carries everything between them. All
three are console scripts over one package, `portal_rebot/`.

```
reBot 102 leader ──► teleoperator ──► Portal ──► robot ──► B601-DM follower
      (desk)              │              │                    (bench)
                          │◄─────────────┤◄── state + frames ───┘
              add_keypoint│              │
                          ▼              ▼
                       recorder ◄── state + frames + actions
                          │
                          └──► session.rrd
```

The teleoperator does not record. It flies the arm and marks boundaries;
everything that lands on disk lands there because the recorder saw it on the
wire. That split is the point:

- **The operator loop stays a control loop.** No dataset writer, no encoder,
  no disk, nothing on the receive path but decoding a frame and showing it.
- **Recording survives the operator.** Restart the teleoperator mid-session
  and the archive keeps going; run the recorder on a different machine — one
  with the disk — and the operator's laptop never touches the data.
- **What is recorded is what was executed.** The recorder subscribes to
  actions off the wire rather than to a local copy, so a packet the robot
  never received is not in the archive either.

## Portal in one section

Portal has two roles. A **robot** publishes camera frames plus joint
state and applies whatever action it is told to run. An **operator**
receives those observations and sends back actions — a human on a leader
arm and a policy are the same thing to Portal.

Portal handles the two hard parts of doing this over a network:

- **Synchronization.** Frames and state travel separately and arrive at
  different times. Portal's synchronizer matches them by timestamp into a
  single `Observation` before your callback sees it. Nothing here consumes
  that path — see [Recording](#recording--portal_rebotrecorder) for what
  replaced it — but the synchronizer still runs, and its counters are what
  `metrics().sync` reports.
- **The active-operator gate.** One robot, many possible operators, but
  only one drives. The robot starts with `active_operator = None` and
  drops every action until someone claims it.

**Two operators, one gate.** The recorder is a Portal operator too, and it
earns its place by never claiming the gate: `set_active_operator` is not
called anywhere in `portal_rebot/recorder/`, so that process cannot move the
arm however it is started or misconfigured. What it does instead is opt in to
seeing the actions:

```python
cfg = OperatorConfig.from_yaml_file(CONFIG_PATH, room)
cfg.set_action_subscription(True)   # off by default
op.on_action(archive.log_action)    # never fires without the line above
```

Portal names recorders as the reason that switch exists. It is a local receive
preference, not part of the contract, which is why it is set in code rather
than in `portal.yaml`: the teleoperator has no use for an echo of its own
sends. Actions arrive with `sender` (the identity captured at the gate, which
cannot race a handoff) and `in_reply_to_ts_us` — the field the whole archive
is aligned on.

Both sides get `metrics()` (RTT, sync delta, jitter, drops) for free.

## The wire contract — `portal.yaml`

Every peer must agree on fps, video tracks, state schema, and action
schema. Disagreement means packets are dropped at the receiver with a
fingerprint warning. This project keeps that agreement in one file at the
repo root, loaded by both sides with the role supplied at the call site:

```python
cfg = RobotConfig.from_yaml_file(CONFIG_PATH, room)      # robot side
cfg = OperatorConfig.from_yaml_file(CONFIG_PATH, room)   # teleoperator and recorder
```

Identity — the LiveKit room and the participant identity — is never in
the YAML. It comes from env vars and the JWT, which is what makes one
file reusable across every process. The identities themselves are
constants in `common.py` rather than labels, because the teleoperator
addresses its keypoint RPC to `RECORDER_IDENTITY` by name.

Nothing downstream restates the schema. The archive names its entities
from the keys Portal delivers, so adding a field on the wire adds a series
to the next recording with no second list to update.

Rules worth knowing:

- Field order is significant; reordering changes the fingerprint.
- Dtypes are checked at send time. An `int` widens into an `f32` field;
  an `int` into a `bool` field raises `PortalError.DtypeMismatch` before
  the packet leaves the sender.
- Adding a field is backwards-incompatible. All peers update at once.
  There is no schema migration.

See [configuration.md](configuration.md) for what each key does and how
the codec choice affects latency and archive quality.

## Robot loop — `portal_rebot/robot/__main__.py`

Three things per tick: read hardware, publish, apply the latest action.
All hardware glue lives in `follower.py` so `__main__.py` is the Portal
flow alone.

```python
ts_us = int(time.time() * 1_000_000)
state, frames = split_state_frames(follower.get_observation(), tracks)
robot.send_state(state, timestamp_us=ts_us)
for name, frame in frames.items():
    robot.send_video_frame(name, frame, timestamp_us=ts_us)
if latest_action:
    follower.send_action(latest_action)
```

- **One timestamp per tick.** `send_state` and every `send_video_frame`
  share the same `ts_us`. That is what the operator's synchronizer
  matches on; different timestamps would never pair.
- **Frames are RGB uint8.** `send_video_frame` takes `bytes` or an
  `np.ndarray(H, W, 3)`. The cameras are configured `color_mode=RGB`
  (the `OpenCVCameraConfig` default), which is the layout Portal expects.
- **Anything outside the YAML schema is dropped.** `split_state_frames`
  keeps only `*.pos` keys and the declared camera tracks. Sending a field
  with no matching YAML entry logs a warning and discards.

Actions are not polled. The Portal callback fires on the asyncio loop the
moment a packet arrives; we cache the values and apply them on the next
tick, because the callback runs at packet rate (which can spike above the
tick rate) and the CAN bus has its own cadence.

`on_operator_left` clears the gate when the active operator disconnects.
Portal's default is to pin the pointer across a disconnect so a
same-identity reconnect resumes control — the wrong default for a
physical arm, which would hold its last commanded pose indefinitely.

## Operator loop — `portal_rebot/teleoperator/__main__.py`

Read the leader, send one action per tick, show the newest frame. That is
the whole loop — there is no recorder here to feed and no dataset state
machine to drive. `leader.py` holds the arm, `hotkeys.py` the two keyboard taps,
`preview.py` the window and `session.py` the startup questions, so
`__main__.py` is the Portal flow alone.

```python
async for tick in pace(fps):
    action = await hardware.run(leader.get_action)
    op.send_action(action,
                   timestamp_us=int(time.time() * 1_000_000),
                   in_reply_to_ts_us=view.anchor())
```

### The anchor is the whole story

`in_reply_to_ts_us` is what makes a separate recorder possible at all. It
carries **the capture timestamp of the newest frame on the operator's
screen**, so an action states, on the wire, which observation produced it.
The recorder then files that action at that timestamp instead of at the
moment it arrived, and the archive lines up without a matching pass and
without a second clock.

It is a *frame* timestamp and not a state timestamp, and that is deliberate.
Teleop is a closed loop through the operator's eyes: what they respond to is
the picture, and the picture is generally a tick or two behind the state on
the wire, because video and state travel on separate paths. Anchoring to
state would file the action against a sample the operator had not seen yet.

`View.anchor()` is the *oldest* of the newest per-camera timestamps, and it
is withheld until every camera has produced at least once. It has to name an
instant at which everything on screen was current, and with one view still
blank there is no such instant. Actions sent before then go out with no
anchor; the recorder counts them and files none of them, which is why the
first ticks of a session are missing from the archive by design.

`frame_bytes_to_numpy_rgb(f.data, f.width, f.height)` turns a
`VideoFrameData` into a typed `(H, W, 3)` array, zero-copy. Portal makes the
same guarantee whatever the codec: H264, MJPEG, PNG and RAW all decode to RGB
before delivery.

### The window — `preview.py`

One image per camera, logged `static=True`, and nothing else. Static data in
rerun is timeless — one row per entity, latest wins — so the window is a live
mirror at constant memory, with no timeline to scrub and no history piling up
behind a session that runs for hours.

That is the right shape *because* the recorder exists. Everything worth
scrubbing is in the `.rrd`, filed against the robot's clock; a second
operator-clock copy here would only give you two timelines that disagree
about when things happened. Frames go in uncompressed — the viewer is a local
process, and a JPEG encode on this side would cost the tick loop more than it
saves on the wire.

If rerun cannot spawn — no viewer binary, no display — the window is skipped
with a warning and the session carries on. Flying blind is bad; taking the
rig down over a missing window is worse, and the archive is unaffected either
way.

### Keys — `hotkeys.py`

**Two taps, whichever delivers.** pynput taps the windowing system, which is
what lets a key land while the operator is looking at the rerun window rather
than the terminal — and which every platform can starve silently, without
raising. A terminal read has the opposite profile: no display server, no
permissions, no X11, so it works under Wayland and over SSH where pynput
cannot, but only while the terminal has focus. Neither covers every rig, so
both run and `drain()` merges them.

The terminal tap uses `cbreak`, not raw: keys arrive without ENTER but `ISIG`
stays on, so ctrl-c still reaches the process. The old attributes are restored
on `stop()` — a terminal left in cbreak is one the shell no longer echoes into.
It reads the **raw descriptor** with `os.read`, never `sys.stdin.read(1)`:
`sys.stdin` is buffered, so one `read(1)` pulls up to 8 KB off the descriptor,
returns one character and keeps the rest where `select` cannot see it — the
keys already typed then sit there until the next keystroke shakes one loose.

`KEYS` is `r`, `[`, `x` and the digits `1`-`9`. Nine tasks is the ceiling
because the hotkeys *are* the digits — a tenth would need a modifier or a
two-key sequence, for a rig that has never wanted more than a handful.

One keystroke can reach both taps at once. Delivered twice, `r` toggles
recording on and straight back off, which reads as a dead keyboard — so a
repeat of the same key from the *other* tap inside 250 ms is dropped as an
echo. Two presses from the same tap are never suppressed, so a genuine
double-tap still counts.

### Switching task

A session carries up to nine task descriptions, asked at startup, and `1`-`9`
selects between them. `select_task` is the whole of it, and two decisions in it
are worth the words:

**A switch is marked even though nothing is recording.** The keypoint is what
moves the recorder's `state/task` band, so the archive names what was being
collected in the gaps between takes as well as inside them.

**A switch is refused mid-take.** A span that changes task halfway is
mislabelled whichever label it ends up with, and the archive has no way to say
"the first half of this one". Refusing costs one keypress and keeps every
recorded span single-task, which is what makes the archive sliceable by task
later.

The active task also goes to the rerun window as a `TextDocument`, not just to
the console. The operator is looking at the video; a hotkey whose only
confirmation is a line in a terminal behind that window is a hotkey you cannot
tell landed.

### The clock — a subtraction and half an RTT

Actions are stamped on the **robot's** clock, not this machine's, which is what
lets the archive say when an action happened rather than only which frame it
answered. The estimate is deliberately the simplest thing that works.

Every frame says what robot time it was captured at. Subtract the local time it
arrived:

```
frame.timestamp_us - arrival_us  =  offset - delay
```

One equation, two unknowns — the clock offset, and how long the frame took to
get here. Two steps close it:

- **The least delayed frame is the best sample.** Take the *largest* value of
  that subtraction over the last ten seconds. The biggest one is the frame that
  spent the least time in transit, so it is closest to the true offset.
- **Half the round trip is the delay.** That still leaves the transit time of
  even the best frame, and Portal already pings for RTT.

```
offset = max(frame_ts - arrival) + rtt / 2
```

That is the whole model. No drift term and no filter: the window slides, so a
drifting clock is followed by old samples falling out of it. Before the first
frame the offset is zero, which is simply trusting the two wall clocks — what
the rig did before.

`now_us()` never repeats and never goes backwards. The offset can step *down*
when the best sample ages out of the window, and two actions landing on the
same microsecond would collapse into one row — the exact loss `action_time`
exists to prevent. A microsecond of distortion is cheaper than a lost action.

**The anchor cannot be derived from the estimate**, which is why both are
sent. `timestamp_us` answers *when did this happen*; `in_reply_to_ts_us`
answers *what was the operator looking at*. Those are different questions:
because of downlink delay, the newest frame **captured** before an action has
not arrived yet, so looking up the newest frame at or before an action's
robot-clock time picks one the operator had not seen. Recovering the right
frame would mean subtracting a delay estimate — exactly what the anchor
already carries, exactly, for free.

So `in_reply_to_ts_us` still carries the robot's own timestamp verbatim, which
is why the alignment tests can assert with zero tolerance: no clock model can
be wrong about a value that was never modelled.

Because both stamps are now on one clock, their difference has a sign that
means something: an action dated **before** its own anchor is physically
impossible, so it is a direct check on the estimator. The recorder counts
those as `impossible_lag` and says so. The estimate is reported on the operator's 5s line and
rides along on every keypoint, so a session records what it believed the offset
was rather than leaving you to infer it.

### Marking boundaries

A hotkey does not change any local state worth speaking of. It sends an
`add_keypoint` RPC to the recorder and prints what came back:

```python
payload = {"state": "recording", "task": task, "at_ts_us": view.anchor()}
await op.perform_rpc("add_keypoint", json.dumps(payload),
                     destination=RECORDER_IDENTITY)
```

Three things about that call:

- **Addressed by name.** Portal routes an operator's RPCs to the robot by
  default, and the robot has no idea what a keypoint is.
- **Fire and forget.** It is a round trip to another peer; the tick loop
  creates a task and moves on, because the leader arm cannot wait on the
  network. The task is held in a set until it completes so the loop cannot
  collect it mid-flight.
- **Anchored the same way an action is.** `at_ts_us` is `view.anchor()`, so
  the boundary lands where the operator saw it rather than where the RPC
  happened to arrive.

Nothing in that path raises. A keypoint that went nowhere — no recorder in
the room, an RPC that timed out — is reported on the console and the session
continues. Losing a boundary costs one demonstration; taking the loop down
mid-motion costs the arm.

**Claiming control is a loop, not a one-shot.** `set_active_operator` is
an RPC *to the robot*, so calling it before the robot has joined raises,
and nothing orders the two processes. The robot also clears the pointer
when we leave the room, and a robot restart resets it to `None`.
`maintain_control` polls once a second and re-claims whenever the pointer
is not us, which covers all three cases regardless of start order. Both
reads are local state; the RPC only fires when the gate is not ours.

`pace(fps)` in `portal_rebot/common.py` is an async generator that yields
tick indices and `await`s between them. A synchronous `time.sleep` here would
block the Portal callbacks scheduled on the same loop.

It awaits on **both** paths, and the over-budget one matters more than it
looks. `async for` is not itself a suspension point — resuming a generator
that does not `await` hands nothing back to the event loop. So when a tick
body consistently runs past `1/fps`, an over-budget path that only snapped
the schedule forward turned the loop into a busy-loop that never serviced
the callbacks it depends on: on the robot `on_action` stopped firing, the
arm stopped following, and reported RTT climbed without bound because the
transport went unserviced too. The `await asyncio.sleep(0)` there is what
keeps that from happening.

That yield is a floor, not a budget: one yield per tick at 50ms of blocking
work is 20 Hz of callback service, not 30. Which is why the device I/O does
not run on the loop at all.

`HardwareThread` in `portal_rebot/common.py` holds one dedicated thread per
device, and both tick bodies await their driver calls on it —
`follower.get_observation()` (seven CAN feedback round-trips plus the camera
read) and `follower.send_action()` on the robot, `leader.get_action()` on
the operator. The tick still takes as long as the hardware takes, but the
event loop is free for all of it, so callbacks are serviced at full rate
rather than once per tick:

```
                             ticks/s   callbacks serviced
on the event loop              19          9 /s
on HardwareThread              19         94 /s
```

**One worker per device, not a pool.** A device's calls share its serial bus
and must never overlap — `send_action` interleaved with `get_observation`
corrupts both — and awaiting each in turn on a single thread is what
guarantees that regardless of how the tick body is ordered later. `close()`
waits for the in-flight call before the caller disconnects: cancelling the
await abandons the await, not the thread, and disconnecting the device while
a driver call is mid-transaction is how a CAN adapter gets corrupted on
ctrl-c.

## Recording — `portal_rebot/recorder/`

A Portal operator with no interface. It joins, subscribes, and starts writing
the moment each stream arrives; `add_keypoint` is the only thing that ever
talks to it. `__main__.py` is the Portal flow, `archive.py` the `.rrd` writer.

**There is no tick loop here.** The two loops above exist because a device has
to be polled at a rate; this process has no device. Everything is driven by
the Portal callbacks, and `main()` only wakes once a second to sample
`metrics()` and print a line.

### Two timelines, and why it is not one

`robot_time` is the robot's own capture clock, and everything is filed on it:

| Entity | `robot_time` | `action_time` |
|---|---|---|
| `observation/<field>` | `state.timestamp_us` | — |
| `observation/<camera>` | `frame.timestamp_us` | — |
| `action/<field>` | `action.in_reply_to_ts_us` | `action.timestamp_us` |
| `latency/see_to_act_ms` | the same | the same |
| `state/recording`, `state/task` | the keypoint's anchor | — |
| `keypoints` | the keypoint's anchor | — |

The action row is the one that matters. Filing an action at the timestamp of
the frame it answered — rather than at its own — is what makes the aligned
view possible, and what a reader gets out of it is direct:

```
row at robot_time T  →  the frame the operator saw
                        the joint state at that instant
                        the action they took in response
```

They co-locate exactly, with no tolerance, because the robot stamps one
`ts_us` across its state and every frame in a tick, and the operator anchors
to one of those frames. Reading an `.rrd` back on `robot_time` gives literally
one arrow row per instant carrying all three — which is what
`test_recorder.py` asserts, over a real link, against the tick each side
encodes into its payload.

**`action_time` exists because a rerun index keeps one value per entity.**
Log two actions at one `robot_time` and the first is *gone from the file*, not
merely hidden behind the second. That is not a rare collision: the operator
ticks at a fixed rate while frames arrive irregularly, so every frame that is
late or dropped leaves two or more actions anchored to the frame before it.
Measured over a 240-tick run:

| profile | actions sent | distinct anchors | lost, on `robot_time` alone |
|---|---|---|---|
| clean (local link) | 240 | 240 | 0 — 0.0% |
| jitter ±20 ms | 240 | 175 | 65 — **27.1%** |
| camera drops 30% | 240 | 171 | 69 — **28.8%** |
| stalls 250 ms | 240 | 176 | 64 — **26.7%** |

Those are real actions the follower executed, and they are exactly the ones
produced while the picture was frozen — which is when a demonstration is most
worth having. The operator's own send timestamp is what tells them apart, so
it becomes a second index instead of being thrown away. With it, all 240
survive in every profile.

So there are two views of the same file, and a reader picks one:

- **`robot_time`** — one row per observation, carrying the freshest answer to
  it. The aligned view; what you scrub, and what an alignment check reads.
- **`action_time`** — every action the human produced, in order, none lost.
  Each row still carries its `robot_time` value, so the observation an action
  answered is one lookup away. This is the view a dataset converter wants:
  several actions sharing one observation is correct, not redundant, because
  the human's arm kept moving while the picture did not.

**Both indices are on the robot's clock, and they are still separate.** The
teleoperator estimates the offset between the two machines and stamps each
action in robot time (see [The clock](#the-clock--a-subtraction-and-half-an-rtt)),
so the two are directly comparable. They stay separate because one is
*measured* and the other *estimated*: merging them into a single index would
make the measured positions unverifiable. `robot_time` is what the robot said;
`action_time` is the operator's best guess at the same clock.

An action arriving with no `in_reply_to_ts_us` is still counted and *not
placed*. A guess is good enough to give an action its own position on its own
index; it is not good enough to claim which observation produced it, and that
claim is the one thing in the archive that has to be exact.

Rerun's current time is **sticky and per-thread**, which shapes the code:
`_at()` calls `reset_time()` before setting anything, or the next state logged
on that thread would inherit the last action's `action_time` and appear on a
timeline it has no business being on. Per-thread is also what makes the frame
writer safe — it sets its own time from the frame it is holding.

### Keypoints — `portal_rebot/keypoints.py`

`portal.yaml` fixes what the robot publishes. The keypoint payload fixes what
the teleoperator tells the recorder, which is everything the wire contract
cannot carry: where a demonstration starts and stops, and what task it is.

```json
{"state": "recording", "task": "Stack the red cube", "at_ts_us": 1788148347844282}
```

- `state` drives the `state/recording` band, `task` the `state/task` band.
- **Every other key survives verbatim** to the `keypoints` text log —
  `discard: true` from the `[` hotkey, and `by`, the caller identity the
  recorder stamps in. A mark the recorder does not interpret is still a mark,
  so the vocabulary can grow without a schema change on either side.
- Bands are logged **once per transition**, never per keypoint: `StateChange`
  opens a new phase wherever it lands. There is no null state to close one
  with — rerun 0.36 stringifies `None` into the literal `"None"` and drops an
  empty list — hence the explicit `idle`.
- The `idle` lane cannot be opened in the constructor. `StateChange` lands
  wherever the timeline currently sits, and before the first packet
  `robot_time` does not exist, so the band would go in with no index and never
  appear. It is opened on the first timestamp from the robot instead.

A keypoint that arrives before anything has been received comes back as an RPC
error rather than being filed at an invented time. The operator pressed a key
and needs to hear that it did not land.

### Frames go through a writer thread

JPEG-encoding a 640×480 frame costs a couple of milliseconds, and
`log_frame` runs inside a Portal receive callback, where that is a couple of
milliseconds the video path does not get. So the callback enqueues the
*undecoded* frame and one thread decodes, encodes and logs it.

The queue is bounded at 60 because each entry pins one undecoded frame:
unbounded would trade dropped frames for unbounded RSS. Drops are counted and
reported on the 5s line. Rerun keeps its current time **per thread**, which is
what makes this safe — the writer sets its own `robot_time` from the frame it
is holding, and the two threads cannot tread on each other.

`PORTAL_DATA_RRD_QUALITY` is the encode knob: 95 by default, `0` for
uncompressed. It is the difference between roughly 0.3 GB/hour and 2.3
GB/hour for one 640×480@30 track.

### Everything else is reported, not enforced

Nothing raises into a Portal callback. Every rerun call sits behind `_guard`,
which queues the exception for the 5s console line instead. Rerun is a sink,
never the source of truth for the session in flight.

The counters that come out of it:

| Counter | Means | Fix |
|---|---|---|
| `unanchored` | actions with no `in_reply_to_ts_us` — sent before every camera had produced | the first seconds of a session; a steady trickle means a declared camera is silent |
| `dropped_frames` | the archive writer fell behind | disk or encoder bottleneck, not the network — lower `PORTAL_DATA_RRD_QUALITY` or the resolution |
| `see_to_act_ms` | frame capture to the action answering it: the downlink plus the human | a real measurement now that actions are stamped on the robot's clock; it inherits the estimator's error, which the operator's 5s line reports |
| `impossible_lag` | actions dated *before* the frame they answered | cannot happen physically, so it is proof the clock estimate is wrong. `robot_time` placement is unaffected — it uses the exact anchor — but `see_to_act_ms` is not trustworthy while this is non-zero |

## Converting — `portal_rebot/convert/`

`rrd-converter` slices session archives into a `LeRobotDataset`. It is not a
peer and does not run during collection: it decodes every frame and re-encodes
video, and none of that belongs in a loop with an arm on the end of it. That
separation is why the recorder writes one continuous archive marked with
keypoints rather than cutting episodes live.

**It reads actions off `action_time` and observations off `robot_time`**, and
that is the whole reason `reader.py` is not three lines. Reading actions off
`robot_time` would silently drop every action that shared an anchor with a
later one — a quarter of them on a link with any jitter, concentrated in the
moments the video stuttered. Each `action_time` row carries the `robot_time`
it answered, which is what matches an action back to its observation.

**Observations are taken as-of, not exact.** A tick whose state was dropped in
transit still has the frame the operator was looking at, so the lookup takes
the newest state and frame at or before the anchor. That is the same trade the
rig makes live — a stale sample beats a hole, because a hole in a trajectory is
a jump the arm never made — and `--max-stale-ms` is where it stops being true
and the row is dropped instead.

Episodes come from the keypoint log rather than the `state/recording` band: the
band has the spans, but the keypoints carry the task and the `discard` flag,
and a text log keeps every mark rather than only the transitions. A span left
open when the session ended is dropped, on the same reasoning the recorder used
to discard an in-flight episode at shutdown.

**Field order comes from the archive itself.** Rerun entities are independent
— `observation/elbow_flex.pos` says nothing about being third — so the recorder
writes the order down as a static `schema` entity, taken from the order Portal
delivers `values` in. That makes an archive self-describing: converting one
needs no `portal.yaml`, and an archive outlives a schema change.

An order that cannot be established is an **error**, never a guess. Sorting the
names alphabetically produces a dataset that looks entirely normal with its
joints permuted, nothing downstream can detect it, and a policy trained on it
is quietly wrong. `portal.yaml` remains a fallback for archives written before
the recorder recorded its own schema.

**Archives that disagree cannot share a dataset.** One dataset has one feature
set, built from the first archive; a second with an extra camera or a renamed
joint would have its column silently dropped, or a same-shaped one written
under the wrong name. `schema_conflicts` refuses the batch and names what
differs.

## Hardware glue — `portal_rebot/robot/follower.py`

`build_follower` wires one `OpenCVCameraConfig` per declared video track
and the Damiao CAN bus, all from env vars. Linux forces the V4L2 backend:
lerobot defaults to `CAP_ANY`, and OpenCV's auto-picker can land on a
backend that opens the device but no-ops `S_FOURCC`/`S_FMT`, showing up
as `fourcc=''` and a 1920-wide frame that refuses to drop to 640.

`_ResilientCamera` stops a camera hiccup from tearing down the control
loop. `read_latest()` raises `TimeoutError` when the newest buffered
frame is older than `max_age_ms`, and `RuntimeError` once the background
read thread has died. Either one, raised mid-`get_observation()`, throws
away the motor state already read that tick — so the wrapper serves the
last good frame across the gap instead. It also reports `is_connected`
True while holding a buffered frame, because `RebotB601Follower` guards
its motor I/O on `all(cam.is_connected)` and a camera blip would
otherwise raise `DeviceNotConnectedError` out of the *motor* calls.

Before the first frame the true device state shows through, so a real
startup failure still surfaces. Installation shadows `cam.read_latest`
and patches the class's `is_connected` property; `async_read` — which
`connect()` warms up through — is left alone.

Recovery: a dead read thread reconnects immediately; a camera that is
alive but has stopped producing escalates to a reconnect after 30
consecutive stale reads. `_reconnect` calls `connect`'s undecorated body,
because the decorated one tests the very `is_connected` property this
class patches.

`_ResilientBus` does the same job for the motors, which had nothing. A CAN
write that fails — a serial hiccup, an adapter that re-enumerated, a motor in
an error state — raised out of `send_action`, through `HardwareThread.run`,
past the tick loop's `except` (which catches only cancellation) and into its
`finally`: the rig quit, mid-motion, over something usually gone by the next
tick.

It swallows, counts and reports. One failure is almost always transient and the
next tick retries; `B601_BUS_ERRORS_BEFORE_RECONNECT` consecutive failures mean
it is not, and the bus is rebuilt. A failed read still returns the camera
frames — they hang off USB, not CAN — so video keeps flowing and the operator
can see the arm has stopped following rather than guess. `__main__.py` skips
`send_state` on that tick rather than publishing a state it did not read.

**The rebuild runs inline, on the calling thread** — the opposite of
`_ResilientCamera`, and the difference is the bus. Every follower call funnels
through one `HardwareThread` worker precisely so two can never overlap on the
serial port; rebuilding from a background thread would tear the port out from
under an in-flight transaction, which is how a CAN adapter gets corrupted. The
tick blocks for the duration, which is correct.

Two things the rebuild cannot do the obvious way. `follower.connect()` is
wrapped in `@check_if_already_connected`, and it calls `cam.connect()` on every
camera — which `_ResilientCamera` reports as connected while it holds a frame,
so the call raises `DeviceAlreadyConnectedError` and never reaches
`configure()`, the part that re-enables the motors. So the rebuild empties
`cameras` for the duration and calls the undecorated body with
`calibrate=False`, which runs the driver's own bus path and skips the camera
path. Copying the bus construction into this repo instead would rot against
the driver.

**Recovery re-energizes the arm.** `configure()` calls `enable_all()`, so the
follower resumes tracking the leader on the next action — and if it sagged
under gravity while the bus was down, that catch-up would be a snap. It is not,
because of the layer below.

`_StepLimiter` rate-limits the commanded target: it may advance at most
`B601_MAX_STEP_DEG` per command, so the arm is never asked to jump.

**It ramps the target and does not clamp the tracking error**, and getting
that backwards freezes the arm. The B601 runs `control_mode = "mit"` —
impedance control, where the motor torque *is* the position error:

```
torque = kp * (target - present) + kd * (target_vel - vel)
```

Clamping `target` to within N degrees of `present` therefore does not cap
speed, it caps **force**, to `kp * N` radians-worth. At 5 degrees that is 0.79
for `wrist_yaw` (kp 9) and 3.9 for `elbow_flex` (kp 45) — too little to hold a
gravity-loaded joint, which stops moving while the stiffer joints keep working.
The symptom is a limp wrist and a sagging elbow, not a slow arm. Ramping the
target instead leaves the error alone, so the motor keeps full authority to
reach each setpoint; what is bounded is how fast the setpoint travels.

The target is **re-seeded from the present position** at startup, after a bus
rebuild (`_ResilientBus.on_recovered`), and whenever commands stop for half a
second. A stale target is what would make recovery a snap: come back from a
fault and the pre-fault target is wherever the arm was before it sagged.
Re-seeding makes every discontinuity a ramp from where the arm actually is — a
bus recovery, an operator handover, a cold start — which is what a
present-relative clamp was reaching for, without capping the torque to get
there.

The **gripper is exempt** (`B601_UNCAPPED_JOINTS`). It is the one joint not
under impedance control — `gripper_control_mode = "force_pos"` — so its force
is already bounded by `gripper_torque_ratio` at 7% however far the command
sits from the jaw, and the argument for the ramp never reaches it. Nor does
the arithmetic: 270° of travel against 200° for the largest arm joint, and
900 °/s from the driver against 150, so an arm-sized cap turns a grasp into a
two-second squeeze. Its soft joint limits still apply.

`B601_MAX_LAG_DEG` is the one present-relative bound left, and it is a windup
guard rather than a speed limit: it stops the setpoint running away from an arm
that is blocked or overloaded, which would wind the error — and so the torque —
up without limit. It sits well above any tracking error normal teleop produces,
so it never binds while the arm moves freely.

`present` is the last `get_observation`, not a fresh read: the loop takes one
immediately before every command, so it is a tick old at worst, and reading
again would double the CAN feedback traffic on the hot path. Before the first
observation the command is refused rather than passed through — an unramped
first command is exactly the one that would hurt.

The two wrappers are installed limiter-first, so the bus wraps the limiter: a
motor fault is caught *outside* the ramp, and the ramp itself can never be what
raises.

## Layout

| Path | Role |
|---|---|
| `portal.yaml` | the wire contract all three peers load |
| `portal_rebot/common.py` | env loading, LiveKit token minting, participant identities, the tick pacer, the per-device hardware thread |
| `portal_rebot/keypoints.py` | the `add_keypoint` payload contract, shared by the two operators |
| `portal_rebot/robot/__main__.py` | Portal `Robot` flow — `uv run robot` |
| `portal_rebot/robot/follower.py` | B601-DM hardware: CAN motors and cameras, both wrapped so a fault reconnects instead of ending the run |
| `portal_rebot/teleoperator/__main__.py` | Portal `Operator` flow — `uv run teleoperator` |
| `portal_rebot/teleoperator/leader.py` | reBot 102 leader arm |
| `portal_rebot/teleoperator/hotkeys.py` | keyboard listener, drained once per tick |
| `portal_rebot/teleoperator/session.py` | the startup questions: the session's task list |
| `portal_rebot/teleoperator/preview.py` | the operator's window: one static image per camera |
| `portal_rebot/recorder/__main__.py` | listen-only Portal `Operator` — `uv run recorder` |
| `portal_rebot/recorder/archive.py` | the `.rrd` writer: two timelines, keypoint bands, frame writer thread |
| `portal_rebot/convert/__main__.py` | `.rrd` → LeRobot v3 dataset, offline — `uv run rrd-converter` |
| `portal_rebot/convert/reader.py` | reads an archive back: both indices, episode spans, as-of observation lookup |
| `scripts/deploy_to_robot.sh` | rsync the repo to the robot host |
| `test/` | synthetic peers and assertions that run the real stack without hardware — see [test/README.md](../test/README.md) |

`portal.yaml` and `.env` stay at the repo root rather than inside the
package: they are what you edit per deployment, and `common.py` resolves
both one level up from itself.
