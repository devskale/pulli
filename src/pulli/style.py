"""ANSI styling — one palette, one gate, one place.

Every visual decision in pulli goes through this module: the palette
constants, the should-this-be-colored gate (TTY, --no-color, NO_COLOR,
TERM=dumb), and the whole-line highlight that survives nested SGR resets.

Before this module existed the palette was defined four times (tree.py,
pull.py, spinner.py, cli.py) with three different helper shapes, and the
NO_COLOR gate only lived in cli.py — so any new consumer could (and did)
forget it. Now the gate is part of the interface: a Colorizer is either
on or off, and everything rendered through it honors the same rules.
"""
from __future__ import annotations

import re
import sys

# ── palette ──────────────────────────────────────────────────────────────

RESET = "\x1b[0m"
DIM = "\x1b[2m"
BOLD = "\x1b[1m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
CYAN = "\x1b[36m"
MAGENTA = "\x1b[35m"

# Matches a single ANSI SGR escape sequence (color / style codes).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def visible_width(s: str) -> int:
    """Visible column count, ignoring ANSI escape sequences."""
    return len(_ANSI_RE.sub("", s))


def should_color(stream=None, *, override: bool | None = None) -> bool:
    """The one color gate: TTY + no NO_COLOR + not TERM=dumb.

    `override` is the --no-color / forced-color decision from the CLI and
    wins over everything. Kept separate from the Colorizer so callers can
    ask "would color be on?" without constructing one.
    """
    if override is False:
        return False
    stream = stream if stream is not None else sys.stdout
    try:
        if not stream.isatty():
            return False
    except (AttributeError, OSError):
        return False
    # no-color.org convention: NO_COLOR set to any non-empty value.
    import os
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return True


class Colorizer:
    """Wrap text in ANSI codes — or don't, when color is off.

    One callable: `C(text, *codes)`. When disabled it is the identity,
    so render code has no `if use_color` branches — it always calls C.
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, s: str, *codes: str) -> str:
        if not self.enabled or not codes:
            return s
        return "".join(codes) + s + RESET


def highlight_line(line: str, color: str) -> str:
    """Color-wrap a whole line, surviving nested SGR resets.

    Inner resets (e.g. the count's own cyan+bold) would end the outer
    color mid-line and make the line render two-tone; re-apply the color
    after each reset so the line stays one color throughout.
    Returns the input unchanged when it carries no ANSI codes (color off)
    — nothing to highlight then.
    """
    if "\x1b[" not in line:
        return line
    # Drop dim segments: dim-color renders darker than the color, which
    # would split the highlighted line into bright and dark halves. The
    # point of the highlight is one uniform color.
    line = line.replace(DIM, "")
    return color + line.replace(RESET, RESET + color) + RESET
