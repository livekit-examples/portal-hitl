# 7. Running the rig

This chapter runs the full cycle: collect demonstrations, train ACT, run the
policy, and record human corrections for the next training run.

## Set up

You need a B601-DM follower with a wrist camera, a reBot 102 leader arm, a
LiveKit server, Python 3.12, and `uv`. Run the robot on the follower host,
teleop on the leader host, and the recorder wherever you have disk space.
ACT inference can run on another machine in the same room.

From the repository root on each host:

```bash
cp .env.example .env
uv sync
```

Fill in the LiveKit credentials and room, follower CAN port, leader serial
port, camera device, and command step limit. Keep `portal.yaml` identical on
all hosts. Use `.env.local` for machine-specific overrides.

On Linux, use `ls /dev/serial/by-id/` to find arm ports and
`v4l2-ctl --list-devices` for cameras. On macOS, serial devices appear under
`/dev/tty.usbmodem*` or `/dev/tty.usbserial*`; cameras use integer indices.
Both arms prompt for calibration on first connection. Press ENTER to accept
saved calibration or `c` then ENTER to recalibrate.

Calibration files live under `~/.cache/huggingface/lerobot/calibration/`, in
`robots/rebot_b601_follower/<B601_ID>.json` and
`teleoperators/rebot_102_leader/<REBOT102_LEADER_ID>.json`.

## Collect demonstrations

Run these in separate terminals, on the appropriate hosts:

```bash
uv run src/robot.py
uv run src/recorder.py
uv run src/teleoperator.py
```

Enter the task description when teleop starts. Wait for live video and the
recorder’s output path, then:

```text
c       select the human operator
        move to the starting pose
r       begin a take
        demonstrate the task
r       end the take
        repeat, or use [ to discard a bad take
x       quit teleop
```

Use `1`–`9` to switch tasks between takes. Stop the recorder with Ctrl-C after
collection so it flushes the archive. Files are written under `data/sessions`
unless `PORTAL_DATA_RRD_DIR` overrides it.

Global hotkeys require desktop permissions. If they do not respond, focus the
teleop terminal and press them there. `NOT RECORDED` means a boundary did not
reach an observer.

## Convert and inspect

```bash
uv run livekit-portal convert data/sessions --output lerobotv3 --dry-run
uv run livekit-portal convert data/sessions --output lerobotv3 --repo-id local/portal-hitl
```

The dataset is written under `data/local/portal-hitl` by default. Discarded and
unclosed takes are excluded. Keep the original `.rrd` files so you can inspect
them in Rerun or change conversion settings later.

For a dataset hosted on Hugging Face, use your repo ID and add `--push`.

## Train ACT

On a CUDA training machine:

```bash
DATASET_REPO_ID=local/portal-hitl \
DATASET_ROOT=data/local/portal-hitl \
OUTPUT_DIR=outputs/my-act \
NUM_STEPS=20000 \
BATCH_SIZE=64 \
uv run --only-group train python src/policies/act/train.py
```

For a hosted dataset, set `DATASET_REPO_ID` and omit `DATASET_ROOT`. Training
saves checkpoints every quarter of the requested steps, with a minimum interval
of 1,000 steps. The command above ends at
`outputs/my-act/checkpoints/020000/pretrained_model`.

The wrapper resumes the latest checkpoint with saved configuration and training
weights, processor configuration, and optimizer/RNG state. It refuses an existing output directory without such a checkpoint.
Use a new `OUTPUT_DIR` for a new experiment. `DEVICE` defaults to `cuda`;
`NUM_WORKERS` defaults to `16`. `LR` and `CHUNK_SIZE` control the ACT optimizer
and action horizon. Video decoding uses PyAV by default; set `VIDEO_BACKEND`
to use another installed LeRobot backend.

For cloud training, edit the resources and storage mount in
[`src/policies/act/skypilot.yaml`](../src/policies/act/skypilot.yaml), then launch:

```bash
sky launch src/policies/act/skypilot.yaml -e DATASET_REPO_ID=you/your-dataset
```

The example uses a Nebius H100 and an `/outputs` object-storage mount. Change
the bucket and output directory for your own account and experiment.

## Run ACT and collect corrections

Start the robot, recorder, and teleoperator again. On the inference host, make
the checkpoint and training dataset metadata available, then run:

```bash
uv run --group train python src/policies/act/inference.py \
  --checkpoint outputs/my-act/checkpoints/020000/pretrained_model \
  --dataset-root data/local/portal-hitl
```

ACT joins as `policy-act` and waits for control. It uses CUDA, MPS, or CPU in
that order of availability. On the teleoperator:

1. Press `c` to select the human and position the arm.
2. Press `r` to start a take.
3. Press `c` to hand control to ACT.
4. Press `c` when you need to correct it with the leader arm.
5. Press `c` to let ACT continue, or `r` to end the take.

Recording spans the handoffs. Move the leader near the follower’s current pose
before taking over. The command ramp limits target changes, but does not replace
careful operation around the arm.

After stopping the recorder, convert the combined sessions into a new dataset:

```bash
uv run livekit-portal convert data/sessions --output lerobotv3 --repo-id local/portal-hitl-v2
```

Train on that dataset with a new output directory, then repeat the policy and
correction cycle. To rebuild an existing dataset root, conversion requires
`--overwrite`; it does not append to a non-empty root.

## Deploy and troubleshoot

```bash
./scripts/deploy_to_robot.sh robotuser@robot.local '~/workspace/portal-hitl'
```

The script syncs source and `.env`; `.env.local`, datasets, checkpoints, and
training outputs are excluded. Run `uv sync` on the destination afterwards.

The processes report throughput, clock synchronization, and recording counts.
For hardware faults, missing hotkeys, or the known timestamp regression after
stalls in Portal 0.3.0rc1, see [Troubleshooting](#troubleshooting).


## Configuration

Copy `.env.example` for the settings needed to connect the rig. `src/_common.py`
loads repo-root `.env`, then `.env.local`, then the same files from the working
directory. `.env` fills unset values; `.env.local` overrides them, including
shell values. Relative output paths use the working directory.

| Variable | Default | Purpose |
|---|---|---|
| `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `LIVEKIT_ROOM` | required | Same server and room for all peers |
| `PORTAL_FPS` | `portal.yaml` fps | Robot, human, and policy tick rate |
| `LOG_LEVEL` | `INFO` | Python logs |
| `RUST_LOG` | warn with noisy modules suppressed | Set in the shell before startup for Portal diagnostics |
| `PORTAL_HOTKEYS_GLOBAL` | `1` | Set `0` for terminal-only input |
| `PORTAL_DATA_TASK` | `reBot B601 manipulation` | Default task description |
| `PORTAL_DATA_TASKS` | unset | Semicolon-separated tasks; skips prompts, maximum nine |
| `PORTAL_DATA_RRD_DIR` | `data/sessions` | Archive output directory |
| `PORTAL_DATA_RRD_QUALITY` | `95` | JPEG quality; `0` stores decoded frames uncompressed |

### Hardware

| Variable | Default | Notes |
|---|---|---|
| `B601_ID` | `follower1` | keys the saved calibration file |
| `B601_PORT` | `/dev/ttyACM0` | CAN adapter port, or the `canN` channel for socketcan |
| `B601_CAN_ADAPTER` | `damiao` | `damiao` = USB-serial dongle; `socketcan` = a `canN` channel |
| `B601_DM_SERIAL_BAUD` | `921600` | |
| `B601_CAM_<TRACK>` | `/dev/video0`, `/dev/video2`, … | one per video track; trailing `_camera` dropped and uppercased (`arm_camera` → `B601_CAM_ARM`). Linux: `/dev/videoN`. macOS: an integer index. |
| `B601_CAM_WIDTH` / `_HEIGHT` | `640` / `480` | |
| `B601_CAM_MAX_FRAME_AGE_MS` | `500` | how old a buffered frame may be before the tick logs it stale and serves it anyway |
| `B601_BUS_ERRORS_BEFORE_RECONNECT` | `5` | Consecutive motor I/O failures before rebuilding the bus. |
| `B601_BUS_RECONNECT_INTERVAL_S` | `2.0` | Minimum seconds between rebuild attempts. |
| `B601_MAX_STEP_DEG` | `5.0` | Maximum target step per command; `0` disables the limit. |

| Variable | Default | Notes |
|---|---|---|
| `REBOT102_LEADER_PORT` | **required** | the wiki uses `/dev/ttyUSB0` |
| `REBOT102_LEADER_ID` | `rebot_arm_102_leader` | keys the saved calibration file |
| `REBOT102_BAUDRATE` | `1000000` | |
| `REBOT102_JOINT_DIRECTIONS` | package defaults | JSON object, e.g. `{"gripper": -6}`. Set only if a leader joint drives the follower the wrong way. |

`B601_MAX_STEP_DEG` limits target movement per command (5° × 30 Hz is
150°/s of target movement). Use a lower value for bring-up. `0` disables it.
`B601_MAX_LAG_DEG` defaults to 30° and bounds target lead when a joint is blocked.
`B601_UNCAPPED_JOINTS` defaults to `gripper`, which uses force-position control
with its own torque limit. Arm joints use MIT impedance control: ramping the
target preserves torque, while a small tracking-error clamp can prevent motion.

### Training

| Training variable | Default | Purpose |
|---|---|---|
| `DATASET_REPO_ID` | `local/portal-hitl` | Local or Hugging Face dataset ID |
| `DATASET_ROOT` | unset | Local dataset directory; otherwise load by repo ID |
| `OUTPUT_DIR` | `outputs/<generated run name>` | Checkpoints and training output |
| `RUN_NAME` | `act-rebot-b601` | Prefix for the generated name, followed by steps, batch, LR, and chunk size |
| `NUM_STEPS` | `100000` | Training steps |
| `BATCH_SIZE` | `64` | Training batch size |
| `LR` | `5e-5` | ACT optimizer learning rate |
| `CHUNK_SIZE` | `50` | Predicted and executed action horizon |
| `DEVICE` | `cuda` | Training device |
| `NUM_WORKERS` | `16` | Dataset workers |
| `VIDEO_BACKEND` | `pyav` | Dataset video decoder; use `torchcodec` with compatible Torch/FFmpeg libraries |

Inference requires `--checkpoint` and `--dataset-root`. `--task` defaults to
`PORTAL_DATA_TASK`, then `reBot B601 manipulation`.
It reads LiveKit settings and `PORTAL_FPS` as the teleoperator does, selects
CUDA/MPS/CPU automatically, and waits for the `c` handoff before producing actions.

## Troubleshooting

- **No motion:** press `c` and check the active identity. Confirm the server and
  room match on all hosts. A failed claim reports its RPC error.
- **No hotkeys:** focus the teleop terminal. Global keys require X11 on Linux or
  Accessibility permission for the terminal on macOS. Relaunch after granting it.
- **No observations in ACT:** every declared camera must publish; the checkpoint
  must use cameras available in `portal.yaml`.
- **Motor-bus errors:** check the CAN adapter, USB cable, and port. Reconnection
  re-energizes the arm; keep the leader still during recovery.
- **Low frame rate:** compare device and tick times in robot diagnostics. Lower
  the requested rate or camera resolution if the hardware cannot keep up.
- **Archive writer drops:** lower JPEG quality or resolution. Check disk capacity.
- **Wrong pairing just after a stall:** Portal 0.3.0rc1 has a known video timestamp
  regression when ticks arrive very close together. The `stalls` degradation test
  can detect it; the test README records the limitation.

Back to [the tutorial](../README.md#the-tutorial).
