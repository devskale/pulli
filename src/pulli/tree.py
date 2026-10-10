"""Render the repo tree to the terminal.

Layout:

  ~/code
  ├── clones/
  │   ├── herdr  ogulcancelik/herdr  master  ↓0 ↑0  ●
  │   ├── pi     earendil-works/pi   main    ↓5 ↑0  ◐ dirty 2
  │   └── ghostty  ghostty-org/ghostty  main  ↓0 ↑0  ●
  ├── kontext.one/  devskale/kontext.one  main  ↓0 ↑3  ●
  │   ├── klark0
  │   └── python-utils
  ...

Conventions:
  - Plain (non-repo) directories are shown with a trailing `/` and no status.
  - Repos get a status line: remote, branch, ahead/behind, state glyph.
    Clean is the unmarked default (just the green dot); deviations
    (dirty, offline, operation) are spelled out.
  - A repo with no upstream (fresh init, detached HEAD, no remote) shows
    `·  ·` instead of a misleading `↓0 ↑0`.
  - A repo whose fetch failed shows `offline (stale)` — the numbers may be
    out of date and we say so instead of implying freshness.
  - Repos with an operation in progress (merge/rebase/bisect) are marked
    and treated as unsafe to touch.
  - Symlinks are suffixed with ` -> target` and a `(symlink)` marker.
    A link whose target is also reachable under its real name is shown as
    an alias, and never as a second pull target.
  - Submodules are suffixed with `(submodule)`.
  - Bare repos (`x.git/`) are shown but marked: no working tree, no pull.
  - Errors are shown in red.

Every value here comes from `RepoNode` fields filled in by `status.py`;
this module never shells out to git. That keeps the walk fast (git runs
once per repo during status collection, not again during rendering) and
keeps the credential scrubbing in exactly one place.
"""
from __future__ import annotations

import re
import shutil
import sys
import threading

from .discovery import RepoNode, iter_repos

# ANSI colors — kept minimal; degrade gracefully on non-TTY (we strip them).
_RESET = "\x1b[0m"
_DIM = "\x1b[2m"
_BOLD = "\x1b[1m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"
_CYAN = "\x1b[36m"
_MAGENTA = "\x1b[35m"

_BARE_NOTE = "(bare repo — nothing to pull)"

# Matches a single ANSI SGR escape sequence (color / style codes).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_width(s: str) -> int:
    """Visible column count, ignoring ANSI escape sequences."""
    return len(_ANSI_RE.sub("", s))


def _clip_ansi(s: str, width: int) -> str:
    """Clip `s` to `width` *visible* columns, preserving ANSI escape codes.

    The live tree rewrites lines in place with cursor-up/down arithmetic that
    assumes every line occupies exactly one terminal row. A line wider than
    the terminal wraps onto a second row, and the arithmetic then lands on
    the wrong row — corrupting the display (the `mytestdir` bug). Clipping to
    the terminal width guarantees no line ever wraps.

    If truncation cuts into a colored region we append a reset so the color
    doesn't bleed into the next line.
    """
    if width <= 0 or _visible_width(s) <= width:
        return s
    out: list[str] = []
    vis = 0
    i = 0
    n = len(s)
    truncated = False
    while i < n:
        c = s[i]
        if c == "\x1b":
            # Copy the whole escape sequence through unchanged.
            if i + 1 < n and s[i + 1] == "[":
                j = i + 2
                while j < n and not ("\x40" <= s[j] <= "\x7e"):
                    j += 1
                j += 1  # the final byte (letter)
            else:
                j = i + 1
            out.append(s[i:j])
            i = j
        else:
            if vis >= width:
                truncated = True
                break
            out.append(c)
            vis += 1
            i += 1
    if truncated:
        out.append(_RESET)
    return "".join(out)


def _shorten_url(url: str) -> str:
    """git@github.com:owner/repo.git -> owner/repo

    Strips credentials (user:pass@ or token@) so secrets never hit the
    terminal. This is the only place a remote URL becomes display text.
    """
    if not url:
        return ""
    # Strip userinfo from scheme://user:pass@host/path
    if "://" in url:
        scheme, rest = url.split("://", 1)
        if "@" in rest:
            rest = rest.split("@", 1)[1]
        url = f"{scheme}://{rest}"
    elif url.startswith("git@"):
        # git@host:owner/repo.git — scp-like; 'git' is a username, not a
        # secret. Drop the git@host: prefix for display.
        url = url.split(":", 1)[1] if ":" in url else url
    elif "@" in url and ":" in url.split("@", 1)[0]:
        # user:pass@host:path — strip creds
        url = url.split("@", 1)[1]
    # For the common hosts, drop scheme+host so the display is just
    # `owner/repo` — the same compact form as a scp-style `git@host:owner/repo`
    # URL. A less common host keeps its name so the repo is identifiable.
    for host in ("https://github.com/", "https://gitlab.com/",
                 "https://bitbucket.org/", "http://github.com/",
                 "http://gitlab.com/", "http://bitbucket.org/"):
        if url.startswith(host):
            url = url[len(host):]
            break
    if url.endswith(".git"):
        url = url[: -len(".git")]
    return url


def _status_glyph(node: RepoNode) -> tuple[str, str]:
    """Return (glyph, color) for the ok/dirty/error state."""
    if node.error:
        return "✗", _RED
    if node.dirty or node.operation:
        return "◐", _YELLOW
    return "●", _GREEN


def _ahead_behind(node: RepoNode, C=lambda s, *c: s) -> str:
    """↓behind ↑ahead, or a clear placeholder when there is no upstream.

    Showing `↓0 ↑0` for a repo with no upstream reads as "in sync with
    something", which is a lie — there is nothing to be in sync with.
    A repo that is behind is the one actionable state in this list — that
    is what `pulli pull` acts on — so the count is highlighted (cyan,
    bold) instead of sitting in the same grey as `↑0`. Up to date stays
    quiet: no news takes no color.
    """
    if not node.upstream:
        return "·  ·"
    behind, ahead = node.behind or 0, node.ahead or 0
    b = C(f"↓{behind}", _CYAN, _BOLD) if behind else f"↓{behind}"
    a = C(f"↑{ahead}", _CYAN, _BOLD) if ahead else f"↑{ahead}"
    return f"{b} {a}"


def _repo_tail_parts(node: RepoNode, C) -> list[str]:
    """The status columns for a repo node, as a list of aligned-able parts.

    Returns `[url, branch, ahead/behind, state]` so a flat renderer can pad
    the url column to a fixed width before joining. The tree renderer just
    joins them with two spaces.
    """
    if node.error:
        return [C(f"✗ {node.error}", _RED)]

    url = _shorten_url(node.url)
    parts = [
        C(url, _DIM) if url else "",
        C(node.branch or "?", _BOLD),
        _ahead_behind(node, C),
    ]

    if node.operation:
        state = C(f"◐ {node.operation} — skipping", _YELLOW)
    elif node.fetch_failed:
        # Fetch didn't work, so ↓↑ may be stale. Say so rather than lie.
        state = C(f"◐ offline — {node.fetch_reason or 'unreachable'}", _YELLOW)
    else:
        glyph, gcolor = _status_glyph(node)
        if node.dirty:
            # Untracked-only is not real dirt: git itself says a submodule
            # with only untracked content is "not considered dirty", and a
            # pull can't conflict with an untracked file. So it gets its own
            # word and a softer color than tracked changes, which can.
            if node.untracked_only:
                state = C(f"◐ untracked {len(node.dirty_files)}", _DIM)
            else:
                state = C(f"◐ dirty {len(node.dirty_files)}", _YELLOW)
        else:
            state = C(glyph, gcolor)

    parts.append(state)
    return parts


def _repo_tail(node: RepoNode, C) -> str:
    """The status column for a repo node: remote, branch, ↓↑, glyph + state."""
    return "  ".join(p for p in _repo_tail_parts(node, C) if p)


def _label(node: RepoNode, C) -> str:
    name = node.name
    if node.is_submodule:
        return C(name, _CYAN) + C(" (submodule)", _DIM)
    if node.is_symlink:
        s = C(name, _MAGENTA) + C(" -> " + (node.symlink_target or ""), _DIM)
        # A link that is the only route to its target is just a
        # symlink. One whose target is also listed under its real name
        # is an alias: it shows a second name for a repo that appears
        # elsewhere in the tree, and must not look like a second one.
        return s + (C(" (alias)", _DIM) if node.is_alias else C(" (symlink)", _DIM))
    return name


def _build_lines(
    root: RepoNode,
    C,
    tail,
) -> tuple[list[str], dict[int, int], dict[int, str]]:
    """Build the tree lines.

    `tail(node)` renders the status column for a repo node (the batch
    renderer passes `_repo_tail`; the live renderer passes a placeholder).
    Returns `(lines, repo_line, repo_base)`:
      * `lines` — the rendered strings, one per row;
      * `repo_line` — maps `id(node)` -> line index for every repo node;
      * `repo_base` — maps `id(node)` -> the `prefix+connector+name` part of
        that line, so a live renderer can swap just the status tail in place.
    """
    lines: list[str] = []
    repo_line: dict[int, int] = {}
    repo_base: dict[int, str] = {}

    # Root header — annotated too when the root is itself a repo, so the
    # head of the tree isn't a bare path with no status next to it.
    root_label = C(str(root.link_path or root.path), _BOLD)
    header = root_label
    if root.is_repo:
        header += "  " + tail(root)
        repo_line[id(root)] = 0
        repo_base[id(root)] = root_label
    elif root.is_bare:
        header += "  " + C(_BARE_NOTE, _DIM)
    lines.append(header)

    def render_node(node: RepoNode, prefix: str, is_last: bool) -> None:
        connector = "└── " if is_last else "├── "
        name = _label(node, C)

        if node.is_alias:
            # The target is listed under its real name elsewhere; this is a
            # second name for it, not a second repo to pull. No status
            # columns — there is nothing here to report.
            lines.append(f"{prefix}{connector}{name}")
        elif node.is_repo:
            base = f"{prefix}{connector}{name}"
            lines.append(base + "  " + tail(node))
            repo_line[id(node)] = len(lines) - 1
            repo_base[id(node)] = base
        elif node.is_bare:
            lines.append(f"{prefix}{connector}{name}/  {C(_BARE_NOTE, _DIM)}")
        elif node.is_symlink:
            # A link to a plain dir: the label already carries the marker,
            # and a trailing / would land *after* it — "(symlink)/".
            lines.append(f"{prefix}{connector}{name}")
        else:
            # plain directory — show with trailing /, no status
            lines.append(f"{prefix}{connector}{C(name + '/', _DIM)}")

        child_prefix = prefix + ("    " if is_last else "│   ")
        for i, child in enumerate(node.children):
            render_node(child, child_prefix, i == len(node.children) - 1)

    last = len(root.children) - 1
    for i, child in enumerate(root.children):
        render_node(child, "", i == last)

    return lines, repo_line, repo_base


def _build_flat_lines(
    root: RepoNode,
    C,
    tail,
    repos=None,
) -> tuple[list[str], dict[int, int], dict[int, str]]:
    """Build a flat list: one line per repo, no tree structure.

    `tail(node)` renders the status column for a repo node. `repos` overrides
    the default "all repos sorted by path" — the CLI passes a filtered or
    attention-sorted list for --behind / --attention. Returns
    `(lines, repo_line, repo_base)` with the same contract as
    `_build_lines`, so a live renderer can swap a repo's status in place.
    """
    lines: list[str] = []
    repo_line: dict[int, int] = {}
    repo_base: dict[int, str] = {}
    if repos is None:
        repos = sorted(iter_repos(root), key=lambda n: n.rel)
    if not repos:
        return lines, repo_line, repo_base
    # Pad every path to the widest one so the status columns line up like a
    # table instead of starting wherever the previous path happened to end.
    path_width = max(_visible_width(C(n.rel, _DIM)) for n in repos)
    # Pad the url column too, so branch / ahead-behind / state also align.
    url_width = max((_visible_width(_shorten_url(n.url)) for n in repos), default=0)
    for n in repos:
        base = C(n.rel, _DIM)
        padded = base + " " * (path_width - _visible_width(base))
        # Rebuild the tail with a url column padded to a fixed width. Always
        # emit the url cell (even when empty) so the branch column lines up
        # for repos that have no remote too.
        cols = _repo_tail_parts(n, C)
        if len(cols) >= 4:
            cols[0] = cols[0] + " " * (url_width - _visible_width(cols[0]))
        text = "  ".join(c for c in cols if c)
        line = padded + "  " + text
        # A repo that is behind is the actionable one — `pulli pull` acts on
        # it — so the whole line is highlighted (cyan) instead of just the
        # count. Everything up to date stays quiet: no news takes no color.
        if n.behind and not n.error:
            line = C(line, _CYAN)
        lines.append(line)
        repo_line[id(n)] = len(lines) - 1
        repo_base[id(n)] = padded
    return lines, repo_line, repo_base


def render(root: RepoNode, *, use_color: bool = True) -> str:
    """Render the full tree as a string."""

    def C(s: str, *codes: str) -> str:
        if not use_color:
            return s
        return "".join(codes) + s + _RESET

    lines, _, _ = _build_lines(root, C, lambda n: _repo_tail(n, C))
    return "\n".join(lines)


def render_flat(root: RepoNode, *, use_color: bool = True, repos=None) -> str:
    """Render a flat list of repos (one line each), not the tree.

    `repos` overrides the default "all repos sorted by path" list.
    """

    def C(s: str, *codes: str) -> str:
        if not use_color:
            return s
        return "".join(codes) + s + _RESET

    lines, _, _ = _build_flat_lines(root, C, lambda n: _repo_tail(n, C), repos=repos)
    return "\n".join(lines)


class LiveTree:
    """Streams the tree to a terminal.

    Prints the skeleton immediately — every repo line shows a dim `…` where
    its status will go — then rewrites each repo's line in place as its
    status becomes available. The final output is identical to `render()`,
    it just appears incrementally instead of all at once after a multi-second
    fetch.

    Only meaningful when the stream is a real terminal. Off a TTY it falls
    back to printing the fully-rendered tree once, and `update()` becomes a
    no-op, so it is safe to construct anywhere.
    """

    def __init__(self, root: RepoNode, *, use_color: bool = True, stream=None,
                 flat: bool = False) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.use_color = use_color
        self._lock = threading.Lock()
        # Terminal width: every rendered line is clipped to this so nothing
        # wraps. A wrapped line is two rows, which breaks the cursor
        # arithmetic the in-place rewrite relies on.
        self._width = self._terminal_width()

        def C(s: str, *codes: str) -> str:
            if not use_color:
                return s
            return "".join(codes) + s + _RESET

        self._C = C
        self._flat = flat
        # When flat, pad the url column so branch / ahead-behind / state
        # align; store the width so update() can match the skeleton.
        self._url_width = (
            max((_visible_width(_shorten_url(c.url)) for c in iter_repos(root)), default=0)
            if flat else 0
        )
        builder = _build_flat_lines if flat else _build_lines

        if not self._is_tty():
            # Not a real terminal: there is no in-place cursor control, so
            # streaming is impossible. Print the skeleton (with `…`
            # placeholders) once and leave update() inert. The CLI only uses
            # LiveTree on a TTY, so this path is defensive.
            self._active = False
            lines, repo_line, repo_base = builder(root, C, lambda n: C("…", _DIM))
            self.lines = [_clip_ansi(line, self._width) for line in lines]
            self.repo_line = repo_line
            self.repo_base = repo_base
            self._bottom = len(self.lines)
            self._print_skeleton()
            return

        self._active = True
        lines, repo_line, repo_base = builder(root, C, lambda n: C("…", _DIM))
        self.lines = [_clip_ansi(line, self._width) for line in lines]
        self.repo_line = repo_line
        self.repo_base = repo_base
        self._bottom = len(self.lines)
        self._print_skeleton()

    def _is_tty(self) -> bool:
        try:
            return self.stream.isatty()
        except (AttributeError, OSError):
            return False

    def _terminal_width(self) -> int:
        """Columns of the terminal, or a sane default when unknown."""
        try:
            return shutil.get_terminal_size().columns
        except (OSError, ValueError):
            return 80

    def _print_skeleton(self) -> None:
        for line in self.lines:
            self.stream.write(line + "\n")
        self.stream.flush()

    def update(self, node: RepoNode) -> None:
        """Swap a repo's `…` placeholder for its real status, in place."""
        if not self._active:
            return
        nid = id(node)
        i = self.repo_line.get(nid)
        if i is None:
            return
        cols = _repo_tail_parts(node, self._C)
        if self._flat and len(cols) >= 4:
            cols[0] = cols[0] + " " * (self._url_width - _visible_width(cols[0]))
        text = self.repo_base[nid] + "  " + "  ".join(c for c in cols if c)
        # Same highlight rule as _build_flat_lines: the whole line goes
        # cyan when the repo is behind — that is the actionable state.
        if self._flat and node.behind and not node.error:
            text = self._C(text, _CYAN)
        text = _clip_ansi(text, self._width)
        with self._lock:
            self._rewrite(i, text)

    def _rewrite(self, i: int, text: str) -> None:
        """Rewrite line `i` in place, then restore the cursor to the bottom."""
        up = self._bottom - i
        if up > 0:
            self.stream.write(f"\x1b[{up}A")
        self.stream.write("\r\x1b[2K" + text)
        if up > 0:
            self.stream.write(f"\x1b[{up}B")
        self.stream.write("\r")
        self.stream.flush()
