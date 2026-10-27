# 4. HITL recording

Record the session as raw streams, then build a training dataset offline.
This separates storage and video encoding from the control loop and lets you
change episode boundaries, action selection, or alignment without repeating
data collection.

## Record with an observer

An **observer** is a third Portal role next to robot and operator. It
receives state, video, keypoints, and subscribed actions without sending
commands to the robot. `recorder.py` uses this pattern:

```python
# recorder.py
from livekit.portal import Observer, ObserverConfig
from livekit.portal.recording import RrdSink

obs = Observer(ObserverConfig.from_yaml_file(CONFIG_PATH, room))
synced = asyncio.Event()
obs.on_time_synced(synced.set)
await obs.connect(url, token)
await synced.wait()                       # start on the robot's clock

sink = RrdSink("data/recordings")
obs.record_to(sink)                       # one .rrd per run
...
await obs.disconnect()                    # flushes and closes the file
```

`RrdSink` writes a [Rerun](https://rerun.io) archive. Open it with
`rerun data/recordings/<file>.rrd` to scrub through the session: both
cameras, every joint, every action, and who sent it.

Three things make the archive trustworthy as training material:

- **Everything is on the robot's clock.** Portal syncs every peer's clock
  to the robot's, so state, frames, actions and keypoints share one
  timeline no matter which machine they came from.
- **Raw streams, not observations.** An observer doesn't fuse state and
  frames into observations the way an operator does, so no sample is
  dropped or reused to make a match. Each frame and each state sample is
  stored with its own timestamp. Pairing happens later, when you convert.
- **Actions carry sender and control status.** By default, the observer
  receives actions from the active operator. `Action.sender` identifies the
  source, and `Action.active` reflects the observer's control gate. These
  fields don't confirm hardware execution; use measured robot state for that.
  Set `cfg.set_action_subscription("all")` to also record inactive policy
  actions for offline comparison.

## Mark episodes with keypoints

The recorder doesn't know when a demonstration starts. The human at the
teleoperator does, so the `r` and `[` hotkeys send **keypoints**:
`{type, payload}` annotations that the recorder stores on the same
timeline.

Keypoints don't start or stop the archive writer. This project uses the
converter's default episode markers:

| Hotkey | Keypoint | Payload | Meaning to the converter |
|---|---|---|---|
| `r` (start) | `recording` | `{"task_description": "Grab the black cube"}` | Opens an episode; the payload becomes its task. |
| `r` (stop) | `idle` | none | Closes the episode and keeps it. |
| `[` | `discard` | none | Closes the episode and drops it. |

```python
# teleoperator.py
await op.send_keypoint("recording", {"task_description": task})
await op.send_keypoint("idle")
await op.send_keypoint("discard")
```

The task comes from `PORTAL_HITL_TASK` in `.env`. Any other keypoint
types you send (a grasp, a failure, a takeover) are recorded too, and
the converter ignores them. If you'd rather use your own names, the
converter takes them as flags; see the
[keypoint convention](https://github.com/livekit/portal/blob/main/docs/03-data-recording.md#keypoint-convention)
in Portal's docs.

`send_keypoint` returns the observers present in the room. An empty list
triggers a warning in the teleoperator. A non-empty list doesn't acknowledge
receipt or persistence: an observer can be connected without an active sink.
Wait for the recorder's output-path message before marking the first take.

## Answer the right observation

For a clean `(observation, action)` pair the dataset needs to know which
observation each action answered. Every operator says so with
`in_reply_to_ts_us`:

```python
# policies/act/inference.py: the obs the policy consumed
op.send_action(action, in_reply_to_ts_us=obs.timestamp_us)

# teleoperator.py: the latest synchronized observation
op.send_action(
    leader.get_action(),
    in_reply_to_ts_us=latest_obs.timestamp_us if latest_obs else None,
)
```

The recorder stores that anchor on `robot_time` and the action's send time
on `action_time`. The second timeline preserves separate actions that answer
the same observation. Actions without an anchor use their send timestamp on
`robot_time`.

This project anchors human actions to the latest synchronized observation.
The policy anchors actions to the observation used for inference. Conversion
uses the declared anchor; it doesn't infer which frame a human actually saw.
Keypoints in this project use the default send time on the robot's clock.

## Build the dataset

Once you have archives, `livekit-portal convert` turns them into a
`LeRobotDataset`:

```bash
uv run livekit-portal convert data/recordings --output lerobotv3 \
    --repo-id local/portal-hitl --robot-type seeed_b601_dm_follower
```

The converter selects actions marked active at the recorder's gate and reads
them from `action_time`. For each action, it selects the newest state and each
camera frame at or before its anchor, allowing up to 200 ms of staleness.
Set `--max-stale-ms 0` to require exact timestamp matches. A row is skipped
if any required state or frame is missing or too old.

Episodes use the keypoints above. Discarded and unclosed takes are excluded;
another `recording` marker closes the previous episode and opens a new one.
If no `recording` marker exists, the full archive becomes one episode.
Use `--sender policy-act` to select that sender, including inactive actions
if the recorder captured them with the `"all"` subscription.

Add `--dry-run` to inspect episodes, cameras, senders, and schema compatibility
without writing a dataset. It doesn't validate every output row. Add `--push`
with your Hugging Face repository ID to upload the result. The full
rules are in Portal's
[data recording guide](https://github.com/livekit/portal/blob/main/docs/03-data-recording.md#convert-recordings-into-training-datasets).

The archives stay the source of truth. Convert again with a different
fps, staleness window or episode split whenever you like, without
recollecting anything. The converter is a quick way to get a dataset, not
the only one: when your training stack needs something it doesn't do,
read the `.rrd` files directly and write your own pipeline.

## One row per action

Each selected action with complete observations produces one dataset row.
The converter doesn't resample actions or insert rows for missing samples.

The dataset's `fps` defaults to the robot's state rate. LeRobot assigns output
timestamps from row indices and that rate, so a 10 Hz action stream written
at 30 fps is compressed in time; missing rows don't preserve timing gaps.
Choose `--fps` to match the cadence your training data requires. If you need
resampling or the original irregular timing, use the Python reader and a
custom conversion step.

## Recording while a policy drives

The recorder records whoever is driving. The teleoperator only needs to
be running if you want to mark episodes from it:

```
1. uv run robot.py
2. uv run recorder.py
3. uv run teleoperator.py        # press 'r' to start an episode
4. uv run policies/act/inference.py
5. On the teleop window: 'c' to hand control to the policy.
   Recording continues; rows now have Action.sender == "policy-act".
```

The policy doesn't record itself. It only sends actions; the robot's
gate accepts the active operator's. The recorder stores received actions with
their sender and `in_reply_to_ts_us`. Take over with `c` and your
corrections remain in the same archive, labelled as yours. Conversion includes
them when they fall within a retained episode and have the required observations.

Next: [05. Handoff](05-handoff.md).
