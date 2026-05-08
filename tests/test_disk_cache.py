"""Tests for :mod:`tradingagents.dataflows._disk_cache`."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tradingagents.dataflows._disk_cache import DiskCache, _safe_key


def test_roundtrip(tmp_path: Path) -> None:
    cache = DiskCache("test_ns", cache_root=tmp_path)
    assert cache.get("foo") is None
    cache.set("foo", {"a": 1, "b": [2, 3]})
    assert cache.get("foo") == {"a": 1, "b": [2, 3]}


def test_namespace_isolation(tmp_path: Path) -> None:
    a = DiskCache("ns_a", cache_root=tmp_path)
    b = DiskCache("ns_b", cache_root=tmp_path)
    a.set("k", "in_a")
    b.set("k", "in_b")
    assert a.get("k") == "in_a"
    assert b.get("k") == "in_b"


def test_ttl_expires(tmp_path: Path) -> None:
    cache = DiskCache("test", ttl_seconds=0.05, cache_root=tmp_path)
    cache.set("k", "v")
    assert cache.get("k") == "v"
    time.sleep(0.1)
    assert cache.get("k") is None


def test_ttl_none_never_expires(tmp_path: Path) -> None:
    cache = DiskCache("test", ttl_seconds=None, cache_root=tmp_path)
    cache.set("k", "v")
    # Backdate to ensure no implicit TTL
    p = cache._path("k")
    very_old = time.time() - 365 * 24 * 3600
    import os as _os
    _os.utime(p, (very_old, very_old))
    assert cache.get("k") == "v"


def test_unsafe_keys_sanitized(tmp_path: Path) -> None:
    cache = DiskCache("test", cache_root=tmp_path)
    cache.set("ticker/AAPL_2026-01-01", {"x": 1})
    assert cache.get("ticker/AAPL_2026-01-01") == {"x": 1}
    # The on-disk filename should not contain the slash
    files = list(cache.dir.glob("*.json"))
    assert len(files) == 1
    assert "/" not in files[0].name


def test_safe_key_replaces_specials() -> None:
    assert _safe_key("AAPL/2026") == "AAPL_2026"
    assert _safe_key("a:b c") == "a_b_c"
    assert _safe_key("plain.key-1") == "plain.key-1"


def test_atomic_write_no_tmp_leftover(tmp_path: Path) -> None:
    cache = DiskCache("test", cache_root=tmp_path)
    cache.set("foo", "bar")
    assert not list(cache.dir.glob("*.tmp"))


def test_corrupt_file_returns_none(tmp_path: Path) -> None:
    cache = DiskCache("test", cache_root=tmp_path)
    cache._path("k").write_text("{not valid json")
    assert cache.get("k") is None


def test_unserializable_value_does_not_corrupt_existing(tmp_path: Path) -> None:
    cache = DiskCache("test", cache_root=tmp_path)
    cache.set("k", {"good": "value"})

    class _Unserializable:
        pass

    cache.set("k", _Unserializable())  # silently logs + cleans up tmp
    # Either the previous value remains or the entry is gone — never corrupt
    val = cache.get("k")
    assert val is None or val == {"good": "value"}
    # No tmp leftover
    assert not list(cache.dir.glob("*.tmp"))


def test_delete(tmp_path: Path) -> None:
    cache = DiskCache("test", cache_root=tmp_path)
    cache.set("k", 1)
    cache.delete("k")
    assert cache.get("k") is None
    cache.delete("k")  # no error on missing key


def test_clear(tmp_path: Path) -> None:
    cache = DiskCache("test", cache_root=tmp_path)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.clear()
    assert cache.get("a") is None
    assert cache.get("b") is None


def test_get_missing_key_no_error(tmp_path: Path) -> None:
    cache = DiskCache("test", cache_root=tmp_path)
    assert cache.get("never_set") is None
