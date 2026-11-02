"""Seeed reBot Arm B601-DM follower (Damiao CAN motors, USB cameras) and reBot 102 leader.

The lerobot driver exposes 7 joints as `.pos` keys in degrees. Three wrappers
keep a hardware hiccup from stopping the control loop or snapping the arm; see
tutorial/02-robot-loop.md.
"""
from __future__ import annotations

import json
import logging
import platform
import threading
import time
from typing import Sequence

import numpy as np
from lerobot.cameras import Cv2Backends, Cv2Rotation
from lerobot.cameras.opencv import OpenCVCameraConfig
from lerobot.robots.rebot_b601_follower import RebotB601Follower, RebotB601FollowerRobotConfig
from lerobot.teleoperators.rebot_102_leader import RebotArm102Leader, RebotArm102LeaderTeleopConfig

from _common import env_camera_id, env_float, env_int, env_str, required_env

logger = logging.getLogger(__name__)

_STALE_CEILING_MS = 60_000


class _ResilientCamera:
    """Serves the last good frame through a camera hiccup and reconnects in the background.

    A raising camera would otherwise throw away the motor state read that tick.
    It also reports `is_connected` while it holds a frame, because the follower
    refuses motor I/O when any camera is disconnected.
    """

    def __init__(self, name: str, cam, max_age_ms: float, reconnect_interval_s: float = 2.0,
                 stale_before_reconnect: int = 30) -> None:
        self._name = name
        self._cam = cam
        self._max_age_ms = max_age_ms
        self._reconnect_interval_s = reconnect_interval_s
        # A hung camera only times out; reconnect it after ~1s of that.
        self._stale_before_reconnect = stale_before_reconnect
        self._consecutive_stale = 0
        self._orig_read_latest = cam.read_latest
        self.last_frame: np.ndarray | None = None
        self._last_reconnect = 0.0
        self._reconnecting = False
        self._lock = threading.Lock()

    def install(self) -> None:
        self._cam.read_latest = self.read_latest
        self._cam._resilient_camera = self
        _patch_is_connected(type(self._cam))

    def read_latest(self, *args, **kwargs) -> np.ndarray | None:
        kwargs.setdefault("max_age_ms", self._max_age_ms)
        try:
            self.last_frame = self._orig_read_latest(*args, **kwargs)
        except TimeoutError as exc:
            # Older than our bar, but a stale frame beats a hole in the row.
            logger.warning("%s: %s - serving stale frame", self._name, exc)
            try:
                self.last_frame = self._orig_read_latest(max_age_ms=_STALE_CEILING_MS)
            except Exception:
                pass  # thread died between the two calls
            self._consecutive_stale += 1
            if self._consecutive_stale >= self._stale_before_reconnect:
                logger.warning("%s: %d consecutive stale reads - camera appears hung, reconnecting",
                               self._name, self._consecutive_stale)
                self._consecutive_stale = 0
                self._maybe_reconnect()
        except Exception as exc:
            logger.warning("%s: read failed (%s) - reconnecting, reusing last frame",
                           self._name, exc)
            self._consecutive_stale = 0
            self._maybe_reconnect()
        else:
            self._consecutive_stale = 0
        return self.last_frame

    def _maybe_reconnect(self) -> None:
        now = time.monotonic()
        with self._lock:
            if self._reconnecting or now - self._last_reconnect < self._reconnect_interval_s:
                return
            self._reconnecting, self._last_reconnect = True, now
        threading.Thread(target=self._reconnect, name=f"{self._name}-reconnect",
                         daemon=True).start()

    def _reconnect(self) -> None:
        try:
            try:
                self._cam.disconnect()
            except Exception:
                pass  # already down; connect() is what matters
            # The undecorated connect: its already-connected guard reads the
            # `is_connected` we patched, and would refuse every reconnect.
            connect = type(self._cam).connect
            getattr(connect, "__wrapped__", connect)(self._cam, warmup=False)
            logger.info("%s: reconnected", self._name)
        except Exception as exc:
            logger.warning("%s: reconnect failed (%s) - will retry", self._name, exc)
        finally:
            self._reconnecting = False


def _patch_is_connected(cam_cls: type) -> None:
    """Wrap the camera class's `is_connected` property, once; unmanaged instances are unaffected."""
    if getattr(cam_cls, "_resilient_patched", False):
        return
    original = cam_cls.is_connected

    def is_connected(self) -> bool:
        managed = getattr(self, "_resilient_camera", None)
        return original.fget(self) or (managed is not None and managed.last_frame is not None)

    cam_cls.is_connected = property(is_connected)
    cam_cls._resilient_patched = True


class _ResilientBus:
    """Keeps a motor I/O error from ending the control loop.

    Errors are counted and reported, and the bus is rebuilt after
    `errors_before_reconnect` in a row. The rebuild runs inline on the
    hardware thread, unlike the camera's, so it never overlaps a transaction.
    """

    def __init__(self, follower: RebotB601Follower, *, errors_before_reconnect: int,
                 reconnect_interval_s: float) -> None:
        self._follower = follower
        self._errors_before_reconnect = errors_before_reconnect
        self._reconnect_interval_s = reconnect_interval_s
        self._orig_get_observation = follower.get_observation
        self._orig_send_action = follower.send_action
        self._consecutive = 0
        self._last_reconnect = 0.0
        self._last_report = 0.0
        self.errors = 0
        self.reconnects = 0
        self.on_recovered = lambda why="": None

    def install(self) -> None:
        self._follower.get_observation = self.get_observation
        self._follower.send_action = self.send_action

    @property
    def degraded(self) -> bool:
        return self._consecutive > 0

    def get_observation(self) -> dict:
        try:
            observation = self._orig_get_observation()
        except Exception as exc:
            self._failed("get_observation", exc)
            return self._frames_only()  # cameras are on USB, not the CAN bus
        self._consecutive = 0
        return observation

    def send_action(self, action: dict) -> dict:
        try:
            applied = self._orig_send_action(action)
        except Exception as exc:
            self._failed("send_action", exc)
            return {}
        self._consecutive = 0
        return applied

    def _frames_only(self) -> dict:
        frames = {}
        for name, cam in self._follower.cameras.items():
            try:
                frames[name] = cam.read_latest()
            except Exception:
                pass  # _ResilientCamera already reports its own trouble
        return frames

    def _failed(self, what: str, exc: BaseException) -> None:
        self.errors += 1
        self._consecutive += 1
        now = time.monotonic()
        # Report the first failure, then at most once a second.
        if self._consecutive == 1 or now - self._last_report >= 1.0:
            self._last_report = now
            logger.warning("motor bus: %s failed (%s) - %d consecutive",
                           what, exc, self._consecutive)
        if self._consecutive < self._errors_before_reconnect:
            return
        if now - self._last_reconnect < self._reconnect_interval_s:
            return
        self._last_reconnect = now
        self._reconnect()

    def _reconnect(self) -> None:
        """Rebuild the CAN bus with the driver's own connect, cameras left alone.

        The cameras are hidden for the call because they report connected and
        would make it refuse. `calibrate=False`: a control loop must never block on `input()`.
        """
        logger.warning("motor bus: rebuilding after %d consecutive failures", self._consecutive)
        for motor in self._follower.motors.values():
            for call in ("disable", "clear_error", "close"):
                try:
                    getattr(motor, call)()
                except Exception:
                    pass  # tearing down a bus that is already broken
        try:
            self._follower.bus.close()
        except Exception:
            pass
        self._follower.bus = None
        self._follower.motors = {}

        cameras, self._follower.cameras = self._follower.cameras, {}
        try:
            connect = type(self._follower).connect
            getattr(connect, "__wrapped__", connect)(self._follower, calibrate=False)
        except Exception as exc:
            logger.warning("motor bus: rebuild failed (%s) - will retry in %.0fs",
                           exc, self._reconnect_interval_s)
            return
        finally:
            self._follower.cameras = cameras
        self.reconnects += 1
        self._consecutive = 0
        self.on_recovered("motor bus rebuilt")
        logger.warning("motor bus: reconnected and re-enabled; the arm is live again")


class _StepLimiter:
    """Moves the commanded target at most `max_step_deg` per command, so the arm never jumps.

    It ramps the TARGET and never clamps the tracking error: under MIT control
    torque is `kp * (target - present)`, so clamping the error would cap force
    and leave a gravity-loaded joint stuck. `max_lag_deg` only stops the target
    running away from an arm that cannot follow.

    The ramp restarts from the present pose at startup, after a bus rebuild, and
    after a pause in commands. `uncapped` joints (the gripper, whose force is
    already bounded) skip it. See tutorial/02-robot-loop.md.
    """

    _IDLE_RESEED_S = 0.5

    def __init__(self, follower: RebotB601Follower, max_step_deg: float,
                 max_lag_deg: float, uncapped: Sequence[str] = ()) -> None:
        self._follower = follower
        self.max_step_deg = max_step_deg
        self.max_lag_deg = max_lag_deg
        self.uncapped = {name.strip() for name in uncapped if name.strip()}
        self._orig_get_observation = follower.get_observation
        self._orig_send_action = follower.send_action
        self._present: dict[str, float] = {}
        self._target: dict[str, float] = {}
        self._last_command = 0.0
        self.clamped = 0
        self.refused = 0

    def install(self) -> None:
        self._follower.get_observation = self.get_observation
        self._follower.send_action = self.send_action

    @property
    def enabled(self) -> bool:
        return self.max_step_deg > 0

    def reseed(self, why: str = "") -> None:
        """Start the next ramp from wherever the arm actually is."""
        if self._target:
            logger.info("speed cap: re-seeding the ramp from the present pose%s",
                        f" ({why})" if why else "")
        self._target = {}

    def get_observation(self) -> dict:
        observation = self._orig_get_observation()
        for key, value in observation.items():
            if key.endswith(".pos"):
                self._present[key] = float(value)
        return observation

    def send_action(self, action: dict) -> dict:
        if not self.enabled:
            return self._orig_send_action(action)
        if not self._present:
            self.refused += 1
            logger.warning("speed cap: no observation yet, refusing to command the arm")
            return {}
        now = time.monotonic()
        if self._last_command and now - self._last_command > self._IDLE_RESEED_S:
            self.reseed("commands resumed after a pause")
        self._last_command = now
        return self._orig_send_action(self.step(action))

    def step(self, action: dict) -> dict:
        stepped = {}
        for name, goal in action.items():
            goal = float(goal)
            present = self._present.get(name)
            if present is None or name.removesuffix(".pos") in self.uncapped:
                stepped[name] = goal
                continue
            base = self._target.get(name, present)
            move = max(-self.max_step_deg, min(self.max_step_deg, goal - base))
            target = base + move
            target = max(present - self.max_lag_deg,
                         min(present + self.max_lag_deg, target))
            if target != goal:
                self.clamped += 1
            self._target[name] = target
            stepped[name] = target
        return stepped


def build_follower(tracks: Sequence[str], fps: int) -> RebotB601Follower:
    """Build the follower with one camera per `portal.yaml` video track.

    Track `arm_camera` reads its device from `B601_CAM_ARM`, defaulting to
    /dev/video0, /dev/video2, and so on. Linux forces V4L2, because other
    OpenCV backends can ignore the requested format and size.
    """
    width, height = env_int("B601_CAM_WIDTH", 640), env_int("B601_CAM_HEIGHT", 480)
    cameras = {
        name: OpenCVCameraConfig(
            index_or_path=env_camera_id(
                f"B601_CAM_{name.removesuffix('_camera').upper()}", f"/dev/video{2 * i}"),
            fps=fps,
            width=width,
            height=height,
            rotation=Cv2Rotation.NO_ROTATION,
            fourcc="MJPG",
            backend=Cv2Backends.V4L2 if platform.system() == "Linux" else Cv2Backends.ANY,
        )
        for i, name in enumerate(tracks)
    }
    follower = RebotB601Follower(RebotB601FollowerRobotConfig(
        id=env_str("B601_ID", "follower1"),
        port=env_str("B601_PORT", "/dev/ttyACM0"),
        can_adapter=env_str("B601_CAN_ADAPTER", "damiao"),
        dm_serial_baud=env_int("B601_DM_SERIAL_BAUD", 921600),
        cameras=cameras,
    ))
    max_age_ms = env_int("B601_CAM_MAX_FRAME_AGE_MS", 500)
    for name, cam in follower.cameras.items():
        _ResilientCamera(name, cam, max_age_ms).install()

    # The limiter wraps the driver, and the bus wraps the limiter, so motor
    # faults are caught outside the ramp.
    limiter = _StepLimiter(
        follower,
        max_step_deg=env_float("B601_MAX_STEP_DEG", 5.0),
        max_lag_deg=env_float("B601_MAX_LAG_DEG", 30.0),
        uncapped=env_str("B601_UNCAPPED_JOINTS", "gripper").split(","),
    )
    limiter.install()
    bus = _ResilientBus(
        follower,
        errors_before_reconnect=env_int("B601_BUS_ERRORS_BEFORE_RECONNECT", 5),
        reconnect_interval_s=env_float("B601_BUS_RECONNECT_INTERVAL_S", 2.0),
    )
    bus.install()
    # After a rebuild the arm is wherever gravity left it; ramp from there.
    bus.on_recovered = limiter.reseed
    follower.resilient_bus = bus
    follower.step_limiter = limiter
    logger.info("speed cap: %.2f deg per command (%.0f deg/s at %d fps); uncapped: %s",
                limiter.max_step_deg, limiter.max_step_deg * fps, fps,
                ", ".join(sorted(limiter.uncapped)) or "none")
    return follower


def split_state_frames(
    observation: dict, tracks: Sequence[str]
) -> tuple[dict[str, float], dict[str, np.ndarray]]:
    """Split the follower's observation into (state, frames). Only `.pos` values are published."""
    state = {k: float(v) for k, v in observation.items() if k.endswith(".pos")}
    frames = {t: observation[t] for t in tracks if observation.get(t) is not None}
    return state, frames


def build_leader() -> RebotArm102Leader:
    """The package's default joint ranges already match the B601 follower."""
    kwargs: dict = dict(
        id=env_str("REBOT102_LEADER_ID", "rebot_arm_102_leader"),
        port=required_env("REBOT102_LEADER_PORT"),
        baudrate=env_int("REBOT102_BAUDRATE", 1_000_000),
    )
    if directions := env_str("REBOT102_JOINT_DIRECTIONS", ""):
        kwargs["joint_directions"] = json.loads(directions)
    return RebotArm102Leader(RebotArm102LeaderTeleopConfig(**kwargs))
