"""Robot safety test: what the arm does when things go wrong.

    uv run python -m test.test_resilience      (no LiveKit server, no hardware)

Two layers, both between the network and the motors.

`_ResilientBus`: a CAN write that fails used to raise out of `send_action`,
through `HardwareThread.run`, past the tick loop's `except` and into its
`finally` -- the rig quit, mid-motion, over something usually gone by the next
tick.

`_StepLimiter`: the commanded target advances at most `max_step_deg` per
command. The case that matters is the discontinuity -- after a bus fault the
arm has sagged and the leader has not moved, so an unramped command would send
it straight back at full speed.

The checks below pin down the thing that is easy to get backwards. The cap
ramps the TARGET; it must never clamp the tracking error, because under MIT
control that error is the torque command, and capping it stops the
gravity-loaded joints moving at all.

The fake stands in for `RebotB601Follower` at exactly the surface the wrapper
touches: `get_observation`, `send_action`, `cameras`, `motors`, `bus`, and a
`connect` the rebuild path calls. It is deliberately not a mock -- the rebuild
has to actually put the bus back for the recovery check to mean anything.
"""
from __future__ import annotations

import sys

from portal_rebot.robot.follower import _ResilientBus
from test._harness import Checks

ERRORS_BEFORE_RECONNECT = 3


class FakeCamera:
    def __init__(self, name):
        self.name = name

    def read_latest(self):
        return f"{self.name}-frame"


class FakeFollower:
    """Fails motor I/O while `failing` is set; `connect` puts the bus back."""

    def __init__(self, connect_fails: int = 0) -> None:
        self.failing = False
        self.connect_fails = connect_fails
        self.bus = object()
        self.motors = {"shoulder_pan": FakeMotor()}
        self.cameras = {"arm_camera": FakeCamera("arm_camera")}
        self.connects = 0
        self.cameras_seen_at_connect: list[int] = []
        self.calibrate_args: list[bool] = []
        self.actions: list[dict] = []

    def get_observation(self):
        if self.failing:
            raise OSError("write failed: [Errno 5] Input/output error")
        return {"shoulder_pan.pos": 1.0, "arm_camera": "arm_camera-frame"}

    def send_action(self, action):
        if self.failing:
            raise OSError("write failed: [Errno 5] Input/output error")
        self.actions.append(action)
        return action

    def connect(self, calibrate: bool = True) -> None:
        self.connects += 1
        self.calibrate_args.append(calibrate)
        # The real driver iterates `self.cameras` here and calls cam.connect(),
        # which raises on an already-connected camera. Record what it would
        # have seen.
        self.cameras_seen_at_connect.append(len(self.cameras))
        if self.connects <= self.connect_fails:
            raise OSError("serial port busy")
        self.bus = object()
        self.motors = {"shoulder_pan": FakeMotor()}


class FakeMotor:
    def __init__(self):
        self.closed = False

    def disable(self):
        pass

    def clear_error(self):
        raise OSError("bus already gone")   # teardown must tolerate this

    def close(self):
        self.closed = True


def bus_for(follower, interval=0.0):
    return _ResilientBus(follower, errors_before_reconnect=ERRORS_BEFORE_RECONNECT,
                         reconnect_interval_s=interval)


def main() -> int:
    c = Checks("robot safety")

    # -- a transient failure must not raise, and must not reconnect ----------
    f = FakeFollower()
    bus = bus_for(f)
    bus.install()
    c.check(f.get_observation == bus.get_observation and f.send_action == bus.send_action,
            "install shadows both driver calls")

    f.failing = True
    observation = f.get_observation()
    c.check(observation == {"arm_camera": "arm_camera-frame"},
            "a failed read still returns camera frames", str(observation))
    c.check(f.send_action({"shoulder_pan.pos": 5.0}) == {},
            "a failed write returns empty instead of raising")
    c.check(bus.errors == 2 and f.connects == 0,
            "below the threshold nothing is rebuilt", f"errors={bus.errors}")
    c.check(bus.degraded, "the fault is visible while it lasts")

    f.failing = False
    f.get_observation()
    c.check(not bus.degraded, "one good tick clears the fault")
    c.check(f.connects == 0, "a transient fault never rebuilt the bus")

    # -- a sustained failure must rebuild, and recover ------------------------
    f = FakeFollower()
    bus = bus_for(f)
    bus.install()
    f.failing = True
    for _ in range(ERRORS_BEFORE_RECONNECT):
        f.send_action({"shoulder_pan.pos": 5.0})
    c.check(f.connects == 1, "the bus is rebuilt at the threshold", f"connects={f.connects}")
    c.check(f.calibrate_args == [False],
            "the rebuild never runs calibration -- a control loop cannot block on input()")
    c.check(f.cameras_seen_at_connect == [0],
            "the rebuild hides the cameras, which are still up and would raise",
            str(f.cameras_seen_at_connect))
    c.check(f.cameras and len(f.cameras) == 1, "and puts them back afterwards")
    c.check(bus.reconnects == 1 and not bus.degraded, "recovery is recorded and clears the fault")

    f.failing = False
    c.check(f.send_action({"shoulder_pan.pos": 7.0}) == {"shoulder_pan.pos": 7.0},
            "the arm follows again after the rebuild")

    # -- a rebuild that itself fails must retry, not raise --------------------
    f = FakeFollower(connect_fails=1)
    bus = bus_for(f)
    bus.install()
    f.failing = True
    for _ in range(ERRORS_BEFORE_RECONNECT * 2):
        f.send_action({"shoulder_pan.pos": 5.0})
    c.check(f.connects >= 2, "a failed rebuild is retried", f"connects={f.connects}")
    c.check(bus.reconnects == 1, "only the rebuild that worked is counted",
            f"reconnects={bus.reconnects}")
    f.failing = False
    c.check(f.get_observation()["shoulder_pan.pos"] == 1.0,
            "the loop is still alive after both failures")

    # -- a bus that stays down must not bury the console ---------------------
    f = FakeFollower(connect_fails=99)
    bus = bus_for(f, interval=30.0)
    bus.install()
    f.failing = True
    import portal_rebot.robot.follower as follower_module
    lines = []
    original = follower_module.logger.warning
    follower_module.logger.warning = lambda msg, *a: lines.append(msg % a if a else msg)
    try:
        for _ in range(90):          # three seconds of ticks at 30fps
            f.send_action({"shoulder_pan.pos": 5.0})
    finally:
        follower_module.logger.warning = original
    failures = [line for line in lines if "failed" in line]
    c.check(len(failures) <= 5, "a sustained fault reports once a second, not once a tick",
            f"{len(failures)} lines for 90 ticks")

    # -- the speed cap --------------------------------------------------------
    from portal_rebot.robot.follower import _StepLimiter

    def limiter_for(f, step=5.0, lag=30.0, uncapped=()):
        limiter = _StepLimiter(f, max_step_deg=step, max_lag_deg=lag, uncapped=uncapped)
        limiter.install()
        return limiter

    f = FakeFollower()
    limiter = limiter_for(f)
    c.check(f.send_action({"shoulder_pan.pos": 50.0}) == {} and limiter.refused == 1,
            "a command before the first observation is refused, not passed through")

    f.get_observation()                       # present = 1.0 deg
    c.check(f.send_action({"shoulder_pan.pos": 50.0}) == {"shoulder_pan.pos": 6.0},
            "the first command ramps one step from the present pose", str(f.actions[-1]))
    c.check(f.send_action({"shoulder_pan.pos": 3.0}) == {"shoulder_pan.pos": 3.0},
            "a goal inside one step is reached exactly, not overshot")

    # THE regression this file exists for. Under MIT control torque is
    # kp*(target - present), so a cap that clamped the target to present +- step
    # capped the FORCE -- 0.79 for wrist_yaw at kp 9 -- and the gravity-loaded
    # joints stopped moving. The target must keep advancing even when the arm
    # does not, or the arm can never build the torque to catch up.
    f = FakeFollower()
    limiter = limiter_for(f)
    f.get_observation()                       # present pinned at 1.0: a stuck arm
    targets = [f.send_action({"shoulder_pan.pos": 90.0})["shoulder_pan.pos"]
               for _ in range(6)]
    c.check(targets == [6.0, 11.0, 16.0, 21.0, 26.0, 31.0],
            "the target keeps ramping while the arm is stuck, so torque can build",
            str(targets))
    c.check(targets[-1] - 1.0 > 5.0,
            "tracking error is allowed to exceed one step -- it is NOT the cap",
            f"error={targets[-1] - 1.0:g} deg with a {5.0:g} deg step")

    # ...but not without limit: the windup guard stops the setpoint running away
    # from an arm that cannot follow it.
    for _ in range(50):
        final = f.send_action({"shoulder_pan.pos": 900.0})["shoulder_pan.pos"]
    c.check(final == 1.0 + 30.0, "the windup guard caps how far the target may lead",
            f"target={final} present=1.0 lag=30")

    # A discontinuity must restart the ramp from reality, or recovery is a snap.
    limiter.reseed("test")
    limiter._present = {"shoulder_pan.pos": -40.0}     # the arm sagged
    c.check(f.send_action({"shoulder_pan.pos": 90.0})["shoulder_pan.pos"] == -35.0,
            "after a re-seed the ramp restarts from the present pose",
            str(f.actions[-1]))

    # The bus rebuild has to trigger that re-seed, since it re-enables torque.
    f = FakeFollower()
    limiter = limiter_for(f)
    bus = bus_for(f)
    bus.on_recovered = limiter.reseed
    bus.install()
    f.get_observation()
    f.send_action({"shoulder_pan.pos": 90.0})
    c.check(limiter._target, "a target is established while things are healthy")
    f.failing = True
    for _ in range(ERRORS_BEFORE_RECONNECT):
        f.send_action({"shoulder_pan.pos": 90.0})
    c.check(not limiter._target, "a bus rebuild re-seeds the ramp")

    # The gripper is exempt: it is not under impedance control, its force is
    # already bounded by gripper_torque_ratio, and an arm-sized cap would turn
    # a 270-degree grasp into a two-second squeeze.
    f = FakeFollower()
    limiter = limiter_for(f, uncapped=("gripper",))
    f._present = {"shoulder_pan.pos": 1.0, "gripper.pos": 0.0}
    limiter._present = dict(f._present)
    applied = f.send_action({"shoulder_pan.pos": 90.0, "gripper.pos": -270.0})
    c.check(applied["gripper.pos"] == -270.0, "the gripper closes in one command",
            str(applied["gripper.pos"]))
    c.check(applied["shoulder_pan.pos"] == 6.0,
            "while the arm joints in the same command are still ramped",
            str(applied["shoulder_pan.pos"]))

    # Off means off: the driver sees the command untouched.
    f = FakeFollower()
    limiter_for(f, step=0.0)
    c.check(f.send_action({"shoulder_pan.pos": 900.0}) == {"shoulder_pan.pos": 900.0},
            "max_step_deg=0 disables the cap entirely")

    # -- backoff: a rebuild is not attempted every single tick ---------------
    f = FakeFollower(connect_fails=99)
    bus = bus_for(f, interval=30.0)
    bus.install()
    f.failing = True
    for _ in range(ERRORS_BEFORE_RECONNECT * 10):
        f.send_action({"shoulder_pan.pos": 5.0})
    c.check(f.connects == 1, "the backoff holds off a rebuild storm", f"connects={f.connects}")
    return c.exit_code


if __name__ == "__main__":
    sys.exit(main())
