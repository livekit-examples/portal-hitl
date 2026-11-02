# Operations

From a fresh clone to a recorded session.

## Requirements

- A LiveKit project (URL, API key, API secret).
- A reBot Arm B601-DM follower: 6-DOF arm + gripper on Damiao CAN motors,
  one USB camera, arm-mounted at the wrist. (The rig supports a second,
  overhead camera; it is not declared in `portal.yaml` by default.)
- A reBot 102 leader arm, on whichever machine runs the teleoperator.
- Somewhere to run the recorder, with disk. It needs no hardware, so the
  operator's machine will do — but roughly 0.3 GB/hour per camera lands
  there, so a machine you would rather not fill is worth avoiding.
- Python 3.12 (`pyproject.toml` pins `>=3.12,<3.13`) and `uv`.
- A display for the live preview. Global hotkeys need **X11** on Linux or
  **Accessibility** access on macOS; terminal hotkeys also work over SSH
  or on Wayland while the terminal has focus. See [Hotkeys](#hotkeys).

```bash
git clone <this repo>
cd portal-rebot
cp .env.example .env      # LIVEKIT_*, serial/CAN ports, camera devices
uv sync
```

Edit `.env` for your credentials, hardware addresses, and `B601_MAX_STEP_DEG`. The task you are
collecting is asked when the teleoperator starts, and everything else has
a default in code — see [configuration.md](configuration.md) for the full
table.

The project declares two PyPI packages: `livekit-portal[rerun,lerobot]>=0.3.0rc1`
and `lerobot[rebot,hardware]>=0.6.1`.
The latter carries
`lerobot.robots.rebot_b601_follower` and
`lerobot.teleoperators.rebot_102_leader`. Both arms ship upstream as of
lerobot 0.6.0, so no out-of-tree hardware plugins are involved. The VLA
extras and their `transformers` pin are avoided entirely.

## Find the ports and cameras

The follower talks to its Damiao motors over CAN. With the default
`B601_CAN_ADAPTER=damiao` (a USB-serial dongle) the port is a
`/dev/ttyACM*` or `/dev/ttyUSB*` device; with `socketcan` it is a `canN`
channel. The leader is a plain serial device.

```bash
# Linux
ls /dev/serial/by-id/ /dev/tty{ACM,USB}*     # arms
v4l2-ctl --list-devices                      # cameras -> /dev/videoN

# macOS
ls /dev/tty.usbmodem* /dev/tty.usbserial*    # arms
# camera is an integer index (0, 1, ...) per AVFoundation
```

## Calibrate

Both arms need calibration before first use, and both run the standard
lerobot prompts on first connect: center the arm, walk the ranges of
motion, save. If either process appears to hang at startup, that is the
prompt — press ENTER to accept the saved file, or `c` then ENTER to
recalibrate.

```
~/.cache/huggingface/lerobot/calibration/robots/rebot_b601_follower/<B601_ID>.json
~/.cache/huggingface/lerobot/calibration/teleoperators/rebot_102_leader/<REBOT102_LEADER_ID>.json
```

If you previously ran this rig on the out-of-tree Seeed plugins, the old
files live under `seeed_b601_dm_follower/` and `rebot_arm_102_leader/`.
The upstream drivers will not find them and will prompt for a fresh
calibration; the format is unchanged, so copying the JSON across works.

## Run

Three terminals, three processes, same LiveKit room. `uv sync` installs
all three as console scripts, so `uv run` finds them from anywhere in the
repo; `portal.yaml` and `.env` are resolved from the repo root, not the
cwd.

```bash
# Terminal 1 — the B601-DM host
uv run robot
# waits for cameras + CAN bus, prints "connected" when ready

# Terminal 2 — wherever the disk is
uv run recorder
# joins, waits for the robot's clock, prints the .rrd it is writing

# Terminal 3 — the operator's machine, with the reBot 102 leader
uv run teleoperator
# asks what you are collecting, claims the action gate, opens the rerun window
```

The startup questions are the task list. One task is the common case; give a
count up to 9 and each gets a digit hotkey:

```
[teleop] session -- ENTER accepts the default
  how many tasks [1]: 3
  task 1 [reBot B601 manipulation]: Pick up the red cube
  task 2: Stack it on the blue
  task 3: Push the tray left
```

```
[teleop]   > 1  Pick up the red cube
[teleop]     2  Stack it on the blue
[teleop]     3  Push the tray left
[teleop] hotkeys: r=record  [=discard the take  x=quit  1-3=switch task
```

For an unattended run, `PORTAL_DATA_TASKS` supplies the whole list and skips
the questions — semicolon-separated, because a task description is a sentence
and sentences contain commas.

`uv run python -m portal_rebot.robot` and the other two module forms are
the same entry points if you prefer them.

**Start order does not matter for the robot and the teleoperator** —
`maintain_control` re-claims the gate every second until it holds it. Both
sides say so when it lands:

```
[teleop] claimed control as 'teleoperator' on robot 'robot'
[robot]  active operator now: teleoperator
```

**Until those two lines appear the arm will not move** — the robot drops
every action from an operator the gate does not name. If instead the
teleoperator repeats

```
[teleop] no robot in the room yet; actions we send are dropped
```

the two processes are not in the same room: check that `LIVEKIT_URL` and
`LIVEKIT_ROOM` match on both machines. A `could not claim control` line
carries the RPC error verbatim. Move the leader gently on the first
connect until you have confirmed the arm behaves the way you expect.

**Start the recorder before you press `r`**, though. A keypoint is only
recorded by an observer that is in the room, and a boundary drawn while none
is says so:

```
[teleop] NOT RECORDED: no recorder in the room
```

The recorder itself needs no coordination: it records from the moment the
robot's clock reaches it, so a late start costs you the beginning of the
session and nothing else. The teleoperator can be restarted freely under a
running recorder — the archive does not notice.

### Hotkeys

| Key | Action |
|---|---|
| `r` | start or end a take — the boundaries of one demonstration |
| `[` | end the take and mark it discarded |
| `1`–`9` | switch the active task, when the session has more than one |
| `x` | clean quit — discards an open take first |

Keys are read two ways at once, because neither works everywhere:

| Tap | Works | Does not |
|---|---|---|
| global (pynput) | while you are looking at the rerun window | Wayland, SSH without X11, macOS without Accessibility |
| terminal | anywhere — no display server, no permissions | only while the teleoperator's terminal has focus |

So if the global tap is starved, **the keys still work — press them in the
teleoperator's terminal.** The startup line tells you which situation you are
in:

```
[teleop] global hotkeys will not fire (this is a Wayland session and pynput
         can only tap X11 ...); press them in THIS terminal
```

A keystroke that reaches both taps counts once, so nothing double-fires on a
setup where both work. See [Known issues](#known-issues) for why the global tap
is starved on Wayland and macOS.

The console says what happened in your terms, not the protocol's:

```
[teleop] recording 'Pick up the red cube'
[teleop] recorded -- 4 episodes of 'Pick up the red cube'
[teleop] discarded
```

**The episode count moves only when a recorder received the keypoint.**
A take it never heard about is not an episode however firmly the key was
pressed, and a tally that claims otherwise is worse than none. That is also
the only thing printed on the success path — a failure is always loud, because
it means you were demonstrating into the void:

```
[teleop] NOT RECORDED: no recorder in the room
```

Quitting prints the session's tally, per task:

```
[teleop] this session:
[teleop]   1  4 episodes of 'Pick up the red cube'
[teleop]   2  2 episodes of 'Stack it on the blue'
[teleop]   3  0 episodes of 'Push the tray left'
```

### Switching task

Press the digit. The window you are already looking at shows which is active,
so you do not have to turn back to the terminal to check:

```
[teleop] task 2: 2 episodes of 'Stack it on the blue' so far
```

Each take's `recording` keypoint carries the task it was collected under.

**Switching is refused mid-take.** A span that changes task halfway is
mislabelled whichever label it ends up with, and the archive has no way to say
"the first half of this one":

```
[teleop] recording; press r to end the take before switching to 3
```

That costs one keypress and keeps every recorded span single-task, which is
what makes the archive sliceable by task later.

A typical session:

```
(all three running; arm follows the leader)
move to start position
r        → `recording`: the take starts
perform the task
r        → `idle`: that span is one demonstration
move back to start position
... repeat ...
2        → switch to task 2; collect a few under that
[        → `discard`: the take ends and is thrown away instead
x        → quit
```

Nothing is refused and nothing is queued: the archive is one continuous
recording and a keypoint only writes a mark into it. Quitting mid-take
sends `discard` rather than leaving the take open.

## Watch the 5s lines

The robot prints its tick budget every 5s — this is the answer to "is it
actually running at 30fps":

```
[robot] 18.1 fps of 30 requested; device 50ms (worst 52ms), tick 54ms (worst 56ms) of 33ms budget; 150/150 over
```

`device` is time inside the driver calls (`get_observation` plus
`send_action`), `tick` the whole body. They separate the two ways to miss
the budget, which need different fixes:

| Reading | Means | Do |
|---|---|---|
| `device` ≈ `tick`, both over | the CAN bus and cameras are the cost | drop `PORTAL_FPS`, or `B601_CAM_WIDTH`/`HEIGHT` |
| `device` well under `tick` | publishing is the cost | lower `max_bitrate_kbps` or the camera resolution |
| `0/150 over` | you are inside budget | nothing |

A second robot line appears only when the motor bus has had trouble:

```
[robot] motor bus: 14 errors, 1 reconnects - CURRENTLY DEGRADED
```

And a third when the speed cap is doing work:

```
[robot] speed cap: 47 joint commands clamped to 5 deg in 5s
```

An occasional clamp is the cap catching a fast move. A count that never drops
means `B601_MAX_STEP_DEG` is below how fast you are actually moving the
leader, and every demonstration you record is being flattened — raise the cap
or move more slowly.

A handful of errors with no reconnect is CAN noise the next tick absorbed.
Reconnects climbing means the adapter or a motor is genuinely dropping out —
check the USB cable and `B601_PORT` before blaming the link. `CURRENTLY
DEGRADED` means it is failing *right now*: state has stopped publishing and the
arm is not following, though video keeps flowing so you can still see it.

Being over budget is no longer a collapse — the device I/O runs on a
`HardwareThread`, so callbacks keep flowing and the loop just runs at the
rate the hardware allows. It does mean the arm follows at that rate, so if
`fps of 30 requested` is far below 30, lower `PORTAL_FPS` to match what the
hardware can actually do rather than requesting a rate you never get.


The teleoperator prints one line every 5s:

```
[teleop] view 41ms old, 29.8 fps in, clock +3ms (±1ms), task 2
```

`clock` is Portal's offset to the robot's clock, which every action is stamped
on. Watch that it is **stable**, not that it is small — a constant +3 ms or
+2 s are both fine. `clock unsynced` means Portal has not synced yet, and
actions are stamped on this machine's clock until it does.

`task N` appears only when the session has more than one, and is the answer to
"which task have I been collecting for the last ten minutes".

| Reading | What it means | What to do about it |
|---|---|---|
| `view ... old` | how stale the picture you are flying on is | tens of ms is normal; hundreds means the link stalled and you were flying blind through it |
| `fps in` | frames arriving per second | well under `PORTAL_FPS` means the uplink cannot carry the video — see [the codec trade-off](configuration.md#codec-choice) |

The recorder prints what it received and what it wrote:

```
[recorder] 1482 states, 1476 frames, 1470 actions; 1476 frames written, 0 dropped by the writer
```

| Reading | What it means | What to do about it |
|---|---|---|
| the three received counts | what reached the recorder | actions far below states means the gate is not the teleoperator's |
| `dropped by the writer` | JPEG encoding could not keep up, so Portal dropped frames rather than stall | lower `PORTAL_DATA_RRD_QUALITY`, or the camera resolution. State, actions and keypoints are never dropped |

Each keypoint is printed as it arrives, with who sent it.

## Inspect what you collected

The teleoperator's window is live-only and holds no history — that is
deliberate. The record is the recorder's `.rrd`:

```bash
rerun ~/portal-sessions/portal-rebot-20260831T105141.rrd
```

What to look at:

- **The keypoints.** `keypoints/recording`, `keypoints/idle` and
  `keypoints/discard` mark every take, with the task in the payload.
- **Scrub anywhere at all.** Every `robot_time` instant carries the frame,
  the joint state and the action taken in response — aligned by
  construction, not by a matching pass.
- **Switch to `action_time` to see every action.** `robot_time` shows one
  row per observation, so when several actions answered the same frame — any
  time the video stuttered — you are seeing the freshest of them.
  `action_time` has them all, in the order the human produced them.
- **Read the metrics.** A demonstration that felt wrong usually has its
  explanation in the seconds *before* it, and the archive runs continuously,
  so that context is in the same file.

To read one back programmatically, use the reader the converter uses. It
pairs each action with the newest state and frames at or before its anchor,
allowing up to 200 ms of staleness by default. Use
`Selection(max_stale_us=0)` to require exact timestamps, as the test harness does:

```python
from livekit.portal.convert import EpisodeRule, Selection, read_archive

archive = read_archive("session.rrd")
for episode in archive.episodes(EpisodeRule()):
    for row in archive.rows(episode, Selection()):
        row.state, row.action, row.frames
```

## Convert to a LeRobot dataset

Portal's converter, installed with this project:

```bash
uv run livekit-portal convert data/sessions --output lerobotv3 --dry-run   # look first
uv run livekit-portal convert data/sessions --output lerobotv3 --repo-id local/cubes
uv run livekit-portal convert data/sessions --output lerobotv3 --repo-id me/cubes --push
```

```
[convert] portal-rebot-20260831T223346.rrd: 4 episodes, cameras=['arm_camera'], senders=['teleoperator']
[convert] fps=30, from the robot's state rate
[convert]   portal-rebot-20260831T223346.rrd #1: 118 rows, task='Pick up the red cube'
[convert] wrote 4 episodes, 460 rows
```

Out comes a LeRobot v3 dataset, which loads the ordinary way:

```python
from lerobot.datasets.lerobot_dataset import LeRobotDataset
ds = LeRobotDataset("local/cubes", root="data/local/cubes")
```

**Build a corpus by converting the folder again.** The converter writes one
dataset from every archive it is given and refuses a non-empty `--root`
without `--overwrite`, so point it at the folder of sessions each time rather
than appending to a dataset.

Takes ended with `[` are skipped, and a take left open when the session ends
is dropped. If an archive has no `recording` keypoints, the converter treats
its full span as one episode. Start the recorder before marking the first take.
Archives are converted together only if they agree on cameras and fields; a
mismatch is refused with a line naming what differs.

Every flag, and how rows and episodes are built, is in Portal's
`docs/03-data-recording.md`, under *Convert recordings into training datasets*.
Conversion is deliberately offline: it decodes every frame and re-encodes
video, and none of that belongs in a loop with an arm on the end of it.

## Deploy to the robot host

```bash
./scripts/deploy_to_robot.sh binh@the-mini-anton
./scripts/deploy_to_robot.sh binh@the-mini-anton '~/code/portal-rebot'
```

The host is required; the destination is the full remote path and
defaults to `~/workspace/portal-rebot`. Quote a leading `~` in a path you
pass — unquoted, your own shell expands it to *your* home before the
script sees it, and that path is then used on the robot.

It `mkdir -p`s the destination, `rsync -azP --delete`s the tree honoring
`scripts/deploy.rsyncignore`, and prints the next steps.

| Synced | Excluded |
|---|---|
| source, `portal.yaml`, `pyproject.toml`, `uv.lock` | `.git`, `.venv`, Python caches |
| `.env`, so the robot inherits LiveKit and port config | `.env.local` — each machine keeps its own overrides |
| | `data/` — session archives, recorder side only |

Then on the robot: `cd <destination> && uv sync && uv run robot`. Rerun the
script after any local change; `--delete` keeps the remote tree
byte-identical to your working copy minus the exclusions.

The same script pushes to a recorder host, if you run the recorder
somewhere other than the operator's machine — it is the same tree, and
`uv run recorder` needs no hardware at all.

## Known issues

- **A frame can carry its neighbour's timestamp right after a stall** on
  Portal 0.3.0rc1 (first seen on pre-0.3 `main`). When the robot loop catches up after a stall it
  publishes two ticks a few hundred microseconds apart, and one of those frames
  arrives stamped with the other tick's timestamp. An action anchored to it is
  then paired with the state one tick later. `test_degraded`'s `stalls` profile
  catches it, one or two rows in 120; Portal 0.2.5 does not do it. The fix
  belongs in Portal's video timestamp path, not here.
- **Wayland starves the *global* hotkey tap.** pynput's only Linux backend
  is XRecord over `$DISPLAY` (`pynput/_util/__init__.py:64`). In a Wayland
  session that display is XWayland, which never sees keys typed into
  Wayland-native windows — so `Listener.start()` succeeds, the thread stays
  alive, and receives nothing. Check with
  `loginctl show-session $(loginctl | awk '/'$USER'/{print $1;exit}') -p Type`.
  **The keys still work in the teleoperator's own terminal**, which is the
  fallback tap. To get them back globally, run the teleoperator from an X11
  session (on GNOME, "Ubuntu on Xorg" at the login screen). A true global tap
  under Wayland would mean reading `/dev/input/event*` via evdev, which needs
  the user in the `input` group — not worth the setup for three keys.
- **macOS starves it without Accessibility access.** Same failure, different
  cause: the tap is a `CGEventTap`, and unless the terminal binary is listed
  under System Settings > Privacy & Security > Accessibility it receives
  nothing. Grant it *and relaunch that terminal* — the check is per process
  tree, so a running shell keeps the old answer. Until then, the terminal tap
  carries the keys.
- **A joint that will not move, while the others follow.** Almost always the
  speed cap set too low for that joint's gains, or a stale build where the cap
  clamped the tracking error instead of ramping the target — under MIT control
  the error *is* the torque, so clamping it leaves `wrist_yaw` (kp 9) and a
  gravity-loaded `elbow_flex` with too little force to move at all. Confirm
  with `B601_MAX_STEP_DEG=0 uv run robot`: if the joint comes back, it is the
  cap. If it does not, check the joint's feedback — `_present_pos` reports
  **0.0** for any motor whose CAN feedback did not arrive, so a joint whose
  state reads a constant 0.0 in the archive is a wiring or CAN-ID problem, not
  a control one.
- **The arm re-energizes on a bus reconnect.** Recovering the CAN bus runs the
  driver's `configure()`, which calls `enable_all()`, so the follower resumes
  tracking the leader on the next action. If it sagged under gravity while the
  bus was down, the catch-up is a ramp at `B601_MAX_STEP_DEG × PORTAL_FPS`
  degrees per second rather than a snap — 150 °/s at the defaults, which is
  still brisk — because the rebuild re-seeds the ramp from the arm's present
  pose. Lower the cap if the arm is somewhere a mistake is expensive, and keep
  the leader still when you see `CURRENTLY DEGRADED`.

Previously listed and now fixed:

- **The speed cap froze the gravity-loaded joints.** Its first version clamped
  the commanded target to within `B601_MAX_STEP_DEG` of the *present* position.
  That is right for a position servo and wrong for this arm: `control_mode` is
  `"mit"`, so torque is `kp * (target - present)` and clamping the error caps
  the force to `kp * 5°` — 0.79 for `wrist_yaw`, 3.9 for `elbow_flex`. Those
  two stopped moving while the stiffer joints kept following. It now ramps the
  target instead, re-seeding from the present pose at startup, after a bus
  rebuild, and whenever commands pause, so the discontinuity is still a ramp
  but the tracking error — and the torque — is left alone.

- **A motor write error ended the run.** `send_action` and `get_observation`
  raised straight through `HardwareThread.run`, past the tick loop's `except`
  (which catches only cancellation) and into its `finally` — so a serial
  hiccup or a re-enumerated adapter quit the rig mid-motion over something
  usually gone by the next tick. `_ResilientBus` now swallows and counts
  them, retries on the next tick, and rebuilds the CAN bus after
  `B601_BUS_ERRORS_BEFORE_RECONNECT` consecutive failures. Video keeps
  publishing throughout, so a stopped arm is visibly a stopped arm.
- **Hotkeys were dead on Wayland and over SSH** with no recourse. The
  teleoperator now runs a terminal tap alongside the global one, so the keys
  work anywhere the terminal has focus.

- **Recording rode on the operator's tick loop.** The dataset writer, the
  frame decode and the episode state machine all lived in
  `teleoperator/__main__.py`, so a disk stall or a slow encode was a stall
  in the loop driving the arm, and restarting the teleoperator ended the
  recording. Recording is now its own process — a Portal observer — and
  the two are independent. What
  made the split possible is `in_reply_to_ts_us`: the operator declares
  which frame each action answers, so the recorder can align the streams
  without having produced them.

- **Camera reconnect could never succeed.** `_ResilientCamera._reconnect`
  called the `@check_if_already_connected`-decorated `connect()`, which
  tested the same `is_connected` property the wrapper patches to report
  True while holding a buffered frame — so every reconnect after the first
  frame raised `DeviceAlreadyConnectedError`. It now calls the undecorated
  body via `__wrapped__`.
- **A hung camera never triggered a reconnect.** Only a dead read thread
  did. A camera whose thread is alive but has stopped producing now
  escalates to a reconnect after 30 consecutive stale reads (~1s at 30fps).
- **`except KeyboardInterrupt` was dead code** in both loops. Under
  `asyncio.run`, Ctrl-C raises at the `run_until_complete` frame and the
  runner then cancels the task, so both loops now also catch
  `asyncio.CancelledError`.
- **`deploy_to_robot.sh` created a literal `~` directory** on the remote.
  The `mkdir` path is no longer single-quoted, so a leading `~` expands
  the same way rsync expands it.
- **An over-budget tick starved the event loop completely.** `pace()`
  awaited nothing when a tick ran past its interval, and `async for` is
  not itself a suspension point — so a loop whose body consistently
  overran `1/fps` never handed control back. On the robot that meant
  `on_action` stopped firing and the arm stopped following while the loop
  still looked healthy, with the reported RTT climbing without bound
  because the transport was never serviced either. The over-budget path
  now `await asyncio.sleep(0)`. That alone only restores *one* yield per
  tick, so the blocking device I/O moved off the loop as well — both tick
  bodies await their driver calls on a `HardwareThread` (9 callbacks/s
  under a 50ms tick before, 94/s after). Symptoms if it regresses: RTT
  climbing without bound, video arriving in bursts, and the arm going
  limp while both processes still look healthy.
- **A failed gate claim was invisible.** `maintain_control` swallowed
  every exception at `debug` and never said anything when no robot was in
  the room, so an unclaimed gate presented as a leader arm that simply did
  not drive the follower. It now reports each distinct situation once.
- **A missing rerun viewer binary took the whole rig down** at startup.
  The operator's window is now optional; teleop continues without it, and
  the recorder never spawns one at all — it writes a file.
