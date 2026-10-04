"""Collect git status for each discovered repo.

For each repo we run a small set of git commands to get:
  - branch (current ref name)
  - ahead/behind vs upstream
  - dirty (uncommitted changes)
  - remote url (for display)

We avoid `git status -sb` parsing quirks by using porcelain + rev-list.

The single most important correctness rule in this module: **behind/ahead
must come from one rev-list invocation.** A previous version ran
`rev-list @{u}...HEAD` and then `rev-list HEAD..@{u}` as two separate git
calls. If the remote moved in between (or a fetch interleaved), the two
halves describe different worlds and the result is a bogus ahead/behind
pair. `rev-list --left-right --count A...B` answers both sides
atomically in one process, so that is the only form we use.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from .discovery import RepoNode, iter_repos
from .spinner import Spinner

# ── shared git plumbing ──────────────────────────────────────────────────

#: Fetch exit codes that only mean "the network/remote was unavailable".
#: Anything else (bad config, broken objects, permission denied) is a real
#: failure and should be surfaced.
_OFFLINE_FETCH_CODES = frozenset({
    5,   # couldn't read Username / auth prompt failed
    7,   # couldn't connect
    17,  # remote end hung up unexpectedly
    23,  # write error
    28,  # operation timed out
    35,  # SSL connect error
    56,  # connection reset by peer
    58,  # could not read from remote repository
    65,  # no match for destination
    68,  # ssh: could not resolve hostname
    69,  # ssh: no route to host
    110,  # connection timed out
    111,  # connection refused
})

_env_lock = threading.Lock()
_env_cache: dict[str, str] | None = None


def git_env() -> dict[str, str]:
    """A process-wide git environment that never blocks on prompts.

    Built once and cached: the subprocess API is global state, so mutating
    os.environ per-call would race across the fetch threads. Read-only
    checkouts must not abort because a remote is unreachable.
    """
    global _env_cache
    with _env_lock:
        if _env_cache is None:
            env = dict(os.environ)
            # Non-interactive: never block a fetch on a credential prompt.
            env["GIT_TERMINAL_PROMPT"] = "0"
            env["GIT_ASKPASS"] = env.get("GIT_ASKPASS", "echo")
            env["SSH_ASKPASS"] = env.get("SSH_ASKPASS", "echo")
            # Hanging the SSH connection is better than a 30s frozen tree.
            # We still bound the subprocess with a timeout.
            env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
            # Deterministic, locale-independent parsing of git output.
            env.setdefault("LC_ALL", "C")
            _env_cache = env
        return _env_cache


@contextmanager
def _push_env() -> Iterator[None]:
    """Temporarily apply the sanitized git environment to os.environ.

    Only for strictly sequential code paths. Everywhere else (fetch and
    status both run in a thread pool) the env is passed per-subprocess:
    mutating os.environ there would race across threads and leak
    GIT_TERMINAL_PROMPT into unrelated processes.
    """
    wanted = git_env()
    saved = {k: os.environ.get(k) for k in wanted}
    os.environ.update(wanted)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run_git(
    repo: Path,
    *args: str,
    timeout: int = 30,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run a git command in `repo`, return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env if env is not None else git_env(),
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 128, "", f"git timed out after {timeout}s"
    except FileNotFoundError:
        return 127, "", "git executable not found"
    except OSError as e:  # e.g. cwd vanished between discovery and now
        return 128, "", str(e)


def _git_one(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    """Run git, return stdout (stripped) or '' on failure."""
    rc, out, _ = run_git(repo, *args, env=env)
    return out if rc == 0 else ""


# ── remote urls ──────────────────────────────────────────────────────────


def _remote_url(repo: Path, env: dict[str, str] | None = None) -> str:
    """URL of the preferred remote, or '' when there is none."""
    url = _git_one(repo, "remote", "get-url", "origin", env=env)
    if url:
        return url
    # No `origin` (fresh `git init`, a renamed remote, a submodule whose
    # remote is only recorded in .gitmodules). Fall back to the first
    # configured remote so the tree is still informative.
    out = _git_one(repo, "remote", env=env)
    for name in out.splitlines():
        name = name.strip()
        if not name:
            continue
        url = _git_one(repo, "remote", "get-url", name, env=env)
        if url:
            return url
    return ""


# ── fetch ────────────────────────────────────────────────────────────────


def _classify_fetch(rc: int, err: str) -> str | None:
    """Return None if the fetch genuinely succeeded, else a short reason."""
    if rc == 0:
        return None
    if rc == 128:
        low = err.lower()
        # A hung/failed remote is *not* a repo problem — the tree can still be
        # built from local refs, and pulli stays useful offline.
        if any(s in low for s in ("timed out", "timeout", "could not resolve",
                                  "connection", "network", "unreachable",
                                  "no route to host", "couldn't read",
                                  "authentication", "permission denied (publickey)",
                                  "could not read from remote", "early eof",
                                  "the remote end hung up", "unable to access")):
            return "unreachable"
        if "not a git repository" in low or "does not appear" in low:
            return "not a git repository"
        return err.splitlines()[0] if err else "fetch failed"
    if rc in _OFFLINE_FETCH_CODES:
        return "unreachable"
    return err.splitlines()[0] if err else f"fetch failed (exit {rc})"


def fetch_all(
    nodes: list[RepoNode],
    *,
    timeout: float = 8.0,
    quiet: bool = False,
    workers: int = 8,
    use_color: bool = True,
) -> list[RepoNode]:
    """Fetch every repo so ahead/behind reflects the remote, not the last
    local fetch.

    Fetches run in a bounded thread pool (the default spawns one OS thread
    per repo, which is a lot for a 50-repo tree) and each one is bounded by
    `timeout`. Returns the list of repos whose fetch failed; `node.error` is
    filled in with a short reason, so callers can tell "offline" (harmless)
    from "broken repo" (worth reporting).

    When stderr is a TTY, a spinner animates on stderr with live progress
    ("Fetching remotes… 3/8"), so a multi-second fetch never looks frozen.
    Off a TTY it is a no-op, so piped/captured output stays byte-clean.
    """
    if not nodes:
        return []

    candidates = [n for n in nodes if not n.error]
    failed: list[RepoNode] = []
    lock = threading.Lock()
    env = git_env()

    spinner = Spinner(f"Fetching remotes… 0/{len(candidates)}", use_color=use_color)
    done = 0

    def _do(node: RepoNode) -> None:
        nonlocal done
        rc, _, err = run_git(node.path, "fetch", "--quiet", "--prune",
                             timeout=timeout, env=env)
        reason = _classify_fetch(rc, err)
        if reason is None:
            pass
        else:
            if not quiet:
                print(f"  fetch failed: {node.rel} — {reason}", file=sys.stderr)
            with lock:
                # An unreachable remote is expected (offline, VPN, credentials)
                # and is NOT a broken repo. Recording it as `error` would make
                # pulli report a perfectly healthy repo as broken and exit
                # non-zero, which is exactly wrong on a train.
                node.fetch_failed = True
                node.fetch_reason = reason
                failed.append(node)
        with lock:
            done += 1
            spinner.update(f"Fetching remotes… {done}/{len(candidates)}")

    spinner.start()
    try:
        n_workers = max(1, min(workers, len(candidates)))
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            # pool.map re-raises; _do never raises (git failures are captured
            # as return codes), so consume the iterator to completion.
            for _ in pool.map(_do, candidates):
                pass
    finally:
        spinner.stop()
    return failed


# ── streaming status ────────────────────────────────────────────────────


def fetch_and_status(
    nodes: list[RepoNode],
    *,
    timeout: float = 8.0,
    quiet: bool = False,
    workers: int = 8,
    use_color: bool = True,
    fetch: bool = True,
    on_done=None,
) -> list[RepoNode]:
    """Fetch + collect status for every repo, streaming results as they land.

    Each repo is processed by its own worker: fetch (unless `fetch=False`),
    then collect status, then `on_done(node)` is called so a live renderer
    can show the repo's line the moment it is ready — instead of waiting for
    the slowest remote and printing everything at once.

    Returns the list of repos whose fetch failed (unreachable / offline),
    the same contract as `fetch_all`. `on_done` is called from worker
    threads, so it must be thread-safe (the `LiveTree` locks internally).
    """
    if not nodes:
        return []

    candidates = [n for n in nodes if not n.error]
    failed: list[RepoNode] = []
    lock = threading.Lock()
    env = git_env()

    def _do(node: RepoNode) -> None:
        if fetch:
            rc, _, err = run_git(node.path, "fetch", "--quiet", "--prune",
                                 timeout=timeout, env=env)
            reason = _classify_fetch(rc, err)
            if reason is not None:
                if not quiet:
                    print(f"  fetch failed: {node.rel} — {reason}", file=sys.stderr)
                with lock:
                    node.fetch_failed = True
                    node.fetch_reason = reason
                    failed.append(node)
        _collect_one(node)
        if on_done is not None:
            on_done(node)

    n_workers = max(1, min(workers, len(candidates)))
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        for _ in pool.map(_do, candidates):
            pass
    return failed


# ── status collection ────────────────────────────────────────────────────


def collect_status(root: RepoNode, workers: int = 8) -> None:
    """Fill in branch/ahead/behind/dirty/upstream/error on every repo below
    `root` (and on `root` itself if it is one).

    Repos are inspected concurrently: the work is a handful of small git
    calls per repo, so it is entirely I/O- and process-bound, and doing it
    sequentially made a 50-repo tree visibly sluggish. Each repo writes only
    to its own node, so no locking is needed.
    """
    nodes = list(iter_repos(root))
    if not nodes:
        return
    if len(nodes) == 1 or workers <= 1:
        for n in nodes:
            _collect_one(n)
        return
    n_workers = max(1, min(workers, len(nodes)))
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        for _ in pool.map(_collect_one, nodes):
            pass


def _short_reason(err: str) -> str:
    """First meaningful line of a git error, for a status line."""
    for line in (err or "").splitlines():
        s = line.strip()
        if s and not s.lower().startswith("hint:"):
            return s
    return ""


def _collect_one(node: RepoNode) -> None:
    """Inspect one repo and fill in its status fields."""
    repo = node.path
    # _push_env mutates os.environ, which is process-global, so it cannot be
    # used from the thread pool. Pass the sanitized env explicitly instead.
    _collect_one_locked(node, repo, env=git_env())


def _collect_one_locked(node: RepoNode, repo: Path, env: dict[str, str]) -> None:
    rc, _, err = run_git(repo, "rev-parse", "--is-inside-work-tree", env=env)
    if rc != 0:
        node.error = _short_reason(err) or "not a git work tree"
        return

    node.url = _remote_url(repo, env)

    # Branch / ref name. HEAD may be detached.
    branch = _git_one(repo, "symbolic-ref", "--quiet", "--short", "HEAD", env=env)
    if not branch:
        # detached HEAD — show short sha
        sha = _git_one(repo, "rev-parse", "--short", "HEAD", env=env)
        node.branch = f"({sha})" if sha else "(detached)"
    else:
        node.branch = branch

    # Upstream tracking ref, e.g. origin/main
    upstream = _git_one(
        repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", env=env
    )
    node.upstream = upstream or None

    # ahead/behind vs upstream — ONE rev-list for both sides, so the two
    # numbers can never describe different revisions.
    if upstream:
        counts = _git_one(repo, "rev-list", "--left-right", "--count",
                          f"{upstream}...HEAD", env=env)
        if counts:
            parts = counts.split()
            if len(parts) == 2:
                # `rev-list --left-right --count A...B` prints "<left>\t<right>"
                # i.e. "behind\tahead" for A=upstream, B=HEAD.
                try:
                    node.behind, node.ahead = int(parts[0]), int(parts[1])
                except ValueError:
                    pass

    # in-progress merge/rebase/bisect: pulling is at best rude, at worst
    # corrupting. Surface it as dirty so every consumer skips the repo.
    state = _git_one(repo, "rev-parse", "--git-path", "MERGE_HEAD", env=env)
    if state and (repo / state).exists():
        node.operation = "merge in progress"
    else:
        for marker, label in (
            ("rebase-merge", "rebase in progress"),
            ("rebase-apply", "rebase in progress"),
            ("rebase-merge-interactive", "rebase in progress"),
            ("BISECT_LOG", "bisect in progress"),
            ("CHERRY_PICK_HEAD", "cherry-pick in progress"),
            ("REVERT_HEAD", "revert in progress"),
        ):
            p = _git_one(repo, "rev-parse", "--git-path", marker, env=env)
            if p and (repo / p).exists():
                node.operation = label
                break

    # dirty? porcelain status; parse the file paths for display.
    rc, out, _ = run_git(repo, "status", "--porcelain", env=env)
    if rc == 0:
        node.dirty_files = [_porcelain_path(l) for l in out.splitlines() if l.strip()]
        node.dirty = bool(node.dirty_files)
    else:
        node.dirty = None
        node.dirty_files = []


def _porcelain_path(line: str) -> str:
    """Path from a `git status --porcelain` line (`XY PATH` or
    `XY OLD -> NEW`); strips git's quoting and rename source. Works on
    stripped lines (leading status-column space may already be gone)."""
    s = line.strip()
    path = s[2:].lstrip() if len(s) > 2 else s
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return _unquote(path.strip())


def _unquote(s: str) -> str:
    """Decode git's C-style quoting (`"a b\\t.txt"`) used for odd paths."""
    if len(s) < 2 or not s.startswith('"') or not s.endswith('"'):
        return s
    body = s[1:-1]
    out: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}.get(nxt, nxt))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)
