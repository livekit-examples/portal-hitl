# 6. Plugging in a policy

A policy uses the same `Operator` interface as the human. This repo ships two:

| Policy | Identity | When to use it |
|---|---|---|
| [Dream-1](../src/policies/dream1/inference.py) | `policy-dream-1` | Try a pretrained model with a language prompt before collecting your own data |
| [ACT](../src/policies/act/inference.py) | `policy-act` | Train on your own demonstrations and corrections once you have data |

Both start without control and wait for `c` in teleop. Both send actions with
`in_reply_to_ts_us` set to their input observation, and both reset when control
changes. The robot and recorder need no policy-specific changes.

Start with Dream-1 to exercise handoff, correction, and recording before training
your own policy. Its corrected takes can become ACT’s first dataset. You can
also skip Dream-1 and train ACT entirely from human demonstrations.

## Dream-1: zero-shot

[Dream-1](https://dreamlabs.ai/research/dream-1/) is Dream Labs’ pretrained
robot-control model, described as a World Action Model. It takes camera images,
robot state, and a free-form task prompt and predicts actions. “Zero-shot” means
you do not need to collect demonstrations or train it on your task before trying
it. Hardware setup and calibration are still required.

In this example, inference runs on Dream Labs’ servers: camera images, state,
and the prompt go to its API. The model was trained on YAM arms. The `dream`
client handles the B601: it adds the hand pose
computed from our joints to each request, then solves the model’s hand poses
back to B601 joints.

Use it to explore a tabletop task, see where the model struggles, and record
human recoveries. For example, let it attempt a cube pickup, take control to
correct a missed grasp, then hand control back to finish the take. Review these
takes before training: a completed episode can still contain unsuccessful
policy actions. The recording captures the attempt and correction together;
it does not automatically turn every action into a good demonstration.

Corrections in this repo train a separate ACT checkpoint offline. Handing
control back to Dream-1 resumes inference; it does not fine-tune the hosted model.

### Set it up once

1. **Get a key.** Create an API key at
   [platform.dreamlabs.ai/api-keys](https://platform.dreamlabs.ai/api-keys).
   Step 2 of their [getting started guide](https://platform.dreamlabs.ai/api-documentation/)
   checks it and writes `~/.dreamlabs/credentials`; `DREAM_API_KEY` in the
   environment also works. Each action request is
   billed from prepaid credits; new accounts start with free credit.
2. **Install the client.** `uv sync --group dream`.
3. **Measure the tool center point.** Run `uv run --group dream dreamcli tcp`
   and open the address it prints. Load the B601 URDF (a folder or a `.zip` with
   meshes) and confirm where the arm ends. Place the marker where the closed
   fingertips meet, oriented like the YAM’s, then press **Set tool center point**.
   Set `DREAM_ARM_FILE` to the path it prints.

Measure the tool center point on the real gripper. Do not estimate it from the
URDF: a 2 cm error moves every grasp by 2 cm. The script refuses an arm file
that `dreamcli` has not marked calibrated. Measure again if you change the
gripper or its mount. The follower does not need to be connected.

### Run it

Dry-run first. It runs the whole loop and prints target joints once a second,
but sends no actions. Start the robot and teleop, run:

```bash
uv run --group dream src/policies/dream1/inference.py \
  --task "pick up the red cube" --dry-run
```

Then press `c` in teleop to select `policy-dream-1`. The policy only runs while it
holds control, so nothing prints until then. The arm holds its last target.
Check that the printed targets follow the arm. This confirms that the joint
order, signs, and zeros in `portal.yaml` match the URDF, and that
`DREAM_GRIPPER_OPEN_DEG` (default `-270`) is fully open. Then drop `--dry-run`,
select `policy-dream-1` again, and it drives.

`--task` is the main control. Describe only one arm: Dream-1 drives its right
arm and parks the left at the arm file’s `rest` pose. Startup takes about 10 s
while IK compiles. The first request can return `pool_warming`; the policy
retries until the model is ready.

### What happens each step

Each request returns 48 rows of actions, played at 20 rows per second. The next
request goes out before the chunk runs out, so motion continues during the
250–450 ms round trip. The Portal loop still ticks at the `portal.yaml` rate and
sends the row that is due.

Dream-1 reads three cameras: `overhead`, `wrist_l`, and `wrist_r`. By default,
`DREAM_CAMERAS=wrist_r=arm_camera` and the other two are black. That works, but
the model relies on a scene view. Add a fixed overhead camera to `portal.yaml`
and map it with `DREAM_CAMERAS=wrist_r=arm_camera,overhead=<track>`.

`DREAM_MAX_JOINT_RAD_S` (default `0.5`) caps how fast the commanded target moves.
It also ramps the arm in from the measured pose when Dream-1 takes control. The
gripper moves at most 5% of its range per row. The policy holds if the newest
observation is more than 0.2 s old. The robot’s step limiter and the driver’s
joint limits still apply. Keep the e-stop in reach.

Expect modest zero-shot success: Dream-1 is a beta model trained on another arm,
and it may lean on the cameras and prompt more than on the B601 joint state.
That is the gap HITL corrections fill.

## ACT: train on your takes

```bash
uv run --group train python src/policies/act/inference.py \
  --checkpoint outputs/my-act/checkpoints/020000/pretrained_model \
  --dataset-root data/local/portal-hitl
```

The dataset root supplies `meta/info.json`. State and action names are read in
training order and checked against `portal.yaml`. Do not alphabetize joint
names: normalization vectors and output tensors use the dataset’s order.

Camera names come from the checkpoint’s input features. A checkpoint that needs
an overhead camera cannot run with only the wrist track declared.

### Follow one inference step

The observation callback updates a latest-observation slot. While ACT holds the
gate, the loop:

1. Packs joint state in dataset order and converts RGB images to CHW float
   tensors in `[0, 1]`.
2. Runs the saved preprocessor, `policy.select_action`, and postprocessor.
3. Maps the output back to joint names and sends it with the input timestamp.

```python
op.send_action(action, in_reply_to_ts_us=obs.timestamp_us)
```

Portal supplies the action’s send timestamp. Inference runs on a dedicated
worker so it does not block network callbacks. The result is sent only if
control remained with ACT throughout inference.

### Chunks and handoff

ACT manages its action chunks internally; Portal receives one flat action per
tick. Temporal ensembling is enabled by default with coefficient `0.01`.
The example replans each tick and resets the ensemble after a handoff.

When control returns after a human correction, the policy resets its buffered
actions before continuing. There is no separate engagement key or automatic claim.

### Train

[`train.py`](../src/policies/act/train.py) runs LeRobot’s ACT trainer on the converted
dataset. [`skypilot.yaml`](../src/policies/act/skypilot.yaml) is a cloud training
example; customize its cloud resources and storage mount before use.

## Add another model

Keep the Portal connection, observation anchor, handoff checks, and field-order
validation. Replace the model loading and preprocessing with the new model’s
requirements. Dream-1 shows a remote model with client-side kinematics; ACT shows
a local checkpoint.

Next: [07. Running the rig](07-running-the-rig.md).
