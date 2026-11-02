# CLAUDE.md

Teleoperated data collection on top of [LiveKit
Portal](https://github.com/livekit/portal). A Seeed reBot Arm B601-DM
follower is driven over the network by an operator holding a reBot 102
leader arm; a third peer, a Portal observer, records state, video, actions
and keypoints into one `.rrd` on the robot's clock with Portal's `RrdSink`. Every action carries
`in_reply_to_ts_us` — the capture timestamp of the frame the operator was
looking at — which is what lets a separate process align the three streams
without having produced any of them.

This repo is a reference project. It is meant to be *read*, so clarity of
the three `__main__.py` files and the accuracy of `docs/` matter as much as
the code working.

## Documentation is part of the change

**`docs/` is the source of truth for how this repo works. Any change that
alters behavior, configuration, or structure must update the relevant doc
in the same change.** Not a follow-up, not a TODO — the same change.

Before editing code:

1. **Read the doc that covers the area you are touching.** It carries the
   reasoning behind the current design — the receive-clock choice in the
   recorder, why control is claimed in a loop, why the camera wrapper
   lies about `is_connected`. Do not undo one of those without
   understanding why it is there.
2. **Cite it.** When explaining a change, reference the doc section it
   comes from or contradicts.

After editing code, update the docs in the same pass:

| If you change… | Update |
|---|---|
| the tick loops, Portal usage, the archive's alignment, hardware glue | [docs/architecture.md](docs/architecture.md) |
| `portal.yaml`, any env var, a default value, the codec | [docs/configuration.md](docs/configuration.md) — and `.env.example` |
| how the rig is set up, run, calibrated, or deployed | [docs/operations.md](docs/operations.md) |
| the keypoint types or payload | `RECORDING` / `IDLE` / `DISCARD` in `teleoperator/__main__.py`, which must match `livekit-portal convert`'s episode rule; then architecture.md |
| a known bug — fixed it, or found a new one | the **Known issues** list in [docs/operations.md](docs/operations.md#known-issues) |
| the file layout | the layout table in [docs/architecture.md](docs/architecture.md#layout) and `README.md` |
| what the tests cover, or add a profile | [test/README.md](test/README.md) |

If a change makes a doc statement false, fix the statement. Stale docs in
a reference project are worse than none — this repo has already been
through one round of cleanup for exactly that.

Do not add new top-level docs without reason; the three files above are
meant to stay the whole set.

## Docs

| File | Covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Portal roles and the sync/gate model, the wire contract, both tick loops, the anchor and the archive's two timelines, marking takes, the camera resilience wrapper, file layout |
| [docs/configuration.md](docs/configuration.md) | every `portal.yaml` key and env var, the codec trade-off table |
| [docs/operations.md](docs/operations.md) | setup, calibration, running all three, hotkeys, the 5s diagnostics, reading an archive, deploy, known issues |

## Layout

```
portal.yaml             # the wire contract all three peers load
portal_rebot/
  common.py             # env loading, LiveKit tokens, identities, the tick pacer
  robot/
    __main__.py         # Portal Robot flow           -> `uv run robot`
    follower.py         # B601-DM hardware: CAN motors, resilient cameras
  teleoperator/
    __main__.py         # Portal Operator flow        -> `uv run teleoperator`
    leader.py           # reBot 102 leader arm
    hotkeys.py          # keyboard listener, drained once per tick
    session.py          # startup questions: the session's task list
    preview.py          # the window: one static image per camera
  recorder/
    __main__.py         # Portal Observer + RrdSink   -> `uv run recorder`
scripts/                # deploy_to_robot.sh, deploy.rsyncignore
test/                   # hardware-free validation against a local LiveKit server
docs/                   # the three files above
```

## Commands

```bash
uv sync                          # install (also installs the three console scripts)
uv run robot                     # robot side, on the B601-DM host
uv run recorder                  # listen-only archive writer, wherever the disk is
uv run teleoperator              # operator side, with the leader arm
uv run livekit-portal convert <paths> --output lerobotv3   # archives -> LeRobot v3, offline
./scripts/deploy_to_robot.sh     # rsync to the robot host

# validation -- needs a local `livekit-server --dev`, no hardware
uv run python -m test.test_pubsub     # transport, gate, frame/state sync
uv run python -m test.test_recorder   # three peers, archive round-trip, alignment
uv run python -m test.test_degraded   # the same under sender-side degradation
uv run python -m test.test_resilience # bus faults + speed cap   (no server)
uv run python -m test.test_teleop     # anchor, tasks, hotkeys   (no server)
```

All three are `[project.scripts]` entry points onto a `run()` function, so
`uv run python -m portal_rebot.robot` is the same thing. `portal.yaml` and
`.env` live at the repo root. `portal_rebot/common.py` always resolves the
schema there, but also loads environment files from the working directory.
Relative recording paths use the working directory. See the environment-loading
order in [Configuration](docs/configuration.md#environment-variables).

`test/` runs the real Portal stack against a local LiveKit server with
synthetic peers, so transport, gate, keypoints and alignment are all
checkable without an arm. Run it after touching either loop, the archive
or `portal.yaml`. Only the hardware drivers themselves need real hardware.
See [test/README.md](test/README.md).

## Conventions

- **`portal.yaml` is the one schema.** All three peers load it; peers that
  disagree drop each other's packets with a fingerprint warning. Never
  hardcode a field list in Python that duplicates it — read
  `cfg.state_schema` / `cfg.action_schema`.
- **Hardware glue stays out of the loops.** `robot/__main__.py` and
  `teleoperator/__main__.py` should read as the Portal flow alone;
  anything device-specific belongs in `robot/follower.py` or
  `teleoperator/leader.py`.
- **The teleoperator does not record.** It flies the arm, shows a picture
  and marks boundaries. Anything that writes to disk belongs in
  `recorder/`, on the other side of the link -- a disk stall must never be
  a stall in the loop driving the arm.
- **`robot_time` aligns, `action_time` preserves.** An action is filed on
  `robot_time` at its `in_reply_to_ts_us` -- never at its own
  `timestamp_us`, which is when it was sent, not what it answered -- and that is what
  co-locates it with the frame and state it answered. It carries
  `action_time` as a second index because a rerun index keeps ONE value
  per entity: without it, two actions answering one frame lose the first
  outright (25-28% of them on a link with any jitter). Both indices are on
  the ROBOT's clock -- Portal syncs the teleoperator to it and stamps actions
  in robot time -- but they stay separate because one is measured and one
  synced, and merging them would make the measured positions unverifiable.
  `RrdSink` keeps both. Never let the synced clock touch
  `in_reply_to_ts_us`: that is the one column no clock model can be wrong
  about, and the zero-tolerance alignment tests rest on it.
- **No command reaches the motors unramped.** `_StepLimiter` sits inside
  `send_action` and advances the commanded TARGET by at most one step, then
  re-seeds that target from the present pose at startup, after a bus rebuild,
  and whenever commands pause -- which is what makes a bus recovery, an
  operator handover and a cold start all ramps instead of lunges. It must
  never clamp the tracking error instead: `control_mode` is `"mit"`, so
  torque is `kp * (target - present)` and clamping the error caps the FORCE,
  which stops the gravity-loaded joints moving at all. Do not add a path
  around it, and do not "simplify" it back to a present-relative clamp.
- **Nothing hardware- or IO-shaped may raise into the tick loop.** The
  camera wrapper, the motor-bus wrapper and the preview logger all swallow
  and report rather than propagate. A device fault reconnects; it never ends
  the run. Keep that property -- and the same goes for a Portal callback.
- **Config comes from env vars with defaults in code**, documented in
  the `docs/configuration.md` tables — that file is the complete set, and
  a new variable must appear there. `.env.example` is not a mirror of it:
  it lists what has no usable default (credentials, hardware addresses)
  plus anything whose right value is a per-deployment judgement call even
  though a default exists -- currently just `B601_MAX_STEP_DEG`, because
  how fast the arm may be commanded to move depends on what it is near.
  That exception is for safety knobs, not for tuning. Per-session choices are asked at startup instead, in
  `teleoperator/session.py`; add one there rather than growing `.env`.
  A session's task list lives there, and nine is its ceiling because the
  hotkeys are the digits `1`-`9`.
  Defaults that restate `portal.yaml` are a bug — read the config
  (`cfg.fps`), do not hardcode a second copy.
- **Comment only what the code cannot say.** Delete any comment that
  restates the line below it, names what a function obviously does, or
  labels a section. Keep the ones carrying a fact you cannot recover by
  reading the code: an upstream library bug and the workaround it forces
  (`OpenCVCamera.connect`'s decorator, libwebrtc's degradation
  preference), a clock choice, gate ordering, an OpenCV backend quirk, a
  threshold's units. If a comment could be replaced by a better name or a
  smaller function, do that instead of writing it. When you touch a file,
  strip the comments in it that fail this bar.
- **Verify against the installed libraries, not from memory.** lerobot's
  hardware APIs move between versions — check `.venv` before asserting a
  signature or a driver's behavior.
