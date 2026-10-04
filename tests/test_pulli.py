"""Tests for pulli.

The interesting behaviour of pulli is *what it decides to do with a repo*,
so most of these build real git repositories in a temp dir and assert on
the decision — not on the formatting. Formatting is checked by a few
snapshot-ish assertions at the end.

Run with:  uv run pytest        (or: python -m pytest)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pulli import cli  # noqa: E402
from pulli.discovery import discover, iter_repos, set_rels  # noqa: E402
from pulli.pull import _classify, _first_meaningful_line, pull  # noqa: E402
from pulli.status import collect_status, fetch_all  # noqa: E402
from pulli.tree import _shorten_url, render  # noqa: E402


# ── fixtures ─────────────────────────────────────────────────────────────


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(path), capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _isolated_git_config(tmp_path_factory, monkeypatch):
    """A private global git config so tests never touch the user's, and so
    `git init` doesn't depend on machine-level defaults."""
    cfg = tmp_path_factory.mktemp("gitcfg") / "gitconfig"
    cfg.write_text(
        "[user]\n\tname=t\n\temail=t@example.com\n"
        "[init]\n\tdefaultBranch=main\n"
        "[advice]\n\tdetachedHead=false\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")


class Lab:
    """A directory of real git repos in every state pulli must reason about."""

    def __init__(self, root: Path):
        self.root = root
        self.upstream = root / "upstream"
        self.bare = root / "origin.git"
        self.upstream.mkdir()
        _git(self.upstream, "init", "-q", ".")
        (self.upstream / "a.md").write_text("one\n")
        _git(self.upstream, "add", "-A")
        _git(self.upstream, "commit", "-qm", "one")
        _git(root, "init", "-q", "--bare", "origin.git")
        self._push("main")

    def _push(self, branch: str) -> None:
        _git(self.upstream, "push", "-q", str(self.bare), branch)

    def advance_upstream(self) -> None:
        """Add an upstream commit and publish it — the thing that makes
        local clones 'behind'."""
        (self.upstream / "b.md").write_text("two\n")
        _git(self.upstream, "add", "-A")
        _git(self.upstream, "commit", "-qm", "two")
        self._push("main")

    def clone(self, name: str) -> Path:
        d = self.root / name
        _git(self.root, "clone", "-q", str(self.bare), name)
        return d

    def commit_in(self, repo: Path, fname: str, msg: str) -> None:
        (repo / fname).write_text(f"{msg}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", msg)

    def head(self, repo: Path) -> str:
        return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def lab(tmp_path) -> Lab:
    return Lab(tmp_path)


def statuses(root: Path) -> dict[str, object]:
    """path -> RepoNode for every repo under root, with status filled in."""
    tree = discover(root)
    set_rels(tree)
    collect_status(tree)
    return {n.rel: n for n in iter_repos(tree)}


def actions(root: Path, **kw) -> tuple[dict[str, str], int, str]:
    """Run pull() and return (path -> action bucket, exit code, output).

    The buckets are read off the same tree pull() built, so the decision
    under test is the decision that was acted on.
    """
    import contextlib
    import io

    tree = discover(root)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = pull(root, use_color=False, **kw)
    set_rels(tree)
    collect_status(tree)
    return {n.rel: _classify(n) for n in iter_repos(tree)}, rc, buf.getvalue()


# ── discovery ────────────────────────────────────────────────────────────


def test_finds_repos_at_several_depths(lab):
    lab.clone("a")
    (lab.root / "mid").mkdir()
    (lab.root / "mid" / "b").mkdir()
    _git(lab.root / "mid" / "b", "init", "-q", ".")
    found = statuses(lab.root)
    assert {"a", "mid/b"} <= set(found)


def test_nested_repo_inside_repo_is_found(lab):
    outer = lab.clone("outer")
    (outer / "vendor" / "inner").mkdir(parents=True)
    _git(outer / "vendor" / "inner", "init", "-q", ".")
    assert "outer/vendor/inner" in statuses(lab.root)


def test_nested_repo_in_vendored_dir_is_found(lab):
    """A repo checked out under vendor/ is still a repo with its own remote."""
    d = lab.root / "vendor" / "inner"
    d.mkdir(parents=True)
    _git(d, "init", "-q", ".")
    assert "vendor/inner" in statuses(lab.root)


def test_symlinked_repo_is_followed_and_marked(lab):
    """A link to a repo outside the tree is shown, and marked as a link."""
    outside = lab.root.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    _git(outside, "init", "-q", ".")
    os.symlink(outside, lab.root / "link")
    tree = discover(lab.root)
    set_rels(tree)
    link = [n for n in iter_repos(tree) if n.name == "link"][0]
    assert link.is_symlink
    assert link.path == outside.resolve()


def test_symlink_does_not_duplicate_a_repo(lab):
    """The same work tree reachable twice must be visited once, or `pull`
    would fast-forward it twice. The direct visit wins: if a repo is both
    ~/code/pi and ~/clones/pi, the user means the real directory."""
    lab.clone("real")
    os.symlink(lab.root / "real", lab.root / "link")
    tree = discover(lab.root)
    set_rels(tree)
    names = {n.name for n in iter_repos(tree)}
    assert "real" in names          # the direct visit wins
    assert "link" not in names      # and the link is not shown as well


def test_no_symlinks_flag_skips_links(lab):
    lab.clone("real")
    os.symlink(lab.root / "real", lab.root / "link")
    tree = discover(lab.root, follow_symlinks=False)
    assert "link" not in {n.name for n in iter_repos(tree)}
    assert "real" in {n.name for n in iter_repos(tree)}


def test_symlink_loop_terminates(lab):
    """A link back to an ancestor must not send the walk into itself."""
    (lab.root / "sub").mkdir()
    os.symlink(lab.root, lab.root / "sub" / "up")
    os.symlink(lab.root, lab.root / "self")
    tree = discover(lab.root, max_depth=10)
    assert tree is not None  # did not hang or crash


def test_bare_repo_is_marked_and_excluded(lab):
    _git(lab.root, "init", "-q", "--bare", "mirror.git")
    tree = discover(lab.root)
    set_rels(tree)
    bare = [n for n in tree.children if n.name == "mirror.git"][0]
    assert bare.is_bare
    # the bare repo is a child node, but never a pullable repo
    assert all(n.path != bare.path for n in iter_repos(tree))


def test_bare_repo_contents_not_expanded(lab):
    _git(lab.root, "init", "-q", "--bare", "mirror.git")
    tree = discover(lab.root)
    bare = [n for n in tree.children if n.name == "mirror.git"][0]
    assert bare.children == []


def test_pruned_dir_shown_as_leaf_but_nested_repo_found(lab):
    """node_modules is not expanded into thousands of plain dirs, but a git
    repo checked out inside one is still discovered and pullable."""
    d = lab.root / "node_modules" / "pkg"
    d.mkdir(parents=True)
    _git(d, "init", "-q", ".")
    (d / "src").mkdir()  # a plain dir that must NOT become a node
    tree = discover(lab.root)
    set_rels(tree)
    nm = [n for n in tree.children if n.name == "node_modules"][0]
    assert nm.is_repo is False
    names = {c.name for c in nm.children}
    assert "pkg" in names          # the repo is found
    assert "src" not in names      # its plain subdirs are not


def test_max_depth_limits_the_walk(lab):
    deep = lab.root / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    _git(deep, "init", "-q", ".")
    assert "a/b/c/d" in statuses(lab.root)
    shallow = discover(lab.root, max_depth=2)
    set_rels(shallow)
    names = {n.rel for n in iter_repos(shallow)}
    assert "a/b/c/d" not in names


def test_submodule_is_its_own_node(lab):
    """A submodule is a real repo on disk with its own remote."""
    inner = lab.root / "inner"
    inner.mkdir()
    _git(inner, "init", "-q", ".")
    (inner / "x").write_text("x\n")
    _git(inner, "add", "-A")
    _git(inner, "commit", "-qm", "x")
    inner_bare = lab.root / "inner.git"
    _git(lab.root, "init", "-q", "--bare", "inner.git")
    _git(inner, "push", "-q", str(inner_bare), "main")

    outer = lab.root / "outer"
    _git(lab.root, "clone", "-q", str(lab.bare), "outer")
    _git(outer, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
          str(inner_bare), "sm")
    _git(outer, "commit", "-qm", "add submodule")

    tree = discover(lab.root)
    set_rels(tree)
    outer_node = [n for n in iter_repos(tree) if n.name == "outer"][0]
    sm = [c for c in outer_node.children if c.is_submodule]
    assert sm, "submodule node missing"
    assert sm[0].name == "sm"


# ── status ───────────────────────────────────────────────────────────────


def test_ahead_behind_is_correct(lab):
    r = lab.clone("r")
    lab.advance_upstream()
    _git(r, "fetch", "-q")
    assert statuses(lab.root)["r"].behind == 1
    assert statuses(lab.root)["r"].ahead == 0


def test_ahead_only(lab):
    r = lab.clone("r")
    lab.advance_upstream()
    _git(r, "fetch", "-q")
    lab.commit_in(r, "local.md", "local")
    n = statuses(lab.root)["r"]
    assert (n.behind, n.ahead) == (1, 1)


def test_no_upstream_is_reported_as_none(lab):
    r = lab.clone("r")
    _git(r, "remote", "remove", "origin")
    n = statuses(lab.root)["r"]
    assert n.upstream is None
    assert (n.behind, n.ahead) == (None, None)


def test_dirty_files_are_listed(lab):
    r = lab.clone("r")
    (r / "a.md").write_text("changed\n")
    (r / "untracked").write_text("new\n")
    n = statuses(lab.root)["r"]
    assert n.dirty
    assert set(n.dirty_files) == {"a.md", "untracked"}


def test_rename_path_is_the_new_name(lab):
    r = lab.clone("r")
    _git(r, "mv", "a.md", "renamed.md")
    n = statuses(lab.root)["r"]
    assert n.dirty_files == ["renamed.md"]


def test_detached_head(lab):
    r = lab.clone("r")
    _git(r, "checkout", "-q", "--detach", "HEAD")
    n = statuses(lab.root)["r"]
    assert n.branch.startswith("(")


def test_merge_in_progress_is_detected(lab):
    r = lab.clone("r")
    _git(lab.upstream, "checkout", "-q", "-b", "feat")
    (lab.upstream / "c.md").write_text("c\n")
    _git(lab.upstream, "add", "-A")
    _git(lab.upstream, "commit", "-qm", "feat")
    lab._push("feat")
    _git(r, "fetch", "-q", "origin")
    lab.commit_in(r, "local.md", "local")
    _git(r, "merge", "--no-commit", "--no-ff", "origin/feat")
    assert (r / ".git" / "MERGE_HEAD").exists()
    n = statuses(lab.root)["r"]
    assert n.operation == "merge in progress"


def test_fetch_failure_is_not_a_broken_repo(lab):
    """Offline is a normal condition, not a broken repo — it must not make
    the command fail."""
    r = lab.clone("r")
    _git(r, "remote", "set-url", "origin", str(lab.root / "gone.git"))
    tree = discover(lab.root)
    set_rels(tree)
    repos = list(iter_repos(tree))
    fetch_all(repos, quiet=True)
    collect_status(tree)
    n = repos[0]
    assert n.fetch_failed
    assert n.error is None
    _, rc, out = actions(lab.root)
    assert rc == 0
    assert "unreachable" in out


# ── pull decisions ───────────────────────────────────────────────────────


def test_dry_run_changes_nothing(lab):
    r = lab.clone("r")
    lab.advance_upstream()
    _git(r, "fetch", "-q")
    before = lab.head(r)
    kinds, rc, out = actions(lab.root, dry_run=True)
    assert kinds["r"] == "pullable"
    assert "would pull" in out
    assert lab.head(r) == before


def test_pull_fast_forwards(lab):
    r = lab.clone("r")
    lab.advance_upstream()
    _git(r, "fetch", "-q")
    actions(lab.root)
    assert (r / "b.md").exists()
    assert _git(r, "rev-list", "--count", "HEAD..origin/main") == "0"


def test_dirty_repo_is_not_pulled(lab):
    r = lab.clone("r")
    lab.advance_upstream()
    _git(r, "fetch", "-q")
    before = lab.head(r)
    (r / "a.md").write_text("local edit\n")
    kinds, rc, out = actions(lab.root)
    assert kinds["r"] == "dirty"
    assert "skipping pull" in out
    assert lab.head(r) == before


def test_diverged_repo_is_not_pulled(lab):
    r = lab.clone("r")
    lab.advance_upstream()
    _git(r, "fetch", "-q")
    lab.commit_in(r, "local.md", "local")
    before = lab.head(r)
    kinds, rc, out = actions(lab.root)
    assert kinds["r"] == "diverged"
    assert "diverged" in out
    assert lab.head(r) == before  # untouched: no merge, no rebase


def test_ahead_only_repo_is_left_alone(lab):
    r = lab.clone("r")
    lab.commit_in(r, "local.md", "local")
    before = lab.head(r)
    kinds, _, out = actions(lab.root)
    assert kinds["r"] == "ahead"
    assert lab.head(r) == before


def test_merge_in_progress_is_never_pulled(lab):
    r = lab.clone("r")
    _git(lab.upstream, "checkout", "-q", "-b", "feat")
    (lab.upstream / "c.md").write_text("c\n")
    _git(lab.upstream, "add", "-A")
    _git(lab.upstream, "commit", "-qm", "feat")
    lab._push("feat")
    _git(r, "fetch", "-q", "origin")
    lab.commit_in(r, "local.md", "local")
    _git(r, "merge", "--no-commit", "--no-ff", "origin/feat")
    before = lab.head(r)
    kinds, _, out = actions(lab.root)
    assert kinds["r"] == "busy"
    assert "merge in progress" in out
    assert lab.head(r) == before
    assert (r / ".git" / "MERGE_HEAD").exists()  # merge left intact


def test_up_to_date_repos_are_silent(lab):
    lab.clone("r")
    _, _, out = actions(lab.root)
    # 'upstream' is the lab's own fixture repo and is the only up-to-date one
    assert "Pulled 0" in out
    assert "up to date" in out


def test_no_repos_found(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    _, rc, out = actions(empty)
    assert rc == 0
    assert "No git repos found." in out


def test_broken_repo_exits_nonzero(lab, tmp_path):
    """A directory that claims to be a repo but is broken is a real problem
    and must be visible in the exit code."""
    lab.clone("ok")
    broken = lab.root / "broken"
    (broken / ".git").mkdir(parents=True)
    (broken / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    kinds, rc, out = actions(lab.root)
    assert "broken" in kinds
    assert rc == 1


# ── helpers ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:devskale/klark0.git", "devskale/klark0"),
        ("https://github.com/devskale/klark0.git", "https://github.com/devskale/klark0"),
        ("https://github.com/devskale/klark0", "https://github.com/devskale/klark0"),
        ("https://user:token@github.com/devskale/klark0.git", "https://github.com/devskale/klark0"),
        ("https://ghp_secret@github.com/devskale/klark0.git", "https://github.com/devskale/klark0"),
        ("/local/path/repo.git", "/local/path/repo"),
        ("", ""),
    ],
)
def test_shorten_url_strips_credentials(url, expected):
    """Credentials must never survive into display text.

    The host prefix is only dropped for scp-style `git@host:owner/repo`
    URLs; a full https:// URL keeps its scheme, so the assertion is about
    the absence of the secret, not about shortening.
    """
    out = _shorten_url(url)
    assert out == expected
    for secret in ("token", "SECRETTOKEN", "ghp_secret", "user:"):
        assert secret not in out


def test_credentials_never_reach_the_output(lab):
    r = lab.clone("r")
    _git(r, "remote", "set-url", "origin", "https://user:SECRETTOKEN@github.com/o/r.git")
    tree = discover(lab.root)
    set_rels(tree)
    collect_status(tree)
    out = render(tree, use_color=False)
    assert "SECRETTOKEN" not in out
    assert "user:" not in out


def test_first_meaningful_line_skips_hints():
    text = (
        "hint: Diverging branches can't be fast-forwarded, you need to either:\n"
        "hint:\n"
        "hint: \tgit merge --no-ff\n"
        "fatal: Not possible to fast-forward, aborting.\n"
    )
    assert _first_meaningful_line(text).startswith("fatal:")


# ── rendering ────────────────────────────────────────────────────────────


def test_render_marks_states(lab):
    lab.clone("clean")
    r2 = lab.clone("dirty")
    (r2 / "a.md").write_text("edit\n")
    tree = discover(lab.root)
    set_rels(tree)
    collect_status(tree)
    out = render(tree, use_color=False)
    assert "clean" in out
    assert "dirty" in out


def test_no_upstream_renders_placeholder(lab):
    r = lab.clone("r")
    _git(r, "remote", "remove", "origin")
    tree = discover(lab.root)
    set_rels(tree)
    collect_status(tree)
    out = render(tree, use_color=False)
    assert "↓0 ↑0" not in out
    assert "·  ·" in out


# ── CLI ──────────────────────────────────────────────────────────────────


def test_cli_injects_tree_subcommand():
    assert cli._inject_tree([]) == ["tree"]
    assert cli._inject_tree(["~/code"]) == ["tree", "~/code"]
    assert cli._inject_tree(["--no-color", "."]) == ["tree", "--no-color", "."]
    assert cli._inject_tree(["pull", "."]) == ["pull", "."]
    assert cli._inject_tree(["tree", "."]) == ["tree", "."]
    assert cli._inject_tree(["-V"]) == ["-V"]


def test_cli_missing_root_exits_2(tmp_path):
    assert cli.main([str(tmp_path / "nope")]) == 2


def test_cli_file_as_root_exits_2(tmp_path):
    f = tmp_path / "afile"
    f.write_text("x")
    assert cli.main([str(f)]) == 2


def test_cli_json_is_parseable(lab, capsys):
    lab.clone("r")
    assert cli.main(["--json", "--no-fetch", str(lab.root)]) == 0
    out = capsys.readouterr().out
    recs = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert recs and recs[0]["repo"] is True


def test_cli_pull_json_does_not_pull(lab, capsys):
    r = lab.clone("r")
    lab.advance_upstream()
    _git(r, "fetch", "-q")
    before = lab.head(r)
    assert cli.main(["pull", "--json", "--no-fetch", str(lab.root)]) == 0
    capsys.readouterr()
    assert lab.head(r) == before


# ── spinner ──────────────────────────────────────────────────────────────


class _FakeStream:
    """A stderr stand-in that records writes and reports its TTY-ness."""

    def __init__(self, isatty: bool) -> None:
        self._isatty = isatty
        self.buf: list[str] = []

    def isatty(self) -> bool:
        return self._isatty

    def write(self, s: str) -> None:
        self.buf.append(s)

    def flush(self) -> None:
        pass

    @property
    def text(self) -> str:
        return "".join(self.buf)


def test_spinner_is_noop_off_a_tty():
    """Captured/piped stderr must stay byte-clean: no animation, no ESC."""
    from pulli.spinner import Spinner

    stream = _FakeStream(isatty=False)
    sp = Spinner("Fetching remotes… 0/3", stream=stream)
    sp.start()
    sp.update("Fetching remotes… 1/3")
    sp.stop()
    assert stream.text == ""


def test_spinner_animates_on_a_tty_and_clears():
    from pulli.spinner import Spinner

    stream = _FakeStream(isatty=True)
    sp = Spinner("Fetching remotes… 0/3", stream=stream)
    sp.start()
    # Let a couple of frames land, then stop and confirm the line is wiped.
    import time
    time.sleep(0.05)
    sp.stop()
    assert "Fetching remotes" in stream.text
    assert "\r" in stream.text          # carriage-return rewrites
    assert "\x1b[2K" in stream.text     # erase-to-end-of-line on stop


def test_spinner_color_toggle():
    from pulli.spinner import Spinner

    colored = _FakeStream(isatty=True)
    sp = Spinner("x", stream=colored, use_color=True)
    sp._draw()
    assert "\x1b[36m" in colored.text  # cyan glyph
    assert "\x1b[2m" in colored.text   # dim message

    plain = _FakeStream(isatty=True)
    sp2 = Spinner("x", stream=plain, use_color=False)
    sp2._draw()
    assert "\x1b[" not in plain.text


def test_spinner_not_started_when_disabled():
    """start() on a non-TTY stream must not spawn a thread at all."""
    from pulli.spinner import Spinner

    stream = _FakeStream(isatty=False)
    sp = Spinner("x", stream=stream)
    sp.start()
    assert sp._thread is None
    sp.stop()
    assert stream.text == ""


def test_clip_ansi_truncates_to_width_preserving_colors():
    """A line wider than the terminal must be clipped, or it wraps and
    breaks the live tree's in-place cursor arithmetic."""
    from pulli.tree import _clip_ansi, _visible_width

    # A colored line wider than a narrow terminal.
    line = "\x1b[2mhttps://github.com/Kilo-Org/kilocode\x1b[0m  " \
           "\x1b[1m(detached)\x1b[0m  ·  ·  " \
           "\x1b[33m◐ offline — unreachable\x1b[0m"
    assert _visible_width(line) > 40
    clipped = _clip_ansi(line, 40)
    assert _visible_width(clipped) <= 40
    # Colors survive the clip and the truncated region is reset.
    assert "\x1b[2m" in clipped
    assert clipped.endswith("\x1b[0m")
    # A short line passes through untouched.
    assert _clip_ansi("short", 40) == "short"
