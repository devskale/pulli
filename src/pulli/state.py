"""RepoState — one classifier, one precedence, every consumer a lookup.

"What state is this repo in?" used to be decided five times (pull's
_classify, cli's _needs_attention, tree's _line_color and _status_glyph,
cli's _summary counts), each with its own subtly different precedence —
and tree and pull could genuinely disagree about the same repo.

Now there is one precedence table, and it is pull.py's (the safety-
relevant one): a repo is classified by the first rule that matches.

    error        → BROKEN        report, skip, exit non-zero
    operation    → BUSY          report, skip
    diverged     → DIVERGED      report, skip (ff impossible)
    dirty-tracked→ DIRTY         report, skip (commit or stash first)
    behind       → PULLABLE      git pull --ff-only
                   (untracked-only dirty does not block a pull)
    ahead        → AHEAD         note: push when you like
    otherwise    → CURRENT       silent

Every derived view — glyph, line color, attention filter, report bucket,
summary count — is a lookup on this enum, so the views can no longer
disagree with each other or with what pull actually does.
"""
from __future__ import annotations

from enum import Enum

from .discovery import RepoNode
from .style import CYAN, GREEN, RED, YELLOW


class RepoState(Enum):
    BROKEN = "broken"          # git itself failed on this repo
    BUSY = "busy"              # merge/rebase/bisect in progress
    DIVERGED = "diverged"      # ahead *and* behind — ff impossible
    DIRTY = "dirty"            # tracked changes — pull would clobber
    PULLABLE = "pullable"      # behind only — git pull --ff-only
    AHEAD = "ahead"            # commits not pushed
    CURRENT = "current"        # nothing to do


def classify(node: RepoNode) -> RepoState:
    """The one precedence table. First match wins."""
    if node.error:
        return RepoState.BROKEN
    if node.operation:
        return RepoState.BUSY
    behind, ahead = node.behind or 0, node.ahead or 0
    if behind and ahead:
        return RepoState.DIVERGED
    if node.dirty and not node.untracked_only:
        return RepoState.DIRTY
    if behind:
        return RepoState.PULLABLE
    if ahead:
        return RepoState.AHEAD
    return RepoState.CURRENT


# ── derived views: lookups, not logic ────────────────────────────────────

#: Glyph shown in the tree's state column.
_GLYPHS: dict[RepoState, tuple[str, str]] = {
    RepoState.BROKEN: ("✗", RED),
    RepoState.BUSY: ("◐", YELLOW),
    RepoState.DIVERGED: ("◐", YELLOW),
    RepoState.DIRTY: ("◐", YELLOW),
    RepoState.PULLABLE: ("●", GREEN),
    RepoState.AHEAD: ("●", GREEN),
    RepoState.CURRENT: ("●", GREEN),
}


def glyph(node: RepoNode) -> tuple[str, str]:
    """(symbol, color) for the state column."""
    return _GLYPHS[classify(node)]


#: Whole-line highlight color for actionable rows, None for quiet rows.
#: Behind (pull me) and dirty-tracked (commit/stash me) are the two
#: states that ask for action; the eye scans rows before columns.
_LINE_COLORS: dict[RepoState, str | None] = {
    RepoState.BROKEN: None,
    RepoState.BUSY: None,
    RepoState.DIVERGED: None,
    RepoState.DIRTY: YELLOW,
    RepoState.PULLABLE: CYAN,
    RepoState.AHEAD: None,
    RepoState.CURRENT: None,
}


def line_color(node: RepoNode) -> str | None:
    """Whole-line highlight color, or None for a quiet row."""
    return _LINE_COLORS[classify(node)]


def needs_attention(node: RepoNode) -> bool:
    """A repo the user should look at: anything that is not CURRENT."""
    return classify(node) is not RepoState.CURRENT


#: Report order: what needs attention first. Tie-break on display path.
_RANKS: dict[RepoState, int] = {
    RepoState.BROKEN: 0,
    RepoState.BUSY: 1,
    RepoState.DIVERGED: 2,
    RepoState.PULLABLE: 3,
    RepoState.DIRTY: 4,
    RepoState.AHEAD: 5,
    RepoState.CURRENT: 6,
}


def sort_key(node: RepoNode) -> tuple[int, str]:
    return (_RANKS[classify(node)], node.rel)
