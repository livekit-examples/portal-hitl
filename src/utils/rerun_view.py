"""The operator's window: the newest frame from each camera, and nothing else.

`static=True` is the whole design. Static data in rerun is timeless -- one row
per entity, latest wins -- so this is a live mirror at constant memory, with no
history accumulating behind a session that runs for hours. Everything worth
scrubbing later is in the recorder's `.rrd`. Frames go in uncompressed: the
viewer is a local process, and a JPEG encode here would cost the tick loop more
than it saves.
"""
from __future__ import annotations

import logging
from typing import Optional, Sequence

import numpy as np
import rerun as rr
import rerun.blueprint as rrb

logger = logging.getLogger(__name__)


class Preview:
    def __init__(self, room: str, cameras: Sequence[str]) -> None:
        self._stream: Optional[rr.RecordingStream] = rr.RecordingStream(f"portal-rebot-{room}")
        try:
            self._stream.spawn()
            self._stream.send_blueprint(rrb.Blueprint(
                rrb.Vertical(
                    rrb.Horizontal(*(
                        rrb.Spatial2DView(origin=f"/{cam}", name=cam) for cam in cameras)),
                    rrb.TextDocumentView(origin="/task", name="Task"),
                    row_shares=[5, 1],
                ),
                collapse_panels=True,
            ))
        except Exception as exc:
            # No viewer binary on PATH, no display, spawn refused -- none of
            # which is a reason to take the rig down.
            logger.warning("rerun viewer unavailable (%s); continuing without a window", exc)
            self._stream = None

    def show(self, camera: str, image: np.ndarray) -> None:
        if self._stream is None:
            return
        try:
            self._stream.log(camera, rr.Image(image), static=True)
        except Exception:
            logger.debug("rerun log failed", exc_info=True)

    def show_task(self, tasks: Sequence[str], active: int) -> None:
        """The active task, in the window the operator is actually looking at: a
        hotkey whose only confirmation is a line in a terminal behind the window
        is a hotkey you cannot tell landed."""
        if self._stream is None:
            return
        if len(tasks) == 1:
            text = f"# {tasks[0]}"
        else:
            text = "\n".join(
                [f"# {active + 1} · {tasks[active]}", ""]
                + [f"- {'**' if i == active else ''}`{i + 1}` {task}"
                   f"{'**' if i == active else ''}" for i, task in enumerate(tasks)])
        try:
            self._stream.log("task", rr.TextDocument(text, media_type=rr.MediaType.MARKDOWN),
                             static=True)
        except Exception:
            logger.debug("rerun task log failed", exc_info=True)

    def close(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.flush(timeout_sec=5.0)
        except Exception:
            logger.debug("rerun flush failed", exc_info=True)
