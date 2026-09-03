"""Keyboard input for the operator loop, off the tick's back.

Two taps, whichever delivers: pynput taps the windowing system, so a key lands
while the operator is looking at the rerun window -- and every platform can
starve that tap silently (see `input_backend_warning`). A terminal read has the
opposite profile: it works under Wayland and over SSH where pynput cannot, but
only while the terminal has focus. `drain()` merges both and the tick loop
never blocks. See docs/architecture.md#keys.
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

# `1`-`9` select the session's task; see teleoperator/session.py for why nine.
KEYS = {"r", "[", "x"} | {str(digit) for digit in range(1, 10)}

# One keystroke can reach both taps at once -- X11 with the terminal focused
# sees it on XRecord and on stdin -- and delivered twice, `r` would toggle
# recording straight back off. A repeat of the same key from the OTHER tap
# inside this window is that echo; the same tap twice is a real double-tap.
_ECHO_WINDOW_S = 0.25


def input_backend_warning() -> Optional[str]:
    """Why the *global* tap will never deliver a key, when that is knowable up
    front. The terminal tap is unaffected by all of these, so a warning here
    means "focus the terminal", not "the hotkeys are dead".

    Every platform starves the tap in its own way *without raising* -- the
    thread starts, stays alive and receives nothing -- which is why this is
    checked rather than waited on.
    """
    if sys.platform == "darwin":
        # The tap is a CGEventTap, which needs the terminal binary listed under
        # Privacy & Security > Accessibility. pynput logs one warning from its
        # own thread and carries on.
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
        # pynput's only Linux backend is XRecord on $DISPLAY. Under Wayland that
        # display is XWayland, which never sees keys typed into Wayland-native
        # windows, so the tap is live and permanently empty.
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
    """Reads single keystrokes from the controlling terminal.

    `cbreak` rather than raw: keys arrive without ENTER but ISIG stays on, so
    ctrl-c still reaches the process. `select` with a timeout rather than a
    blocking read, which on a daemon thread could not be woken at shutdown --
    the terminal restore would race the interpreter tearing down. And `os.read`
    on the raw descriptor, never `sys.stdin.read(1)`: `sys.stdin` is buffered,
    so one `read(1)` pulls up to 8 KB off the descriptor and keeps the rest
    where `select` cannot see it, leaving typed keys stuck until the next
    keystroke shakes one loose.
    """

    def __init__(self, deliver) -> None:
        self._deliver = deliver
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._fd: Optional[int] = None
        self._saved = None

    def start(self) -> bool:
        """False when there is no terminal to read -- a pipe, a service unit,
        a detached process. The global tap is then the only one."""
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
    """`global_tap=False` leaves the terminal as the only source: the global tap
    reads the whole machine, keys meant for other windows included. That is the
    answer for a shared desktop, and what makes a test of the terminal tap
    deterministic."""

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
