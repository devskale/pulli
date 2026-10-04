# pulli

Discover git repositories under a directory, show their status, and
fast-forward the ones that are behind upstream.

## Install

```bash
cd ~/code/puller
uv tool install --force .
```

This puts `pulli` on your PATH (`~/.local/bin/pulli`).

## Usage

```bash
pulli                          # status tree of the current dir
pulli ~/code                   # status tree of ~/code  (= `pulli tree ~/code`)
pulli --no-fetch ~/code        # flags may come before or after the path
pulli pull                     # pull the repos that are behind
pulli pull --dry-run ~/code    # show what would be pulled, don't pull
```

### `tree` (default)

| flag | effect |
| --- | --- |
| `--no-fetch` | don't fetch remotes before showing status |
| `--no-symlinks` | don't follow symlinks (default: follow, marked) |
| `--max-depth N` | limit recursion (default: 50) |
| `--no-color` | disable ANSI colours (also implied off a TTY) |
| `--json` | one JSON object per line, for scripts |

Fetches run in parallel by default, so ahead/behind is current, and each is
bounded by a short timeout — if you're offline (on a train) unreachable
remotes are reported as `offline` and the tree still renders.

While remotes are being fetched, a spinner animates on stderr with live
progress (`⠋ Fetching remotes… 3/8`), so a multi-second fetch never looks
frozen. The spinner only appears on a real terminal; when output is piped
or captured (`pulli | less`, CI, scripts) it is a no-op and the output stays
byte-clean. `--no-color` disables the spinner's colouring too.

### `pull`

| flag | effect |
| --- | --- |
| `--dry-run` | decide and report, change nothing |
| `--no-fetch` | use local refs only (faster, may be stale) |
| `--json` | the same decision as data; implies `--dry-run` |
| `--no-color`, `--no-symlinks`, `--max-depth` | as above |

Fetches every repo first, then fast-forwards the ones that are behind.

## What `pull` does and does not touch

Every rule below is a **skip, never a force** — pulli will not destroy or
corrupt work to make progress.

| repo state | action |
| --- | --- |
| behind upstream, clean | `git pull --ff-only` |
| up to date | nothing (silent) |
| ahead only | nothing — local commits need a *push*, not a pull |
| uncommitted changes | skipped, reported |
| diverged (ahead **and** behind) | skipped — a fast-forward is impossible; merge or rebase is your call |
| merge / rebase / bisect in progress | skipped, reported — never touched |
| no upstream (detached HEAD, no remote) | nothing to pull |
| remote unreachable | reported as `offline`; **not** a failure |
| broken repo | reported; exit code 1 |

`--ff-only` is the important part: a plain `git pull` can *create* a merge
commit (and a merge conflict) in a repo you never touched. `--ff-only`
refuses instead.

Exit code is `0` when nothing needs you, `1` on a broken repo or a failed
pull. Being offline is not a failure.

```
$ pulli pull --dry-run ~/code
  ↕ aiuis/pi-gui                ↓4 ↑2   diverged — needs merge or rebase, skipping
  ◐ www/chopdok                 ↓5      dirty 1 (MERGE-REVIEW.md), skipping pull
  ↑ throway                     ↑1      ↑1 ahead (not pushed)
  ◐ klark0                              merge in progress — skipping pull
  ↓ chopdok                     ↓5      would pull

Dry run — no pulls performed.

Would pull 1 repo(s).
```

## What the tree shows

```
~/code
├── clones/
│   ├── herdr    ogulcancelik/herdr    master  ↓0 ↑0  ● clean
│   ├── pi       earendil-works/pi     main    ↓33 ↑0 ● clean
│   └── gogcli   openclaw/gogcli       main    ↓0 ↑0  ◐ dirty 5
├── handoffs -> code/skaleshare/handoffs (alias)
├── kontext.one  devskale/kontext.one  main    ↓0 ↑0  ◐ dirty 5
│   ├── klark0     devskale/klark0      dev     ↓0 ↑0  ● clean
│   └── python-utils (submodule)                   ↓0 ↑0  ● clean
└── backups/
    └── model-proxy.git/  (bare repo — nothing to pull)
```

- **`↓N ↑M`** — commits behind / ahead of upstream; `·  ·` means there is
  no upstream to compare against (a fresh `git init`, a detached HEAD, a
  remote-less clone), which is not the same as "in sync"
- **`●` clean / `◐` dirty N / `✗` error** — `◐ offline` means the fetch
  failed, so the numbers may be stale
- **symlinks** are followed by default and marked; one whose target is also
  reachable under its real name is shown as an `(alias)` and is never a
  second pull target. `--no-symlinks` skips them
- **submodules** are their own nodes, marked `(submodule)`
- **nested repos** (a repo inside another repo) are found and shown
- **bare repos** (`x.git/`) have no working tree: marked, never pulled
- **credentials** in remote URLs (tokens, `user:pass@`) are stripped before
  display

## Development

```bash
uv run --extra dev pytest     # 45 tests, all against real git repos in tmpdirs
```

Tests build actual repositories in every state pulli reasons about
(behind, ahead, diverged, dirty, detached, mid-merge, offline, bare,
symlinked, vendored) and assert on the *decision*, not the formatting —
plus a cross-check that ahead/behind matches what `git rev-list` reports.

## Layout

```
src/pulli/
├── cli.py        # argparse entry point (tree + pull subcommands)
├── discovery.py  # tree walk: repos, nested repos, submodules, symlinks, bare
├── status.py     # per-repo git status (branch, ahead/behind, dirty, operation)
├── pull.py       # the decision: what to skip, what to fast-forward
└── tree.py       # ANSI tree renderer with credential scrubbing
```

`status.py` is the only module that shells out for status; `tree.py` renders
from the fields it fills in. That keeps git to one call per repo and keeps
credential scrubbing in exactly one place.
