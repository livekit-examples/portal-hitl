"""Operator-side test: the parts the wire tests cannot reach.

    uv run python -m test.test_teleop      (no LiveKit server, no hardware)

`test_recorder` and `test_degraded` drive the operator through `FakeOperator`,
which stands in for the tick loop. What they do not exercise is the logic
around it: which frame the anchor names, what a digit key does to the task, and
whether the keyboard taps deliver at all. Those are pure functions and one pty
away from testable, so there is no excuse for leaving them to the rig.
"""
from __future__ import annotations

import os
import pty
import sys
import termios
import time
from dataclasses import dataclass

import numpy as np

from portal_rebot.teleoperator.__main__ import Tally, View, select_task
from portal_rebot.teleoperator.preview import Preview
from test._harness import Checks


@dataclass
class Frame:
    timestamp_us: int


class RecordingPreview:
    """Captures what the window was told to show."""

    def __init__(self):
        self.shown: list[tuple[list[str], int]] = []

    def show_task(self, tasks, active):
        self.shown.append((list(tasks), active))


def check_view(c: Checks) -> None:
    view = View(["arm_camera", "overhead_camera"])
    c.check(view.anchor() is None, "no anchor before any frame")
    view.saw("arm_camera", Frame(2_000))
    c.check(view.anchor() is None, "no anchor while one camera is still blank")
    view.saw("overhead_camera", Frame(1_000))
    c.check(view.anchor() == 1_000,
            "the anchor is the OLDEST of the newest per camera -- the instant "
            "everything on screen was current", str(view.anchor()))
    view.saw("overhead_camera", Frame(3_000))
    c.check(view.anchor() == 2_000, "and advances as the laggard catches up",
            str(view.anchor()))
    c.check("fps in" in view.report(), "the 5s line renders", view.report())


def check_task_switch(c: Checks) -> None:
    tasks = ["Pick up the red cube", "Stack it on the blue", "Push the tray left"]
    preview = RecordingPreview()
    tally = Tally(tasks)

    active, task = select_task(tasks, 0, "2", False, preview, tally)
    c.check((active, task) == (1, tasks[1]), "a digit selects that task", f"{active} {task!r}")
    c.check(preview.shown == [(tasks, 1)], "and the window is told, not just the terminal")

    active, task = select_task(tasks, 1, "2", False, preview, tally)
    c.check((active, task) == (1, tasks[1]) and len(preview.shown) == 1,
            "re-selecting the active task is a no-op")

    active, task = select_task(tasks, 1, "7", False, preview, tally)
    c.check((active, task) == (1, tasks[1]), "a digit past the end of the list changes nothing")

    active, task = select_task(tasks, 0, "3", True, preview, tally)
    c.check((active, task) == (0, tasks[0]),
            "switching is refused mid-take, so no span is labelled two things")


def check_tally(c: Checks) -> None:
    tally = Tally(["Pick", "Stack"])
    c.check(tally.line(0) == "0 episodes of 'Pick'", "an empty tally reads naturally",
            tally.line(0))
    tally.record(0)
    c.check(tally.line(0) == "1 episode of 'Pick'", "and singular at one", tally.line(0))
    tally.record(0)
    tally.record(1)
    c.check((tally.count(0), tally.count(1)) == (2, 1), "counts are per task",
            f"{tally.count(0)} / {tally.count(1)}")
    c.check(len(tally.summary()) == 2, "the shutdown summary covers every task")


def check_hotkeys(c: Checks) -> None:
    """Over a pty, because the terminal tap is the one that works on Wayland
    and over SSH -- the two places this rig actually runs."""
    primary, secondary = pty.openpty()
    saved, sys.stdin = sys.stdin, os.fdopen(secondary, "r")
    before = termios.tcgetattr(secondary)
    try:
        from portal_rebot.teleoperator.hotkeys import Hotkeys

        # Terminal tap only: the global tap reads the whole machine, so a key
        # typed in another window would arrive here as a real event.
        keys = Hotkeys(global_tap=False)
        keys.start()
        c.check(keys.terminal_active, "the terminal tap attaches to a tty")

        # All at once: a buffered read would hand back one and strand the rest
        # until the next keystroke.
        os.write(primary, b"r3[x")
        time.sleep(0.5)
        got = keys.drain()
        c.check(got == ["r", "3", "[", "x"], "every key arrives, in order, in one drain",
                str(got))

        os.write(primary, b"qz0")
        time.sleep(0.3)
        c.check(keys.drain() == [], "keys outside the set are ignored, 0 included")

        os.write(primary, b"22")
        time.sleep(0.4)
        c.check(keys.drain() == ["2", "2"], "a real double-press from one tap survives")

        keys._deliver("2", "terminal")
        keys._deliver("2", "global")
        c.check(keys.drain() == ["2"], "the same key from both taps counts once")

        keys.stop()
        time.sleep(0.2)
        c.check(termios.tcgetattr(secondary) == before,
                "the terminal is restored on stop -- cbreak left behind is a dead shell")
    finally:
        sys.stdin = saved
        os.close(primary)


def answer(script: str) -> list[str]:
    """Run `prompt_tasks` against a pty preloaded with `script`.

    A real terminal, not a pipe: the prompt takes the default without asking
    when stdin is not a tty, which is the branch that must NOT run here.
    """
    from portal_rebot.teleoperator.session import prompt_tasks

    primary, secondary = pty.openpty()
    saved, sys.stdin = sys.stdin, os.fdopen(secondary, "r")
    try:
        os.write(primary, script.encode())
        return prompt_tasks()
    finally:
        sys.stdin = saved
        os.close(primary)


def check_session(c: Checks) -> None:
    from portal_rebot.teleoperator.session import DEFAULT_TASK, MAX_TASKS

    for key in ("PORTAL_DATA_TASKS", "PORTAL_DATA_TASK"):
        os.environ.pop(key, None)

    c.check(answer("\n\n") == [DEFAULT_TASK], "ENTER twice takes the default, one task")
    c.check(answer("1\nPick up the cube\n") == ["Pick up the cube"], "one task, named")
    c.check(answer("3\nPick\nStack\nPush\n") == ["Pick", "Stack", "Push"],
            "three tasks come back in hotkey order")
    c.check(answer("12\nPick\n" + "x\n" * MAX_TASKS)[:1] == ["Pick"]
            and len(answer("12\nPick\n" + "x\n" * MAX_TASKS)) == MAX_TASKS,
            f"a count above the ceiling clamps to {MAX_TASKS}, not a crash")
    c.check(answer("banana\nPick\n") == ["Pick"], "a non-numeric count falls back to one")
    # A blank second task would silently duplicate the first and mislabel every
    # span collected under it, so it is re-asked rather than defaulted.
    c.check(answer("2\nPick\n\n\nStack\n") == ["Pick", "Stack"],
            "a blank task is re-asked, never accepted")

    os.environ["PORTAL_DATA_TASKS"] = "Pick it up; Put it down ;; Push it, gently"
    c.check(answer("") == ["Pick it up", "Put it down", "Push it, gently"],
            "PORTAL_DATA_TASKS skips the questions entirely, and splits on ';' "
            "so a task may contain a comma")
    del os.environ["PORTAL_DATA_TASKS"]


def check_preview(c: Checks) -> None:
    preview = Preview("selftest", ["arm_camera"])
    preview.show("arm_camera", np.zeros((48, 64, 3), np.uint8))
    preview.show_task(["only one"], 0)
    preview.show_task(["a", "b"], 1)
    preview.close()
    c.check(True, "Preview survives a missing viewer binary and stays usable")


def main() -> int:
    c = Checks("teleoperator")
    check_view(c)
    check_task_switch(c)
    check_session(c)
    check_tally(c)
    check_hotkeys(c)
    check_preview(c)
    return c.exit_code


if __name__ == "__main__":
    sys.exit(main())
