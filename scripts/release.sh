#!/bin/bash
# Local release builder — zero GitHub Actions minutes.
# Gates: clean worktree → tests green → version bump → tag → build →
# (optional) publish to PyPI.
#
# Usage:
#   bash scripts/release.sh 0.2.2          # bump, tag, build
#   bash scripts/release.sh 0.2.2 --publish # ... and upload to PyPI
#
# Publish needs a PyPI token: UV_PUBLISH_TOKEN in the env, or pass it
# when prompted. Everything else runs fully offline.
set -euo pipefail

REPO="devskale/pulli"
VERSION="${1:?usage: release.sh <version> [--publish] (no leading v)}"
VERSION="${VERSION#v}"
TAG="v$VERSION"
PUBLISH="${2:-}"

cd "$(dirname "$0")/.."

echo "── Gate: clean worktree ──"
if [ -n "$(git status --porcelain)" ]; then
    echo "  worktree is dirty — commit or stash first:" >&2
    git status --porcelain >&2
    exit 1
fi

echo "── Gate: on main, up to date with origin ──"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" = "main" ] || { echo "  not on main (is $BRANCH)" >&2; exit 1; }
git fetch -q origin main
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || {
    echo "  main is not in sync with origin/main — push/pull first" >&2
    exit 1
}

echo "── Gate: tag does not exist yet ──"
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && {
    echo "  $TAG already exists" >&2
    exit 1
}

echo "── Gate: tests green ──"
uv run --extra dev pytest

echo "── Bump version → $VERSION ──"
INIT="src/pulli/__init__.py"
sed -i '' "s/^__version__ = .*/__version__ = \"$VERSION\"/" "$INIT"
git add "$INIT" uv.lock
git commit -q -m "v$VERSION" -m "Released via scripts/release.sh (local build, zero CI minutes)."
git tag -a "$TAG" -m "v$VERSION"

echo "── Sanity: built wheel reports the right version ──"
rm -rf dist && mkdir -p dist
uv build -q
uv run --no-project --with "./dist/pulli-$VERSION-py3-none-any.whl" \
    pulli --version | grep -Fx "pulli $VERSION" || {
    echo "  version mismatch in wheel" >&2
    exit 1
}

echo "── Push commit + tag ──"
git push origin main "refs/tags/$TAG"

if [ "$PUBLISH" = "--publish" ]; then
    echo "── Publish to PyPI ──"
    # Load the token from .env.local when not already in the env.
    ENVLOCAL="$(cd "$(dirname "$0")/.." && pwd)/.env.local"
    if [ -z "${UV_PUBLISH_TOKEN:-}" ] && [ -f "$ENVLOCAL" ]; then
        # shellcheck disable=SC1090
        set -a; . "$ENVLOCAL"; set +a
    fi
    if [ -z "${UV_PUBLISH_TOKEN:-}" ]; then
        echo "  UV_PUBLISH_TOKEN not set — get one at pypi.org (account settings"
        echo "  → API tokens, scope: this project) and re-run with it set:"
        echo "    UV_PUBLISH_TOKEN=pypi-... bash scripts/release.sh $VERSION --publish"
        exit 1
    fi
    uv publish "dist/pulli-$VERSION-py3-none-any.whl" "dist/pulli-$VERSION.tar.gz"
    echo "── Done: https://pypi.org/project/pulli/$VERSION ──"
else
    echo "── Built (not published). To publish: ──"
    echo "    UV_PUBLISH_TOKEN=pypi-... uv publish dist/pulli-$VERSION*"
fi
