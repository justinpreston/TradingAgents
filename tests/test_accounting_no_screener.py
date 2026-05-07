"""Smoke tests for `build_run_accounting.py` when invoked WITHOUT --screener-run.

Guards against the bug where `_run_auto_report` in `run_copilot_matrix.py`
called accounting without a screener arg (because ad-hoc matrix runs have no
upstream screener), and accounting crashed with argparse exit code 2 because
`--screener-run` was `required=True`. The downstream effect was silent: the
matrix runner logged a warning, then options-overlay and HTML report ran on a
missing/empty `verdict_ledger.json` and overwrote the cross-run HTML with a
broken shell.

This test fakes a minimal matrix run on disk and confirms accounting can
reconstruct a valid ledger from just the cells/<profile>/<T>/<T>.state.json
files, with no screener present.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build_run_accounting.py"


def _write_cell(matrix_dir: Path, profile: str, ticker: str, decision: str) -> None:
    cell_dir = matrix_dir / "cells" / profile / ticker
    cell_dir.mkdir(parents=True, exist_ok=True)
    (cell_dir / f"{ticker}.state.json").write_text(
        json.dumps({"final_trade_decision": decision}), encoding="utf-8"
    )


@pytest.fixture
def fake_matrix(tmp_path: Path):
    """Build a minimal matrix-run directory tree with one PICK and one VETO."""
    matrix = tmp_path / "matrix_smoke_test"
    matrix.mkdir()

    # PICK: Overweight aggressive + Hold conservative
    _write_cell(
        matrix,
        "aggressive",
        "AAA",
        "**Rating**: Overweight\n\n**Executive Summary**: AI thesis.\n\n"
        "**Price Target**: 200.0\n\n**Time Horizon**: 6-12 months\n",
    )
    _write_cell(
        matrix,
        "conservative",
        "AAA",
        "**Rating**: Hold\n\n**Executive Summary**: Skeptical but engaged.\n\n"
        "**Price Target**: 180.0\n\n**Time Horizon**: 6-12 months\n",
    )

    # VETOED: aggressive Sell
    _write_cell(
        matrix,
        "aggressive",
        "BBB",
        "**Rating**: Sell\n\n**Executive Summary**: Cyclical top.\n\n"
        "**Price Target**: 50.0\n",
    )

    # Manifest gives the script some metadata to work with.
    (matrix / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": "matrix_smoke_test",
                "tickers": ["AAA", "BBB"],
                "profiles": ["aggressive", "conservative"],
                "screener_run": None,
            }
        )
    )
    return matrix


def test_accounting_runs_without_screener(fake_matrix, monkeypatch):
    """The script should exit 0 and produce all expected output files when
    invoked without `--screener-run`.
    """
    # Avoid Polygon API calls during test — feed a stub current_prices.json.
    (fake_matrix / "current_prices.json").write_text(
        json.dumps(
            {
                "AAA": {"close": 175.0, "as_of": "2026-05-06"},
                "BBB": {"close": 60.0, "as_of": "2026-05-06"},
            }
        )
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--matrix-run", str(fake_matrix)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
    )

    assert result.returncode == 0, (
        f"accounting failed without --screener-run.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )

    # Outputs exist
    assert (fake_matrix / "verdict_ledger.json").exists()
    assert (fake_matrix / "verdict_ledger.csv").exists()
    assert (fake_matrix / "trade_synthesis.md").exists()
    assert (fake_matrix / "README.md").exists()
    assert (fake_matrix / "per_ticker" / "AAA.md").exists()
    assert (fake_matrix / "per_ticker" / "BBB.md").exists()

    # Ledger schema is intact and screener_run is None
    ledger = json.loads((fake_matrix / "verdict_ledger.json").read_text())
    assert ledger.get("screener_run") is None
    rows = ledger.get("rows") or ledger.get("tickers") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    tickers = sorted(r.get("ticker") for r in rows if isinstance(r, dict))
    assert tickers == ["AAA", "BBB"]

    # Markdown does not silently embed "None" or break — it should explicitly
    # call out the ad-hoc nature of the run.
    synth = (fake_matrix / "trade_synthesis.md").read_text().lower()
    readme = (fake_matrix / "README.md").read_text().lower()
    assert "ad-hoc" in synth or "no screener" in synth or "screener: —" in synth, (
        "trade_synthesis.md should call out ad-hoc/no-screener context"
    )
    assert "ad-hoc" in readme or "no screener" in readme or "screener: —" in readme, (
        "README.md should call out ad-hoc/no-screener context"
    )


def test_accounting_log_line_marks_no_screener(fake_matrix):
    """The console log line should make it visually obvious there is no
    screener — easy spot-check during ops.
    """
    (fake_matrix / "current_prices.json").write_text(
        json.dumps(
            {
                "AAA": {"close": 175.0, "as_of": "2026-05-06"},
                "BBB": {"close": 60.0, "as_of": "2026-05-06"},
            }
        )
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--matrix-run", str(fake_matrix)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0
    assert "ad-hoc" in result.stdout.lower() or "none" in result.stdout.lower()
