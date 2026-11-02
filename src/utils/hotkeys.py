"""Keyboard input for the operator loop, read on background threads.

Two sources, merged by `drain()`: a global tap (pynput) that hears keys while
the rerun window has focus, and the terminal, which also works under Wayland
and over SSH. See tutorial/03-operator-loop.md.
"""
from __future__ import annotations

import os
import select
import sys
import termios
import threading
import time
import tty
from typing import Optional

from pynput import keyboard

# `1`-`9` select the session's task; see src/utils/session.py for why nine.
KEYS = {"c", "r", "[", "x"} | {str(digit) for digit in range(1, 10)}

# One keystroke can reach both sources. The same key from the OTHER source
# within this window is that echo, not a second press.
_ECHO_WINDOW_S = 0.25


def input_backend_warning() -> Optional[str]:
    """Why the global tap will silently receive nothing, when that is knowable up front.

    The terminal still works in every case; the warning means "focus the terminal".
    """
    if sys.platform == "darwin":
        try:
            from ApplicationServices import AXIsProcessTrusted
        except ImportError:
            return None
        if not AXIsProcessTrusted():
            return ("macOS has not granted this terminal Accessibility access. "
                    "Add it under System Settings > Privacy & Security > "
                    "Accessibility, then relaunch the terminal")
        return None

    if sys.platform.startswith("linux"):
        if os.environ.get("WAYLAND_DISPLAY") or (
                os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"):
            return ("this is a Wayland session and pynput can only tap X11 "
                    "(XRecord via XWayland), so keys typed into a Wayland-native "
                    "window never arrive")
        if os.environ.get("SSH_CONNECTION") and os.environ.get("DISPLAY"):
            return ("$DISPLAY is forwarded over SSH; forwarding often refuses "
                    "XRecord, and when it does not the keys it taps are the ones "
                    "on the machine you sshed *from*")
        if not os.environ.get("DISPLAY"):
            return "no $DISPLAY, so there is no X server for pynput to tap"
        return None

    return None


class _TerminalTap:
    """Reads single keystrokes from the terminal.

    `cbreak` keeps ctrl-c working. `select` with a timeout lets the thread stop.
    `os.read` on the raw descriptor, because buffered `sys.stdin` would hide
    typed keys from `select`.
    """

    def __init__(self, deliver) -> None:
        self._deliver = deliver
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._fd: Optional[int] = None
        self._saved = None

    def start(self) -> bool:
        """False when stdin is not a terminal."""
        try:
            if not sys.stdin.isatty():
                return False
            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        except Exception:
            return False
        self._thread = threading.Thread(target=self._run, name="hotkeys-tty", daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                if not select.select([self._fd], [], [], 0.2)[0]:
                    continue
                if not (chunk := os.read(self._fd, 64)):
                    return  # EOF: the terminal went away
                for char in chunk.decode("utf-8", "ignore"):
                    self._deliver(char, "terminal")
        except Exception:
            pass  # stdin closed under us; the global tap carries on

    def stop(self) -> None:
        self._stop.set()
        if self._fd is not None and self._saved is not None:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            except Exception:
                pass


class Hotkeys:
    """Merged key presses from both sources.

    `global_tap=False` reads the terminal only; the global tap hears keys typed into any window."""

    def __init__(self, global_tap: bool = True) -> None:
        self._lock = threading.Lock()
        self._pending: list[str] = []
        self._last: tuple[str, str, float] = ("", "", 0.0)
        self._listener = keyboard.Listener(on_press=self._on_press) if global_tap else None
        self._terminal = _TerminalTap(self._deliver)
        self.terminal_active = False

    def start(self) -> None:
        if self._listener is not None:
            self._listener.start()
        self.terminal_active = self._terminal.start()

    def stop(self) -> None:
        self._terminal.stop()
        if self._listener is not None:
            self._listener.stop()

    def drain(self) -> list[str]:
        with self._lock:
            pressed, self._pending = self._pending, []
        return pressed

    def _deliver(self, char: str, source: str) -> None:
        if char not in KEYS:
            return
        now = time.monotonic()
        with self._lock:
            last_char, last_source, last_at = self._last
            if char == last_char and source != last_source and now - last_at < _ECHO_WINDOW_S:
                return
            self._last = (char, source, now)
            self._pending.append(char)

    def _on_press(self, key) -> None:
        if (char := getattr(key, "char", None)) is not None:
            self._deliver(char, "global")
