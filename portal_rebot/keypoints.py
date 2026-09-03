"""The `add_keypoint` contract: the one message the two operators exchange.

    {"state":    "recording" | "idle",
     "task":     "<natural language>",
     "at_ts_us": <robot capture timestamp this mark belongs at>,
     ...}

`at_ts_us` is the anchor -- the capture timestamp of the newest frame the
operator had on screen -- so a boundary lands where the operator saw it rather
than where the RPC arrived. Omitted, the recorder falls back to the newest
timestamp it has seen. `state` drives the recording band and `task` the task
band; every other key is written to the archive verbatim, which is how `by`
gets there. See docs/architecture.md#keypoints.
"""
from __future__ import annotations

METHOD = "add_keypoint"

IDLE = "idle"
RECORDING = "recording"
