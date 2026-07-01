"""Tests for the per-cell auto-retry added to run_copilot_matrix.py.

On 2026-06-26 all 15 Stage-A cells failed systemically with no retry,
killing the whole window. `_run_cell` now retries ONCE (a fresh subprocess,
identical args) when the first attempt exits nonzero or produces no
parseable rating, before recording the error. The retry is logged to
events.jsonl via the live writer (when provided) so it's visible in the
event stream, and the final CellResult carries an `attempt` field.
"""

from __future__ import annotations

import json
import subprocess as _sp
from pathlib import Path

import pytest

import importlib
matrix_mod = importlib.import_module("run_copilot_matrix")

CellResult = matrix_mod.CellResult


def _state_path(cell_dir: Path, ticker: str) -> Path:
    return cell_dir / f"{ticker}.state.json"


class TestRunCellRetry:

    def test_first_attempt_success_no_retry(self, tmp_path, monkeypatch):
        """Happy path: attempt 1 succeeds → no retry, attempt field == 1."""
        calls = {"n": 0}

        def fake_run(cmd, **kwargs):
            calls["n"] += 1
            # Write a state.json with a parseable rating before "returning"
            cell_dir = tmp_path / "cells" / "aggressive" / "FOO"
            cell_dir.mkdir(parents=True, exist_ok=True)
            _state_path(cell_dir, "FOO").write_text(
                json.dumps({"final_trade_decision": "**Rating**: Overweight\n**Price Target**: 100.0\n"}),
                encoding="utf-8",
            )

            class _Completed:
                returncode = 0
                stdout = ""
                stderr = ""
            return _Completed()

        monkeypatch.setattr(matrix_mod.subprocess, "run", fake_run)

        result = matrix_mod._run_cell(
            ticker="FOO", profile="aggressive", trade_date="2026-06-26",
            matrix_dir=tmp_path, env={}, no_dashboard=True,
        )
        assert calls["n"] == 1
        assert result.status == "ok"
        assert result.rating == "Overweight"
        assert result.attempt == 1

    def test_first_attempt_fails_second_succeeds(self, tmp_path, monkeypatch):
        """First subprocess exits nonzero with no rating; retry succeeds → attempt=2."""
        calls = {"n": 0}

        def fake_run(cmd, **kwargs):
            calls["n"] += 1
            cell_dir = tmp_path / "cells" / "aggressive" / "FOO"
            cell_dir.mkdir(parents=True, exist_ok=True)
            if calls["n"] == 1:
                # First attempt: process crashes, no state.json written
                class _Completed:
                    returncode = 1
                    stdout = ""
                    stderr = "boom"
                return _Completed()
            else:
                # Retry: succeeds, writes a parseable state.json
                _state_path(cell_dir, "FOO").write_text(
                    json.dumps({"final_trade_decision": "**Rating**: Buy\n**Price Target**: 55.0\n"}),
                    encoding="utf-8",
                )

                class _Completed:
                    returncode = 0
                    stdout = ""
                    stderr = ""
                return _Completed()

        monkeypatch.setattr(matrix_mod.subprocess, "run", fake_run)

        result = matrix_mod._run_cell(
            ticker="FOO", profile="aggressive", trade_date="2026-06-26",
            matrix_dir=tmp_path, env={}, no_dashboard=True,
        )
        assert calls["n"] == 2, "expected exactly one retry (two total subprocess calls)"
        assert result.status == "ok"
        assert result.rating == "Buy"
        assert result.attempt == 2

    def test_both_attempts_fail_records_error_with_attempt_2(self, tmp_path, monkeypatch):
        """Both attempts fail → error is recorded (not retried a third time)."""
        calls = {"n": 0}

        def fake_run(cmd, **kwargs):
            calls["n"] += 1

            class _Completed:
                returncode = 1
                stdout = ""
                stderr = "persistent failure"
            return _Completed()

        monkeypatch.setattr(matrix_mod.subprocess, "run", fake_run)

        result = matrix_mod._run_cell(
            ticker="FOO", profile="aggressive", trade_date="2026-06-26",
            matrix_dir=tmp_path, env={}, no_dashboard=True,
        )
        assert calls["n"] == 2, "must retry exactly once, not loop indefinitely"
        assert result.status == "error"
        assert result.attempt == 2

    def test_zero_returncode_but_no_rating_triggers_retry(self, tmp_path, monkeypatch):
        """Subprocess exits 0 but state.json has no parseable rating → still retried."""
        calls = {"n": 0}

        def fake_run(cmd, **kwargs):
            calls["n"] += 1
            cell_dir = tmp_path / "cells" / "aggressive" / "FOO"
            cell_dir.mkdir(parents=True, exist_ok=True)
            if calls["n"] == 1:
                # exits 0 but state.json has no parseable rating
                _state_path(cell_dir, "FOO").write_text(
                    json.dumps({"final_trade_decision": "no structured rating here"}),
                    encoding="utf-8",
                )
            else:
                _state_path(cell_dir, "FOO").write_text(
                    json.dumps({"final_trade_decision": "**Rating**: Hold\n**Price Target**: 40.0\n"}),
                    encoding="utf-8",
                )

            class _Completed:
                returncode = 0
                stdout = ""
                stderr = ""
            return _Completed()

        monkeypatch.setattr(matrix_mod.subprocess, "run", fake_run)

        result = matrix_mod._run_cell(
            ticker="FOO", profile="aggressive", trade_date="2026-06-26",
            matrix_dir=tmp_path, env={}, no_dashboard=True,
        )
        assert calls["n"] == 2
        assert result.rating == "Hold"
        assert result.attempt == 2

    def test_retry_uses_fresh_subprocess_same_args(self, tmp_path, monkeypatch):
        """The retry must re-invoke with identical cmd/env (fresh subprocess, same args)."""
        captured_cmds = []

        def fake_run(cmd, **kwargs):
            captured_cmds.append(list(cmd))

            class _Completed:
                returncode = 1
                stdout = ""
                stderr = "fail"
            return _Completed()

        monkeypatch.setattr(matrix_mod.subprocess, "run", fake_run)

        matrix_mod._run_cell(
            ticker="FOO", profile="conservative", trade_date="2026-06-26",
            matrix_dir=tmp_path, env={"X": "1"}, no_dashboard=True,
        )
        assert len(captured_cmds) == 2
        assert captured_cmds[0] == captured_cmds[1]

    def test_retry_still_passes_stdin_devnull(self, tmp_path, monkeypatch):
        """The hard subprocess invariant must survive the retry wrapper."""
        kwargs_seen = []

        def fake_run(cmd, **kwargs):
            kwargs_seen.append(kwargs)

            class _Completed:
                returncode = 1
                stdout = ""
                stderr = "fail"
            return _Completed()

        monkeypatch.setattr(matrix_mod.subprocess, "run", fake_run)

        matrix_mod._run_cell(
            ticker="FOO", profile="aggressive", trade_date="2026-06-26",
            matrix_dir=tmp_path, env={}, no_dashboard=True,
        )
        assert len(kwargs_seen) == 2
        for kwargs in kwargs_seen:
            assert kwargs.get("stdin") is _sp.DEVNULL

    def test_retry_logged_to_events_jsonl_via_writer(self, tmp_path, monkeypatch):
        """When a writer is supplied, the failed first attempt is appended to
        events.jsonl (with an attempt field) before the retry runs."""
        calls = {"n": 0}

        def fake_run(cmd, **kwargs):
            calls["n"] += 1
            cell_dir = tmp_path / "cells" / "aggressive" / "FOO"
            cell_dir.mkdir(parents=True, exist_ok=True)
            if calls["n"] == 2:
                _state_path(cell_dir, "FOO").write_text(
                    json.dumps({"final_trade_decision": "**Rating**: Overweight\n**Price Target**: 10.0\n"}),
                    encoding="utf-8",
                )
                class _Completed:
                    returncode = 0
                    stdout = ""
                    stderr = ""
                return _Completed()
            class _Completed:
                returncode = 1
                stdout = ""
                stderr = "fail"
            return _Completed()

        monkeypatch.setattr(matrix_mod.subprocess, "run", fake_run)

        manifest = {
            "run_id": "test_matrix", "trade_date": "2026-06-26",
            "trade_date_label": "today", "tickers": ["FOO"],
            "profiles": ["aggressive"], "started_at": "2026-06-26T06:30:00",
        }
        writer = matrix_mod._LiveWriter(tmp_path, manifest)

        result = matrix_mod._run_cell(
            ticker="FOO", profile="aggressive", trade_date="2026-06-26",
            matrix_dir=tmp_path, env={}, no_dashboard=True,
            writer=writer, stage="A",
        )
        assert result.attempt == 2
        assert result.status == "ok"

        events_path = tmp_path / "events.jsonl"
        assert events_path.exists()
        lines = events_path.read_text(encoding="utf-8").strip().splitlines()
        # One event for the failed first attempt (via writer.append inside
        # _run_cell), one for the final successful result appended by the
        # caller in a real _run_stage loop — here we only invoked _run_cell
        # directly, so only the intermediate retry event should be present.
        assert len(lines) == 1
        event = json.loads(lines[0])
        assert event["ticker"] == "FOO"
        assert event["stage"] == "A-retry"
        assert event["attempt"] == 1
        assert event["status"] == "error"


class TestCellResultAttemptField:

    def test_default_attempt_is_1(self):
        r = CellResult(ticker="X", profile="aggressive", status="ok")
        assert r.attempt == 1

    def test_attempt_serializes_via_asdict(self):
        from dataclasses import asdict
        r = CellResult(ticker="X", profile="aggressive", status="ok", attempt=2)
        d = asdict(r)
        assert d["attempt"] == 2
