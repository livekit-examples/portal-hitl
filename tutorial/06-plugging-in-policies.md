# 6. Plugging in a policy

A policy is just another `Operator`. It loads the same `portal.yaml`,
subscribes to observations, and sends one action per tick. The
Portal-facing flow is identical regardless of which model class is
behind `select_action`. The example in this repo is
`policies/act/inference.py`.

This page walks the per-tick flow that policies use.

## One action per tick

Portal sends one action per control tick; there is no action-chunk
message on the wire. That suits lerobot: the policies (ACT, Diffusion,
SmolVLA, VQBeT) all expose `policy.select_action(batch)` returning a
single action, and handle chunk buffering and temporal ensembling
internally. One tick, one `op.send_action(...)`.

## The tick body

Stripped of model-specific code, every `policies/<algo>/inference.py`
runs this loop:

```python
async for _ in pace(fps):
    obs = latest_obs                 # written by on_observation callback
    if obs is None:
        continue                     # robot warming up

    # 1. Build the model batch from the synced observation.
    state_typed = dict(obs.state)
    frames = {
        cam: frame_bytes_to_numpy_rgb(obs.frames[cam])
        for cam in policy_cameras
        if cam in obs.frames
    }
    batch = build_batch(state_typed, frames, policy_cameras, task_prompt)

    # 2. Run the policy.
    processed = preprocessor(batch)
    with torch.inference_mode():
        action_t = policy.select_action(processed)
    action_t = postprocessor(action_t)

    # 3. Reshape back to a dict and send.
    action = {k: float(v) for k, v in zip(ACTION_KEYS, action_t.tolist())}
    op.send_action(action, in_reply_to_ts_us=obs.timestamp_us)
```

Three steps: build batch, run inference, send. `in_reply_to_ts_us=obs.timestamp_us`
closes the e2e latency loop (see [05. Handoff](05-handoff.md)) and tells
the recording which observation the action answered.

## Self-claim and the safety gate

Policies self-claim on connect by default so a fresh autonomous run
just works:

```python
await op.connect(url, token)
me = op.local_identity()
if args.self_claim:
    await op.set_active_operator(me)
```

The `engaged` flag layered on top is a safety: SPACE toggles whether
we actually call `op.send_action` (we always run inference and log to
rerun). Starts disengaged.

```python
engaged = False
async for _ in pace(fps):
    if poll_key() == " ":
        engaged = not engaged

    # ... build batch, run policy, get action ...

    if engaged:
        op.send_action(action, ...)
```

Two reasons for the engaged gate even though the active-operator gate
exists:

1. The teleoperator can mid-session cycle to us and the gate would
   pass our actions. The engaged flag is a local kill switch the
   operator owns regardless of who is active.
2. We log inference outputs to rerun even when disengaged, which is
   useful for visualizing what the policy *would* do without letting
   it actually drive.

## Field ordering

The `STATE_KEYS` and `ACTION_KEYS` constants in each policy follow
`portal.yaml`'s declaration order:

```python
STATE_KEYS = (
    "shoulder_pan.pos",
    "shoulder_lift.pos",
    "elbow_flex.pos",
    "wrist_flex.pos",
    "wrist_yaw.pos",
    "wrist_roll.pos",
    "gripper.pos",
)
ACTION_KEYS = STATE_KEYS
```

That ordering matches the `names` list `livekit-portal convert` writes
into the dataset, which comes from the schema recorded in each archive. The policy was trained with
this order, so the action tensor it returns is indexed this way.
Reordering would silently mis-route values to the wrong motors.

## Adding a new policy

Copy `policies/act/inference.py` as a starting point. Replace:

- The model class (`ACTPolicy.from_pretrained`).
- The model-specific runtime knobs (e.g. the temporal ensembler for
  ACT).
- The CLI arguments.

Keep:

- The Portal flow (connect, on_observation slot, per-tick build batch
  + send_action).
- The `engaged` gate.
- The `--no-claim` flag for HITL setups.
- The `STATE_KEYS` / `ACTION_KEYS` definitions, unless your training
  data has a different field set.

The training side (`policies/<algo>/train.py`) is independent of
Portal. It is just lerobot training driven by `runpy`, with a few
shims for object-storage symlinks and worker prefetch. Adapt the
flags for your model, leave the rest.

That is the whole pattern. The robot side never changes.

Next: [07. Running the rig](07-running-the-rig.md) — the runbook for
bringing the hardware online and running the full loop.
