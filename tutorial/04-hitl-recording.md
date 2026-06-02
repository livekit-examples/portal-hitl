# 4. HITL recording

Recording is the slightly subtle part. The naive version,
`leader.get_action()` paired with `op.get_observation()` on every
tick, fails in a few ways once you look closely:

1. Tick rate beats observation arrival. Same `(obs, action)` row
   written N times, bit-identical consecutive frames.
2. Observations arrive faster than ticks. Some frames silently lost.
3. Misaligned pairs. The recorded action was generated in response to
   an observation different from the one stored next to it.
4. Stale slot at episode start. First rows pair against a previous
   episode's data.

Fixing all of these comes down to two YAML flags, two Portal ideas,
and one design choice in the recording loop.

## YAML flags that matter for data quality

```yaml
# portal.yaml
reuse_stale_frames: true
action_subscription: true
```

`reuse_stale_frames`: when a state sample arrives at the operator's
synchronizer with no fresh frame inside the sync window, reuse the
last good frame instead of dropping the state. State drives obs
emission; frames are best-effort. Without this, a transient camera
drop punches holes in the recording (state without obs); with it the
dataset retains every tick, paying at most a one or two tick visual
lag during loss. For training data this is the right tradeoff: a
slightly stale frame is more useful than a missing row.

`action_subscription`: tells the operator role to receive the executed
action stream over the wire. With it on, `op.on_action(...)` fires
every time the robot's gate forwards an action to the arm:

- Actions you sent when you were the active operator.
- Actions a remote policy sent when it was the active operator.
- Nothing while no operator is active (the gate dropped them).

The data in this callback is exactly what the robot applied.

## Action.sender

Each `Action` carries a `sender` field set at gate time on the robot:

```python
def on_action(action):
    print(action.sender, action.values)
```

Use this to label rows. The active-operator pointer can race with
handoff between two `set_active_operator` calls, but the robot stamps
`sender` at the gate so it can't be wrong by the time you see it.
Prefer `action.sender` over `op.active_operator()` for labelling.

## Drive recording from on_action, not from the tick

For training data we want one row per executed action, paired with
the observation the operator was responding to. The action stream
drives the writer. All of that plumbing — the observation ring
buffer, the action↔obs pairing, the stale-obs guard, and the
lazily-built dataset recorder — lives in one place, `HitlRecorder`
in the top-level `hitl_recorder.py`. The teleoperator's Portal
callbacks shrink to feeders:

```python
# teleoperator.py
hitl = HitlRecorder(...)

def on_observation(obs):
    # ... update the rerun mirror ...
    hitl.observe(obs)        # stamps local receive time, appends to ring

def on_action(action):
    hitl.record(action)      # pairs with an obs, writes one row
```

`observe` appends each observation to a bounded ring together with
the local wall-clock time it *arrived*. `record` does the pairing:

```python
# hitl_recorder.py
def record(self, action):
    if not self._recorder.is_recording:
        return

    # Two operators, two clocks. A policy passes `in_reply_to_ts_us`,
    # the robot-clock capture time of the exact obs it consumed, so we
    # match against each obs's capture timestamp (same clock, exact).
    # The leader passes nothing: the human reacts to whatever obs they
    # were *looking at*, so we match the action's teleop send time
    # against each obs's local *receive* time (both teleop clock).
    match_on_recv = action.in_reply_to_ts_us is None
    target_ts = action.timestamp_us if match_on_recv else action.in_reply_to_ts_us

    aligned, age = None, 0
    for obs, recv_us in reversed(self._history):
        obs_ts = recv_us if match_on_recv else obs.timestamp_us
        if obs_ts <= target_ts:
            aligned, age = obs, target_ts - obs_ts
            break
    if aligned is None:
        return  # episode start, no obs old enough yet
    if age > self._max_obs_age_us:
        self.skipped_frames += 1
        return  # obs stream stalled, would mislabel

    self._recorder.push_frame(self._obs_to_record(aligned), dict(action.values))
```

What this gives:

- One row per executed action. No duplicates.
- No silently lost observations: every `on_observation` lands in the
  ring; obs that aren't matched at arrival time are picked up by a
  later `on_action`.
- `(obs, action)` pairs the agent's actual decision with the obs it
  was looking at, not whatever happened to be in `latest_obs` at
  write time.
- Stale-stream rejection. If the obs stream stalls relative to the
  action stream, the threshold check catches it (and bumps a skip
  counter the loop surfaces every 5s) instead of silently recording
  a wrong pair.
- Episode boundaries clean. `HitlRecorder.start_episode()` clears the
  ring, so the first row of a new episode pairs against an obs from
  inside the episode, not from before.

## Why matching on the right clock matters

The pairing looks fragile — just "newest obs at or before the
target" — but it's robust because each operator is matched on a clock
where the comparison is skew-free.

Policy actions carry `in_reply_to_ts_us`, the robot-clock capture
time of the exact obs the policy consumed. Matching that against each
obs's capture timestamp (`obs.timestamp_us`, the same robot clock)
names the precise obs — no estimation.

Leader actions are open-loop: the human reacts to whatever obs they
were *looking at*, i.e. the most recently received one. So we compare
the action's teleop send time against each obs's local *receive* time
— both on the teleop clock. Comparing the leader's teleop send time
against the obs's robot-clock capture timestamp instead would fold in
the robot/teleop clock offset plus the full transport latency,
inflating every gap and tripping the threshold even when nothing is
misaligned.

`reuse_stale_frames: true` keeps the obs receive stream dense and
roughly tick-paced, so "newest received obs ≤ send time" lands on the
frame the operator actually saw. The `MAX_OBS_AGE` threshold then
measures only how stale that view was: when the obs stream the
operator was watching genuinely stalls, the row is dropped and
counted rather than mislabelled.

Without `reuse_stale_frames`, sync would block on missing frames and
those bounds break down; a dual-buffer pending-action queue would be
needed. With it on, the receive-time lookup is the right tool.

## Why not drive from on_observation

You could. But the action stream is the throughput-limiting one:
human-driven leader emits one action per tick, and slow policies emit
fewer. Driving from observations gives 30 rows/sec even when the
action stream is at 10Hz, which means most rows would carry the same
action repeated. That isn't gaps in time, but it bloats the dataset
with redundancy and obscures the agent's actual decision rate.
Driving from actions keeps row count honest with what the agent
actually produced.

The flip side is that driving from on_action means one row per
action, even if the policy is slow, so a 10Hz policy produces ~10
rows/sec into a dataset whose `fps` metadata says 30. lerobot reads
in arrival order; missing rows appear as dataset gaps, not
mistimestamped data, which is the right failure mode.

## Recording while a policy drives

The teleoperator must be running for any recording to happen, even
when a policy is the active operator. Workflow:

```
1. uv run robot.py
2. uv run teleoperator.py        # press 'r' to start recording
3. uv run policies/act/inference.py
4. On teleop window: 'c' twice to hand control to the policy.
   Recording continues; rows now show Action.sender == "policy-act".
```

The policy doesn't record itself. It only sends actions; the robot's
gate forwards them to the teleop's `on_action` callback (because of
`action_subscription: true`), and the teleop's recorder pairs each
one with the synced obs the policy was responding to (via
`in_reply_to_ts_us`).

If you want headless policy-only recording (no human present at all),
peel a "passive recorder" operator out of `teleoperator.py` that
doesn't drive a leader and never claims control. It is not in this
repo, but the implementation is just the recorder slice without the
hardware.

## What the recorder does

Recording is split in two layers. `HitlRecorder` (top-level
`hitl_recorder.py`) is the Portal-aware layer covered above: it owns
the obs ring, the action↔obs alignment, and the lazy build. Underneath,
`DatasetRecorder` in `utils/recorder.py` wraps `LeRobotDataset` with
three things:

- Off-thread `save_episode`. The hot path never blocks on parquet or
  video encoding.
- Resume. If `data/<repo_id>/meta/tasks.parquet` exists, reuse the
  dataset and append. Otherwise create.
- `start_episode` / `end_episode` / `discard_episode`, which
  `HitlRecorder` forwards from the `r` and `[` hotkeys.

`DatasetRecorder` is intentionally not Portal-aware. Hand it
`observation` and `action` as flat dicts; it shapes them into a
`LeRobotDataset` frame. The flat-dict conversion (decoding camera
frames to ndarrays) is `HitlRecorder._obs_to_record`.

Next: [05. Handoff](05-handoff.md).
