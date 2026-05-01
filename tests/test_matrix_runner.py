"""Unit tests for the parallel matrix runner.

We don't exercise the subprocess pipeline (that needs LLM credentials and
~hours of compute). Instead we test the pure-logic surfaces: state.json
parsing, high-conviction filtering, live-writer rendering, and stage-B
gating around PROMOTE_RATINGS / VETO_RATINGS.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import importlib
matrix_mod = importlib.import_module("run_copilot_matrix")

CellResult = matrix_mod.CellResult
PROMOTE_RATINGS = matrix_mod.PROMOTE_RATINGS
VETO_RATINGS = matrix_mod.VETO_RATINGS


# ---------------------------------------------------------------------------
# state.json extraction
# ---------------------------------------------------------------------------

def _write_state(tmp_path: Path, ticker: str, decision_text: str) -> Path:
    p = tmp_path / f"{ticker}.state.json"
    p.write_text(json.dumps({"final_trade_decision": decision_text}), encoding="utf-8")
    return p


class TestExtractFromState:

    def test_overweight_with_pt(self, tmp_path):
        p = _write_state(tmp_path, "ADI",
            "**Rating**: Overweight\n\n**Executive Summary**: ...\n\n**Price Target**: 425.0\n")
        rating, pt = matrix_mod._extract_from_state(p)
        assert rating == "Overweight"
        assert pt == "425.0"

    def test_underweight_no_pt(self, tmp_path):
        p = _write_state(tmp_path, "DAR", "**Rating**: Underweight\nReduce exposure.")
        rating, pt = matrix_mod._extract_from_state(p)
        assert rating == "Underweight"
        assert pt is None

    def test_missing_file(self, tmp_path):
        rating, pt = matrix_mod._extract_from_state(tmp_path / "nope.json")
        assert rating is None
        assert pt is None

    def test_corrupt_json(self, tmp_path):
        p = tmp_path / "x.state.json"
        p.write_text("not valid json {", encoding="utf-8")
        rating, pt = matrix_mod._extract_from_state(p)
        assert rating is None
        assert pt is None

    def test_no_final_trade_decision(self, tmp_path):
        p = tmp_path / "y.state.json"
        p.write_text(json.dumps({"company_of_interest": "ADI"}), encoding="utf-8")
        rating, pt = matrix_mod._extract_from_state(p)
        assert rating is None

    def test_decimal_price_target(self, tmp_path):
        p = _write_state(tmp_path, "X",
            "**Rating**: Buy\n**Price Target**: 132.5\n")
        rating, pt = matrix_mod._extract_from_state(p)
        assert rating == "Buy"
        assert pt == "132.5"


# ---------------------------------------------------------------------------
# Promote / veto sets — guard against accidental edits
# ---------------------------------------------------------------------------

class TestRatingSets:

    def test_promote_ratings(self):
        assert "Overweight" in PROMOTE_RATINGS
        assert "Buy" in PROMOTE_RATINGS
        assert "Hold" not in PROMOTE_RATINGS
        assert "Underweight" not in PROMOTE_RATINGS

    def test_veto_ratings(self):
        assert "Underweight" in VETO_RATINGS
        assert "Sell" in VETO_RATINGS
        assert "Hold" not in VETO_RATINGS
        assert "Overweight" not in VETO_RATINGS


# ---------------------------------------------------------------------------
# High-conviction filter logic
# ---------------------------------------------------------------------------

def _mk(ticker: str, profile: str, rating: str | None,
        pt: str | None = None, status: str = "ok") -> CellResult:
    return CellResult(
        ticker=ticker, profile=profile, status=status,
        rating=rating, price_target=pt,
        elapsed_s=10.0, cell_dir=f"/tmp/{ticker}",
    )


class TestHighConvictionFilter:

    def test_picks_aggressive_overweight_no_secondary(self, tmp_path):
        manifest = {"run_id": "rid", "tickers": ["ADI", "DAR"]}
        results = {
            ("aggressive", "ADI"): _mk("ADI", "aggressive", "Overweight", "425"),
            ("aggressive", "DAR"): _mk("DAR", "aggressive", "Underweight", "58"),
        }
        picks = matrix_mod._write_high_conviction(
            tmp_path, manifest, results, primary="aggressive", secondary=None
        )
        assert picks == ["ADI"]
        text = (tmp_path / "high_conviction.md").read_text(encoding="utf-8")
        assert "ADI" in text
        assert "DAR" not in text  # not promoted, shouldn't be listed

    def test_secondary_veto_drops_pick(self, tmp_path):
        manifest = {"run_id": "rid", "tickers": ["ABNB", "ADI"]}
        results = {
            ("aggressive",   "ABNB"): _mk("ABNB", "aggressive", "Overweight"),
            ("conservative", "ABNB"): _mk("ABNB", "conservative", "Underweight"),
            ("aggressive",   "ADI"):  _mk("ADI",  "aggressive", "Overweight", "425"),
            ("conservative", "ADI"):  _mk("ADI",  "conservative", "Hold"),
        }
        picks = matrix_mod._write_high_conviction(
            tmp_path, manifest, results, primary="aggressive", secondary="conservative"
        )
        # ABNB drops because conservative says Underweight; ADI survives.
        assert picks == ["ADI"]

    def test_secondary_sell_also_vetoes(self, tmp_path):
        manifest = {"run_id": "rid", "tickers": ["TVTX"]}
        results = {
            ("aggressive",   "TVTX"): _mk("TVTX", "aggressive", "Buy"),
            ("conservative", "TVTX"): _mk("TVTX", "conservative", "Sell"),
        }
        picks = matrix_mod._write_high_conviction(
            tmp_path, manifest, results, primary="aggressive", secondary="conservative"
        )
        assert picks == []

    def test_secondary_hold_does_not_veto(self, tmp_path):
        """Hold in the conservative profile is the desired-pattern outcome — keep the pick."""
        manifest = {"run_id": "rid", "tickers": ["APD"]}
        results = {
            ("aggressive",   "APD"): _mk("APD", "aggressive", "Overweight", "340"),
            ("conservative", "APD"): _mk("APD", "conservative", "Hold"),
        }
        picks = matrix_mod._write_high_conviction(
            tmp_path, manifest, results, primary="aggressive", secondary="conservative"
        )
        assert picks == ["APD"]

    def test_missing_secondary_does_not_veto(self, tmp_path):
        """If secondary cell never ran (e.g. crashed), pick survives by default."""
        manifest = {"run_id": "rid", "tickers": ["ADI"]}
        results = {
            ("aggressive", "ADI"): _mk("ADI", "aggressive", "Overweight"),
        }
        picks = matrix_mod._write_high_conviction(
            tmp_path, manifest, results, primary="aggressive", secondary="conservative"
        )
        assert picks == ["ADI"]

    def test_buy_in_primary_also_promotes(self, tmp_path):
        manifest = {"run_id": "rid", "tickers": ["ADI"]}
        results = {
            ("aggressive", "ADI"): _mk("ADI", "aggressive", "Buy", "500"),
        }
        picks = matrix_mod._write_high_conviction(
            tmp_path, manifest, results, primary="aggressive", secondary=None
        )
        assert picks == ["ADI"]

    def test_no_picks_writes_empty_report(self, tmp_path):
        manifest = {"run_id": "rid", "tickers": ["ABNB"]}
        results = {
            ("aggressive", "ABNB"): _mk("ABNB", "aggressive", "Underweight"),
        }
        picks = matrix_mod._write_high_conviction(
            tmp_path, manifest, results, primary="aggressive", secondary=None
        )
        assert picks == []
        text = (tmp_path / "high_conviction.md").read_text(encoding="utf-8")
        assert "No tickers passed" in text


# ---------------------------------------------------------------------------
# LiveWriter
# ---------------------------------------------------------------------------

class TestLiveWriter:

    def _manifest(self, tmp_path):
        return {
            "run_id": "test_matrix",
            "trade_date": "2026-04-30",
            "trade_date_label": "today",
            "tickers": ["ADI", "DAR"],
            "profiles": ["aggressive", "conservative"],
            "started_at": "2026-04-30T18:00:00",
        }

    def test_renders_table_with_completed_cells(self, tmp_path):
        w = matrix_mod._LiveWriter(tmp_path, self._manifest(tmp_path))
        w.append(_mk("ADI", "aggressive", "Overweight", "425"), stage="A")
        w.append(_mk("DAR", "aggressive", "Underweight"), stage="A")
        text = (tmp_path / "live_results.md").read_text(encoding="utf-8")
        assert "ADI" in text and "DAR" in text
        assert "Overweight" in text
        assert "Underweight" in text
        assert "🟢" in text  # promote marker for ADI
        assert "🔴" in text  # veto marker for DAR

    def test_pending_cells_shown_with_dash(self, tmp_path):
        w = matrix_mod._LiveWriter(tmp_path, self._manifest(tmp_path))
        w.append(_mk("ADI", "aggressive", "Overweight"), stage="A")
        text = (tmp_path / "live_results.md").read_text(encoding="utf-8")
        # DAR/aggressive completed = no, DAR/conservative not run; both should be —
        # Find the DAR row
        for line in text.splitlines():
            if line.startswith("| **DAR**"):
                assert "—" in line  # at least one — for the unrun cells
                break
        else:
            pytest.fail("DAR row not found in live results")

    def test_event_stream_appends(self, tmp_path):
        w = matrix_mod._LiveWriter(tmp_path, self._manifest(tmp_path))
        w.append(_mk("ADI", "aggressive", "Overweight"), stage="A")
        w.append(_mk("ADI", "conservative", "Hold"), stage="B")
        events_file = tmp_path / "events.jsonl"
        lines = events_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        e1, e2 = json.loads(lines[0]), json.loads(lines[1])
        assert e1["ticker"] == "ADI" and e1["profile"] == "aggressive" and e1["stage"] == "A"
        assert e2["profile"] == "conservative" and e2["stage"] == "B"

    def test_get_completed_filters_by_profile(self, tmp_path):
        w = matrix_mod._LiveWriter(tmp_path, self._manifest(tmp_path))
        w.append(_mk("ADI", "aggressive", "Overweight"), stage="A")
        w.append(_mk("ADI", "conservative", "Hold"), stage="B")
        w.append(_mk("DAR", "aggressive", None, status="error"), stage="A")
        agg = w.get_completed("aggressive")
        # error cell with no rating is excluded
        assert "ADI" in agg
        assert "DAR" not in agg


# ---------------------------------------------------------------------------
# Screener loader
# ---------------------------------------------------------------------------

class TestScreenerLoader:

    def test_loads_top_n(self, tmp_path):
        screener = tmp_path / "screener_2026-04-30_1234"
        screener.mkdir()
        (screener / "screener.json").write_text(json.dumps({
            "candidates": [
                {"rank": 1, "ticker": "ABNB"},
                {"rank": 2, "ticker": "TVTX"},
                {"rank": 3, "ticker": "DAR"},
                {"rank": 4, "ticker": "APD"},
                {"rank": 5, "ticker": "ADI"},
            ]
        }))
        result = matrix_mod._load_screener_tickers(screener, top=3)
        assert result == ["ABNB", "TVTX", "DAR"]

    def test_handles_missing_candidates(self, tmp_path):
        screener = tmp_path / "screener_x"
        screener.mkdir()
        (screener / "screener.json").write_text(json.dumps({"trading_date": "2026-04-30"}))
        result = matrix_mod._load_screener_tickers(screener, top=10)
        assert result == []

    def test_uppercases_tickers(self, tmp_path):
        screener = tmp_path / "screener_y"
        screener.mkdir()
        (screener / "screener.json").write_text(json.dumps({
            "candidates": [{"ticker": "abnb"}, {"ticker": "tvtx"}],
        }))
        result = matrix_mod._load_screener_tickers(screener, top=10)
        assert result == ["ABNB", "TVTX"]


# ---------------------------------------------------------------------------
# Subprocess invocation hygiene
# ---------------------------------------------------------------------------

class TestRunCellSubprocessInvocation:
    """Regression: when the orchestrator is launched detached (nohup ... &)
    and its parent shell exits, the inherited stdin file descriptor can
    become invalid, causing every child Python to die with
    "Fatal Python error: init_sys_streams: ... Bad file descriptor".

    Fix: explicitly close the child's stdin via subprocess.DEVNULL.
    """

    def test_run_cell_passes_stdin_devnull(self, tmp_path, monkeypatch):
        import subprocess as _sp

        captured: dict = {}

        class _FakeCompleted:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd, **kwargs):
            captured["kwargs"] = kwargs
            captured["cmd"] = cmd
            return _FakeCompleted()

        monkeypatch.setattr(matrix_mod.subprocess, "run", fake_run)

        result = matrix_mod._run_cell(
            ticker="FOO",
            profile="aggressive",
            trade_date="2026-04-30",
            matrix_dir=tmp_path,
            env={},
            no_dashboard=True,
        )

        assert "kwargs" in captured, "_run_cell did not invoke subprocess.run"
        assert captured["kwargs"].get("stdin") is _sp.DEVNULL, (
            "_run_cell must pass stdin=subprocess.DEVNULL so children don't "
            "inherit a (possibly broken) terminal stdin from a detached parent"
        )
        # And it should still capture stdout/stderr for forensics
        assert captured["kwargs"].get("capture_output") is True
