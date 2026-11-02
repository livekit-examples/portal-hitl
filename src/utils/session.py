"""The tasks this session collects: one, or up to nine switched with keys `1`-`9`.

Asked at startup. `PORTAL_DATA_TASKS` (semicolon-separated) skips the
questions, and a non-interactive stdin takes `PORTAL_DATA_TASK` or the default.
"""
from __future__ import annotations

import sys

from _common import env_str

DEFAULT_TASK = "reBot B601 manipulation"
MAX_TASKS = 9

_TASKS_SEPARATOR = ";"  # task descriptions are sentences, which contain commas


def prompt_tasks() -> list[str]:
    """The task list for this session, in hotkey order. Never empty."""
    if preset := env_str("PORTAL_DATA_TASKS", ""):
        tasks = [task.strip() for task in preset.split(_TASKS_SEPARATOR) if task.strip()]
        if tasks:
            return tasks[:MAX_TASKS]

    default = env_str("PORTAL_DATA_TASK", DEFAULT_TASK)
    if not sys.stdin.isatty():
        return [default]

    print("[teleop] session -- ENTER accepts the default")
    count = _ask_count()
    if count == 1:
        return [_ask("task", default)]

    tasks = [_ask("task 1", default)]
    for number in range(2, count + 1):
        # No default past the first: a blank would duplicate task 1's label.
        tasks.append(_ask_required(f"task {number}", fallback=f"{default} {number}"))
    return tasks


def _ask_count() -> int:
    answer = _ask("how many tasks", "1")
    try:
        count = int(answer)
    except ValueError:
        print(f"    '{answer}' is not a number; using 1")
        return 1
    if not 1 <= count <= MAX_TASKS:
        print(f"    {count} is outside 1-{MAX_TASKS}; using {min(max(count, 1), MAX_TASKS)}")
    return min(max(count, 1), MAX_TASKS)


def _ask(label: str, default: str) -> str:
    try:
        return input(f"  {label} [{default}]: ").strip() or default
    except EOFError:
        return default


def _ask_required(label: str, fallback: str) -> str:
    """Keep asking until something is typed; `fallback` if stdin closes."""
    while True:
        try:
            if answer := input(f"  {label}: ").strip():
                return answer
        except EOFError:
            return fallback
        print("    every recorded span is labelled with this, so it needs a description")
