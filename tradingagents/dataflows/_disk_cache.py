"""Generic disk cache utility for screener data layers.

Stores JSON-serializable values keyed by namespace + key, with optional TTL.
Used by:

* :mod:`tradingagents.screener.fundamentals` (TTL=7 days, keyed by ticker)
* :mod:`tradingagents.dataflows.grouped_bars_window` (no TTL, keyed by date)

Implementation: one JSON file per ``(namespace, key)`` under
``<repo>/.cache/<namespace>/<safe_key>.json``. The file's mtime is the
cache-write timestamp used for TTL checks.

Thread/process safety: writes are atomic (write to ``.tmp``, then rename).
Concurrent reads are safe; concurrent writes resolve to last-writer-wins,
which is fine because all our cached payloads are derived deterministically
from external API responses (idempotent).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CACHE_ROOT = REPO_ROOT / ".cache"

_SAFE_KEY = re.compile(r"[^A-Za-z0-9_.\-]")


def _safe_key(key: str) -> str:
    """Sanitize a cache key so it's a valid filename across platforms."""
    return _SAFE_KEY.sub("_", key)


class DiskCache:
    """Lightweight namespaced disk cache with optional TTL.

    :param namespace: subdirectory under the cache root (e.g. ``fundamentals``).
    :param ttl_seconds: if set, ``get()`` returns None for entries older than
        this. ``None`` means no expiry — the value lives until manually
        cleared. Use this for data that's stable once written (e.g. a
        historical day's market data).
    :param cache_root: override the default ``<repo>/.cache``. Mostly for
        tests.
    """

    def __init__(
        self,
        namespace: str,
        *,
        ttl_seconds: float | None = None,
        cache_root: Path | None = None,
    ) -> None:
        self.namespace = namespace
        self.ttl_seconds = ttl_seconds
        root = Path(cache_root) if cache_root else DEFAULT_CACHE_ROOT
        self.dir = root / namespace
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.dir / f"{_safe_key(key)}.json"

    def get(self, key: str) -> Any | None:
        """Return the cached value for ``key``, or ``None`` if missing/expired/corrupt."""
        p = self._path(key)
        if not p.exists():
            return None
        if self.ttl_seconds is not None:
            try:
                age = time.time() - p.stat().st_mtime
            except OSError:
                return None
            if age > self.ttl_seconds:
                return None
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.debug("disk cache: failed to read %s: %s", p, e)
            return None

    def set(self, key: str, value: Any) -> None:
        """Store ``value`` under ``key``. Atomic via tmp-then-rename."""
        p = self._path(key)
        tmp = p.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(value))
            os.replace(tmp, p)
        except (OSError, TypeError, ValueError) as e:
            log.warning("disk cache: failed to write %s: %s", p, e)
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass

    def clear(self) -> None:
        """Delete every entry in the namespace."""
        for f in self.dir.glob("*.json"):
            try:
                f.unlink()
            except OSError:
                pass
