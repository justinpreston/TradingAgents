"""Tests for scripts/friday_options_refresh.py.

Covers:
  - Run discovery is keyed off verdict_ledger.json's snapshot_date field,
    NOT directory mtime (mtime drifts every time a downstream script
    touches the run dir — accounting, options overlay, Chronos, indexing
    all write into the same directory).
  - Refresh invokes build_options_overlay.py per matching run with
    stdin=DEVNULL.
  - It never touches lean/signals.json.
  - build_friday_packet.py is invoked best-effort when present, and its
    absence is not an error.
  - main() exits nonzero when any refresh fails.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess as _sp
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "friday_options_refresh.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("friday_options_refresh", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def refresh_mod():
    return _load_module()


def _write_matrix_run(runs_dir: Path, name: str, snapshot_date: str, mtime_offset: float = 0.0) -> Path:
    run_dir = runs_dir / name
    run_dir.mkdir(parents=True)
    ledger = run_dir / "verdict_ledger.json"
    ledger.write_text(json.dumps({"run_id": name, "snapshot_date": snapshot_date, "rows": []}), encoding="utf-8")
    if mtime_offset:
        t = time.time() + mtime_offset
        import os
        os.utime(ledger, (t, t))
    return run_dir


class TestFindTodaysMatrixRuns:

    def test_matches_by_ledger_snapshot_date(self, tmp_path, refresh_mod):
        runs_dir = tmp_path / "runs"
        _write_matrix_run(runs_dir, "matrix_mid_weekly_2026-07-03_0630_chain", "2026-07-03")
        _write_matrix_run(runs_dir, "matrix_large_weekly_2026-06-26_0630_chain", "2026-06-26")

        found = refresh_mod.find_todays_matrix_runs(runs_dir, "2026-07-03")
        names = sorted(p.name for p in found)
        assert names == ["matrix_mid_weekly_2026-07-03_0630_chain"]

    def test_ignores_mtime_prefers_ledger_date(self, tmp_path, refresh_mod):
        """A run built for 2026-06-26 but touched (mtime bumped) today by a
        downstream script (accounting/overlay/indexer) must NOT be picked
        up for today's refresh — only the ledger's own snapshot_date counts."""
        runs_dir = tmp_path / "runs"
        stale_but_recently_touched = _write_matrix_run(
            runs_dir, "matrix_mega_weekly_2026-06-26_0630_chain", "2026-06-26",
            mtime_offset=0.0,  # freshly written "now" by this test itself
        )
        # Confirm the ledger file's mtime is indeed very recent (i.e. would
        # fool an mtime-based "today" heuristic).
        assert abs(stale_but_recently_touched.stat().st_mtime - time.time()) < 5

        found = refresh_mod.find_todays_matrix_runs(runs_dir, "2026-07-03")
        assert found == []

    def test_no_matches_returns_empty_list(self, tmp_path, refresh_mod):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        found = refresh_mod.find_todays_matrix_runs(runs_dir, "2026-07-03")
        assert found == []

    def test_ignores_non_matrix_dirs(self, tmp_path, refresh_mod):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        (runs_dir / "screener_2026-07-03_0630").mkdir()
        (runs_dir / "screener_2026-07-03_0630" / "screener.json").write_text("{}", encoding="utf-8")
        found = refresh_mod.find_todays_matrix_runs(runs_dir, "2026-07-03")
        assert found == []

    def test_tolerates_corrupt_ledger(self, tmp_path, refresh_mod):
        runs_dir = tmp_path / "runs"
        run_dir = runs_dir / "matrix_broken"
        run_dir.mkdir(parents=True)
        (run_dir / "verdict_ledger.json").write_text("not valid json{", encoding="utf-8")
        found = refresh_mod.find_todays_matrix_runs(runs_dir, "2026-07-03")
        assert found == []

    def test_multiple_matches_same_day(self, tmp_path, refresh_mod):
        runs_dir = tmp_path / "runs"
        _write_matrix_run(runs_dir, "matrix_mid_weekly_2026-07-03_0630_chain", "2026-07-03")
        _write_matrix_run(runs_dir, "matrix_large_weekly_2026-07-03_0630_chain", "2026-07-03")
        _write_matrix_run(runs_dir, "matrix_mega_weekly_2026-07-03_0630_chain", "2026-07-03")
        found = refresh_mod.find_todays_matrix_runs(runs_dir, "2026-07-03")
        assert len(found) == 3


class TestRefreshInvocation:

    def test_refresh_one_passes_stdin_devnull(self, tmp_path, refresh_mod, monkeypatch):
        run_dir = _write_matrix_run(tmp_path / "runs", "matrix_x", "2026-07-03")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            class _C:
                returncode = 0
            return _C()

        monkeypatch.setattr(refresh_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(refresh_mod, "REPO_ROOT", tmp_path)

        rc = refresh_mod._refresh_one(run_dir, "long-call", dry_run=False)
        assert rc == 0
        assert captured["kwargs"].get("stdin") is _sp.DEVNULL
        assert "build_options_overlay.py" in " ".join(captured["cmd"])
        assert "--strategy-mode" in captured["cmd"]
        assert "long-call" in captured["cmd"]

    def test_dry_run_does_not_invoke_subprocess(self, tmp_path, refresh_mod, monkeypatch):
        run_dir = _write_matrix_run(tmp_path / "runs", "matrix_x", "2026-07-03")
        called = {"n": 0}

        def fake_run(cmd, **kwargs):
            called["n"] += 1
            class _C:
                returncode = 0
            return _C()

        monkeypatch.setattr(refresh_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(refresh_mod, "REPO_ROOT", tmp_path)
        rc = refresh_mod._refresh_one(run_dir, "long-call", dry_run=True)
        assert rc == 0
        assert called["n"] == 0

    def test_never_touches_lean_signals(self, refresh_mod):
        """Static guard: no code line (docstrings/comments excluded) may
        reference lean/signals.json — the module's docstring documents the
        invariant, but actual code must never read/write that file."""
        code_lines = [
            line for line in SCRIPT.read_text().splitlines()
            if "signals.json" in line
        ]
        # Every hit must be inside a comment or the module docstring block,
        # not an executable statement (no "open(", "Path(", "write", "read"
        # sharing the line with the reference).
        for line in code_lines:
            stripped = line.strip()
            is_prose = stripped.startswith("#") or not any(
                tok in line for tok in ("open(", "Path(", ".write", ".read", "subprocess")
            )
            assert is_prose, f"executable-looking reference to signals.json: {line!r}"


class TestFridayPacketBestEffort:

    def test_missing_packet_script_is_not_an_error(self, tmp_path, refresh_mod, monkeypatch):
        monkeypatch.setattr(refresh_mod, "REPO_ROOT", tmp_path)
        called = {"n": 0}

        def fake_run(cmd, **kwargs):
            called["n"] += 1
            class _C:
                returncode = 0
            return _C()

        monkeypatch.setattr(refresh_mod.subprocess, "run", fake_run)
        # No scripts/build_friday_packet.py under tmp_path → must no-op quietly.
        refresh_mod._run_friday_packet_best_effort(dry_run=False)
        assert called["n"] == 0

    def test_present_packet_script_is_invoked(self, tmp_path, refresh_mod, monkeypatch):
        monkeypatch.setattr(refresh_mod, "REPO_ROOT", tmp_path)
        (tmp_path / "scripts").mkdir(parents=True)
        (tmp_path / "scripts" / "build_friday_packet.py").write_text("# stub", encoding="utf-8")

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            class _C:
                returncode = 0
            return _C()

        monkeypatch.setattr(refresh_mod.subprocess, "run", fake_run)
        refresh_mod._run_friday_packet_best_effort(dry_run=False)
        assert "cmd" in captured
        assert captured["kwargs"].get("stdin") is _sp.DEVNULL

    def test_packet_failure_does_not_raise(self, tmp_path, refresh_mod, monkeypatch, capsys):
        monkeypatch.setattr(refresh_mod, "REPO_ROOT", tmp_path)
        (tmp_path / "scripts").mkdir(parents=True)
        (tmp_path / "scripts" / "build_friday_packet.py").write_text("# stub", encoding="utf-8")

        def fake_run(cmd, **kwargs):
            class _C:
                returncode = 3
            return _C()

        monkeypatch.setattr(refresh_mod.subprocess, "run", fake_run)
        # Must not raise even though the best-effort step "failed"
        refresh_mod._run_friday_packet_best_effort(dry_run=False)


class TestMainExitCode:

    def test_main_exits_nonzero_when_no_runs_found(self, tmp_path, refresh_mod, monkeypatch):
        monkeypatch.setattr(
            sys, "argv",
            ["friday_options_refresh.py", "--date", "2026-07-03",
             "--runs-dir", str(tmp_path / "runs")],
        )
        rc = refresh_mod.main()
        assert rc != 0

    def test_main_exits_nonzero_when_a_refresh_fails(self, tmp_path, refresh_mod, monkeypatch):
        runs_dir = tmp_path / "runs"
        _write_matrix_run(runs_dir, "matrix_mid_weekly_2026-07-03_0630_chain", "2026-07-03")

        monkeypatch.setattr(refresh_mod, "REPO_ROOT", tmp_path)

        def fake_run(cmd, **kwargs):
            if "build_options_overlay.py" in " ".join(str(c) for c in cmd):
                class _C:
                    returncode = 1
                return _C()
            class _C:
                returncode = 0
            return _C()

        monkeypatch.setattr(refresh_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(
            sys, "argv",
            ["friday_options_refresh.py", "--date", "2026-07-03",
             "--runs-dir", str(runs_dir)],
        )
        rc = refresh_mod.main()
        assert rc != 0

    def test_main_exits_zero_when_all_refresh_succeed(self, tmp_path, refresh_mod, monkeypatch):
        runs_dir = tmp_path / "runs"
        _write_matrix_run(runs_dir, "matrix_mid_weekly_2026-07-03_0630_chain", "2026-07-03")

        monkeypatch.setattr(refresh_mod, "REPO_ROOT", tmp_path)

        def fake_run(cmd, **kwargs):
            class _C:
                returncode = 0
            return _C()

        monkeypatch.setattr(refresh_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(
            sys, "argv",
            ["friday_options_refresh.py", "--date", "2026-07-03",
             "--runs-dir", str(runs_dir)],
        )
        rc = refresh_mod.main()
        assert rc == 0
