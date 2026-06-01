"""Hardware builders for the Seeed reBot Arm B601-DM rig.

Concentrates everything that's about *the robot* — serial/CAN ports,
cameras, motor config — so ``robot.py`` and ``teleoperator.py`` can stay
focused on the Portal-side flow. Two RGB cameras are assumed
(arm-mounted + overhead); the rig is the rig.

The B601-DM follower talks to Damiao CAN motors through ``motorbridge``;
the reBot 102 leader reads FashionStar smart servos through
``motorbridge-smart-servo``. Both expose the same 7 joints with ``.pos``
keys (degrees): shoulder_pan, shoulder_lift, elbow_flex, wrist_flex,
wrist_yaw, wrist_roll, gripper.
"""
from __future__ import annotations

import platform

import numpy as np

from lerobot.cameras import Cv2Backends, Cv2Rotation
from lerobot.cameras.opencv import OpenCVCameraConfig
from lerobot_robot_seeed_b601 import (
    SeeedB601DMFollower,
    SeeedB601DMFollowerConfig,
)
from lerobot_teleoperator_rebot_arm_102 import (
    RebotArm102Leader,
    RebotArm102LeaderConfig,
)

from _common import env_camera_id, env_int, env_str, required_env

CAMERAS: tuple[str, ...] = ("arm_camera", "overhead_camera")
"""Track names. Must match what's declared in ``portal.yaml``; the YAML
is the source of truth for the wire contract. This constant just lets
robot.py and teleoperator.py iterate without reaching back into the
config."""


def build_follower(fps: int) -> SeeedB601DMFollower:
    """Construct a :class:`SeeedB601DMFollower` from environment vars.

    The B601-DM drives Damiao motors over a CAN bus. Two adapters are
    common: a USB-serial Damiao dongle (``can_adapter="damiao"``, a
    ``/dev/ttyACM*`` / ``/dev/ttyUSB*`` port) or a SocketCAN interface
    (``can_adapter="socketcan"``, a ``canN`` channel). Default matches
    the Seeed wiki quick-start (``damiao`` on ``/dev/ttyACM0``).
    """
    width = env_int("B601_CAM_WIDTH", 640)
    height = env_int("B601_CAM_HEIGHT", 480)
    cam_defaults = {
        "arm_camera":      ("B601_CAM_ARM",      "/dev/video0"),
        "overhead_camera": ("B601_CAM_OVERHEAD", "/dev/video2"),
    }
    # Force V4L2 on Linux. lerobot defaults to CAP_ANY, and OpenCV's auto-
    # picker can land on a backend (often GStreamer) that opens the device
    # but no-ops S_FOURCC/S_FMT — manifests as fourcc='' and a 1920-wide
    # frame that refuses to drop to 640. AVFoundation stays auto-picked on
    # macOS because Cv2Backends.V4L2 is Linux-only.
    backend = Cv2Backends.V4L2 if platform.system() == "Linux" else Cv2Backends.ANY
    cameras = {
        name: OpenCVCameraConfig(
            index_or_path=env_camera_id(env_var, default),
            fps=fps,
            width=width,
            height=height,
            rotation=Cv2Rotation.NO_ROTATION,
            fourcc="MJPG",
            backend=backend,
        )
        for name, (env_var, default) in cam_defaults.items()
    }
    return SeeedB601DMFollower(SeeedB601DMFollowerConfig(
        id=env_str("B601_ID", "follower1"),
        port=env_str("B601_PORT", "/dev/ttyACM0"),
        can_adapter=env_str("B601_CAN_ADAPTER", "damiao"),
        dm_serial_baud=env_int("B601_DM_SERIAL_BAUD", 921600),
        cameras=cameras,
    ))


def build_leader() -> RebotArm102Leader:
    """Construct the reBot 102 leader arm. Unlike the leslider's SO-101
    leader there is no slider / arrow-key handler: the leader simply
    exposes a synchronous ``get_action()`` returning one 7-joint ``.pos``
    dict per call (degrees).

    ``joint_directions`` / ``joint_ranges`` keep their package defaults
    (which already match the B601 follower's ranges); override the signs
    per-joint with ``REBOT102_JOINT_DIRECTIONS`` (a JSON object) if a
    leader joint drives the follower the wrong way.
    """
    cfg_kwargs: dict = dict(
        id=env_str("REBOT102_LEADER_ID", "rebot_arm_102_leader"),
        port=required_env("REBOT102_LEADER_PORT"),
        baudrate=env_int("REBOT102_BAUDRATE", 1_000_000),
    )
    directions = env_str("REBOT102_JOINT_DIRECTIONS", "")
    if directions:
        import json

        cfg_kwargs["joint_directions"] = json.loads(directions)
    return RebotArm102Leader(RebotArm102LeaderConfig(**cfg_kwargs))


def split_state_frames(observation: dict) -> tuple[dict[str, float], dict[str, np.ndarray]]:
    """Split a follower observation dict into (state, frames).

    The follower's ``get_observation()`` returns one flat dict mixing
    motor scalars and camera ndarrays. The B601 follower writes ``.pos``,
    ``.vel`` and ``.torq`` per motor at runtime, but the wire schema in
    ``portal.yaml`` publishes only the 7 ``.pos`` values, so we keep
    ``.pos`` keys exclusively (the leslider also kept ``.vel`` for its
    slider — there's no slider here, and per-motor ``.vel`` would blow
    past the 7-field schema). ``OpenCVCameraConfig`` defaults
    ``color_mode=RGB``, so frames arrive in the layout Portal expects.
    """
    state = {
        k: float(v) for k, v in observation.items()
        if k.endswith(".pos")
    }
    frames = {
        cam: observation[cam]
        for cam in CAMERAS
        if cam in observation and observation[cam] is not None
    }
    return state, frames
