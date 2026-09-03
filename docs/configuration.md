# Configuration

Three places, in order of how often you touch them.

1. **Asked at startup.** What you are collecting — the task list — is a
   per-session choice, so `uv run teleoperator` asks for it rather than
   reading it from a file you forget to edit.
2. **`.env`** — per-deployment: credentials, serial ports, camera
   devices. It carries only what has no sensible default. Per-machine
   overrides go in `.env.local`, loaded after `.env` and not synced by
   the deploy script.
3. **`portal.yaml`** — the wire contract, identical on all three peers.

Every variable below still works if you set it; `.env.example` lists the
ones with no usable default, plus `B601_MAX_STEP_DEG` -- the speed cap has
a default, but the right value depends on what the arm is near, so it is
spelled out where you will see it. This table is the full set.

## `portal.yaml`

| Key | Value here | Notes |
|---|---|---|
| `version` | `1` | contract format version |
| `fps` | `30` | observation rate; both sides read `PORTAL_FPS` separately for their tick rate |
| `slack` | Portal default (commented out) | ticks the synchronizer waits for late state/frames before forcing emission. Bigger absorbs more jitter at the cost of end-to-end latency. Was `10` (≈333ms at 30fps) to diagnose `overhead_camera` sync-buffer overflows; that camera is no longer declared, so the override is gone. |
| `tolerance` | default | state/frame timestamp match window, in ticks |
| `reuse_stale_frames` | `true` | see below |
| `videos` | one `h264` track at `max_bitrate_kbps: 20000`, `screencast: true` | `arm_camera`, the wrist-mounted camera. Adding `overhead_camera` back needs a track here *and* `B601_CAM_OVERHEAD`. |
| `state` / `action` | 7 `f32` fields each | `shoulder_pan.pos` … `gripper.pos` |

`action_subscription` is deliberately **not** here even though it is a
loadable key. It is a local receive preference rather than part of the
contract, and only one of the two operators wants it: the recorder sets
`cfg.set_action_subscription(True)` in code, the teleoperator has no use for
an echo of its own sends.

### `slack`, `tolerance`, `reuse_stale_frames`

These three configure Portal's synchronizer — the path that matches state
to frames and emits a single `Observation`. **Nothing in this repo consumes
that path.** The recorder subscribes to the raw `on_state` /
`on_video_frame` / `on_action` streams and files each at its own timestamp,
so none of the three gates anything that reaches the archive. What they
still shape is `metrics().sync`, which the recorder plots, and anything you
add later that calls `on_observation` or `get_observation` — the test peers
do. See [architecture.md](architecture.md#two-timelines-and-why-it-is-not-one)
for what replaced the synchronizer here.

`reuse_stale_frames: true` means a state sample reaching the synchronizer
with no fresh frame in the window reuses the last good frame instead of
dropping the state. It is the same trade the operator makes when it anchors
an action to a frame that has gone stale: the operator was flying on that
picture, so filing the action against it is the honest thing to do.

**Consequence, and it survives the change:** every declared track must
actually publish. The synchronizer withholds observations until every
track has emitted at least once, and `View.anchor()` likewise withholds the
operator's anchor until it holds a frame from *every* camera. With one track
silent, no action carries an `in_reply_to_ts_us` at all, the recorder counts
every one of them as `unanchored`, and the archive gets no actions
whatsoever.

### The state and action schema

Seven absolute joint positions in the B601 follower's native order —
`shoulder_pan`, `shoulder_lift`, `elbow_flex`, `wrist_flex`, `wrist_yaw`,
`wrist_roll`, `gripper` — all `.pos`, in degrees at the lerobot boundary.
Action mirrors state; the reBot 102 leader emits one of these per tick.

`RebotB601Follower.get_observation()` returns `.pos` keys only, so
`split_state_frames` filtering on `.pos` is currently a no-op guard
rather than a filter. If a future driver adds velocities or torques, add
them to both schemas and relax that filter together.

### Codec choice

The one track is `h264` at `max_bitrate_kbps: 20000, screencast: true`.

| Codec | Path | Trade-off |
|---|---|---|
| `mjpeg` | reliable data channel | Per-frame JPEG, frame-independent, sub-ms decode. `quality` is 1..100 (Portal defaults to 90) and is the only rate knob — effective rate is JPEG size × fps. The better source for training data: no inter-frame prediction artifacts. |
| `png` | reliable data channel | Lossless, ~2-3× — bit-exact, and much heavier than mjpeg. |
| `raw` | reliable data channel | Uncompressed RGB24. Largest payload; rarely justified. |
| `h264` | WebRTC media, separate congestion-controlled RTP | Lowest latency, never competes with state. Inter-frame prediction artifacts degrade it as training material. Needs `max_bitrate_kbps` (peak ceiling, not a target; 10 Mbps if omitted) and `screencast: true`. |

**Why h264 here.** Teleop is a closed loop through the operator's eyes,
so it is bounded by latency. h264 rides WebRTC media — its own
congestion-controlled RTP path — so video never competes with
`portal_state`, and under pressure it degrades by dropping and
re-encoding rather than by queueing. Lag stays bounded instead of
accumulating.

**Both extra keys are mandatory, not tuning.** `max_bitrate_kbps` is a
*peak ceiling*, not a target; omitted it is 10 Mbps. 20000 is generous
headroom for one 640×480@30 track (visually transparent by roughly 8
Mbps), so the encoder is never the binding limit. `screencast: true`
picks libwebrtc's degradation preference: the default (camera content) is
MAINTAIN_FRAMERATE, which holds the frame rate and **rescales the frame**
under pressure, silently leaving the dataset with mixed geometry —
discovered at training time. Screen content is MAINTAIN_RESOLUTION:
geometry pinned, frames dropped instead. `simulcast: false` is worth
stating explicitly too: extra spatial layers cost encode CPU and only pay
off with several operators on links of differing quality. All three keys
need portal >= 0.2.5, and Portal ignores them on a byte-stream codec.

**What it costs: inter-frame prediction.** Recorded frames carry P-frame
artifacts that mjpeg's per-frame JPEG does not, and h264 is the weaker
source for training data because of it.

**Going the other way, to mjpeg**, is the trade if the dataset matters
more than the latency: every recorded frame is an independent JPEG, and
the geometry cannot change mid-session because nothing re-negotiates
resolution. The cost is the transport. mjpeg, png and raw are byte-stream
codecs — they ride the same **reliable** data channel as `portal_state`.
Exceed the link and they head-of-line-block and *starve state*:
publish-queue and sync-buffer overflows, state drops, and lag that
accumulates rather than recovering. Budget the video **below** your
sustained uplink, not merely near it. One 640×480 track at 30fps and
quality 90 measures roughly 15–30 Mbps depending on scene detail — a LAN
or a fat, uncontended link. On a thin one, drop `quality` first (75 is
about half the bytes), then `PORTAL_FPS`, then resolution.

This repo has been both ways: it shipped h264 @ 50 Mbps, switched to
mjpeg in `63973d8` for frame independence, and switched back when the
added latency proved to be what teleop actually noticed.

## Environment variables

Copy `.env.example` to `.env` and fill it in. `portal_rebot/common.py` loads
`.env` then `.env.local` (which overrides), from both the repo root and
the cwd.

### LiveKit — required, all three peers

| Variable | Default | Notes |
|---|---|---|
| `LIVEKIT_URL` | — | `wss://your-project.livekit.cloud` |
| `LIVEKIT_API_KEY` | — | used to mint the JWT locally |
| `LIVEKIT_API_SECRET` | — | |
| `LIVEKIT_ROOM` | — | all three peers must use the same room |
| `PORTAL_FPS` | `portal.yaml`'s `fps` | tick rate for the robot and teleoperator loops. Follows the wire contract unless you override it, so the two cannot silently disagree. The recorder has no tick loop and ignores it. |
| `LOG_LEVEL` | `INFO` | Python-side logging |
| `PORTAL_HOTKEYS_GLOBAL` | `1` | `0` leaves the terminal as the only key source. The global tap reads the whole machine, which is what makes it work while you look at the rerun window — and what makes it capture keys meant for whatever else you are doing. |
| `RUST_LOG` | `warn`, minus two noisy modules | the LiveKit/Portal core's own log. See below. |

**`RUST_LOG` defaults to
`warn,livekit_portal::sync_buffer=error,libwebrtc=error`.** Both silenced
sources are noise for this rig specifically: `sync_buffer` emits `[sync-drop]`
warnings about state that found no matching frame, and nothing here consumes
the synchronizer at all — the recorder subscribes to the raw streams and files
each at its own timestamp — while `libwebrtc` reports h264 receive-queue
chatter at frame rate under any load. Everything else stays at `warn`.

Set it yourself to get the full picture back when something needs diagnosing:

```bash
RUST_LOG=info uv run robot
```

It must be set **in the shell, not in `.env`**. `livekit.portal` reads
`RUST_LOG` when its shared library loads, which is the import at the top of
each `__main__.py` — long before `load_env()` runs. `portal_rebot/__init__.py`
sets the default for exactly that reason, since a package's `__init__` runs
before the imports inside its modules.

Tokens are minted in-process by `portal_rebot.common.mint_token` with a 6-hour TTL
and `can_update_own_metadata`, which Portal needs to publish its
`lk.portal.role` attribute for peer discovery.

### B601-DM follower — robot host

| Variable | Default | Notes |
|---|---|---|
| `B601_ID` | `follower1` | keys the saved calibration file |
| `B601_PORT` | `/dev/ttyACM0` | CAN adapter port, or the `canN` channel for socketcan |
| `B601_CAN_ADAPTER` | `damiao` | `damiao` = USB-serial dongle; `socketcan` = a `canN` channel |
| `B601_DM_SERIAL_BAUD` | `921600` | |
| `B601_CAM_<TRACK>` | `/dev/video0`, `/dev/video2`, … | one per video track; trailing `_camera` dropped and uppercased (`arm_camera` → `B601_CAM_ARM`). Linux: `/dev/videoN`. macOS: an integer index. |
| `B601_CAM_WIDTH` / `_HEIGHT` | `640` / `480` | |
| `B601_CAM_MAX_FRAME_AGE_MS` | `500` | how old a buffered frame may be before the tick logs it stale and serves it anyway |
| `B601_BUS_ERRORS_BEFORE_RECONNECT` | `5` | consecutive motor I/O failures before the CAN bus is rebuilt. ~166 ms at 30 fps. Lower reacts faster to a dead adapter; higher rides out more transient CAN noise before blocking a tick on a rebuild. |
| `B601_BUS_RECONNECT_INTERVAL_S` | `2.0` | minimum gap between rebuild attempts, so a bus that is genuinely gone does not retry every tick |
| `B601_MAX_STEP_DEG` | `5.0` | **the speed cap.** How far the commanded target may advance per command. `0` disables it. See below. |

### The speed cap

`_StepLimiter` sits inside `send_action`, so there is no path to the motors
that skips it. Each command advances the **commanded target** by at most
`B601_MAX_STEP_DEG`; the target is re-seeded from the arm's present position at
startup, after a bus rebuild, and whenever commands pause.

**It is per command, so the rate it implies depends on `PORTAL_FPS`:**

| `B601_MAX_STEP_DEG` | at 30 fps | a 90° move takes |
|---|---|---|
| `0.17` | 5 °/s | 18 s |
| `1.0` | 30 °/s | 3 s |
| `5.0` (default) | 150 °/s | 0.6 s |
| `0` | unlimited | — |

The default stays out of the way of normal teleop while making a lunge
impossible; drop it to `1.0` or below for bring-up, or when the arm is
somewhere a mistake is expensive. If the robot's 5s line reports commands being
clamped continuously, the cap is below how fast you are moving the leader —
raise it or move more slowly, because everything above it is being flattened
out of the demonstration you are recording.

**What it does not do is clamp the tracking error, and that distinction is
load-bearing.** The B601 runs `control_mode = "mit"` — impedance control, where
torque *is* the position error:

```
torque = kp * (target - present) + kd * (target_vel - vel)
```

So a cap that held `target` within N degrees of `present` would not limit
speed, it would limit **force** to `kp * N` radians-worth. At 5 degrees that is
0.79 for `wrist_yaw` (kp 9) and 3.9 for `elbow_flex` (kp 45): not enough to
hold a gravity-loaded joint, which then stops moving entirely while the stiffer
joints carry on. Ramping the target leaves the error — and so the torque —
alone.

| Variable | Default | Notes |
|---|---|---|
| `B601_UNCAPPED_JOINTS` | `gripper` | comma-separated joints the ramp skips entirely. See below. |
| `B601_MAX_LAG_DEG` | `30.0` | windup guard: how far the target may lead the present position. Not a speed limit — it only stops the setpoint running away from an arm that is blocked or overloaded, which would wind the torque up without limit. Set well above any tracking error normal teleop produces. |

**The gripper is exempt by default**, and it is the one joint that should be:

| | arm joints | gripper |
|---|---|---|
| control mode | `mit` (impedance) | `force_pos` |
| what bounds its force | `kp` × tracking error | `gripper_torque_ratio` — 7%, always |
| driver's own speed | 150 °/s | 900 °/s |
| travel | ≤ 200° | 270° |

The reasoning behind the cap does not reach it. Its force is already bounded
by the torque ratio no matter how far the command sits from the jaw, so
ramping the setpoint buys no safety — and the arithmetic is actively against
it: 270° of travel at the arm's cap is a 1.8-second squeeze, which is not a
grasp. Its soft joint limits still apply, so exempt is not unbounded.

Add a joint to `B601_UNCAPPED_JOINTS` only with a reason that specific. Every
arm joint is under impedance control, where the ramp is the only thing between
a bad command and a lunge.

It caps the setpoint, not the servo: the motor still picks its own trajectory
toward each target, so the hard limit lives in the motor firmware. What this
guarantees is that no single command asks for a jump. Before the first
observation there is nothing to ramp from, so the command is refused rather
than passed through.

### reBot 102 leader — teleoperator host

| Variable | Default | Notes |
|---|---|---|
| `REBOT102_LEADER_PORT` | **required** | the wiki uses `/dev/ttyUSB0` |
| `REBOT102_LEADER_ID` | `rebot_arm_102_leader` | keys the saved calibration file |
| `REBOT102_BAUDRATE` | `1000000` | |
| `REBOT102_JOINT_DIRECTIONS` | package defaults | JSON object, e.g. `{"gripper": -6}`. Set only if a leader joint drives the follower the wrong way. |

### The task list — teleoperator host

**Asked at startup.** One task is the common case; give a count up to 9 and
each gets a digit hotkey you can switch between mid-session.

```
  how many tasks [1]: 3
  task 1 [reBot B601 manipulation]: Pick up the red cube
  task 2: Stack it on the blue
  task 3: Push the tray left
```

Nine is the ceiling because the hotkeys are `1`-`9`; a tenth would need a
modifier or a two-key sequence for a rig that has never wanted more than a
handful. A blank answer past the first task is re-asked rather than defaulted —
it would otherwise duplicate task 1 and silently mislabel every span collected
under it.

| Variable | Default | Notes |
|---|---|---|
| `PORTAL_DATA_TASK` | `reBot B601 manipulation` | prompt default for task 1 |
| `PORTAL_DATA_TASKS` | unset | the whole list, `;`-separated, skipping the questions entirely. Semicolons because a task description is a sentence and sentences contain commas. Truncated at 9. |

Whichever task is active rides on every `add_keypoint` and is drawn as the
`state/task` band in the archive, so each span names what it was collected
under without a lookup anywhere else. Switching is refused while a take is in
flight: a span that changes task halfway is mislabelled whichever label it ends
up with.

There is no dataset name to answer. The recorder owns its own output path, and
one `.rrd` per session is named by room and timestamp rather than by anything
you type.

### The archive — recorder host

| Variable | Default | Notes |
|---|---|---|
| `PORTAL_DATA_RRD_DIR` | `data/sessions` | where `<room>-<timestamp>.rrd` lands. Created if missing. The recorder is the process that needs the disk, so this is the one worth pointing somewhere with room. |
| `PORTAL_DATA_RRD_QUALITY` | `95` | JPEG quality for archived frames, 1..100. `0` stores them uncompressed. |

**What quality costs.** One 640×480@30 track measures roughly 0.3 GB/hour
at 95 and 2.3 GB/hour uncompressed. The frames have already been through
the transport codec by the time they arrive — under the default `h264` they
already carry P-frame artifacts, so a lossless archive of them is
preserving a lossy source. Set `0` only if you switched the wire to `png`
or `raw` and want the archive to be bit-exact end to end.

The recorder starts writing when it joins the room, not when a keypoint
arrives. A session file therefore holds the minutes around a demonstration
as well as the demonstration itself — which is usually where the
explanation for a bad take is.

### Deploy

No variables. `scripts/deploy_to_robot.sh` takes the host and the
destination as arguments — which machine you are pushing to is a
per-invocation choice, not deployment config. See
[operations.md](operations.md#deploy-to-the-robot-host).
