# 2. Robot loop

Open [`src/robot.py`](../src/robot.py).
The loop reads the follower, publishes observations, and applies the latest
accepted action. Hardware setup and recovery live in `src/utils/rebot.py`.

## Receive an action

Portal calls `on_action` for actions that pass the active-operator gate.
The callback saves their values; the hardware loop applies the latest values
on its next tick. This avoids issuing a motor transaction for every packet
when actions arrive in a burst.

The robot does not need to know whether an action came from a human or a policy.
Both use the same schema and gate.

## Publish one tick

The main loop contains this sequence:

```python
ts_us = int(time.time() * 1_000_000)
observation = await hardware.run(follower.get_observation)
state, frames = split_state_frames(observation, tracks)
if state:
    robot.send_state(state, timestamp_us=ts_us)
for name, frame in frames.items():
    robot.send_video_frame(name, frame, timestamp_us=ts_us)
if latest_action:
    await hardware.run(follower.send_action, latest_action)
```

State and frames share one timestamp. That lets Portal match them into an
observation and lets the recorder align responses later. Frames are RGB
`uint8` arrays with shape `(height, width, 3)`.

`HardwareThread` serializes device calls while leaving the asyncio loop free
for network callbacks. `pace(fps)` also yields when a tick overruns its budget;
otherwise an overloaded hardware loop could starve the network.

## Handle a handoff

The robot starts with no active operator. On a gate change it clears its cached
action, so the previous operator’s command is not reused while waiting for the
new operator. If the active operator disconnects, it also releases the gate.

The follower limits each commanded target step and reseeds that target after
pauses or bus recovery. This limits command changes during takeover; it is not
a guarantee of physical motor speed.

Camera reads reuse the last good frame during a USB interruption and reconnect
in the background. Motor I/O failures are counted and retried; repeated failures
rebuild the CAN bus on the same worker thread. Recovery re-enables the motors.
The target ramp resets from the present pose before commands resume. These
hardware details stay in `src/utils/rebot.py` so the Portal loop remains readable.

Next: [03. Operator loop](03-operator-loop.md).
