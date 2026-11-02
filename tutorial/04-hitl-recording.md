# 4. HITL recording

A correction is useful training data only if it is paired with the observation
that prompted it. The human and policy both send an `in_reply_to_ts_us` anchor:
the human uses the camera view, while ACT uses its input observation.

## Add an observer

[`src/recorder.py`](../src/recorder.py)
connects an `Observer`, waits for robot clock synchronization, and starts a
Portal `RrdSink`:

```python
sink = RrdSink("data/sessions", jpeg_quality=95)
obs.record_to(sink)
```

The observer receives state, frames, active-operator actions, and keypoints.
It writes continuously to `<room>-<timestamp>.rrd`, including time outside
marked takes. Recording continues when control changes or teleop restarts.

An action’s `sender` identifies its operator. Use that recorded identity rather
than looking up the current gate later, when it may have changed. The archive
records accepted action traffic; the robot applies the latest accepted action
per hardware tick, so it is not a log of every motor write.

## Preserve both timestamps

| Timeline | Purpose |
|---|---|
| `robot_time` | State and frames at capture time; actions at their observation anchors |
| `action_time` | Each action at its send time on the robot’s synchronized clock |

Several actions can respond to one frame during a video stall. Reading only
`robot_time` can collapse them into one value per entity. Conversion reads
`action_time` so those responses remain distinct. Unanchored actions fall back
to their send time on `robot_time`.

## Mark episodes without stopping recording

The teleoperator sends a keypoint when you press `r` or `[`:

```python
await op.send_keypoint("recording", {"task_description": task},
                       timestamp_us=view.anchor())
```

`recording` opens a take, `idle` closes it, and `discard` rejects it. The task
label belongs to the take, so task switching is blocked while recording.
Control handoff is allowed: one take can contain policy motion and a human
correction.

## Build the dataset offline

```bash
uv run livekit-portal convert data/sessions --output lerobotv3 --dry-run
uv run livekit-portal convert data/sessions --output lerobotv3 --repo-id local/portal-hitl
```

For each action, conversion selects the newest state and frames at or before
its anchor. The default allows 200 ms of staleness; `--max-stale-ms 0` requires
exact timestamps. Rows without required samples inside the limit are skipped.

Discarded and unclosed takes are skipped. If there are no `recording` markers,
the whole archive becomes one episode. Archives combined into one dataset must
have compatible schemas. Field order comes from the archive.

Keep the archives. You can rebuild with a different episode selection or
alignment limit without collecting again. The hardware-free tests check
alignment after an archive round trip; see [test/README.md](../test/README.md),
including the known Portal 0.3.0rc1 stall regression.

Next: [05. Handoff](05-handoff.md).
