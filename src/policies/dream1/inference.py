"""Run Dream-1 zero-shot as a Portal operator; press c in teleop to hand it control.

Dream-1 runs on Dream Labs' servers and was trained on YAM arms. The `dream`
client bridges to the B601: it adds the hand pose computed from our joints to
each request, and solves the model's hand poses back to our joints, using the
arm file `dreamcli tcp` writes. See tutorial/06-plugging-in-policies.md.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import sys
import time
from pathlib import Path

import cv2
import dream
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from _common import CONFIG_PATH, HardwareThread, env_float, env_int, env_str, load_env, mint_token, pace, required_env
from livekit.portal import Operator, OperatorConfig, frame_bytes_to_numpy_rgb

IDENTITY = "policy-dream-1"
GRIPPER = "gripper.pos"  # 0 deg is closed
ROLES = ("overhead", "wrist_l", "wrist_r")  # the three views Dream-1 reads
FRAME_SIZE = (320, 176)  # width, height
ROW_HZ = 20  # chunk rows per second
STALE_S = 0.2  # hold when the newest observation is older than this


def load_rest_pose(arm_file: Path) -> list[float]:
    """The arm file's `rest` pose, which the parked left arm reports.

    Refuses a tool center point nobody measured: every grasp lands relative to it."""
    arm = json.loads(arm_file.read_text())
    if (arm.get("tcp_calibration") or {}).get("status") != "calibrated":
        raise ValueError(f"{arm_file}: tool center point not confirmed; measure it with `dreamcli tcp`")
    return arm["rest"]


def embodiment(joints: int) -> dict:
    """Where each part sits in a state or action row. The client checks every request and reply against it."""
    spans, start = {}, 0
    for side in ("right", "left"):
        for part, width in (("arm", joints), ("grip", 1), ("eef_xyz", 3), ("eef_rot6", 6)):
            spans[f"{side}_{part}"] = [start, start + width]
            start += width
    return {"state": spans, "action": spans, "video": {role: f"observation.images.{role}" for role in ROLES}}


def camera_roles(spec: str, tracks) -> dict[str, str]:
    """Parse DREAM_CAMERAS, such as `wrist_r=arm_camera,overhead=top_camera`."""
    roles = {}
    for item in filter(None, spec.replace(" ", "").split(",")):
        role, _, track = item.partition("=")
        if role not in ROLES or track not in tracks:
            raise ValueError(f"DREAM_CAMERAS {item!r}: role must be one of {ROLES}, track one of {list(tracks)}")
        roles[role] = track
    if not roles:
        raise ValueError("DREAM_CAMERAS maps no camera")
    return roles


def encode_frame(rgb: np.ndarray) -> str:
    """Center-crop to 16:9, resize to 320x176, and encode as base64 JPEG."""
    h, w = rgb.shape[:2]
    if h * 16 > w * 9:
        top = (h - w * 9 // 16) // 2
        rgb = rgb[top:top + w * 9 // 16]
    small = cv2.resize(rgb, FRAME_SIZE, interpolation=cv2.INTER_AREA)
    _, jpeg = cv2.imencode(".jpg", cv2.cvtColor(small, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 85])
    return base64.b64encode(jpeg.tobytes()).decode("ascii")


BLACK = encode_frame(np.zeros((FRAME_SIZE[1], FRAME_SIZE[0], 3), np.uint8))


def to_model(state: dict, joints: list[str], open_deg: float) -> np.ndarray:
    """Robot state in degrees -> one model row: joints in radians, then grip from 0 closed to 1 open."""
    grip = np.clip(state[GRIPPER] / open_deg, 0.0, 1.0)
    return np.array([math.radians(state[joint]) for joint in joints] + [grip])


def to_robot(row: np.ndarray, joints: list[str], open_deg: float) -> dict[str, float]:
    """One model row -> a Portal action in degrees."""
    action = {joint: math.degrees(value) for joint, value in zip(joints, row)}
    action[GRIPPER] = float(np.clip(row[-1], 0.0, 1.0)) * open_deg
    return action


def step_toward(last: np.ndarray, goal: np.ndarray, max_step: np.ndarray) -> np.ndarray:
    """Move the commanded target at most `max_step` per tick, so the arm never jumps."""
    return last + np.clip(goal - last, -max_step, max_step)


def chunk_rows(reply: dict, joints: int) -> np.ndarray:
    """The right arm's chunk: one row per step, joints in radians then grip."""
    rows = np.asarray(reply["q_r_chunk"], dtype=np.float64)
    if rows.ndim != 2 or rows.shape[1] != joints + 1 or not np.isfinite(rows).all():
        raise dream.DreamActionError(f"q_r_chunk has shape {rows.shape}; expected [rows, {joints + 1}]")
    return rows


def retry_delay(error: dream.DreamError) -> float:
    """Seconds to wait before asking again. Raises the error when asking again cannot help."""
    if isinstance(error, dream.DreamSessionExpired):
        return 0.0  # the client already moved to a new session
    if isinstance(error, (dream.DreamTimeout, dream.DreamProtocolError)) or (
            isinstance(error, dream.DreamServerError) and error.retryable):
        wait = getattr(error, "retry_after_s", None) or 1.0
        print(f"[policy-dream-1] {getattr(error, 'reason', None) or error}; asking again in {wait:.1f}s")
        return wait
    raise error


async def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default=env_str("PORTAL_DATA_TASK", "reBot B601 manipulation"))
    parser.add_argument("--dry-run", action="store_true", help="print the targets instead of sending them")
    args = parser.parse_args()

    url, room = required_env("LIVEKIT_URL"), required_env("LIVEKIT_ROOM")
    cfg = OperatorConfig.from_yaml_file(CONFIG_PATH, room)
    fps = env_int("PORTAL_FPS", cfg.fps)
    joints = [field.name for field in cfg.action_schema if field.name != GRIPPER]  # URDF order, base to flange
    arm_file = Path(required_env("DREAM_ARM_FILE")).expanduser().resolve()
    rest = load_rest_pose(arm_file)
    roles = camera_roles(env_str("DREAM_CAMERAS", "wrist_r=arm_camera"), cfg.video_tracks)
    open_deg = env_float("DREAM_GRIPPER_OPEN_DEG", -270.0)
    max_step = np.full(len(joints) + 1, env_float("DREAM_MAX_JOINT_RAD_S", 0.5) / fps)
    max_step[-1] = 0.05 * ROW_HZ / fps  # the gripper moves at most 5% of its range per row

    client = dream.DreamClient()
    worker = HardwareThread("dream-1")  # one request at a time, off the event loop
    print("[policy-dream-1] compiling IK ...")
    await worker.run(client.policy, {
        "checkpoint": env_str("DREAM_CHECKPOINT", "dreamlabs/dream-1-beta"),
        "embodiment": embodiment(len(joints)),
        "ik": {"arm": str(arm_file), "head": "eef"},
        "action_hz": ROW_HZ,
    })

    def ask() -> tuple[float, np.ndarray]:
        asked_at = time.monotonic()
        return asked_at, chunk_rows(client.get_action(), len(joints))

    def send_observation(obs) -> None:
        """The B601 is Dream-1's right arm. The left arm is parked at `rest` with its gripper open."""
        frames = {}
        for role in ROLES:
            if role not in roles:
                frames[f"video.{role}"] = BLACK
                continue
            frame = obs.frames[roles[role]]
            frames[f"video.{role}"] = encode_frame(frame_bytes_to_numpy_rgb(frame.data, frame.width, frame.height))
        right = to_model(obs.state, joints, open_deg)
        client.set_observation(frames, {
            "prompt": args.task,
            "state.right_arm": right[:-1].tolist(),
            "state.right_grip": right[-1:].tolist(),
            "state.left_arm": rest,
            "state.left_grip": [1.0],
        })

    op = Operator(cfg)
    latest_obs, received_at = None, 0.0
    control_changed = True

    def on_observation(obs) -> None:
        nonlocal latest_obs, received_at
        latest_obs, received_at = obs, time.monotonic()

    def on_active_operator_changed(identity) -> None:
        nonlocal control_changed
        control_changed = True
        print(f"[policy-dream-1] active operator: {identity}")

    op.on_observation(on_observation)
    op.on_active_operator_changed(on_active_operator_changed)

    request = None  # the get_action in flight
    chunk = None  # (asked_at, rows, first row played); row k is due k / ROW_HZ s after asked_at
    target = None  # the last commanded row, in model units
    sent_ts = None  # the observation the client has
    retry_at = 0.0
    try:
        await op.connect(url, mint_token(IDENTITY, room))
        me = op.local_identity()
        print(f"[policy-dream-1] ready{' (dry run)' if args.dry_run else ''}; press c in teleop to select {me}")
        async for tick in pace(fps):
            obs = latest_obs
            if op.active_operator() != me:
                continue
            if (obs is None or time.monotonic() - received_at > STALE_S
                    or any(track not in obs.frames for track in roles.values())):
                continue  # no fresh view of the arm: hold the last target

            if control_changed:
                # Every handoff to Dream-1 starts a new episode: a new session, an
                # empty frame window, and a ramp from where the arm is now.
                control_changed = False
                if request is not None:
                    await asyncio.gather(request, return_exceptions=True)
                    request = None
                await worker.run(client.reset_episode)
                chunk, sent_ts, retry_at = None, None, 0.0
                target = to_model(obs.state, joints, open_deg)

            if obs.timestamp_us != sent_ts:
                send_observation(obs)
                sent_ts = obs.timestamp_us

            now = time.monotonic()
            if request is not None and request.done():
                try:
                    asked_at, rows = request.result()
                    chunk = (asked_at, rows, int((now - asked_at) * ROW_HZ))  # skip rows the round trip used up
                except dream.DreamError as error:
                    retry_at = now + retry_delay(error)
                request = None

            row, played, remaining = None, 0, 0
            if chunk is not None:
                asked_at, rows, first = chunk
                due = int((now - asked_at) * ROW_HZ)
                if due < len(rows):
                    row, played, remaining = rows[due], due - first, len(rows) - due
            if request is None and now >= retry_at and client.refill_due(played, remaining):
                request = asyncio.ensure_future(worker.run(ask))
            if row is None:
                continue  # the chunk ran out: hold until the next one lands

            target = step_toward(target, row, max_step)
            action = to_robot(target, joints, open_deg)
            if not args.dry_run:
                op.send_action(action, in_reply_to_ts_us=obs.timestamp_us)
            elif tick % fps == 0:
                print("[policy-dream-1] target " + ", ".join(f"{k}={v:.1f}" for k, v in action.items()))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[policy-dream-1] stopping")
    finally:
        worker.close()  # waits for a request in flight
        client.close()
        try:
            await op.disconnect()
        finally:
            op.close()


if __name__ == "__main__":
    asyncio.run(main())
