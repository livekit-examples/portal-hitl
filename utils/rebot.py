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

import logging
import platform
import threading
import time

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

logger = logging.getLogger(__name__)

CAMERAS: tuple[str, ...] = ("arm_camera", "overhead_camera")
"""Track names. Must match what's declared in ``portal.yaml``; the YAML
is the source of truth for the wire contract. This constant just lets
robot.py and teleoperator.py iterate without reaching back into the
config."""


class _ResilientCamera:
    """Wraps a lerobot camera so a transient frame stall or a dead read
    thread never propagates out of ``follower.get_observation()``.

    Two failure modes show up on USB webcams under load:

      * ``async_read()`` raises ``TimeoutError`` when no fresh frame lands in
        its window (USB bandwidth contention, a momentary stall). This is
        recoverable on its own — the next tick usually succeeds.
      * ``async_read()`` raises ``RuntimeError`` ("read thread is not running")
        once the background read thread has died after too many consecutive
        hardware-read failures ("Read thread alive: False"). This does *not*
        self-heal: the device must be reconnected.

    Either exception, raised mid-``get_observation()``, otherwise tears down
    the whole control loop and drops the motor state that was already read.
    Instead we serve the last good frame across the gap and kick off a
    rate-limited reconnect (on a background thread, so the control loop never
    blocks on hardware) when the thread is dead. We return ``None`` only until
    the first frame ever arrives — ``split_state_frames`` filters that out, so
    a never-yet-seen camera simply isn't published that tick while motor state
    keeps flowing.

    Installed by shadowing ``cam.async_read`` with an instance attribute, so
    the vendored follower keeps calling its cameras exactly as before.
    """

    def __init__(
        self,
        name: str,
        cam,
        timeout_ms: float,
        reconnect_interval_s: float = 2.0,
    ) -> None:
        self._name = name
        self._cam = cam
        self._timeout_ms = timeout_ms
        self._reconnect_interval_s = reconnect_interval_s
        # Captured before we shadow cam.async_read below — this is the real
        # (decorated) bound method. Never re-grab it: cam.async_read now points
        # at our wrapper, so re-grabbing would recurse forever.
        self._orig_async_read = cam.async_read
        self._last_frame: np.ndarray | None = None
        self._last_reconnect = 0.0
        self._reconnecting = False
        self._lock = threading.Lock()

    def install(self) -> None:
        # Instance attribute shadows the class method; get_observation()'s
        # `cam.async_read()` now routes through us.
        self._cam.async_read = self.async_read

    def async_read(self, *args, **kwargs) -> np.ndarray | None:  # noqa: ANN002
        kwargs.setdefault("timeout_ms", self._timeout_ms)
        try:
            frame = self._orig_async_read(*args, **kwargs)
            self._last_frame = frame
            return frame
        except TimeoutError as exc:
            # Frame just didn't arrive in time. Reuse the last good one.
            logger.warning("%s: %s — reusing last frame", self._name, exc)
            return self._last_frame
        except Exception as exc:
            # Thread dead / device gone. Won't recover without a reconnect.
            logger.warning(
                "%s: read failed (%s) — scheduling reconnect, reusing last frame",
                self._name, exc,
            )
            self._maybe_reconnect()
            return self._last_frame

    def _maybe_reconnect(self) -> None:
        now = time.monotonic()
        with self._lock:
            if self._reconnecting:
                return
            if now - self._last_reconnect < self._reconnect_interval_s:
                return
            self._reconnecting = True
            self._last_reconnect = now
        threading.Thread(
            target=self._reconnect, name=f"{self._name}_reconnect", daemon=True
        ).start()

    def _reconnect(self) -> None:
        try:
            try:
                self._cam.disconnect()
            except Exception:
                pass  # already down; connect() is what matters
            # warmup=False: don't block this thread waiting on a flaky device;
            # the control loop keeps serving the last frame until one lands.
            self._cam.connect(warmup=False)
            logger.info("%s: reconnected", self._name)
        except Exception as exc:
            logger.warning("%s: reconnect failed (%s) — will retry", self._name, exc)
        finally:
            self._reconnecting = False


def _harden_cameras(follower: SeeedB601DMFollower) -> None:
    """Make follower camera reads degrade gracefully instead of crashing the
    control loop on a single dropped frame or a dead read thread.

    Safe to install before ``follower.connect()``: ``connect()`` starts the
    read thread *before* its warmup reads, so the only exception warmup can hit
    is ``TimeoutError`` (which just reuses the last frame — it never triggers a
    reconnect). The dangerous reconnect path requires a *dead* thread, which
    can't happen during the connect that just started it. A genuine startup
    failure still surfaces: ``connect()``'s own ``latest_frame is None`` check
    raises ``ConnectionError`` regardless of what the wrapper swallows.
    """
    timeout_ms = env_int("B601_CAM_READ_TIMEOUT_MS", 200)
    for name, cam in follower.cameras.items():
        _ResilientCamera(name, cam, timeout_ms=timeout_ms).install()


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
    follower = SeeedB601DMFollower(SeeedB601DMFollowerConfig(
        id=env_str("B601_ID", "follower1"),
        port=env_str("B601_PORT", "/dev/ttyACM0"),
        can_adapter=env_str("B601_CAN_ADAPTER", "damiao"),
        dm_serial_baud=env_int("B601_DM_SERIAL_BAUD", 921600),
        cameras=cameras,
    ))
    # Camera reads degrade gracefully (reuse last frame, reconnect dead
    # devices) instead of crashing the control loop on a dropped frame.
    _harden_cameras(follower)
    return follower


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
