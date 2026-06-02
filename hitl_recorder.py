"""Operator-side HITL recording driver.

Sits one level above :class:`~utils.recorder.DatasetRecorder` and owns
everything the operator loop's ``on_action`` callback used to do inline:

* a ring buffer of recently received observations,
* the clock-correct pairing of each executed action with the observation
  the operator was responding to,
* the stale-observation guard (and its skip counter), and
* the lazy construction of the underlying dataset recorder once the wire
  camera resolution is known.

Pulling this out keeps the operator loop's Portal callbacks one-liners and
puts the recording contract in a single unit. The clock-alignment rationale
(why leader actions match on receive time and policy actions on capture
time) lives in the module docstring of ``teleoperator.py``.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Optional, Sequence

from livekit.portal import Action, Observation, frame_bytes_to_numpy_rgb

from utils.recorder import DatasetRecorder, build_from_frame


class HitlRecorder:
    """Drives the dataset recorder from the operator's action/observation
    streams. ``observe`` feeds the obs ring; ``record`` pairs an action
    with the obs the operator was responding to and pushes one row. The
    underlying :class:`DatasetRecorder` is built lazily by
    ``ensure_recorder`` once a frame reveals the wire camera resolution,
    so every method is a no-op until then."""

    def __init__(
        self,
        *,
        fps: int,
        state_field_names: Sequence[str],
        action_field_names: Sequence[str],
        cameras: Sequence[str],
        max_obs_age_us: int,
        repo_id: str,
        task: str,
        root: str | None = None,
        history: int = 16,
    ) -> None:
        self._fps = fps
        self._state_field_names = list(state_field_names)
        self._action_field_names = list(action_field_names)
        self._cameras = tuple(cameras)
        self._max_obs_age_us = max_obs_age_us
        self._repo_id = repo_id
        self._task = task
        self._root = root

        # Ring buffer of recent observations as (obs, local_recv_us) pairs.
        # local_recv_us is this machine's wall clock when the obs arrived, so
        # leader actions can be matched against when the operator *saw* an obs
        # (teleop clock) rather than when the robot *captured* it (robot clock).
        # Sized for ~500ms of history at 30Hz: enough to align an action against
        # the obs the operator was responding to even with sync jitter, but
        # bounded so memory doesn't grow during a long session.
        self._history: deque[tuple[Observation, int]] = deque(maxlen=history)
        self._recorder: Optional[DatasetRecorder] = None
        self.skipped_frames = 0

    @property
    def is_ready(self) -> bool:
        """True once the underlying recorder has been built."""
        return self._recorder is not None

    @property
    def is_recording(self) -> bool:
        return self._recorder is not None and self._recorder.is_recording

    @property
    def episode_count(self) -> int:
        return self._recorder.episode_count if self._recorder is not None else 0

    def observe(self, obs: Observation) -> None:
        """Append an observation to the ring. Stamp arrival on this
        machine's clock; this fires on receipt, so ``time.time()`` here is
        the obs's local receive time."""
        self._history.append((obs, int(time.time() * 1_000_000)))

    def ensure_recorder(self, frame) -> bool:
        """Lazy-build the underlying recorder from the resolution of a
        received frame. The wire schema doesn't pin width/height; the
        follower's camera config does. Returns True if it built on this
        call, False if it was already built."""
        if self._recorder is not None:
            return False
        self._recorder = build_from_frame(
            repo_id=self._repo_id,
            fps=self._fps,
            state_field_names=self._state_field_names,
            action_field_names=self._action_field_names,
            cameras=self._cameras,
            frame=frame,
            task=self._task,
            root=self._root,
        )
        return True

    def record(self, action: Action) -> None:
        """Pair an executed action with the observation the operator was
        responding to and push one dataset row. One action = one row.
        No-op until the recorder exists and an episode is recording."""
        if self._recorder is None or not self._recorder.is_recording:
            return

        # Pick the reference timestamp and the obs clock to compare it against:
        #   * Policy actions carry `in_reply_to_ts_us`, a robot-clock capture
        #     time naming the exact obs the policy consumed. Match on the obs
        #     capture timestamp (same clock) — an exact, skew-free pairing.
        #   * Leader actions are open-loop: the human reacts to whatever obs
        #     they were *looking at*, i.e. the most recently *received* one.
        #     Match the action's teleop send time against each obs's local
        #     receive time (both teleop clock). This avoids the robot/teleop
        #     clock skew and the baseline transport latency that comparing a
        #     teleop send time against a robot capture time would fold in, so
        #     the age below measures how stale the operator's view was — not
        #     the link RTT.
        match_on_recv = action.in_reply_to_ts_us is None
        target_ts = action.timestamp_us if match_on_recv else action.in_reply_to_ts_us

        aligned: Observation | None = None
        age = 0
        for obs, recv_us in reversed(self._history):
            obs_ts = recv_us if match_on_recv else obs.timestamp_us
            if obs_ts <= target_ts:
                aligned = obs
                age = target_ts - obs_ts
                break
        if aligned is None:
            return  # episode start, no obs old enough yet

        if age > self._max_obs_age_us:
            self.skipped_frames += 1
            return  # observation stream stalled; would mislabel the row

        self._recorder.push_frame(
            self._obs_to_record(aligned),
            {k: float(v) for k, v in action.values.items()},
        )
        for err in self._recorder.poll_errors():
            print(f"[teleop] recorder error: {err}")

    def start_episode(self) -> None:
        """Begin recording. Resets the obs ring first so the first row of
        the new episode pairs against an obs from within the episode, not
        from before."""
        self._history.clear()
        self.skipped_frames = 0
        if self._recorder is not None:
            self._recorder.start_episode()

    def end_episode(self) -> None:
        if self._recorder is not None:
            self._recorder.end_episode()

    def discard_episode(self) -> None:
        if self._recorder is not None:
            self._recorder.discard_episode()

    def finalize(self) -> None:
        if self._recorder is not None:
            self._recorder.finalize()

    def drain_skips(self) -> int:
        """Return the skip count accumulated since the last drain, and
        reset it."""
        n = self.skipped_frames
        self.skipped_frames = 0
        return n

    def _obs_to_record(self, obs: Observation) -> dict:
        """Reconstitute the recorder's flat observation dict from a synced
        ``Observation``: state scalars + decoded RGB ndarrays per camera."""
        out: dict = dict(obs.state)
        for cam in self._cameras:
            f = obs.frames.get(cam)
            if f is not None:
                out[cam] = frame_bytes_to_numpy_rgb(f.data, f.width, f.height)
        return out
