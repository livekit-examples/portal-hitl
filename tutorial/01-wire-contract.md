# 1. The wire contract

Every participant needs the same camera tracks, state fields, action fields,
and observation rate. Put them in one file so the robot, human, policy, and
recorder share the same definition: [`portal.yaml`](../portal.yaml).

This rig declares two cameras, one on the arm and one overhead, and seven joint
positions: six arm joints
plus the gripper. Both state and action use `.pos` fields in degrees.

```yaml
version: 1
fps: 30
reuse_stale_frames: true
videos:
  - { name: arm_camera, codec: h264, max_bitrate_kbps: 20000, screencast: true }
  - { name: overhead_camera, codec: h264, max_bitrate_kbps: 20000, screencast: true }
```

The complete file also lists the seven `f32` fields under `state` and `action`.
Their order matters: model tensors and normalization statistics are positional.

## Load it for each role

```python
robot_cfg = RobotConfig.from_yaml_file(CONFIG_PATH, room)
operator_cfg = OperatorConfig.from_yaml_file(CONFIG_PATH, room)
observer_cfg = ObserverConfig.from_yaml_file(CONFIG_PATH, room)
```

Both the leader-arm process and ACT use `OperatorConfig`. The room and identity
come from the environment and token, so the YAML can be shared across hosts.
Mismatched schemas produce fingerprint warnings and dropped packets. Deploy
schema changes to every peer together.

## Choose a transport

H264 uses WebRTC media and keeps video off the reliable state channel.
`screencast: true` preserves resolution under pressure by dropping frames.
The bitrate setting is a ceiling, not a target.

MJPEG sends independent JPEG frames over the reliable data channel. That avoids
inter-frame prediction but can delay state when video exceeds available
bandwidth. PNG and RAW use the same channel with larger payloads. See
[Running the rig](07-running-the-rig.md#configuration) for settings.

State and actions go over unreliable data packets, Portal's default. A reliable
channel retransmits a lost packet and holds every newer one until it arrives; at
30 ticks a second, a newer reading is already on its way, so it is better to
skip the lost one. The cost is that the recorder may miss the occasional row.
Packets can also arrive out of order; Portal drops any state or action stamped
behind the newest it already has from that sender, so neither stream goes
backwards. Keypoints and control stay reliable.

On a congested link, actions can also arrive in order but late. Setting
`action_max_age_ms` in `portal.yaml` makes the robot drop actions older than
that on arrival. It is off by default. Set it above the link's one-way delay plus
jitter, or the robot drops most commands. The robot's 5 s report counts the
drops.

## Cameras

Each track's device comes from `B601_CAM_<TRACK>`, dropping the `_camera`
suffix: `B601_CAM_ARM` and `B601_CAM_OVERHEAD`.

Every declared camera must publish. The human operator waits for a frame from
each before assigning observation anchors; synchronized observations for the
policy also depend on those tracks.

Next: [02. Robot loop](02-robot-loop.md).
