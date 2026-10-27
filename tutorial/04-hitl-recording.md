# 4. HITL recording

Recording is the slightly subtle part. The naive version,
`leader.get_action()` paired with `op.get_observation()` on every
tick, fails in a few ways once you look closely:

1. Tick rate beats observation arrival. Same `(obs, action)` row
   written N times, bit-identical consecutive frames.
2. Observations arrive faster than ticks. Some frames silently lost.
3. Misaligned pairs. The recorded action was generated in response to
   an observation different from the one stored next to it.
4. Recording only works where the human is. If the teleoperator
   process writes the dataset, a policy-only session records nothing.

Portal avoids all four by splitting recording in two: record the raw
session while it happens, and build the dataset afterwards.

## Record with an observer

An **observer** is a third Portal role next to robot and operator. It
sees everything in the room (state, video, the actions the robot
executes, keypoints) and never drives. `recorder.py` is one, and the
whole file is little more than this:

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
- **Only executed actions, labelled.** By default an observer receives the
  actions of the active operator, the ones the arm actually ran, each with
  `Action.sender` stamped by the robot at its gate. Rows from the leader
  and rows from a policy are told apart by sender, and a handoff can't
  mislabel them.

## Mark episodes with keypoints

The recorder doesn't know when a demonstration starts. The human at the
teleoperator does, so the `r` and `[` hotkeys send **keypoints**:
`{type, payload}` annotations that the recorder stores on the same
timeline.

Portal itself attaches no meaning to a keypoint. This project follows
the convention that `livekit-portal convert` reads to find episodes:

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

`send_keypoint` returns the observers that received it. When that list
is empty, the teleoperator warns you that no recorder is running, instead
of letting you record a take into nothing.

## Answer the right observation

For a clean `(observation, action)` pair the dataset needs to know which
observation each action answered. Every operator says so with
`in_reply_to_ts_us`:

```python
# policies/act/inference.py: the obs the policy consumed
op.send_action(action, in_reply_to_ts_us=obs.timestamp_us)

# teleoperator.py: the newest obs on screen when the human moved
op.send_action(
    leader.get_action(),
    in_reply_to_ts_us=latest_obs.timestamp_us if latest_obs else None,
)
```

The recorder files each action under that timestamp. Pairing it with an
observation is then a lookup on one clock: the state and frames the
robot captured at that instant. There's no ring buffer, and no
comparing a send time on one machine against a capture time on another.

## Build the dataset

Once you have archives, `livekit-portal convert` turns them into a
`LeRobotDataset`:

```bash
uv run livekit-portal convert data/recordings --output lerobotv3 \
    --repo-id local/portal-hitl --robot-type seeed_b601_dm_follower
```

It splits each archive into episodes at the keypoints above, drops
discarded takes, and writes one row per executed action with the state
and frames that action answered. Add `--push` to upload to the Hugging
Face Hub, and `--dry-run` to see what each archive holds first. The full
rules are in Portal's
[data recording guide](https://github.com/livekit/portal/blob/main/docs/03-data-recording.md#convert-recordings-into-training-datasets).

The archives stay the source of truth. Convert again with a different
fps, staleness window or episode split whenever you like, without
recollecting anything. The converter is a quick way to get a dataset, not
the only one: when your training stack needs something it doesn't do,
read the `.rrd` files directly and write your own pipeline.

## One row per action

The converter writes one row per executed action, not one per
observation. The action stream is the one that reflects decisions: a
human-driven leader emits one action per tick, and a slow policy emits
fewer. Writing a row per observation would give 30 rows a second even
when the policy decides at 10 Hz, and most of them would repeat the same
action. A 10 Hz policy instead produces about 10 rows a second into a
dataset whose `fps` says 30. Missing rows show up as gaps, not as
mistimestamped data, which is the right failure mode.

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
gate executes the active operator's, and the recorder stores them with
their sender and `in_reply_to_ts_us`. Take over with `c` and your
corrections land in the same episode, labelled as yours.

Next: [05. Handoff](05-handoff.md).
