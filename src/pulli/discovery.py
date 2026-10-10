"""Discover git repositories under a root directory.

Rules (confirmed with the user):
  1b. When we hit a `.git`, we record that dir as a repo AND keep recursing
      into it to find nested repos (a repo can live inside another repo).
  2.  Symlinks are followed by default and marked clearly in the output.
      `--no-symlinks` turns following off.
  3.  Submodules are shown as separate nodes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


@dataclass
class RepoNode:
    """A node in the discovered tree.

    May be a git repo (is_repo=True), a plain directory (is_repo=False),
    or a submodule (is_submodule=True).

    Two paths are tracked, and they are not interchangeable:

    * `path` — where git commands must run and what gets reported. Always
      the *real* (symlink-resolved) path, so `pulli` never pulls the same
      working tree twice because it was reachable by two links.
    * `link_path` — the path as the user wrote it, i.e. what a human expects
      to see in the tree. Falls back to `path` when there is no symlink.
    """

    path: Path  # real path: use this for git commands
    name: str
    depth: int  # 0 = the root arg itself
    parent: "RepoNode | None" = None
    children: list["RepoNode"] = field(default_factory=list)
    is_symlink: bool = False
    symlink_target: str | None = None  # raw readlink target if is_symlink
    link_path: Path | None = None  # path as displayed (default: same as `path`)
    is_submodule: bool = False
    is_repo: bool = False  # True if this dir has a git work tree
    is_alias: bool = False  # symlink whose target is listed elsewhere
    is_bare: bool = False  # True for bare repos — nothing to pull there
    # status fields filled in by status.py
    branch: str | None = None
    ahead: int | None = None
    behind: int | None = None
    dirty: bool | None = None
    dirty_files: list[str] = field(default_factory=list)  # paths from status --porcelain
    untracked_only: bool = False  # dirty, but only untracked files — safe to fast-forward
    upstream: str | None = None
    url: str = ""  # remote url (credentials never stored — scrubbed at render)
    is_fork: bool = False  # origin is a fork: another remote points at a repo of the same name (likely the upstream parent)
    operation: str | None = None  # "merge in progress", "rebase in progress", …
    fetch_failed: bool = False  # fetch did not succeed (offline / unreachable)
    fetch_reason: str | None = None  # why the fetch failed
    error: str | None = None

    def set_rel(self, root: Path) -> None:
        """Display path relative to `root`.

        Built from the *link* path, not the real one: when the walk starts
        at a resolved root, the real paths have no common suffix with the
        root the user typed, and every entry would print as an absolute
        path into the symlink target.

        The root itself gets "." rather than "" — `pulli pull .` printing
        `✗ .  unreachable` is a message about the repo, and a bare "" is
        not a name a human can act on.
        """
        base = self.link_path or self.path
        if base == root:
            self._rel = "."
            return
        try:
            self._rel = str(base.relative_to(root))
        except ValueError:
            self._rel = str(base)

    @property
    def rel(self) -> str:
        # Deliberately loud: a caller that forgets set_rels() used to get a
        # silently-wrong absolute path, which then leaked into the report
        # and into sort order. Failing here points at the actual mistake.
        try:
            return self._rel
        except AttributeError:
            raise RuntimeError(
                f"RepoNode.rel read before set_rels(): {self.path}"
            ) from None


def _readlink(p: Path) -> str | None:
    """The raw target of a symlink, for display. Never raises."""
    try:
        return os.readlink(p)
    except OSError:
        return None


def _is_git_dir(p: Path) -> bool:
    """A directory is a git repo if it has a .git entry (dir for normal
    repos, file for worktrees/submodules pointing elsewhere)."""
    try:
        return (p / ".git").exists()
    except OSError:
        return False


def _is_bare_repo(p: Path) -> bool:
    """True if `p` is itself a git dir (bare repo or `.git` directory).

    Bare repos have no working tree: there is nothing to pull, and running
    `git fetch` on one just writes objects nobody reads — so they are
    recorded but excluded from fetch/pull.
    """
    try:
        return p.is_dir() and _is_git_dir_inner(p)
    except OSError:
        return False


def _is_git_dir_inner(p: Path) -> bool:
    """Cheap structural test for a git directory (no subprocess)."""
    for required in ("HEAD", "objects", "refs"):
        if not (p / required).exists():
            return False
    try:
        return (p / "HEAD").read_text(errors="replace").startswith("ref:")
    except OSError:
        return False


# Directories we never descend into. They may still be shown as leaf nodes
# (so the user sees they exist) but their contents are not expanded. This
# keeps the tree readable on real projects (node_modules alone can be huge).
_PRUNED_DIRS: frozenset[str] = frozenset({
    # JS/TS
    "node_modules", ".pnpm", ".parcel-cache", ".turbo", ".svelte-kit",
    ".next", ".nuxt", ".astro", ".remax", "dist", "build", "out",
    # Python
    ".venv", "venv", "env", "__pycache__", ".mypy_cache", ".ruff_cache",
    ".pytest_cache", ".tox", ".eggs",
    # Rust / Go
    "target", "pkg",
    # VCS / metadata
    ".git", ".hg", ".svn", ".pi", ".claude", ".idea", ".vscode",
    # misc
    ".cache", ".gradle", ".terraform", "coverage", ".nyc_output",
    # dependency trees that are enormous and never contain repos
    "vendor", "Pods", "DerivedData", "site-packages", ".direnv",
    ".bundle", ".yarn", ".cargo", ".rustup", "go", "obj", "bin",
})


def _is_pruned(name: str) -> bool:
    """True if `name` is a directory we should not descend into."""
    if name in _PRUNED_DIRS:
        return True
    if name.endswith(".egg-info"):
        return True
    return False


def _read_submodule_paths(repo_root: Path) -> list[str]:
    """Parse .gitmodules and return the list of submodule `path` values
    that are actually checked out (have a .git file/dir)."""
    gm = repo_root / ".gitmodules"
    if not gm.is_file():
        return []
    out: list[str] = []
    cur_path: str | None = None
    try:
        for line in gm.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s.startswith("[submodule"):
                if cur_path and (repo_root / cur_path / ".git").exists():
                    out.append(cur_path)
                cur_path = None
            elif s.startswith("path") and "=" in s:
                cur_path = s.split("=", 1)[1].strip()
        if cur_path and (repo_root / cur_path / ".git").exists():
            out.append(cur_path)
    except OSError:
        pass
    return out


def discover(
    root: Path,
    *,
    follow_symlinks: bool = True,
    max_depth: int = 50,
    link_root: Path | None = None,
) -> RepoNode | None:
    """Walk `root` and build a tree of RepoNodes.

    Returns the root RepoNode (which may or may not itself be a repo), or
    None if root doesn't exist / isn't a directory.

    `link_root` is the path as the user spelled it. The walk runs on the
    resolved `root` (symlink-safe), while display paths are built from
    `link_root`, so `pulli ~/link-to-code` reports `~/link-to-code/foo`
    instead of leaking the resolved target.
    """
    if not root.is_dir():
        return None

    try:
        root_is_repo = _is_git_dir(root)
    except OSError:
        root_is_repo = False

    display_base = link_root if link_root is not None else root

    root_node = RepoNode(
        path=root,
        link_path=display_base,
        name=display_base.name or str(display_base),
        depth=0,
        is_repo=root_is_repo,
        is_bare=(not root_is_repo) and _is_bare_repo(root),
    )
    root_node.set_rel(display_base)

    # Real paths we've already walked, so a directory is never expanded
    # twice. Seeded with the root.
    visited: set[Path] = {root}

    _walk(
        root,
        display_base,
        root_node,
        max_depth=max_depth,
        visited=visited,
        inside_repo=root_is_repo,
        depth=1,
    )
    if follow_symlinks:
        _walk_links(root_node, root, display_base, max_depth, visited)
    return root_node


def _walk_links(
    node: RepoNode,
    real_dir: Path,
    display_dir: Path,
    max_depth: int,
    visited: set[Path],
) -> None:
    """Second pass: expand symlinks in an already-walked tree.

    Symlinks are handled *after* the plain walk on purpose. During the walk
    a link can claim a path that a real directory would have reached
    moments later — `link -> ../real` sorts before `real`, so the link
    would win and the actual directory would disappear from the tree, which
    is backwards. Walking links last means a real directory always wins, and
    a link is only shown when nothing else reaches its target.
    """
    # As in _walk: a node deeper than max_depth is not expanded. The link
    # pass must respect the same limit as the plain walk, or --max-depth
    # silently stops applying the moment a symlink is followed.
    if node.depth + 1 > max_depth:
        return
    try:
        entries = sorted(os.listdir(real_dir), key=lambda s: s.lower())
    except OSError:
        entries = []

    for name in entries:
        if name == ".git":
            continue
        link = real_dir / name
        try:
            if not link.is_symlink():
                continue
            target = link.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not target.is_dir():
            continue

        display = display_dir / name
        already = target in visited
        if not already:
            visited.add(target)

        try:
            is_repo = _is_git_dir(target)
        except OSError:
            continue

        # A link whose target was already walked is an *alias*: the
        # directory exists here, but its repo is the one already listed
        # elsewhere. Show the link so the tree still reflects what is on
        # disk, but not as a second pull target — `pull` must never
        # fast-forward the same work tree twice.
        child = RepoNode(
            path=target,
            link_path=display,
            name=name,
            depth=node.depth + 1,
            parent=node,
            is_symlink=True,
            symlink_target=_readlink(link),
            is_repo=is_repo and not already,
            is_alias=already,
        )
        node.children.append(child)

        if is_repo and not already:
            _add_submodules(child, target, display, node.depth + 1, visited)
        if not already:
            _walk_links(child, target, display, max_depth, visited)

    node.children.sort(key=lambda c: c.name.lower())


def _add_submodules(
    parent: RepoNode,
    repo_root: Path,
    display: Path,
    depth: int,
    visited: set[Path],
) -> None:
    """Attach each checked-out submodule to `parent` as its own node."""
    for sm_rel in _read_submodule_paths(repo_root):
        sm_dir = repo_root / sm_rel
        if not sm_dir.is_dir():
            continue
        try:
            visited.add(sm_dir.resolve())
        except (OSError, RuntimeError):
            pass
        parent.children.append(
            RepoNode(
                path=sm_dir,
                link_path=display / sm_rel,
                name=Path(sm_rel).name,
                depth=depth + 1,
                parent=parent,
                is_submodule=True,
                is_repo=True,
            )
        )


def _walk(
    current_dir: Path,
    display_dir: Path,
    current_node: RepoNode,
    *,
    max_depth: int,
    visited: set[Path],
    inside_repo: bool = False,
    depth: int,
    repos_only: bool = False,
    search_pruned: bool = False,
) -> None:
    """Recurse into current_dir.

    - Outside any repo: every directory becomes a RepoNode so the tree
      structure is preserved for display/selection.
    - Inside a repo: only nested repos and submodules become nodes; plain
      directories inside a repo's working tree are skipped (we don't want
      to expand the repo's internal src/ layout).

    `depth` is threaded explicitly: a submodule node is *displayed* as a
    child of its parent repo, but on disk it is nested one level deeper, so
    computing depth from the display parent (as an earlier version did)
    inflated it and made `--max-depth` cut walks short.

    `repos_only` descends through pruned dirs (node_modules, vendor, …)
    looking for nested repos without recording the plain directories on the
    way — the point of those dirs is that they are not part of the tree.
    """
    if depth > max_depth:
        return

    try:
        entries = sorted(os.listdir(current_dir), key=lambda s: s.lower())
    except (PermissionError, NotADirectoryError, OSError):
        return

    for name in entries:
        if name == ".git":
            continue  # never descend into the .git metadata dir

        link = current_dir / name  # real path (parent is already real)
        display = display_dir / name  # what the user sees

        traverse = link
        try:
            if not link.is_dir():
                continue
        except OSError:
            continue

        # Mark every directory we descend into as seen, so no directory is
        # ever expanded twice — `pulli pull` must not fast-forward the same
        # work tree from two entries.
        if traverse in visited:
            continue
        visited.add(traverse)

        try:
            is_dir = link.is_dir()
        except OSError:
            continue
        if not is_dir or link.is_symlink():
            continue  # symlinks are handled in the second pass

        # Guard against permission errors on unreadable dirs.
        try:
            is_repo = _is_git_dir(traverse)
        except OSError:
            continue

        if is_repo:
            child = RepoNode(
                path=traverse,
                link_path=display,
                name=name,
                depth=depth,
                parent=current_node,
                is_repo=True,
            )
            current_node.children.append(child)

            # 3. Submodules: add each checked-out submodule as a child node.
            for sm_rel in _read_submodule_paths(traverse):
                sm_dir = traverse / sm_rel  # real path: git commands run here
                if not sm_dir.is_dir():
                    continue
                sm_node = RepoNode(
                    path=sm_dir,
                    link_path=display_dir / sm_rel,
                    name=Path(sm_rel).name,
                    depth=depth + 1,
                    parent=child,
                    is_submodule=True,
                    is_repo=True,
                )
                child.children.append(sm_node)
                # Claim it so the walk below can't rediscover it.
                try:
                    visited.add(sm_dir.resolve())
                except (OSError, RuntimeError):
                    pass

            # 1b: recurse INTO this repo looking for nested repos. Pass
            # inside_repo=True so plain dirs inside it are skipped — but
            # pruned dirs inside the repo (vendor/, node_modules/) are still
            # searched, because a repo checked out under one is a real repo
            # with its own remote.
            _walk(
                traverse,
                display,
                child,
                max_depth=max_depth,
                visited=visited,
                inside_repo=True,
                depth=depth + 1,
                search_pruned=True,
            )
        elif not inside_repo and not repos_only:
            # Plain dir OUTSIDE any repo — record it and recurse to keep
            # the tree structure.
            is_bare = _is_bare_repo(traverse) if name.endswith(".git") else False
            child = RepoNode(
                path=traverse,
                link_path=display,
                name=name,
                depth=depth,
                parent=current_node,
                is_repo=False,
                is_bare=is_bare,
            )
            current_node.children.append(child)
            if is_bare:
                # A bare repo's interior (objects/, refs/) is noise; there is
                # no working tree to show or pull.
                continue
            _walk(
                traverse,
                display,
                child,
                max_depth=max_depth,
                visited=visited,
                inside_repo=False,
                depth=depth + 1,
                search_pruned=_is_pruned(name),
                repos_only=_is_pruned(name),
            )
        elif search_pruned and not repos_only and _is_pruned(name):
            # A pruned dir inside a repo: shown as a node (so the user can
            # see there is a vendored tree) and searched for repos, but its
            # plain contents are never expanded.
            child = RepoNode(
                path=traverse,
                link_path=display,
                name=name,
                depth=depth,
                parent=current_node,
                is_repo=False,
            )
            current_node.children.append(child)
            _walk(
                traverse,
                display,
                child,
                max_depth=max_depth,
                visited=visited,
                inside_repo=inside_repo,
                depth=depth + 1,
                search_pruned=True,
                repos_only=True,
            )
        # else: plain dir INSIDE a repo — skip entirely.


def set_rels(root: RepoNode, *, base: Path | None = None) -> None:
    """Fix up the .rel display paths for every node relative to `base`
    (default: the root's own resolved path)."""

    def _fix(n: RepoNode) -> None:
        n.set_rel(base if base is not None else root.path)
        for c in n.children:
            _fix(c)

    _fix(root)


def iter_repos(root: RepoNode, *, include_bare: bool = False) -> Iterator[RepoNode]:
    """Yield every RepoNode that is a pullable git work tree.

    Bare repos (`repo.git/`) are excluded by default: they have no working
    tree, so there is nothing to pull. Plain dirs are never yielded.
    """
    def _walk_node(n: RepoNode) -> Iterator[RepoNode]:
        if n.is_repo and (include_bare or not n.is_bare):
            yield n
        for c in n.children:
            yield from _walk_node(c)

    yield from _walk_node(root)
