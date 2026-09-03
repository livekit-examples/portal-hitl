"""Reading a session `.rrd` back out.

Rerun dropped the lightweight `rerun.dataframe` reader in 0.34, so the way in
is a local `rr.server.Server` over the file and one arrow table per index.
Both are needed: `robot_time` holds the observations -- state scalars, camera
frames, and the keypoints marking where the demonstrations are -- and
`action_time` holds the actions, one row each, none lost. Reading actions off
`robot_time` instead would silently drop every action that shared an anchor
with a later one, 25-28% of them on a link with any jitter. Each `action_time`
row still carries the `robot_time` it answered.

See docs/architecture.md#converting.
"""
from __future__ import annotations

import bisect
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

import numpy as np

from portal_rebot.keypoints import RECORDING

logger = logging.getLogger(__name__)

Series = list[tuple[int, Any]]


@dataclass
class Episode:
    """One demonstration: a span of `robot_time`, and what it was called."""

    task: str
    start_us: int
    end_us: int
    discarded: bool = False

    def contains(self, ts_us: int) -> bool:
        return self.start_us <= ts_us <= self.end_us


@dataclass
class Session:
    """One `.rrd`, parsed into everything a dataset needs."""

    path: Path
    state_fields: list[str]
    action_fields: list[str]
    cameras: list[str]
    episodes: list[Episode]
    unclosed: int = 0
    # (robot_time, value), sorted, for the as-of lookup.
    _states: Series = field(default_factory=list, repr=False)
    _frames: dict[str, Series] = field(default_factory=dict, repr=False)
    _actions: Series = field(default_factory=list, repr=False)

    def rows(self, episode: Episode, max_stale_us: int) -> Iterator[tuple]:
        """`(state, frames, action)` for every action inside `episode`.

        Observations are taken **as of** the action's anchor -- the newest state
        and frame at or before it -- rather than requiring an exact hit. A tick
        whose state was dropped in transit still has the frame the operator was
        looking at, and holding the previous sample across that gap is the same
        trade the rig makes live: a hole in a trajectory is a jump the arm never
        made. `max_stale_us` is where that stops being true.
        """
        for ts_us, action in self._actions:
            if not episode.contains(ts_us):
                continue
            state = _as_of(self._states, ts_us, max_stale_us)
            if state is None:
                continue
            frames = {camera: _as_of(self._frames.get(camera, []), ts_us, max_stale_us)
                      for camera in self.cameras}
            if any(blob is None for blob in frames.values()):
                continue
            yield state, frames, action


def _as_of(series: Series, ts_us: int, max_stale_us: int) -> Optional[Any]:
    """The newest value at or before `ts_us`, unless it is older than the bound."""
    index = bisect.bisect_right(series, ts_us, key=lambda pair: pair[0]) - 1
    if index < 0 or ts_us - series[index][0] > max_stale_us:
        return None
    return series[index][1]


def read_session(path: Path, state_order: Sequence[str] = (),
                 action_order: Sequence[str] = ()) -> Session:
    """Parse one `.rrd`.

    Field ORDER comes from the archive itself, which records it as it writes.
    `*_order` is only a fallback for archives written before that, and if
    neither is available the vectors cannot be built -- see `_ordered`.
    """
    import rerun as rr

    with rr.server.Server(datasets={"session": [str(path)]}) as server:
        dataset = server.client().get_dataset("session")
        by_robot = dataset.reader(index="robot_time").to_arrow_table()
        by_action = dataset.reader(index="action_time").to_arrow_table()
        stored = _stored_schema(dataset.reader(index=None).to_arrow_table())

    state_fields = _ordered(_entities(by_robot, "/observation/", ":Scalars:"),
                            stored.get("state") or state_order, path, "state")
    action_fields = _ordered(_entities(by_action, "/action/", ":Scalars:"),
                             stored.get("action") or action_order, path, "action")
    cameras = sorted(_entities(by_robot, "/observation/", ":EncodedImage:blob"))

    robot_us = _index_us(by_robot, "robot_time")
    session = Session(path=path, state_fields=state_fields, action_fields=action_fields,
                      cameras=cameras, episodes=[])
    session._states = _scalar_series(by_robot, robot_us, "/observation/", state_fields)
    for camera in cameras:
        session._frames[camera] = _frame_series(by_robot, robot_us, camera)
    session._actions = _scalar_series(
        by_action, _index_us(by_action, "robot_time"), "/action/", action_fields)
    session.episodes, session.unclosed = _episodes(by_robot, robot_us)
    return session


def _entities(table, prefix: str, marker: str) -> set[str]:
    return {name[len(prefix):name.index(marker)]
            for name in table.column_names
            if name.startswith(prefix) and marker in name}


def _stored_schema(table) -> dict[str, list[str]]:
    """The field order the archive recorded about itself, if it did."""
    column = _column(table, "/schema:TextDocument:text")
    if column is None:
        return {}
    for value in table.column(column).to_pylist():
        text = value[0] if isinstance(value, list) and value else value
        if not text:
            continue
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            logger.warning("archive schema is not JSON: %r", text)
    return {}


def _ordered(found: set[str], preferred: Sequence[str], path: Path, kind: str) -> list[str]:
    """The fields in the order they go into a vector.

    **Never guessed.** Sorting the names alphabetically would build a dataset
    that looks entirely normal with its joints permuted, so an order that
    cannot be established is an error rather than a default.
    """
    ordered = [name for name in preferred if name in found]
    if extra := sorted(found - set(ordered)):
        if not ordered:
            raise ValueError(
                f"{path.name} does not record its {kind} field order, and none was "
                f"supplied. Building a vector would mean guessing at the order of "
                f"{extra}, which yields a dataset with permuted columns that looks "
                f"correct. Convert it from a checkout whose portal.yaml matches "
                f"the archive.")
        logger.warning("%s: %s fields %s are not in the known order; appending them",
                       path.name, kind, extra)
    return ordered + extra


def _index_us(table, name: str) -> list[Optional[int]]:
    """A timeline column as integer microseconds. Rerun stores nanoseconds."""
    import pyarrow as pa

    if name not in table.column_names:
        return [None] * table.num_rows
    return [None if value is None else value // 1000
            for value in table.column(name).cast(pa.int64()).to_pylist()]


def _scalar(table, column: str) -> list:
    values = table.column(column).to_pylist()
    return [v[0] if isinstance(v, list) and v else None for v in values]


def _column(table, prefix: str) -> Optional[str]:
    hits = [name for name in table.column_names if name.startswith(prefix)]
    return hits[0] if hits else None


def _scalar_series(table, times, prefix: str, fields: Sequence[str]) -> Series:
    """Every row where all of `fields` have a value -- a partial state is not a
    state -- as (timestamp, values), sorted."""
    columns = {}
    for name in fields:
        if (column := _column(table, f"{prefix}{name}:Scalars:")) is not None:
            columns[name] = _scalar(table, column)
    series: Series = []
    for row, ts_us in enumerate(times):
        values = {name: column[row] for name, column in columns.items()}
        if ts_us is None or any(value is None for value in values.values()):
            continue
        series.append((ts_us, {name: float(value) for name, value in values.items()}))
    series.sort(key=lambda pair: pair[0])
    return series


def _frame_series(table, times, camera: str) -> Series:
    column = _column(table, f"/observation/{camera}:EncodedImage:blob")
    if column is None:
        return []
    series: Series = []
    for ts_us, blob in zip(times, table.column(column).to_pylist()):
        if ts_us is None or blob is None:
            continue
        series.append((ts_us, bytes(blob[0]) if isinstance(blob, list) else bytes(blob)))
    series.sort(key=lambda pair: pair[0])
    return series


def _episodes(table, times) -> tuple[list[Episode], int]:
    """Spans from the keypoint log: `recording` opens one, anything else closes it.

    The band would do as well for the spans, but the keypoints carry the task
    and the `discard` flag on the closing mark, and a text log keeps every mark
    rather than only the transitions.
    """
    column = _column(table, "/keypoints:TextLog:text")
    if column is None:
        return [], 0

    episodes: list[Episode] = []
    open_at: Optional[tuple[int, str]] = None
    for ts_us, raw in zip(times, table.column(column).to_pylist()):
        if ts_us is None or raw is None:
            continue
        text = raw[0] if isinstance(raw, list) else raw
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            logger.warning("keypoint at %s is not JSON: %r", ts_us, text)
            continue
        if payload.get("state") == RECORDING:
            if open_at is not None:
                logger.warning("a second 'recording' mark at %s; the first span is dropped", ts_us)
            open_at = (ts_us, str(payload.get("task", "")))
            continue
        if open_at is None:
            continue
        start_us, task = open_at
        episodes.append(Episode(task=task or str(payload.get("task", "")),
                                start_us=start_us, end_us=ts_us,
                                discarded=bool(payload.get("discard"))))
        open_at = None
    return episodes, int(open_at is not None)


def decode(blob: bytes) -> np.ndarray:
    """One archived frame as `(H, W, 3)` uint8 RGB, the layout lerobot wants.

    OpenCV decodes to BGR whatever the source, so the swap is not optional.
    """
    import cv2

    image = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("could not decode an archived frame")
    return cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
