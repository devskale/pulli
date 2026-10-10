"""Pull — update repos and report ahead/behind.

For every discovered work tree we report how far ahead/behind it is vs its
upstream, then fast-forward the repos that are behind.

Safety rules, in the order they are checked. All of them are *skip*, never
*force* — pulli must never destroy or corrupt work:

  1. broken repo / no git work tree         → report, skip
  2. operation in progress (merge, rebase,
     bisect, cherry-pick, revert)           → report, skip
  3. no upstream (detached HEAD, no remote) → skip, nothing to pull
  4. diverged (ahead *and* behind)          → report, skip (ff impossible)
  5. uncommitted changes                    → report, skip
  6. up to date                             → silent
  7. behind only                            → `git pull --ff-only`

Pull is `git pull --ff-only`, never a merge or rebase. A plain `git pull`
can *create* a merge commit (and a merge conflict) in a repo the user never
touched; `--ff-only` refuses instead. Where a repo has diverged, pulli
reports it and leaves the merge-or-rebase decision to the human.

Repos unreachable from the network are never a failure: fetch is expected to
fail offline, so those repos are reported with a "stale" note and the exit
code stays 0. A non-zero exit means something needs a human — a broken repo
or a pull that actually failed.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .discovery import RepoNode, discover, iter_repos, set_rels
from .status import collect_status, fetch_all, git_env, run_git
from .style import BOLD, CYAN, DIM, GREEN, RED, RESET, YELLOW, Colorizer

_PULL_TIMEOUT = 120


def _color(s: str, code: str, use_color: bool) -> str:
    return code + s + RESET if use_color else s


def _fmt_ahead_behind(node: RepoNode) -> str:
    """Human string for ahead/behind vs upstream."""
    if not node.upstream:
        return "no upstream"
    if node.behind and node.ahead:
        return f"↓{node.behind} ↑{node.ahead}"
    if node.behind:
        return f"↓{node.behind}"
    if node.ahead:
        return f"↑{node.ahead}"
    return "up to date"


def _fmt_dirty(node: RepoNode, use_color: bool) -> str:
    """'dirty 2 (a.md, b.txt)' — count + file names (max 4, then +N more).
    Untracked-only dirty says so: those files cannot conflict with a pull,
    which is what the reader needs to know about them."""
    files = node.dirty_files or []
    if not files:
        return _color("dirty", YELLOW, use_color)
    shown = files[:4]
    more = len(files) - len(shown)
    listing = ", ".join(shown) + (f", +{more} more" if more > 0 else "")
    label = "untracked" if node.untracked_only else "dirty"
    return (
        f"{_color(label, YELLOW, use_color)} {len(files)} "
        f"({_color(listing, DIM, use_color)})"
    )


def _classify(node: RepoNode) -> str:
    """Which bucket a repo falls into. One place, so the report, the
    counters and the summary can never disagree with each other."""
    if node.error:
        return "broken"
    if node.operation:
        return "busy"
    behind, ahead = node.behind or 0, node.ahead or 0
    if not node.upstream or (not behind and not ahead):
        return "current"
    if behind and ahead:
        return "diverged"
    if node.dirty and not node.untracked_only:
        return "dirty"
    if behind:
        return "pullable"
    return "ahead"


def _sort_key(node: RepoNode) -> tuple[int, str]:
    """Report order: what needs attention first. Tie-break on the display
    path so two identical runs always print in the same order."""
    rank = {
        "broken": 0, "busy": 1, "diverged": 2,
        "pullable": 3, "dirty": 4, "ahead": 5, "current": 6,
    }[ _classify(node) ]
    return (rank, node.rel)


def _first_meaningful_line(text: str) -> str:
    """The first line of git's stderr worth showing a human.

    `git pull --ff-only` failures print several lines of `hint:` advice (the
    same three lines every time) before the actual `fatal:`. Leading with
    the hint buries the reason, so prefer a non-hint line.
    """
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.lower().startswith("hint:"):
            continue
        return s
    return ""


def _report(node: RepoNode, kind: str, *, use_color: bool, dry_run: bool) -> str | None:
    """One output line per noteworthy repo. Returns the printed line."""
    C = _color
    rel = node.rel
    ab = _fmt_ahead_behind(node)

    if kind == "broken":
        line = f"  {C('✗', RED, use_color)} {rel}  {C(node.error or 'error', RED, use_color)}"
    elif kind == "busy":
        line = f"  {C('◐', YELLOW, use_color)} {rel}  {C(node.operation + ' — skipping pull', YELLOW, use_color)}"
    elif kind == "diverged":
        line = (
            f"  {C('↕', YELLOW, use_color)} {rel}  {ab}  "
            f"{C('diverged — needs merge or rebase, skipping', YELLOW, use_color)}"
        )
    elif kind == "dirty":
        line = (
            f"  {C('◐', YELLOW, use_color)} {rel}  {ab}  "
            f"{_fmt_dirty(node, use_color)}, skipping pull — "
            f"{C('commit or stash, then pulli pull again', DIM, use_color)}"
        )
    elif kind == "ahead":
        line = f"  {C('↑', CYAN, use_color)} {rel}  {C(f'↑{node.ahead} ahead (not pushed)', CYAN, use_color)}"
    elif kind == "current":
        # Nothing to do. A dirty repo with nothing to pull is only worth
        # mentioning in dry-run mode, where the point is a full inventory.
        if not (dry_run and node.dirty):
            return None
        line = f"  {C('◐', YELLOW, use_color)} {rel}  up to date  {_fmt_dirty(node, use_color)}"
    else:  # pullable
        return None  # printed by the pull loop, which knows the outcome
    print(line)
    return line


def pull(
    root: Path,
    *,
    use_color: bool = True,
    dry_run: bool = False,
    fetch: bool = True,
    follow_symlinks: bool = True,
    max_depth: int = 50,
    link_root: Path | None = None,
    verbose: bool = False,
) -> int:
    """Pull every repo under `root` that is behind upstream.

    Fetches all repos first, so the behind/ahead decision is based on the
    current remote state rather than stale local refs.

    Returns 0 when nothing needs a human, 1 on a broken repo or a failed
    pull. Being offline is not a failure.
    """
    tree = discover(
        root,
        follow_symlinks=follow_symlinks,
        max_depth=max_depth,
        link_root=link_root,
    )
    if tree is None:
        print(f"pulli: could not read {root}", file=sys.stderr)
        return 1

    set_rels(tree)
    # Display paths are relative to what the user typed, not the resolved
    # path, so a root reached through a symlink still reads naturally.
    repos = sorted(iter_repos(tree), key=_sort_key)  # bare repos excluded
    if not repos:
        print("No git repos found.")
        return 0

    # Fetch everything *first* so one pass over the results decides what to
    # do. Interleaving fetch and pull would decide on data that is stale by
    # the time we act on it.
    if fetch:
        fetch_all(repos, quiet=True, use_color=use_color)
    collect_status(tree)
    repos.sort(key=_sort_key)

    buckets: dict[str, list[RepoNode]] = {}
    for node in repos:
        kind = _classify(node)
        buckets.setdefault(kind, []).append(node)
        if kind != "pullable":
            _report(node, kind, use_color=use_color, dry_run=dry_run)

    pullable = buckets.get("pullable", [])
    pulled: list[RepoNode] = []
    failed: list[tuple[RepoNode, str]] = []

    if dry_run:
        print()
        print(_color("Dry run — no pulls performed.", BOLD, use_color))
        for node in pullable:
            note = ""
            if node.dirty and node.untracked_only:
                note = _color("  (untracked files only — safe)", DIM, use_color)
            print(
                f"  {_color('↓', GREEN, use_color)} {node.rel}  "
                f"{_fmt_ahead_behind(node)}  {_color('would pull', GREEN, use_color)}{note}"
            )
    else:
        for node in pullable:
            # The commits this pull will bring in — captured *before* the
            # pull, while the range HEAD..upstream still exists (after a
            # fast-forward it is empty).
            new_commits: list[str] = []
            if verbose and node.upstream:
                rc, out, _ = run_git(
                    node.path,
                    "log", "--oneline", f"HEAD..{node.upstream}",
                    timeout=_PULL_TIMEOUT,
                )
                if rc == 0:
                    new_commits = [l for l in out.splitlines() if l.strip()]
            rc, stdout, stderr = run_git(
                node.path, "pull", "--ff-only", "--quiet", timeout=_PULL_TIMEOUT
            )
            if rc == 0:
                pulled.append(node)
                note = ""
                if node.dirty and node.untracked_only:
                    note = _color("  (untracked files kept)", DIM, use_color)
                print(
                    f"  {_color('✓', GREEN, use_color)} {node.rel}  "
                    f"{_fmt_ahead_behind(node)}  pulled{note}"
                )
                for c in new_commits:
                    print(_color(f"      {c}", DIM, use_color))
            else:
                reason = _first_meaningful_line(stderr or stdout) or f"exit {rc}"
                failed.append((node, reason))
                print(
                    f"  {_color('✗', RED, use_color)} {node.rel}  "
                    f"{_fmt_ahead_behind(node)}  "
                    f"{_color('pull failed: ' + reason, RED, use_color)}"
                )

    # ── summary ──────────────────────────────────────────────────────────
    counts = {k: len(v) for k, v in buckets.items()}
    offline = [n for n in repos if n.fetch_failed]

    print()
    if offline:
        print(
            _color(
                f"note: {len(offline)} repo(s) unreachable — their ↓↑ may be "
                "stale (rerun when online).",
                YELLOW,
                use_color,
            )
        )
    if dry_run:
        print(f"Would pull {len(pullable)} repo(s).")
    else:
        summary = f"Pulled {len(pulled)}"
        if counts.get("diverged"):
            summary += f", {counts['diverged']} diverged"
        if counts.get("dirty"):
            summary += f", {counts['dirty']} skipped dirty"
        if counts.get("busy"):
            summary += f", {counts['busy']} in progress"
        if failed:
            summary += f", {len(failed)} failed"
        summary += f", {counts.get('current', 0)} up to date"
        if counts.get("broken"):
            summary += f", {counts['broken']} broken"
        print(summary + ".")
        if counts.get("ahead"):
            print(
                f"{counts['ahead']} repo(s) ahead of upstream — push them; "
                "pulli only pulls."
            )

    return 1 if (failed or counts.get("broken")) else 0
