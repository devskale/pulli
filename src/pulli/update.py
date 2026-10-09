"""Check PyPI for a newer pulli — at most once a day, never in the way.

Design rules:

  - The check is **cached** (default 24h): repeated runs cost nothing.
  - It **never fails or slows down** the real command: network errors are
    swallowed, the fetch is bounded by a short timeout, and normal runs
    only *read* the cache — a stale cache is refreshed by a daemon thread
    that may not finish before the process exits, which is fine.
  - `pulli --version` is the one place that checks **synchronously**, so
    asking for the version always answers "is there an update?" too.
  - Zero dependencies: stdlib urllib against the PyPI JSON API.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import __version__

#: Seconds a cached result is considered fresh (24h).
TTL = 24 * 60 * 60

#: The fetch is capped so even a hanging connection cannot delay --version.
FETCH_TIMEOUT = 3.0

_PYPI_URL = "https://pypi.org/pypi/pulli/json"


def _cache_dir() -> Path:
    """Platform-appropriate cache dir (XDG on Linux, ~/Library/Caches on
    macOS), overridable for tests via PULLI_CACHE_DIR."""
    if os.environ.get("PULLI_CACHE_DIR"):
        return Path(os.environ["PULLI_CACHE_DIR"])
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "pulli"
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "pulli"


def _cache_file() -> Path:
    return _cache_dir() / "update.json"


def _read_cache() -> dict | None:
    try:
        data = json.loads(_cache_file().read_text(encoding="utf-8"))
        if isinstance(data, dict) and "latest" in data and "checked_at" in data:
            return data
    except (OSError, ValueError):
        pass
    return None


def _write_cache(latest: str) -> None:
    try:
        _cache_dir().mkdir(parents=True, exist_ok=True)
        _cache_file().write_text(
            json.dumps({"latest": latest, "checked_at": time.time()}),
            encoding="utf-8",
        )
    except OSError:
        pass  # a read-only cache dir must never break the command


def _fetch_latest() -> str | None:
    """Latest version on PyPI, or None (offline, blocked, slow, bad JSON).

    GIT_TERMINAL_PROMPT-style discipline: no prompts, no retries — a check
    that costs more than it returns is not worth having.
    """
    try:
        req = urllib.request.Request(_PYPI_URL, headers={"User-Agent": f"pulli/{__version__}"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
        latest = data["info"]["version"]
        return latest if isinstance(latest, str) else None
    except (OSError, ValueError, KeyError):
        return None


def _is_newer(latest: str | None) -> bool:
    """True when `latest` is a newer release than the running one.

    Compared as numeric tuples of dot-separated parts, so 0.10 > 0.9 and
    pre-release suffixes (0.2.4rc1) sort before their final.
    """
    if not latest:
        return False

    def key(v: str):
        parts = []
        for p in v.replace("-", ".").split("."):
            # a numeric segment compares by number, a suffix (rc1, dev)
            # sorts before the final release — approximate: (0, n) < (1, s)
            if p.isdigit():
                parts.append((1, int(p), ""))
            else:
                num = "".join(ch for ch in p if ch.isdigit())
                parts.append((0, int(num) if num else 0, p))
        return parts

    try:
        return key(latest) > key(__version__)
    except (TypeError, ValueError):
        return False


def _cached_latest() -> str | None:
    """The cached latest version if the cache is fresh, else None."""
    data = _read_cache()
    if data and time.time() - data["checked_at"] < TTL:
        return data["latest"]
    return None


def update_hint(sync: bool = False) -> str | None:
    """One-line "an update exists" hint, or None.

    `sync=True` (used by --version) fetches when the cache is stale; a
    normal run only reads the cache and refreshes it in a daemon thread
    so the command's own timing never depends on PyPI.
    """
    latest = _cached_latest()
    if latest is None:
        if not sync:
            # Fire-and-forget refresh; next run (today's later runs, or
            # tomorrow's --version) sees the fresh value.
            t = threading.Thread(target=_refresh, daemon=True)
            t.start()
            return None
        latest = _refresh()
    if _is_newer(latest):
        return f"pulli {latest} is available — update: uv tool upgrade pulli"
    return None


def _refresh() -> str | None:
    latest = _fetch_latest()
    if latest:
        _write_cache(latest)
    return latest
