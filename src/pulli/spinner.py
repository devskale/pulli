"""A minimal TTY spinner for pulli's slow phase.

pulli's slow phase is fetching remotes: network-bound, up to `timeout`
seconds per repo, and every repo is fetched in parallel. Without feedback
the user stares at a blank screen for several seconds. This module renders
a single-line spinner on stderr that updates in place, so it is obvious
that pulli is working — and roughly how far along it is.

The spinner only animates when stderr is a real terminal. When output is
piped or captured (CI, `pulli | less`, pytest) it is a no-op: no escape
codes, no extra lines, no background thread. That keeps every consumer of
pulli's stdout byte-for-byte clean.
"""
from __future__ import annotations

import sys
import threading
import time

# Braille spinner frames, in order. Same set and cadence as pi's loader.
_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_INTERVAL = 0.08
# Carriage return + erase-to-end-of-line: rewrite the line in place, and
# wipe it completely when we're done so it never trails into real output.
_CLEAR = "\r\x1b[2K"

# The palette lives in style.py; the spinner only picks two entries.
from .style import CYAN, DIM, RESET


class Spinner:
    """An animated spinner that rewrites one stderr line in place.

    Only animates when the stream is a TTY. `start()`/`stop()` bracket a
    phase; `update(msg)` swaps the trailing message live (e.g. progress
    counts). The spinner runs on a daemon thread so the caller's work is
    never blocked by it.
    """

    def __init__(self, message: str = "", *, stream=None, use_color: bool = True) -> None:
        self.message = message
        self.stream = stream if stream is not None else sys.stderr
        self.use_color = use_color
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._frame = 0
        # Serializes writes to `stream`: the animator thread and the caller
        # (via `update`) both write, and two threads interleaving a
        # carriage-return rewrite would garble a frame.
        self._write_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """True when we may animate — i.e. the stream is a real terminal."""
        try:
            return self.stream.isatty()
        except (AttributeError, OSError):
            return False

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._draw()
            time.sleep(_INTERVAL)

    def _draw(self) -> None:
        frame = _FRAMES[self._frame % len(_FRAMES)]
        self._frame += 1
        with self._write_lock:
            if self.use_color:
                self.stream.write(f"\r{CYAN}{frame}{RESET} {DIM}{self.message}{RESET}")
            else:
                self.stream.write(f"\r{frame} {self.message}")
            self.stream.flush()

    def update(self, message: str) -> None:
        """Change the message; redraw immediately if we're animating."""
        self.message = message
        if self._thread is not None:
            self._draw()

    def stop(self) -> None:
        """Stop animating and wipe the spinner line."""
        if self._thread is None:
            return
        self._stop.set()
        # The thread sleeps `_INTERVAL` between frames, so it exits quickly.
        self._thread.join(timeout=0.3)
        self._thread = None
        with self._write_lock:
            self.stream.write(_CLEAR)
            self.stream.flush()
