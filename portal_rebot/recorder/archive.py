"""The `.rrd` this process exists to write.

Everything lands on `robot_time`, the robot's own capture clock:

| Entity | `robot_time` | `action_time` |
|---|---|---|
| `observation/<field>` | `state.timestamp_us` | — |
| `observation/<camera>` | `frame.timestamp_us` | — |
| `action/<field>` | `action.in_reply_to_ts_us` | `action.timestamp_us` |
| `latency/see_to_act_ms` | the same | the same |
| `state/recording`, `state/task` | the keypoint's anchor | — |
| `keypoints` | the keypoint's anchor | — |

Filing an action at `in_reply_to_ts_us` is what co-locates it with the frame
and state it answered. `action_time` is the second index it also needs: a rerun
index keeps ONE value per entity, so two actions sharing an anchor -- 25-28% of
them on a link with any jitter -- would lose the first outright. Both indices
are on the robot's clock, one measured and one the operator's estimate of it,
which is why they stay separate. See docs/architecture.md#two-timelines-and-why-it-is-not-one.

Images go through a writer thread: a JPEG encode costs a couple of milliseconds
and this runs inside a Portal receive callback. The queue is bounded because
each entry pins one undecoded frame.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import logging
import pathlib
import queue
import threading
from typing import Any, Optional, Sequence

import rerun as rr
import rerun.blueprint as rrb
from livekit.portal import Action, State, VideoFrameData, frame_bytes_to_numpy_rgb

from portal_rebot.keypoints import IDLE, RECORDING

logger = logging.getLogger(__name__)
_GREY, _GREEN = (0x8A, 0x8F, 0x98), (0x2E, 0xA0, 0x43)

_QUEUE_DEPTH = 60


def blueprint(cameras: Sequence[str]) -> rrb.Blueprint:
    return rrb.Blueprint(
        rrb.Vertical(
            rrb.Horizontal(*(
                rrb.Spatial2DView(origin=f"/observation/{cam}", name=cam)
                for cam in cameras)),
            rrb.Horizontal(
                rrb.TimeSeriesView(origin="/observation", name="Observation (state)"),
                rrb.TimeSeriesView(origin="/action", name="Action (leader)"),
            ),
            rrb.Horizontal(
                rrb.TimeSeriesView(origin="/metrics", name="Portal metrics"),
                rrb.TimeSeriesView(origin="/latency", name="See-to-act"),
                rrb.StateTimelineView(origin="/state", name="Session state"),
                rrb.TextLogView(origin="/keypoints", name="Keypoints"),
            ),
            row_shares=[2, 1, 1],
        ),
        collapse_panels=True,
    )


class Archive:
    """Writes one `.rrd` per session. No viewer is spawned: this process has no
    interface, and a window here would only duplicate the operator's."""

    def __init__(self, room: str, cameras: Sequence[str], *,
                 rrd_dir: str | pathlib.Path, jpeg_quality: int = 95) -> None:
        self._cameras = tuple(cameras)
        self._quality = jpeg_quality

        directory = pathlib.Path(rrd_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        self.rrd_path = directory / f"{room}-{stamp}.rrd"

        self._stream = rr.RecordingStream(f"portal-rebot-{room}")
        self._stream.save(str(self.rrd_path))
        self._stream.send_blueprint(blueprint(self._cameras))
        # Static: the lane describes the recording, not an instant. The bands
        # logged onto it stay timeline-indexed.
        self._stream.log("state/recording",
                         rr.StateConfiguration(values=[IDLE, RECORDING],
                                               colors=[_GREY, _GREEN]),
                         static=True)

        # Newest robot capture timestamp seen on any stream. A keypoint that
        # arrives without its own anchor is filed here -- the operator pressed
        # the key while looking at this instant.
        self._latest_robot_ts_us: Optional[int] = None
        self._states = 0
        self._frames = 0
        self._actions = 0
        self._unanchored_actions = 0
        self._dropped_frames = 0
        self._keypoints = 0
        self._last_lag_us: Optional[int] = None
        self._impossible_lag = 0

        self._schema: dict[str, list[str]] = {}
        self._bands: dict[str, str] = {}
        self._errors: list[str] = []
        self._errors_lock = threading.Lock()

        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_DEPTH)
        self._writer = threading.Thread(
            target=self._write_frames, name="rrd-writer", daemon=True)
        self._writer.start()

    @property
    def counts(self) -> dict[str, int]:
        return {"states": self._states, "frames": self._frames,
                "actions": self._actions, "keypoints": self._keypoints,
                "unanchored": self._unanchored_actions,
                "dropped_frames": self._dropped_frames,
                "impossible_lag": self._impossible_lag}

    @property
    def see_to_act_ms(self) -> Optional[float]:
        """Newest gap between a frame's capture and the action taken in
        response to it: the downlink plus the human. Both stamps are on the
        robot's clock, so it must be positive -- see `counts["impossible_lag"]`."""
        return None if self._last_lag_us is None else self._last_lag_us / 1e3

    def log_state(self, state: State) -> None:
        self._saw_robot_time(state.timestamp_us)
        self._note_schema("state", state.values)
        self._states += 1
        with self._guard("log_state"):
            self._at(state.timestamp_us)
            for name, value in state.values.items():
                self._stream.log(f"observation/{name}", rr.Scalars(float(value)))

    def log_frame(self, name: str, frame: VideoFrameData) -> None:
        self._saw_robot_time(frame.timestamp_us)
        self._frames += 1
        try:
            self._queue.put_nowait((name, frame))
        except queue.Full:
            self._dropped_frames += 1

    def log_action(self, action: Action) -> None:
        self._actions += 1
        if (anchor := action.in_reply_to_ts_us) is None:
            self._unanchored_actions += 1
            return
        self._note_schema("action", action.values)
        self._last_lag_us = action.timestamp_us - anchor
        # An action cannot precede the frame it answered, so a negative gap is
        # not a latency reading: it is proof the operator's clock estimate is
        # wrong. The only direct check on it.
        if self._last_lag_us < 0:
            self._impossible_lag += 1
        with self._guard("log_action"):
            self._at(anchor, action.timestamp_us)
            for name, value in action.values.items():
                self._stream.log(f"action/{name}", rr.Scalars(float(value)))
            self._stream.log("latency/see_to_act_ms",
                             rr.Scalars(self._last_lag_us / 1e3))

    def keypoint(self, payload: dict[str, Any]) -> int:
        """Mark one instant in the recording. Returns the keypoint's index.

        Raises `ValueError` when nothing has arrived from the robot yet: there
        is no timeline to put the mark on, and the caller should hear that
        rather than have it filed at an invented time.
        """
        anchor = payload.get("at_ts_us") or self._latest_robot_ts_us
        if anchor is None:
            raise ValueError("nothing received from the robot yet; no timeline to mark")

        self._keypoints += 1
        with self._guard("keypoint"):
            self._at(int(anchor))
            self._stream.log("keypoints", rr.TextLog(json.dumps(payload, sort_keys=True)))
            if (state := payload.get("state")) is not None:
                self._log_band("state/recording",
                               RECORDING if state == RECORDING else IDLE)
            if (task := payload.get("task")) is not None:
                self._log_band("state/task", str(task))
        return self._keypoints

    def log_metrics(self, metrics) -> None:
        """Optional fields are skipped while None during warm-up, so the series
        carry no phantom zeros."""
        if self._latest_robot_ts_us is None:
            return
        with self._guard("log_metrics"):
            self._at(self._latest_robot_ts_us)
            values = {
                "metrics/rtt_last_ms": _ms(metrics.rtt.rtt_us_last),
                "metrics/state_jitter_ms": _ms(metrics.transport.state_jitter_us),
                "metrics/action_jitter_ms": _ms(metrics.transport.action_jitter_us),
                "metrics/states_dropped": float(metrics.sync.states_dropped),
            }
            for path, value in values.items():
                if value is not None:
                    self._stream.log(path, rr.Scalars(value))

    def poll_errors(self) -> list[str]:
        with self._errors_lock:
            errors, self._errors = self._errors, []
        return errors

    def close(self) -> None:
        """Drain the frame writer, then flush. The `.rrd` is only complete
        after this."""
        self._queue.put(None)
        self._writer.join(timeout=30)
        if self._writer.is_alive():
            logger.warning("frame writer did not drain within 30s")
        try:
            self._stream.flush(timeout_sec=10.0)
        except Exception:
            logger.warning("rerun flush failed; the .rrd may be truncated", exc_info=True)

    def _note_schema(self, kind: str, values: dict) -> None:
        """Record what ORDER the fields came in, once.

        Rerun entities are independent, so without this nothing rebuilding a
        state vector from the archive knows the order -- and an alphabetical
        guess yields a dataset that looks normal with its joints permuted.
        Portal delivers `values` in schema order (`_cast_values` iterates the
        schema), so it is free to capture here.
        """
        names = list(values)
        if self._schema.get(kind) == names:
            return
        self._schema[kind] = names
        with self._guard("schema"):
            self._stream.log("schema", rr.TextDocument(json.dumps(self._schema)),
                             static=True)

    def _saw_robot_time(self, ts_us: int) -> None:
        """Track the newest capture timestamp, and open the recording lane on
        the first one -- not in the constructor, because `StateChange` lands
        wherever the timeline sits and before the first packet `robot_time`
        does not exist yet."""
        first = self._latest_robot_ts_us is None
        self._latest_robot_ts_us = max(self._latest_robot_ts_us or 0, ts_us)
        if first:
            with self._guard("open lanes"):
                self._at(ts_us)
                self._log_band("state/recording", IDLE)

    def _at(self, ts_us: int, action_ts_us: Optional[int] = None) -> None:
        """Move the timelines for this thread.

        `reset_time` first, because rerun's current time is sticky: without it
        the next state logged here would inherit the last action's
        `action_time`. Rerun keeps this per thread, so the frame writer sets
        its own and the two never tread on each other.
        """
        self._stream.reset_time()
        self._stream.set_time("robot_time", timestamp=ts_us / 1e6)
        if action_ts_us is not None:
            self._stream.set_time("action_time", timestamp=action_ts_us / 1e6)

    def _log_band(self, path: str, value: str) -> None:
        """Log a transition, and only a transition: `StateChange` opens a new
        phase wherever it lands, so re-logging an unchanged value would cut the
        lane into one band per keypoint.

        Never pass None to end a band -- rerun 0.36's Utf8 conversion turns it
        into the literal string "None", and an empty list drops the chunk with
        only a log warning. Hence the explicit IDLE state.
        """
        if self._bands.get(path) == value:
            return
        self._bands[path] = value
        self._stream.log(path, rr.StateChange(state=value))

    def _write_frames(self) -> None:
        while (item := self._queue.get()) is not None:
            name, frame = item
            try:
                image = rr.Image(frame_bytes_to_numpy_rgb(
                    frame.data, frame.width, frame.height))
                self._at(frame.timestamp_us)
                self._stream.log(f"observation/{name}",
                                 image.compress(jpeg_quality=self._quality)
                                 if self._quality > 0 else image)
            except Exception as exc:
                self._note_error(f"frame({name})", exc)

    def _note_error(self, what: str, exc: BaseException) -> None:
        logger.exception("%s failed", what, exc_info=exc)
        with self._errors_lock:
            self._errors.append(f"{what}: {exc}")

    @contextlib.contextmanager
    def _guard(self, what: str):
        """Rerun is a sink, never the source of truth for the session: a
        logging hiccup is reported on the 5s line, not raised into the Portal
        callback."""
        try:
            yield
        except Exception as exc:
            self._note_error(what, exc)


def _ms(microseconds: Optional[int]) -> Optional[float]:
    return None if microseconds is None else microseconds / 1e3
