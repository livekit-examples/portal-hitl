"""reBot 102 leader arm: FashionStar smart servos over motorbridge-smart-servo.

Hardware glue only, so `__main__.py` is the Portal flow alone. One synchronous
`get_action()` returns one 7-joint `.pos` dict in degrees.
"""
from __future__ import annotations

import json

from lerobot.teleoperators.rebot_102_leader import (
    RebotArm102Leader,
    RebotArm102LeaderTeleopConfig,
)

from portal_rebot.common import env_int, env_str, required_env


def build_leader() -> RebotArm102Leader:
    """`joint_directions` / `joint_ranges` keep their package defaults, which
    already match the B601 follower's ranges."""
    kwargs: dict = dict(
        id=env_str("REBOT102_LEADER_ID", "rebot_arm_102_leader"),
        port=required_env("REBOT102_LEADER_PORT"),
        baudrate=env_int("REBOT102_BAUDRATE", 1_000_000),
    )
    if directions := env_str("REBOT102_JOINT_DIRECTIONS", ""):
        kwargs["joint_directions"] = json.loads(directions)
    return RebotArm102Leader(RebotArm102LeaderTeleopConfig(**kwargs))
