"""Seeed reBot Arm B601-DM follower: Damiao CAN motors plus USB cameras.

Hardware glue only, so `__main__.py` is the Portal flow alone. The driver ships
in stock lerobot (the `rebot` extra) and exposes 7 joints with `.pos` keys in
degrees. See docs/architecture.md#hardware-glue for the reasoning behind the
three wrappers below.
"""
from __future__ import annotations

import logging
import platform
import threading
import time
from typing import Sequence

import numpy as np
from lerobot.cameras import Cv2Backends, Cv2Rotation
from lerobot.cameras.opencv import OpenCVCameraConfig
from lerobot.robots.rebot_b601_follower import (
    RebotB601Follower,
    RebotB601FollowerRobotConfig,
)

from portal_rebot.common import env_camera_id, env_float, env_int, env_str

logger = logging.getLogger(__name__)

_STALE_CEILING_MS = 60_000


class _ResilientCamera:
    """Stops a camera hiccup from tearing down the control loop.

    `read_latest()` raises TimeoutError past `max_age_ms` and RuntimeError once
    the background read thread has died; either one, raised mid-
    `get_observation()`, throws away the motor state already read that tick. So
    serve the last good frame across the gap and reconnect on a background
    thread. `None` comes back only before the first frame ever arrives, which
    `split_state_frames` drops.

    It also reports `is_connected` True while holding a buffered frame, because
    `RebotB601Follower` guards its motor I/O on `all(cam.is_connected)` -- a
    camera blip would otherwise raise DeviceNotConnectedError out of the *motor*
    calls. Before the first frame the true device state shows through, so a real
    startup failure surfaces.
    """

    def __init__(self, name: str, cam, max_age_ms: float, reconnect_interval_s: float = 2.0,
                 stale_before_reconnect: int = 30) -> None:
        self._name = name
        self._cam = cam
        self._max_age_ms = max_age_ms
        self._reconnect_interval_s = reconnect_interval_s
        # A camera alive but no longer producing only ever raises TimeoutError,
        # which alone would serve the same frozen frame forever. ~1s at 30fps.
        self._stale_before_reconnect = stale_before_reconnect
        self._consecutive_stale = 0
        # Grabbed before install() shadows cam.read_latest. Never re-grab: that
        # attribute now points at us and would recurse forever.
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
            # A frame is buffered, just older than our bar: take it anyway, a
            # stale frame beats a hole in the row.
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
            # Connect's UNDECORATED body: `OpenCVCamera.connect` is wrapped in
            # @check_if_already_connected, which tests the very `is_connected`
            # we patch to report True while holding a frame, so the decorated
            # call raises DeviceAlreadyConnectedError on every reconnect that
            # is ever needed. warmup=False so a flaky device cannot stall this
            # thread; the loop keeps serving the last frame until one lands.
            connect = type(self._cam).connect
            getattr(connect, "__wrapped__", connect)(self._cam, warmup=False)
            logger.info("%s: reconnected", self._name)
        except Exception as exc:
            logger.warning("%s: reconnect failed (%s) - will retry", self._name, exc)
        finally:
            self._reconnecting = False


def _patch_is_connected(cam_cls: type) -> None:
    """Wrap a camera class's `is_connected` getter, once. The property lives on
    the class, so an instance attribute cannot shadow it; instances without the
    back-reference fall through unchanged."""
    if getattr(cam_cls, "_resilient_patched", False):
        return
    original = cam_cls.is_connected

    def is_connected(self) -> bool:
        managed = getattr(self, "_resilient_camera", None)
        return original.fget(self) or (managed is not None and managed.last_frame is not None)

    cam_cls.is_connected = property(is_connected)
    cam_cls._resilient_patched = True


class _ResilientBus:
    """Stops a motor I/O error from tearing down the control loop.

    A failing CAN call otherwise raises through `HardwareThread.run`, past the
    tick loop's `except` (which catches only cancellation) and into its
    `finally`: the rig quits, mid-motion, over something usually gone by the
    next tick. So: swallow, count, report, and rebuild the bus after
    `errors_before_reconnect` consecutive failures.

    **The reconnect runs inline, on the calling thread** -- the opposite of
    `_ResilientCamera`. Every follower call funnels through one
    `HardwareThread` worker precisely so two can never overlap on the serial
    port, and rebuilding from anywhere else would tear the port out from under
    an in-flight transaction, which is what corrupts a CAN adapter. The tick
    blocks for the duration, and that is correct.
    """

    def __init__(self, follower: RebotB601Follower, *, errors_before_reconnect: int,
                 reconnect_interval_s: float) -> None:
        self._follower = follower
        self._errors_before_reconnect = errors_before_reconnect
        self._reconnect_interval_s = reconnect_interval_s
        # Grabbed before install() shadows them. Bound methods, so they keep
        # working across a bus rebuild.
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
            # Cameras hang off USB, not the CAN bus, so they are still good --
            # and video through a motor fault is what lets the operator see the
            # arm has stopped following rather than guess.
            return self._frames_only()
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
        # The first failure always speaks, then at most once a second: a bus
        # that stays down fails every tick, and 30 identical lines a second
        # bury the reconnect result that matters.
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
        """Rebuild the CAN bus and leave the cameras alone.

        `follower.connect()` cannot be used as-is: it is wrapped in
        `@check_if_already_connected`, and it calls `cam.connect()` on every
        camera -- which `_ResilientCamera` reports as connected while it holds a
        frame, so the call raises `DeviceAlreadyConnectedError` and never
        reaches `configure()`, the part that re-enables the motors. Emptying
        `cameras` runs the driver's own bus path and skips the camera path,
        which beats copying the bus construction into this repo where it would
        rot. `calibrate=False` because a control loop must never block on
        `input()`.
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
    """Rate-limits the commanded target: it may advance at most `max_step_deg`
    per command, so the arm is never asked to jump.

    **It ramps the target, and does NOT clamp the tracking error.** The B601
    runs `control_mode = "mit"`, so torque *is* the position error --
    `kp * (target - present)` -- and clamping that error caps FORCE, not speed:
    at 5 degrees, too little to hold a gravity-loaded joint, which stops moving
    while the stiffer ones keep working. Ramping the target leaves the motor
    full authority to reach each setpoint and bounds only how fast the setpoint
    travels.

    The target is re-seeded from the present position at startup, after a bus
    rebuild, and whenever commands stop for `_IDLE_RESEED_S`, which is what
    makes every discontinuity a ramp from where the arm actually is.
    `max_lag_deg` is the only present-relative bound left, and it is a windup
    guard rather than a speed limit. `uncapped` joints skip the ramp entirely;
    the gripper is there because it is not under impedance control at all
    (`gripper_control_mode = "force_pos"`), so its force is already bounded.

    See docs/architecture.md#hardware-glue -- getting any of this backwards
    freezes the arm.
    """

    # Longer than this without a command and the arm may have been moved,
    # released, or have sagged, so the ramp restarts from reality.
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
            # An unseeded joint starts its ramp at the present position, which
            # is what makes startup and recovery ramps rather than jumps.
            base = self._target.get(name, present)
            move = max(-self.max_step_deg, min(self.max_step_deg, goal - base))
            target = base + move
            # Windup guard, not a speed limit: keep the setpoint from running
            # away from an arm that cannot follow it.
            target = max(present - self.max_lag_deg,
                         min(present + self.max_lag_deg, target))
            if target != goal:
                self.clamped += 1
            self._target[name] = target
            stepped[name] = target
        return stepped


def build_follower(tracks: Sequence[str], fps: int) -> RebotB601Follower:
    """Build the follower with one camera per `portal.yaml` video track.

    Each track's device comes from `B601_CAM_<TRACK>`, dropping a trailing
    `_camera` and uppercasing (`arm_camera` -> `B601_CAM_ARM`), defaulting to
    /dev/video0, /dev/video2, ...

    Linux forces V4L2: lerobot defaults to CAP_ANY and OpenCV's auto-picker can
    land on a backend (often GStreamer) that opens the device but no-ops
    S_FOURCC/S_FMT, showing up as fourcc='' and a 1920-wide frame that refuses
    to drop to 640.
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

    # Order matters: the limiter goes on first so it captures the driver's own
    # methods, then the bus wraps the limiter. A motor fault is then caught
    # OUTSIDE the ramp, which can never be what raises.
    limiter = _StepLimiter(
        follower,
        max_step_deg=env_float("B601_MAX_STEP_DEG", 5.0),
        max_lag_deg=env_float("B601_MAX_LAG_DEG", 30.0),
        # "gripper" mirrors the driver's own GRIPPER_MOTOR.
        uncapped=env_str("B601_UNCAPPED_JOINTS", "gripper").split(","),
    )
    limiter.install()
    bus = _ResilientBus(
        follower,
        errors_before_reconnect=env_int("B601_BUS_ERRORS_BEFORE_RECONNECT", 5),
        reconnect_interval_s=env_float("B601_BUS_RECONNECT_INTERVAL_S", 2.0),
    )
    bus.install()
    # A rebuild re-enables torque with the arm wherever gravity left it, so the
    # ramp restarts from there rather than from the pre-fault target.
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
    """Split the follower's flat observation into (state, frames).

    The follower writes `.pos`, `.vel` and `.torq` per motor, but `portal.yaml`
    publishes only the 7 `.pos` values. `OpenCVCameraConfig` defaults to
    color_mode=RGB, which is the layout Portal expects.
    """
    state = {k: float(v) for k, v in observation.items() if k.endswith(".pos")}
    frames = {t: observation[t] for t in tracks if observation.get(t) is not None}
    return state, frames
