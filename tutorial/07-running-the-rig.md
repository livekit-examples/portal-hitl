# 7. Running the rig

Set up the hardware, record demonstrations, build a dataset, and train a
policy. Then run the policy alongside the teleoperator to collect human
corrections. Follow the steps in order for your first session.

## What you need

- A LiveKit project (URL, API key, API secret) for the wire layer.
- A reBot Arm B601-DM follower: 6-DOF arm + gripper (Damiao CAN motors),
  two USB cameras (arm-mounted, overhead).
- A reBot 102 leader arm for teleop, on whichever machine runs
  `teleoperator.py`.
- Python 3.12 and `uv` (`pyproject.toml` requires `>=3.12,<3.13`).

```bash
git clone <this repo>
cd portal-hitl
cp .env.example .env       # fill in LIVEKIT_*, serial/CAN ports, camera devices
uv sync
```

Requires `livekit-portal>=0.3.0`, with the `rerun` extra for the
recorder and the `lerobot` extra for `livekit-portal convert`. The B601 follower and
reBot 102 leader come from Seeed's lerobot plugins (`lerobot-robot-seeed-b601`,
`lerobot-teleoperator-rebot-arm-102`) plus `motorbridge` /
`motorbridge-smart-servo` on PyPI; `lerobot` itself is stock PyPI
`lerobot[training]` (ACT imports cleanly with no transformers).

### Calibrate the arms

The follower and leader each need calibration before first use. Both
sides run the standard lerobot prompts on first connect: center the arm,
walk through ranges of motion, save. Calibration files are stored under
`~/.cache/huggingface/lerobot/calibration/` keyed by the `B601_ID` and
`REBOT102_LEADER_ID` env vars. Once calibrated, subsequent connects
accept the saved file.

If `robot.py` or `teleoperator.py` blocks at startup waiting for input,
that's the calibration prompt; press ENTER to use the saved file or `c`
then ENTER to recalibrate.

### Find the right ports / cameras

The B601 follower talks to its Damiao motors over CAN. With the default
`B601_CAN_ADAPTER=damiao` (a USB-serial dongle) the port is a
`/dev/ttyACM*` / `/dev/ttyUSB*` device; with `socketcan` it's a `canN`
channel. The reBot 102 leader is a serial device (the wiki uses
`/dev/ttyUSB0`).

Linux: `ls /dev/serial/by-id/` and `ls /dev/tty{ACM,USB}*` for the arms,
`v4l2-ctl --list-devices` for cameras (use `/dev/video*`). macOS:
`ls /dev/tty.usbmodem* /dev/tty.usbserial*`, camera index is an integer
(0, 1, ...) per AVFoundation.

## Collect data

Three terminals, three processes, all in the same LiveKit room:

```bash
# Terminal 1: physical B601-DM host.
uv run robot.py
# Waits for camera frames + CAN bus, prints "connected" when ready.

# Terminal 2: anywhere with the LiveKit creds, usually the operator's machine.
uv run recorder.py
# Waits for the robot's clock, then prints the .rrd path it records to.

# Terminal 3: human operator's machine, with the reBot 102 leader.
uv run teleoperator.py
# Spawns rerun, prints hotkey help.
```

Once both are connected, on the teleoperator window:

| Key | Action |
|---|---|
| `c` | Toggle active operator between human (self) and policy. The arm only moves when an operator is active. |
| `r` | Start an episode (`recording` keypoint), or end it (`idle`). |
| `[` | Discard the in-flight episode (`discard` keypoint). |
| `x` | Clean quit. |

A typical recording session:

```
(robot.py, recorder.py and teleoperator.py running, no operator active)
press c        → arm follows the leader
move arm to start position
press r        → episode starts
perform task
press r        → episode ends
... repeat; press [ instead to throw a bad take away ...
press x        → quit teleop; ctrl-c the recorder to close its archive
```

The hotkeys follow the keypoint convention `livekit-portal convert`
reads: `recording` with a `{"task_description": ...}` payload opens an
episode, `idle` closes it, `discard` drops it. The task is
`PORTAL_HITL_TASK` from `.env`. See
[04. HITL recording](04-hitl-recording.md#mark-episodes-with-keypoints).

The recorder writes one `.rrd` per run to `data/recordings/` (override
with `PORTAL_HITL_RECORDING_DIR`). Watch any of them back with
`rerun data/recordings/<file>.rrd`.

## Build the dataset

Convert every archive into one `LeRobotDataset`:

```bash
uv run livekit-portal convert data/recordings --output lerobotv3 --dry-run   # look first
uv run livekit-portal convert data/recordings --output lerobotv3 \
    --repo-id local/portal-hitl --robot-type seeed_b601_dm_follower
```

The dataset lands in `data/local/portal-hitl/`. Pass `--repo-id
you/your-recording-repo --push` to upload it to the Hugging Face Hub
(log in with `hf auth login` first), and `--overwrite` to rebuild it
after recording more sessions. Keep the archives: they're the source of
truth, and you can convert them again with different options any time.
`--overwrite` replaces the output directory; conversion doesn't append rows.

The dry run reports archive contents and checks schema compatibility without
validating every output row. Conversion selects active actions by default,
with observations up to 200 ms older than their anchors. Use
`--max-stale-ms 0` for exact matches. Rows missing required observations are
skipped. Discarded and unclosed takes are excluded; an archive with no start
marker is treated as one episode.

The dataset frame rate defaults to the robot's state rate. The converter
writes one row per selected action with complete observations and doesn't
resample or preserve gaps. See [One row per action](04-hitl-recording.md#one-row-per-action)
before mixing human and policy streams with different action rates.

## Train a policy

`policies/<algo>/train.py` wraps `lerobot.scripts.lerobot_train` with a
few shims (DataLoader tuning, FUSE-safe symlinks, optional single-camera
ablation). Configure via env:

```bash
DATASET_REPO_ID=you/your-recording-repo \
NUM_STEPS=20000 \
BATCH_SIZE=64 \
uv run --only-group train python policies/act/train.py
```

For cloud GPU runs, each algo ships a `skypilot.yaml`:

```bash
sky launch policies/act/skypilot.yaml -e DATASET_REPO_ID=you/your-repo
```

ACT trains on whatever cameras the dataset has.
Checkpoints are saved every quarter of total steps to
`/outputs/<RUN_NAME>/checkpoints/<NNNNNN>/pretrained_model/`.

Training is plain lerobot; Portal isn't involved at all.

## Run inference

Hand control off to a trained checkpoint with the same robot and teleop
running in the background:

```bash
# Terminal 3: any machine with the LiveKit creds and the checkpoint.
uv run policies/act/inference.py --checkpoint path/to/025000
```

The policy starts disengaged. Press SPACE in the policy window to
engage; press again to disengage. Press `x` to quit. By default the
policy self-claims the active operator on connect, so it can drive even
without a teleop running. With a teleop also running, the teleop's `c`
hotkey takes control back from the policy mid-episode (useful for HITL
corrections, all of which are recorded).

Useful flags:

```
policies/act/inference.py
  --checkpoint PATH                # or env ACT_CHECKPOINT
  --no-temporal-ensemble           # execute full chunk before replanning
  --temporal-ensemble-coeff 0.01   # smoother (lower) vs more reactive (higher)
  --no-claim                       # don't auto-claim active operator
```

This is the full HITL loop: the policy drives, you catch the failures by
pressing `c` to take over, and the observer records your corrections with
your sender identity. Convert the retained episodes, inspect the resulting
data, and use it for the next training run. The mechanics behind
the handoff are covered in [05. Handoff](05-handoff.md).

## Deploy to the robot host

When the B601-DM host is a separate machine (the common case),
`scripts/deploy_to_robot.sh` rsyncs the repo over SSH and prints
follow-up commands.

```bash
# One-off, with the remote on the command line.
REMOTE=robotuser@robot.local ./scripts/deploy_to_robot.sh

# Or set defaults in .env so plain `./scripts/deploy_to_robot.sh` works:
#   PORTAL_HITL_ROBOT_REMOTE=robotuser@robot.local
#   PORTAL_HITL_ROBOT_REMOTE_ROOT=~/workspace      # default ~/workspace
```

What it does:

1. `mkdir -p` the destination on the remote.
2. `rsync -azP --delete` the project tree, honoring
   `scripts/deploy.rsyncignore`.
3. Print the next steps to run on the robot.

What gets synced and what doesn't:

| Synced | Excluded |
|---|---|
| Source code, `portal.yaml`, `pyproject.toml`, `uv.lock` | `.git`, `.venv`, Python caches |
| `.env` (so the robot inherits LIVEKIT and port config) | `.env.local` (each machine keeps its own overrides) |
| `policies/` source | `data/`, `checkpoints/`, `outputs/` (recordings, datasets and checkpoints stay off the robot) |

After the rsync, on the robot:

```bash
cd ~/workspace/portal-hitl
uv sync
uv run robot.py
```

First-time calibration walks through ranges of motion when `robot.py`
connects with no saved file; saved files live under
`~/.cache/huggingface/lerobot/calibration/` keyed by `B601_ID`.

If you change anything locally, just rerun the script. `--delete` keeps
the remote tree byte-identical to your working copy minus the
exclusions.

## Where to put each process

The four Portal peers (robot, recorder, teleop, policy) can run on the
same machine or different ones. Common splits:

| | robot.py | recorder.py | teleoperator.py | policies/<algo>/inference.py |
|---|---|---|---|---|
| Single machine demo | localhost | localhost | localhost | localhost |
| Remote teleop, local policy | B601-DM host | operator desk | operator desk | operator desk |
| Cloud policy | B601-DM host | operator desk or a server | operator desk | cloud GPU box |

LiveKit handles the wire in all cases. The `portal.yaml` does not
hardcode IPs; only `LIVEKIT_URL` does, and that's an env var.

---

And that's the whole loop, end to end. We started from a shared wire
contract, brought up a robot that publishes synced observations, drove it
with a human and a policy through the same operator interface,
recorded every correction, and turned the recordings into trainable data. Now it's yours; go build
something with it.
