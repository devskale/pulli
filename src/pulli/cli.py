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
import sys
from pathlib import Path

from . import __version__
from .discovery import discover, iter_repos, set_rels
from .pull import pull
from .status import collect_status, fetch_all, fetch_and_status
from .tree import LiveTree, render

# Flags that belong to a subcommand. Used by _inject_tree to decide whether
# a leading argument is a subcommand or a path/flag for the default one.
_TREE_FLAGS = {
    "--no-symlinks", "--no-fetch", "--no-color", "--max-depth",
    "--dry-run", "--json",
}
_PULL_FLAGS = {"--no-color", "--dry-run", "--json", "--no-fetch"}

SUBCOMMANDS = ("tree", "pull")


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
            action="version",
            version=f"pulli {__version__}",
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

    # `pulli pull` — pull repos that are behind.
    pull_p = sub.add_parser("pull", help="Pull repos that are behind upstream.")
    add_common(pull_p)
    pull_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be pulled without pulling.",
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

    p.add_argument("-V", "--version", action="version", version=f"pulli {__version__}")
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
    if first in SUBCOMMANDS or first in ("-V", "--version", "-h", "--help"):
        return argv
    # Anything that isn't the `pull` subcommand falls through to `tree`.
    return ["tree", *argv]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(
        _inject_tree(list(sys.argv[1:] if argv is None else argv))
    )
    if args.command == "pull":
        return _run_pull(args)
    return _run_tree(args)


def _use_color(args) -> bool:
    # Explicit --no-color wins; otherwise colour only on a TTY, and never
    # when --json (a JSON consumer does not want escape codes in strings).
    if args.no_color or getattr(args, "json", False):
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
    if sys.stdout.isatty():
        # Stream: print the skeleton immediately, then fill each repo's
        # status line in as it becomes ready — no blank-screen wait.
        live = LiveTree(tree, use_color=use_color)
        fetch_and_status(
            repos,
            quiet=True,
            use_color=use_color,
            fetch=not args.no_fetch,
            on_done=live.update,
        )
    else:
        # Not a terminal: no in-place cursor control. Do the work, then
        # print the complete tree once (byte-identical to before).
        if not args.no_fetch:
            fetch_all(repos, quiet=True, use_color=use_color)
        collect_status(tree)
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
