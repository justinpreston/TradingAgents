"""Tests for scripts/grounding_audit.py — risk-scoring + tier rule."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.grounding_audit import (  # noqa: E402
    _count_unsourced_numerics,
    _score_ticker,
    _strip_data_gaps_blocks,
    _tier_for_row,
    audit_matrix_run,
)
from tradingagents.dataflows.analyst_pt_consensus import (  # noqa: E402
    AnalystPriceTargets,
)


def _empty_consensus(ticker: str) -> AnalystPriceTargets:
    return AnalystPriceTargets(
        ticker=ticker,
        current=None,
        high=None,
        low=None,
        mean=None,
        median=None,
        number_of_analysts=None,
        source="unavailable",
    )


def test_strip_data_gaps_blocks_removes_section():
    src = (
        "# Report\n\n"
        "## ⚠️ Data Gaps\n\nfundamentals returned 429\n\n"
        "## Technicals\n\nReal content here.\n"
    )
    cleaned = _strip_data_gaps_blocks(src)
    assert "Real content here" in cleaned
    assert "fundamentals returned 429" not in cleaned


def test_count_unsourced_numerics_catches_analyst_action():
    text = "Goldman raised PT to $250 ahead of the print."
    counts = _count_unsourced_numerics(text)
    assert counts.get("analyst_action", 0) == 1


def test_count_unsourced_numerics_catches_earnings_beat():
    text = "Q3 EPS beat by 12% and revenue grew 8% YoY."
    counts = _count_unsourced_numerics(text)
    assert counts.get("earnings_beat_miss", 0) >= 1
    assert counts.get("yoy_specifics", 0) >= 1


def test_count_unsourced_numerics_skips_data_gaps_block():
    """Numerics inside a Data Gaps block must not be counted."""
    text = (
        "## ⚠️ Data Gaps\n\n"
        "Goldman raised PT to $250 ahead of the print.\n\n"
        "## Real section\n\nSome other content.\n"
    )
    counts = _count_unsourced_numerics(text)
    assert counts.get("analyst_action", 0) == 0


def test_tier_for_row_blocks_a_when_suspect():
    row = {
        "classification": "PICK",
        "conservative_pt": 200.0,
        "pt_compression_pct": 2.5,
        "pt_quality_flags": ["aggressive_pt_suspect"],
    }
    # comp < 5.0 but pt_quality_flags non-empty → must be B, not A.
    assert _tier_for_row(row) == "B"


def test_tier_for_row_allows_a_when_clean():
    row = {
        "classification": "PICK",
        "conservative_pt": 200.0,
        "pt_compression_pct": 2.5,
        "pt_quality_flags": [],
    }
    assert _tier_for_row(row) == "A"


def test_tier_for_row_returns_veto():
    row = {"classification": "VETOED"}
    assert _tier_for_row(row) == "VETO"


def test_score_ticker_clean_row_is_ok():
    row = {
        "ticker": "AAPL",
        "classification": "PICK",
        "conservative_pt": 200.0,
        "aggressive_pt": 210.0,
        "current_price_usd": 195.0,
        "aggressive_pt_distance_pct": 7.7,
        "conservative_pt_distance_pct": 2.5,
        "pt_quality_flags": [],
        "pt_compression_pct": 2.5,
    }
    score = _score_ticker(row, None, None, _empty_consensus("AAPL"), 20.0)
    assert score.risk_level == "OK"
    assert score.risk_score == 0.0


def test_score_ticker_suspect_pt_raises_to_elevated():
    row = {
        "ticker": "ABC",
        "classification": "PICK",
        "conservative_pt": 500.0,  # way off
        "aggressive_pt": 600.0,
        "current_price_usd": 100.0,
        "aggressive_pt_distance_pct": 500.0,
        "conservative_pt_distance_pct": 400.0,
        "pt_quality_flags": ["aggressive_pt_suspect", "conservative_pt_suspect"],
        "pt_compression_pct": 4.0,
    }
    score = _score_ticker(row, None, None, _empty_consensus("ABC"), 20.0)
    # 2 PT flags × 8.0 = 16.0 → HIGH
    assert score.risk_level == "HIGH"
    assert score.risk_score >= 12.0


def test_score_ticker_data_gaps_raise_score():
    row = {
        "ticker": "XYZ",
        "classification": "PICK",
        "conservative_pt": 100.0,
        "aggressive_pt": 110.0,
        "current_price_usd": 95.0,
        "aggressive_pt_distance_pct": 15.0,
        "conservative_pt_distance_pct": 5.0,
        "pt_quality_flags": [],
        "pt_compression_pct": 9.5,
    }
    aggressive_cell = {
        "market_report": "[[TOOL_ERROR: get_stock_data for XYZ: 404 ]]\n\nMarket data unavailable.",
        "news_report": "[[TOOL_ERROR: get_news for XYZ: timeout ]]",
        "fundamentals_report": "",
        "sentiment_report": "",
        "investment_debate_state": {"history": "thin", "bull_history": "", "bear_history": ""},
    }
    score = _score_ticker(row, aggressive_cell, None, _empty_consensus("XYZ"), 20.0)
    assert score.n_analyst_reports_with_gaps == 2
    assert score.risk_level in {"WATCH", "ELEVATED"}


def test_audit_matrix_run_writes_outputs(tmp_path: Path):
    """End-to-end smoke: a minimal matrix run produces both output files."""
    matrix_run = tmp_path / "matrix_test"
    matrix_run.mkdir()
    (matrix_run / "verdict_ledger.json").write_text(json.dumps({
        "rows": [
            {
                "ticker": "AAPL",
                "classification": "PICK",
                "aggressive_pt": 210.0,
                "conservative_pt": 200.0,
                "current_price_usd": 195.0,
                "aggressive_pt_distance_pct": 7.7,
                "conservative_pt_distance_pct": 2.5,
                "pt_quality_flags": [],
                "pt_compression_pct": 2.5,
            },
        ],
    }))
    summary = audit_matrix_run(matrix_run, skip_consensus=True)
    assert summary["n_tickers"] == 1
    assert summary["n_high"] == 0
    assert len(summary["rows"]) == 1
    assert summary["rows"][0]["ticker"] == "AAPL"
