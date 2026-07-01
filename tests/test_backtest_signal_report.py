"""Tests for scripts/backtest_signal_report.py."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from scripts.backtest_signal_report import (
    _pearson,
    _spearman,
    bucket_market_cap,
    build_report,
    main,
    render_markdown,
)


def _make_db(db_path: Path, matrix_rows: list[dict], chronos_rows: list[dict], run_rows: list[dict]) -> None:
    con = sqlite3.connect(str(db_path))
    con.execute(
        """
        CREATE TABLE matrix_picks (
            run_id TEXT NOT NULL, rank INTEGER, ticker TEXT NOT NULL, name TEXT,
            sector_sic TEXT, market_cap REAL, composite_score REAL, current_price REAL,
            aggressive_rating TEXT, aggressive_pt REAL, aggressive_upside_pct REAL,
            aggressive_horizon TEXT, conservative_rating TEXT, conservative_pt REAL,
            conservative_upside_pct REAL, conservative_horizon TEXT,
            pt_compression_pct REAL, classification TEXT, tier TEXT,
            aggressive_executive_summary TEXT,
            PRIMARY KEY (run_id, ticker)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE chronos_forecasts (
            run_id TEXT NOT NULL, ticker TEXT NOT NULL, tier TEXT, classification TEXT,
            current_price REAL, agent_aggressive_pt REAL, agent_conservative_pt REAL,
            agent_horizon TEXT, prediction_length_days INTEGER, context_bars_used INTEGER,
            forecast_p10 REAL, forecast_p50 REAL, forecast_p90 REAL,
            forecast_pct_p10 REAL, forecast_pct_p50 REAL, forecast_pct_p90 REAL,
            agent_pt_quantile REAL, agent_pt_vs_median_pct REAL, model_view TEXT,
            agent_inside_cone INTEGER,
            PRIMARY KEY (run_id, ticker)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, run_type TEXT NOT NULL, run_path TEXT NOT NULL,
            snapshot_date TEXT, generated_at TEXT, n_candidates INTEGER,
            n_picks INTEGER, n_vetoed INTEGER, indexed_at TEXT NOT NULL, source_mtime REAL
        )
        """
    )
    for row in matrix_rows:
        cols = ", ".join(row.keys())
        qs = ", ".join(["?"] * len(row))
        con.execute(f"INSERT INTO matrix_picks ({cols}) VALUES ({qs})", list(row.values()))
    for row in chronos_rows:
        cols = ", ".join(row.keys())
        qs = ", ".join(["?"] * len(row))
        con.execute(f"INSERT INTO chronos_forecasts ({cols}) VALUES ({qs})", list(row.values()))
    for row in run_rows:
        cols = ", ".join(row.keys())
        qs = ", ".join(["?"] * len(row))
        con.execute(f"INSERT INTO runs ({cols}) VALUES ({qs})", list(row.values()))
    con.commit()
    con.close()


@pytest.fixture
def synthetic_backtest(tmp_path: Path) -> Path:
    """12 rows: a clean monotonic-ish market_cap vs roi_hold relationship
    plus one row with no matrix/chronos match (LEFT-join coverage) and one
    row with roi_hold=None (must be skipped)."""
    results = []
    # 10 rows with a strong negative mcap-vs-roi relationship (monotonic
    # decreasing so pearson/spearman should both be close to -1).
    for i in range(10):
        results.append(
            {
                "run_id": f"matrix_synthetic_{i}",
                "ticker": f"T{i}",
                "tier": "A",
                "rec_date": "2026-01-01",
                "rec_px": 100.0,
                "aggr_pt": 110.0,
                "cons_pt": 115.0,
                "strike": 100.0,
                "expiration": "2026-04-15" if i % 2 == 0 else "2026-09-15",
                "occ": f"O:T{i}260415C00100000",
                "measure_date": "2026-06-26",
                "entry": 5.0,
                "hold_exit": 5.0,
                "cons_pt_hit": False,
                "aggr_pt_hit": i < 4,
                "roi_hold": 20.0 - 2.0 * i,  # 20, 18, ..., 2 (monotonic decreasing)
                "roi_exit_aggr": 25.0 - 2.0 * i,
                "exit_aggr_px": 6.0,
                "roi_exit_cons": 10.0,
                "exit_cons_px": 5.5,
                "roi_exit_half": 12.0,
                "status": "ok",
            }
        )
    # One row with no roi_hold -- must be skipped entirely.
    results.append(
        {
            "run_id": "matrix_synthetic_0",
            "ticker": "SKIPME",
            "tier": "A",
            "rec_date": "2026-01-01",
            "rec_px": 50.0,
            "aggr_pt": 55.0,
            "cons_pt": None,
            "strike": 50.0,
            "expiration": "2026-04-15",
            "occ": "O:SKIPME260415C00050000",
            "measure_date": "2026-06-26",
            "entry": 3.0,
            "hold_exit": None,
            "cons_pt_hit": None,
            "aggr_pt_hit": None,
            "roi_hold": None,
            "roi_exit_aggr": None,
            "exit_aggr_px": None,
            "roi_exit_cons": None,
            "exit_cons_px": None,
            "roi_exit_half": None,
            "status": "no_data",
        }
    )
    # One row whose (run_id, ticker) has no catalog match at all.
    results.append(
        {
            "run_id": "matrix_no_such_run",
            "ticker": "ORPHAN",
            "tier": "B",
            "rec_date": "2026-01-01",
            "rec_px": 30.0,
            "aggr_pt": 33.0,
            "cons_pt": 34.0,
            "strike": 30.0,
            "expiration": "2026-07-01",
            "occ": "O:ORPHAN260701C00030000",
            "measure_date": "2026-06-26",
            "entry": 2.0,
            "hold_exit": 2.5,
            "cons_pt_hit": False,
            "aggr_pt_hit": False,
            "roi_hold": 13.8,
            "roi_exit_aggr": 13.8,
            "exit_aggr_px": 2.5,
            "roi_exit_cons": 5.0,
            "exit_cons_px": 2.1,
            "roi_exit_half": 8.0,
            "status": "ok",
        }
    )

    doc = {
        "measure_date": "2026-06-26",
        "deduped": True,
        "results": results,
        "summary": {},
    }
    path = tmp_path / "backtest_synthetic.json"
    path.write_text(json.dumps(doc))
    return path


@pytest.fixture
def synthetic_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "index.db"
    matrix_rows = []
    chronos_rows = []
    for i in range(10):
        matrix_rows.append(
            {
                "run_id": f"matrix_synthetic_{i}",
                "rank": i + 1,
                "ticker": f"T{i}",
                "name": f"Ticker {i}",
                "sector_sic": "TEST SECTOR",
                "market_cap": 1e9 * (i + 1),  # increasing mcap, decreasing roi -> negative corr
                "composite_score": 50.0 + i,
                "current_price": 100.0,
                "aggressive_rating": "BUY",
                "aggressive_pt": 110.0,
                "aggressive_upside_pct": 10.0,
                "aggressive_horizon": "6mo",
                "conservative_rating": "HOLD",
                "conservative_pt": 105.0,
                "conservative_upside_pct": 5.0,
                "conservative_horizon": "6mo",
                "pt_compression_pct": 3.0,
                "classification": "PICK",
                "tier": "A",
                "aggressive_executive_summary": "test",
            }
        )
        chronos_rows.append(
            {
                "run_id": f"matrix_synthetic_{i}",
                "ticker": f"T{i}",
                "tier": "A",
                "classification": "PICK",
                "current_price": 100.0,
                "agent_aggressive_pt": 110.0,
                "agent_conservative_pt": 105.0,
                "agent_horizon": "6mo",
                "prediction_length_days": 90,
                "context_bars_used": 504,
                "forecast_p10": 90.0,
                "forecast_p50": 105.0,
                "forecast_p90": 120.0,
                "forecast_pct_p10": -10.0,
                "forecast_pct_p50": 5.0,
                "forecast_pct_p90": 20.0,
                "agent_pt_quantile": 0.5,
                "agent_pt_vs_median_pct": 5.0,
                "model_view": "inside",
                "agent_inside_cone": 1,
            }
        )
    # SKIPME's matrix row exists but its backtest row has roi_hold=None, so it
    # must never surface in the joined output.
    matrix_rows.append(
        {
            "run_id": "matrix_synthetic_0",
            "rank": 99,
            "ticker": "SKIPME",
            "name": "Skip Me",
            "sector_sic": "TEST SECTOR",
            "market_cap": 5e9,
            "composite_score": 60.0,
            "current_price": 50.0,
            "aggressive_rating": "BUY",
            "aggressive_pt": 55.0,
            "aggressive_upside_pct": 10.0,
            "aggressive_horizon": "6mo",
            "conservative_rating": None,
            "conservative_pt": None,
            "conservative_upside_pct": None,
            "conservative_horizon": None,
            "pt_compression_pct": None,
            "classification": "PICK",
            "tier": "C",
            "aggressive_executive_summary": "test",
        }
    )
    run_rows = [
        {
            "run_id": "matrix_synthetic_0",
            "run_type": "matrix",
            "run_path": "runs/matrix_synthetic_0",
            "snapshot_date": "2026-01-01",
            "generated_at": "2026-01-01T12:00:00+00:00",
            "n_candidates": 25,
            "n_picks": 11,
            "n_vetoed": 14,
            "indexed_at": "2026-01-02T00:00:00+00:00",
            "source_mtime": 0.0,
        },
        {
            "run_id": "matrix_synthetic_5",
            "run_type": "matrix",
            "run_path": "runs/matrix_synthetic_5",
            "snapshot_date": "2026-01-08",
            "generated_at": "2026-01-08T12:00:00+00:00",
            "n_candidates": 25,
            "n_picks": 3,
            "n_vetoed": 22,
            "indexed_at": "2026-01-09T00:00:00+00:00",
            "source_mtime": 0.0,
        },
    ]
    _make_db(db_path, matrix_rows, chronos_rows, run_rows)
    return db_path


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def test_spearman_on_known_monotonic_series():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    ys = [50.0, 40.0, 30.0, 20.0, 10.0]  # perfectly monotonic decreasing
    assert _spearman(xs, ys) == pytest.approx(-1.0)
    assert _pearson(xs, ys) == pytest.approx(-1.0)


def test_spearman_handles_ties():
    xs = [1.0, 1.0, 2.0, 3.0]
    ys = [10.0, 10.0, 20.0, 5.0]
    # Should not raise, and should be a valid correlation in [-1, 1].
    result = _spearman(xs, ys)
    assert result is not None
    assert -1.0 <= result <= 1.0


# ---------------------------------------------------------------------------
# Percent-unit rendering
# ---------------------------------------------------------------------------


def test_percent_unit_rendering_not_multiplied(synthetic_backtest, synthetic_db):
    report = build_report(synthetic_backtest, synthetic_db)
    markdown = render_markdown(report)
    # ORPHAN's roi_hold=13.8 has no catalog match, so it won't appear in
    # bucket tables, but the raw report numbers (buckets from T0..T9) must
    # render as e.g. "+20.0%", never "2000.0%".
    assert "2000.0%" not in markdown
    assert "1380" not in markdown
    # Sanity: a known median value renders with a single %-suffix.
    mcap_bucket = next(b for b in report["buckets"]["market_cap"] if b["n"] > 0)
    assert mcap_bucket["median_roi_hold"] is not None
    rendered = f"{mcap_bucket['median_roi_hold']:.1f}%"
    assert rendered in markdown or f"+{rendered}" in markdown


# ---------------------------------------------------------------------------
# Join behavior
# ---------------------------------------------------------------------------


def test_join_left_join_semantics(synthetic_backtest, synthetic_db):
    report = build_report(synthetic_backtest, synthetic_db)
    # 10 T-rows + ORPHAN = 11 rows with non-null roi_hold (SKIPME excluded).
    assert report["n_rows_with_roi_hold"] == 11
    # ORPHAN has no catalog match at all -> matrix matched count is 10.
    assert report["n_matrix_matched"] == 10
    assert report["n_chronos_matched"] == 10


def test_skips_rows_with_null_roi_hold(synthetic_backtest, synthetic_db):
    report = build_report(synthetic_backtest, synthetic_db)
    # SKIPME must never appear anywhere in the joined/bucketed output.
    report_str = json.dumps(report)
    assert "SKIPME" not in report_str


# ---------------------------------------------------------------------------
# Bucket suppression
# ---------------------------------------------------------------------------


def test_bucket_suppressed_below_min_n(synthetic_backtest, synthetic_db):
    report = build_report(synthetic_backtest, synthetic_db)
    # All 10 T-rows have market_cap in [1B, 10B) -> all land in "mid".
    # ORPHAN has no matrix match, so mid n=10, and large/mega buckets (n=0)
    # must be suppressed entirely (not just zero-filled).
    labels = {b["bucket"] for b in report["buckets"]["market_cap"]}
    assert "mid (<10B)" in labels
    assert "large (10-200B)" not in labels
    assert "mega (>200B)" not in labels


def test_bucket_market_cap_function_directly():
    # 3 rows in one bucket (below MIN_BUCKET_N=4) should be suppressed.
    joined = [
        {
            "backtest": {"roi_hold": 10.0, "roi_exit_aggr": 12.0, "aggr_pt_hit": True},
            "matrix": {"market_cap": 5e9},
            "chronos": None,
        }
        for _ in range(3)
    ]
    result = bucket_market_cap(joined)
    assert result == []


# ---------------------------------------------------------------------------
# Read-only DB
# ---------------------------------------------------------------------------


def test_db_not_modified(synthetic_backtest, synthetic_db):
    before = hashlib.sha256(synthetic_db.read_bytes()).hexdigest()
    build_report(synthetic_backtest, synthetic_db)
    after = hashlib.sha256(synthetic_db.read_bytes()).hexdigest()
    assert before == after


def test_db_opened_readonly_rejects_write_attempt(synthetic_backtest, synthetic_db):
    # Sanity check that the connection helper truly opens read-only: trying
    # to write through it must fail rather than silently succeed.
    from scripts.backtest_signal_report import _connect_readonly

    con = _connect_readonly(synthetic_db)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO runs (run_id, run_type, run_path, indexed_at) VALUES ('x','matrix','p','2026-01-01')")
        con.commit()
    con.close()


# ---------------------------------------------------------------------------
# CLI end-to-end
# ---------------------------------------------------------------------------


def test_cli_end_to_end_produces_both_outputs(tmp_path: Path, synthetic_backtest, synthetic_db):
    md_out = tmp_path / "out" / "report.md"
    json_out = tmp_path / "out" / "report.json"

    rc = main(
        [
            "--backtest",
            str(synthetic_backtest),
            "--db",
            str(synthetic_db),
            "--output",
            str(md_out),
            "--json-output",
            str(json_out),
        ]
    )

    assert rc == 0
    assert md_out.exists()
    assert json_out.exists()

    doc = json.loads(json_out.read_text())
    assert doc["schema_version"] == 1
    assert doc["measure_date"] == "2026-06-26"
    assert "correlations" in doc
    assert "buckets" in doc
    assert "veto_by_week" in doc
    assert "caveats" in doc

    markdown = md_out.read_text()
    assert markdown.startswith("# Backtest Signal Report")
    assert "## Veto rate by week" in markdown
    assert "## Caveats" in markdown


def test_cli_default_output_paths(tmp_path: Path, synthetic_backtest, synthetic_db):
    rc = main(["--backtest", str(synthetic_backtest), "--db", str(synthetic_db)])
    assert rc == 0
    assert (synthetic_backtest.parent / "backtest_signal_report.md").exists()
    assert (synthetic_backtest.parent / "backtest_signal_report.json").exists()


def test_veto_by_week_from_runs_table(synthetic_backtest, synthetic_db):
    report = build_report(synthetic_backtest, synthetic_db)
    weeks = {row["week"]: row for row in report["veto_by_week"]}
    assert weeks["2026-01-01"]["n_picks"] == 11
    assert weeks["2026-01-01"]["n_vetoed"] == 14
    assert weeks["2026-01-01"]["n_total"] == 25
    assert weeks["2026-01-01"]["veto_pct"] == pytest.approx(56.0)
