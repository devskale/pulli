"""pulli CLI — discover and display the repo tree, and pull what's behind.

Two commands:

    pulli [tree] [root] [options]   status tree (default)
    pulli pull [root] [options]     fast-forward the repos that are behind

Invocation is forgiving by design: `pulli ~/code`, `pulli --no-fetch ~/code`
and `pulli tree ~/code` all mean the same thing, and flags may appear before
or after the subcommand.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import __version__
from .update import update_hint
from .discovery import discover, iter_repos, set_rels
from .pull import pull
from .status import collect_status, fetch_all, fetch_and_status
from .tree import LiveTree, render, render_flat
from .style import BOLD, RESET

# Flags that belong to a subcommand. Used by _inject_tree to decide whether
# a leading argument is a subcommand or a path/flag for the default one.
_TREE_FLAGS = {
    "--no-symlinks", "--no-fetch", "--no-color", "--max-depth",
    "--dry-run", "--json", "--tree",
}
_PULL_FLAGS = {"--no-color", "--dry-run", "--json", "--no-fetch"}

SUBCOMMANDS = ("tree", "pull", "update")

class _VersionAction(argparse.Action):
    """Print the version, plus an update hint when one exists.

    The built-in version action prints a static string; this one asks the
    update module (synchronously — asking for the version is the one place
    a 3s PyPI check is worth the wait) and appends the hint below it.
    """

    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        print(f"pulli {__version__}")
        hint = update_hint(sync=True)
        if hint:
            print(hint, file=sys.stderr)
        parser.exit()


class _ExamplesAction(argparse.Action):
    """Print the examples verbatim and exit.

    argparse's built-in version action routes the text through its
    HelpFormatter, which re-wraps and collapses the newlines — one long
    line. Printing directly keeps one example per line.
    """

    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        print(_EXAMPLES)
        parser.exit()


# `pulli --examples` — clig.dev: "Lead with examples. Users tend to use
# examples over other forms of documentation, so show them first."
_EXAMPLES = """\
examples:
  pulli                          status of all repos under the current dir
  pulli ~/code                   status of all repos under ~/code
  pulli --behind ~/code          only the repos that need a pull
  pulli --attention ~/code       dirty, diverged, behind — what needs you first
  pulli pull ~/code              fast-forward the repos that are behind
  pulli pull --dry-run ~/code    show what would be pulled, change nothing
  pulli --json ~/code            one JSON object per line, for scripts"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pulli",
        description=(
            "Discover git repos under a directory, show their status, and "
            "fast-forward the ones that are behind upstream."
        ),
    )
    sub = p.add_subparsers(dest="command")

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "root",
            nargs="?",
            default=".",
            help="Root directory to scan (default: current dir).",
        )
        sp.add_argument(
            "-V", "--version",
            action=_VersionAction,
        )
        sp.add_argument(
            "--no-color",
            action="store_true",
            help="Disable ANSI colors (also implied when not a TTY).",
        )
        sp.add_argument(
            "--json",
            action="store_true",
            help="Machine-readable output (one JSON object per line).",
        )

    # `pulli tree` — the status tree (default when no command given).
    tree_p = sub.add_parser("tree", help="Show the repo status tree (default).")
    add_common(tree_p)
    tree_p.add_argument(
        "--no-symlinks",
        action="store_true",
        help="Do not follow symlinks (default: follow, marked).",
    )
    tree_p.add_argument(
        "--no-fetch",
        action="store_true",
        help="Do not fetch remotes before showing status (default: fetch).",
    )
    tree_p.add_argument(
        "--max-depth",
        type=int,
        default=50,
        help="Maximum recursion depth (default: 50).",
    )
    tree_p.add_argument(
        "--tree",
        action="store_true",
        help="Show the full directory tree instead of a flat repo list.",
    )
    tree_p.add_argument(
        "--behind",
        action="store_true",
        help="Only repos that are behind upstream (need a pull).",
    )
    tree_p.add_argument(
        "--attention",
        action="store_true",
        help="Only repos that need you: dirty, diverged, behind, broken.",
    )
    tree_p.add_argument(
        "--no-summary",
        action="store_true",
        help="Omit the one-line summary under the list.",
    )
    tree_p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="List the incoming commits under each behind repo "
        "(git log --oneline, HEAD..upstream).",
    )

    # `pulli pull` — pull repos that are behind.
    pull_p = sub.add_parser("pull", help="Pull repos that are behind upstream.")
    add_common(pull_p)
    pull_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be pulled without pulling.",
    )
    pull_p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="List the new commits under each pulled repo (git log --oneline).",
    )
    pull_p.add_argument(
        "--no-fetch",
        action="store_true",
        help="Use local refs only (no fetch) — faster, may be stale.",
    )
    pull_p.add_argument(
        "--no-symlinks",
        action="store_true",
        help="Do not follow symlinks (default: follow).",
    )
    pull_p.add_argument(
        "--max-depth",
        type=int,
        default=50,
        help="Maximum recursion depth (default: 50).",
    )

    # `pulli update` — self-update.
    upd_p = sub.add_parser("update", help="Update pulli to the latest PyPI release.")
    upd_p.add_argument(
        "--check",
        action="store_true",
        help="Only show what would be updated, change nothing.",
    )

    p.add_argument("-V", "--version", action=_VersionAction)
    p.add_argument(
        "--examples",
        action=_ExamplesAction,
        help="Show usage examples.",
    )
    return p


def _inject_tree(argv: list[str]) -> list[str]:
    """Make `tree` the default subcommand, wherever the flags are.

    `pulli`, `pulli ~/code`, `pulli --no-fetch --max-depth 3 ~/code` and
    `pulli tree ~/code` all mean `tree`. Without this, a leading flag
    (`pulli --no-color .`) makes argparse reject the path as an invalid
    subcommand choice — which is exactly how the old version behaved.
    """
    if not argv:
        return ["tree"]
    first = argv[0]
    if first in SUBCOMMANDS or first in ("-V", "--version", "-h", "--help", "--examples"):
        return argv
    # Anything that isn't the `pull` subcommand falls through to `tree`.
    return ["tree", *argv]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(
        _inject_tree(list(sys.argv[1:] if argv is None else argv))
    )
    if args.command == "pull":
        return _run_pull(args)
    if args.command == "update":
        return _run_update(args)
    return _run_tree(args)


def _run_update(args) -> int:
    """Self-update: refresh the version cache, then upgrade via uv/pipx.

    The installer that owns the binary decides the upgrade command — uv
    tool installs upgrade differently than pipx or a raw pip install, and
    guessing wrong would "succeed" while changing nothing.
    """
    from .update import _fetch_latest, _is_newer

    latest = _fetch_latest()
    if latest is None:
        print("pulli: could not reach PyPI — check your connection and retry.", file=sys.stderr)
        return 1
    if not _is_newer(latest):
        print(f"pulli {__version__} is up to date (PyPI: {latest}).")
        return 0
    if args.check:
        print(f"pulli {__version__} → {latest} would be installed. Run: pulli update")
        return 0

    exe = Path(sys.argv[0]).resolve()
    candidates: list[tuple[str, list[str]]] = []
    if ".local/bin" in str(exe) or True:  # uv tool and pipx both live here
        candidates.append(("uv", ["uv", "tool", "upgrade", "pulli"]))
        candidates.append(("pipx", ["pipx", "upgrade", "pulli"]))
    import shutil
    for name, cmd in candidates:
        if shutil.which(cmd[0]):
            print(f"pulli {__version__} → {latest} (via {name})")
            rc = subprocess.run(cmd).returncode
            if rc == 0:
                print(f"Updated to pulli {latest}.")
            return rc
    print(
        f"pulli {latest} is available, but no uv/pipx found. Update manually, e.g.:\n"
        f"  pip install --upgrade pulli",
        file=sys.stderr,
    )
    return 1


def _use_color(args) -> bool:
    # Explicit --no-color wins; otherwise colour only on a TTY, and never
    # when --json (a JSON consumer does not want escape codes in strings).
    if args.no_color or getattr(args, "json", False):
        return False
    # no-color.org convention: NO_COLOR set to any non-empty value disables
    # color. TERM=dumb marks a terminal that cannot handle the sequences.
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()


def _root_arg(args) -> Path:
    """Resolve the root, keeping the path the user typed for display.

    `resolve()` is what the walk needs (symlink-safe, comparable paths), but
    showing `/private/tmp/x` when the user asked for `~/code` is confusing —
    so the link path is what gets rendered, the resolved path is what git
    commands run against.
    """
    root = Path(args.root).expanduser()
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        resolved = root.absolute()
    args.link_root = root.absolute()
    return resolved


def _check_root(root: Path) -> int | None:
    if not root.exists():
        print(f"pulli: no such directory: {root}", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"pulli: not a directory: {root}", file=sys.stderr)
        return 2
    return None


def _needs_attention(node) -> bool:
    """A repo the user should look at: broken, busy, diverged, behind, or
    dirty. Mirrors pull.py's bucket order so tree and pull never disagree
    about what "needs attention" means."""
    if node.error or node.operation:
        return True
    if node.behind and node.ahead:
        return True  # diverged
    if node.behind:
        return True
    return bool(node.dirty)


def _summary(repos, use_color: bool) -> str:
    """One line under the list: the answer to "how are my repos doing?",
    without counting lines by hand. Same buckets as pull.py's summary."""
    n = len(repos)
    behind = sum(1 for r in repos if r.behind and not r.ahead)
    diverged = sum(1 for r in repos if r.behind and r.ahead)
    ahead = sum(1 for r in repos if r.ahead and not r.behind)
    dirty = sum(1 for r in repos if r.dirty and not (r.behind or r.ahead))
    offline = sum(1 for r in repos if r.fetch_failed)
    broken = sum(1 for r in repos if r.error)
    parts = [f"{n} repos"]
    if behind:
        parts.append(f"{behind} behind")
    if diverged:
        parts.append(f"{diverged} diverged")
    if ahead:
        parts.append(f"{ahead} ahead")
    if dirty:
        parts.append(f"{dirty} dirty")
    if offline:
        parts.append(f"{offline} offline")
    if broken:
        parts.append(f"{broken} broken")
    text = " · ".join(parts)
    if behind or diverged or broken:
        # Something needs a human — make the line findable at a glance.
        hint = f"{text} — pulli pull would update {behind} of them"
        return BOLD + hint + RESET if use_color else hint
    return text


def _run_tree(args) -> int:
    root = _root_arg(args)
    if (rc := _check_root(root)) is not None:
        return rc

    tree = discover(
        root,
        follow_symlinks=not args.no_symlinks,
        max_depth=args.max_depth,
        link_root=getattr(args, "link_root", None),
    )
    if tree is None:
        print(f"pulli: could not read {root}", file=sys.stderr)
        return 1

    set_rels(tree)
    repos = list(iter_repos(tree))

    if args.json:
        if not args.no_fetch:
            fetch_all(repos, quiet=True, use_color=_use_color(args))
        collect_status(tree)
        _print_json(tree)
        return 0

    use_color = _use_color(args)
    flat = not args.tree
    # Filters decide on fetched status, so they cannot stream: the skeleton
    # is printed before anything is known. Run the batch path instead.
    filtered = bool(getattr(args, "behind", False) or getattr(args, "attention", False))
    if sys.stdout.isatty() and not filtered:
        # Stream: print the skeleton immediately, then fill each repo's
        # status line in as it becomes ready — no blank-screen wait.
        live = LiveTree(tree, use_color=use_color, flat=flat)
        fetch_and_status(
            repos,
            quiet=True,
            use_color=use_color,
            fetch=not args.no_fetch,
            on_done=live.update,
        )
        if flat and not args.no_summary:
            # The live tree is done rewriting lines; the summary lands
            # below it, where the eye ends up anyway.
            print()
            print(_summary(list(iter_repos(tree)), use_color))
    else:
        # Not a terminal: no in-place cursor control. Do the work, then
        # print the complete output once (byte-identical to before).
        if not args.no_fetch:
            fetch_all(repos, quiet=True, use_color=use_color)
        collect_status(tree)
        if flat:
            shown = list(iter_repos(tree))
            if args.behind:
                shown = [r for r in shown if r.behind and not r.ahead]
            elif args.attention:
                shown = [r for r in shown if _needs_attention(r)]
            if args.attention:
                shown.sort(key=lambda n: (not n.behind, n.rel))
            if not shown:
                # clig.dev: "It's rare that printing nothing at all is the
                # best default behavior." Say what was searched and that it
                # is empty — silence reads as a bug, not as a result.
                what = "behind" if args.behind else "needing attention"
                print(f"No repos {what} under {args.link_root}")
                return 0
            print(render_flat(tree, use_color=use_color, repos=shown))
            if not args.no_summary:
                print()
                print(_summary(shown, use_color))
        else:
            print(render(tree, use_color=use_color))
    return 0


def _run_pull(args) -> int:
    root = _root_arg(args)
    if (rc := _check_root(root)) is not None:
        return rc

    if args.json:
        # JSON output is an inventory: report what would happen, change
        # nothing. Silently ignoring --dry-run next to --json would be a
        # trap, so treat the combination as dry-run and say so.
        if not args.dry_run:
            print("pulli: --json implies --dry-run (no changes are made)", file=sys.stderr)
        return _pull_json(root, args)

    return pull(
        root,
        use_color=_use_color(args),
        dry_run=args.dry_run,
        fetch=not args.no_fetch,
        follow_symlinks=not args.no_symlinks,
        max_depth=args.max_depth,
        link_root=getattr(args, "link_root", None),
        verbose=getattr(args, "verbose", False),
    )


def _node_dict(n) -> dict:
    from .tree import _shorten_url

    return {
        "path": n.rel,
        "name": n.name,
        "repo": n.is_repo,
        "bare": n.is_bare,
        "submodule": n.is_submodule,
        "symlink": n.is_symlink or None,
        "url": _shorten_url(n.url) or None,
        "branch": n.branch,
        "upstream": n.upstream,
        "behind": n.behind,
        "ahead": n.ahead,
        "dirty": n.dirty,
        "dirty_files": n.dirty_files or None,
        "operation": n.operation,
        "fetch_failed": n.fetch_failed or None,
        "error": n.error,
    }


def _print_json(tree) -> None:
    for n in iter_repos(tree):
        print(json.dumps(_node_dict(n), ensure_ascii=False))


def _pull_json(root: Path, args) -> int:
    """`pulli pull --json`: the same decision the pull makes, as data."""
    from .pull import _classify

    tree = discover(
        root,
        follow_symlinks=not args.no_symlinks,
        max_depth=args.max_depth,
        link_root=getattr(args, "link_root", None),
    )
    set_rels(tree)
    repos = list(iter_repos(tree))
    if not args.no_fetch:
        fetch_all(repos, quiet=True, use_color=_use_color(args))
    collect_status(tree)

    counts: dict[str, int] = {}
    for n in sorted(repos, key=lambda x: x.rel):
        kind = _classify(n)
        counts[kind] = counts.get(kind, 0) + 1
        print(json.dumps({"path": n.rel, "action": kind, **_node_dict(n)}, ensure_ascii=False))

    print(
        json.dumps(
            {"summary": {"total": len(repos), **{f"n_{k}": v for k, v in counts.items()}}},
            ensure_ascii=False,
        )
    )
    return 1 if counts.get("broken") else 0


if __name__ == "__main__":
    raise SystemExit(main())
