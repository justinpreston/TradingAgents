"""Regression test: each matrix run's auto-built HTML report must use a
per-run filename, not the shared cross_run_<date>/report.html.

Before this fix, three same-day tier runs (mid/large/mega, all launched via
scripts/run_weekly_all_tiers.py) would each auto-report into
runs/cross_run_<date>/report.html — the last one to finish silently
clobbered the other two. _run_auto_report now writes
runs/cross_run_<date>/report_<run_id>.html so every tier's report survives.
Manual multi-run invocations of build_html_report.py are unaffected (they
pass --output explicitly and this fix doesn't touch build_html_report.py).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

import importlib
matrix_mod = importlib.import_module("run_copilot_matrix")


class TestAutoReportHTMLFilename:

    def test_html_output_path_includes_run_id(self, tmp_path, monkeypatch):
        matrix_dir = tmp_path / "runs" / "matrix_mega_weekly_2026-07-03_0630_chain"
        matrix_dir.mkdir(parents=True)
        (matrix_dir / "manifest.json").write_text("{}", encoding="utf-8")

        monkeypatch.setattr(matrix_mod, "REPO_ROOT", tmp_path)

        captured_cmds: list[list[str]] = []

        class _Completed:
            returncode = 0

        def fake_run(cmd, **kwargs):
            captured_cmds.append(list(cmd))
            return _Completed()

        monkeypatch.setattr(matrix_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(matrix_mod, "_chronos_available", lambda: False)

        matrix_mod._run_auto_report(matrix_dir, long_call_delta=0.55, skip_iv_surface=True)

        html_cmds = [c for c in captured_cmds if any("build_html_report.py" in part for part in c)]
        assert len(html_cmds) == 1
        html_cmd = html_cmds[0]
        assert "--output" in html_cmd
        output_path = html_cmd[html_cmd.index("--output") + 1]

        today = datetime.now().strftime("%Y-%m-%d")
        expected = str(tmp_path / "runs" / f"cross_run_{today}" / f"report_{matrix_dir.name}.html")
        assert output_path == expected
        # Explicitly guard against the old shared filename regressing back in.
        assert Path(output_path).name != "report.html"

    def test_two_tiers_same_day_produce_distinct_filenames(self, tmp_path, monkeypatch):
        monkeypatch.setattr(matrix_mod, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(matrix_mod, "_chronos_available", lambda: False)

        captured_cmds: list[list[str]] = []

        class _Completed:
            returncode = 0

        def fake_run(cmd, **kwargs):
            captured_cmds.append(list(cmd))
            return _Completed()

        monkeypatch.setattr(matrix_mod.subprocess, "run", fake_run)

        run_ids = [
            "matrix_mid_weekly_2026-07-03_0630_chain",
            "matrix_large_weekly_2026-07-03_0630_chain",
        ]
        outputs = []
        for run_id in run_ids:
            matrix_dir = tmp_path / "runs" / run_id
            matrix_dir.mkdir(parents=True)
            (matrix_dir / "manifest.json").write_text("{}", encoding="utf-8")
            captured_cmds.clear()
            matrix_mod._run_auto_report(matrix_dir, long_call_delta=0.55, skip_iv_surface=True)
            html_cmd = next(c for c in captured_cmds if any("build_html_report.py" in part for part in c))
            outputs.append(html_cmd[html_cmd.index("--output") + 1])

        assert outputs[0] != outputs[1]
