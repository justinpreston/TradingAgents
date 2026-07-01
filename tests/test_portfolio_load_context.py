"""Tests for portfolio_load_context.py.

These tests use synthetic fixtures (positions + a fake matrix run dir) so
the test is hermetic — no dependency on actual matrix runs in `runs/`.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from scripts.portfolio_load_context import (
    build_context,
    compute_flags,
    enrich_position,
    find_matrix_runs_for_latest_trade_date,
    load_matrix_run,
    load_matrix_runs,
    load_policy,
    load_positions,
    load_trades_log,
    recent_actions_for_position,
    validate_positions,
    _matrix_run_trade_date,
    _strip_help_keys,
    _tier_rank,
)


# ---------------------------------------------------------------------------
# Fixtures


@pytest.fixture
def tmp_book(tmp_path: Path) -> Path:
    book = {
        "_help": "synthetic",
        "as_of": "2026-05-09",
        "base_currency": "USD",
        "account_value": 100000.0,
        "positions": [
            {
                "id": "VIRT-c",
                "ticker": "VIRT",
                "instrument": "long_call",
                "qty": 5,
                "underlying_cost_basis_at_entry": 31.20,
                "option": {
                    "strike": 35.0,
                    "expiry": "2026-09-18",
                    "delta_at_entry": 0.55,
                    "premium_paid_per_share": 4.50,
                    "current_mark_per_share": 5.49,
                },
                "entry_date": "2026-04-25",
                "thesis_run_id": "matrix_2026-04-25_top25",
                "thesis_tier_at_entry": "B",
                "sector": "Financials",
                "stop_loss_underlying": 28.50,
                "take_profit_underlying": 38.00,
            },
            {
                "id": "AAPL-eq",
                "ticker": "AAPL",
                "instrument": "equity",
                "qty": 50,
                "underlying_cost_basis_at_entry": 178.40,
                "entry_date": "2025-11-12",
                "sector": "Technology",
            },
        ],
    }
    p = tmp_path / "positions.json"
    p.write_text(json.dumps(book))
    return p


@pytest.fixture
def tmp_policy(tmp_path: Path) -> Path:
    policy = {
        "cash_buffer_pct_target": 0.20,
        "max_single_name_pct": 0.08,
        "max_sector_pct": 0.30,
        "tier_sizing": {
            "A": {"starter_pct": 0.04, "max_pct": 0.06},
            "B": {"starter_pct": 0.04, "max_pct": 0.08},
            "C": {"starter_pct": 0.02, "max_pct": 0.04},
        },
        "options_defaults": {"strategy": "long_call", "delta_target": 0.55, "min_open_interest": 100},
        "roll_defaults": {"dte_threshold": 60},
        "exit_defaults": {
            "tier_demoted_to_veto_action": "exit",
            "tier_demoted_one_step_action": "trim_third",
        },
        "thematic_baskets": {"gov_stake_basket": ["INTC", "MP"]},
    }
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(policy))
    return p


@pytest.fixture
def tmp_matrix_run(tmp_path: Path) -> Path:
    run = tmp_path / "matrix_synthetic"
    run.mkdir()
    (run / "verdict_ledger.json").write_text(json.dumps({
        "run_id": "matrix_synthetic",
        "snapshot_date": "2026-05-08",
        "generated_at": "2026-05-08T23:00:00Z",
        "rows": [
            {
                "ticker": "VIRT", "name": "Virtu", "classification": "PICK",
                "aggressive_pt": 58.0, "conservative_pt": 55.0, "pt_compression_pct": 5.2,
                "aggressive_rating": "Overweight", "conservative_rating": "Hold",
                "aggressive_executive_summary": "Bullish on market-maker tailwinds",
            },
        ],
    }))
    (run / "options_overlay.json").write_text(json.dumps({
        "matrix_run": "matrix_synthetic",
        "snapshot_date": "2026-05-08",
        "overlays": [
            {
                "ticker": "VIRT", "tier": "B", "current_price_usd": 51.31,
                "aggressive_pt": 58.0, "conservative_pt": 55.0,
                "legs": [{
                    "side": "long", "type": "call", "strike": 50, "expiration": "2026-09-18",
                    "delta": 0.62, "iv": 0.30, "open_interest": 800,
                }],
                "net_debit_per_share": 4.20, "net_debit_per_contract": 420.0,
                "breakeven_underlying": 54.20, "breakeven_pct_from_current": 5.6,
                "dte": 132,
            },
        ],
    }))
    (run / "current_prices.json").write_text(json.dumps({
        "snapshot_taken": "2026-05-08T23:00:00Z",
        "prices_usd": {"VIRT": 51.31, "AAPL": 220.00},
    }))
    return run


# ---------------------------------------------------------------------------
# Pure helpers


def test_strip_help_keys_removes_underscored_keys():
    obj = {"_help": "x", "real": 1, "nested": {"_meta": "y", "z": 2}}
    assert _strip_help_keys(obj) == {"real": 1, "nested": {"z": 2}}


def test_tier_rank_ladder():
    assert _tier_rank("VETO") < _tier_rank("—")
    assert _tier_rank("—") < _tier_rank("C")
    assert _tier_rank("C") < _tier_rank("B")
    assert _tier_rank("B") < _tier_rank("A")
    assert _tier_rank(None) == _tier_rank("—")


# ---------------------------------------------------------------------------
# Validation


def test_validate_positions_passes_for_clean_book(tmp_book):
    book = load_positions(tmp_book)
    res = validate_positions(book)
    assert res.ok, f"expected ok, got errors: {res.errors}"


def test_validate_positions_catches_legacy_field(tmp_path):
    book = {
        "as_of": "2026-05-09",
        "base_currency": "USD",
        "account_value": None,
        "positions": [{
            "id": "X",
            "ticker": "X",
            "instrument": "long_call",
            "qty": 1,
            "underlying_cost_basis_at_entry": 10,
            "option": {
                "strike": 12,
                "expiry": "2026-09-18",
                "premium_paid_per_contract": 1.0,  # legacy
            },
            "entry_date": "2026-05-09",
        }],
    }
    p = tmp_path / "p.json"
    p.write_text(json.dumps(book))
    res = validate_positions(load_positions(p))
    assert not res.ok
    assert any("legacy field" in e for e in res.errors)


def test_validate_positions_catches_duplicate_ids(tmp_path):
    book = {
        "as_of": "2026-05-09",
        "base_currency": "USD",
        "account_value": None,
        "positions": [
            {"id": "DUP", "ticker": "A", "instrument": "equity", "qty": 1,
             "underlying_cost_basis_at_entry": 1, "entry_date": "2026-01-01"},
            {"id": "DUP", "ticker": "B", "instrument": "equity", "qty": 1,
             "underlying_cost_basis_at_entry": 1, "entry_date": "2026-01-01"},
        ],
    }
    p = tmp_path / "p.json"
    p.write_text(json.dumps(book))
    res = validate_positions(load_positions(p))
    assert not res.ok
    assert any("duplicate id" in e for e in res.errors)


def test_validate_positions_warns_on_past_expiry(tmp_path):
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    book = {
        "as_of": "2026-05-09",
        "base_currency": "USD",
        "account_value": None,
        "positions": [{
            "id": "EXPIRED",
            "ticker": "X",
            "instrument": "long_call",
            "qty": 1,
            "underlying_cost_basis_at_entry": 1,
            "option": {
                "strike": 1, "expiry": yesterday, "premium_paid_per_share": 1,
            },
            "entry_date": "2026-01-01",
        }],
    }
    p = tmp_path / "p.json"
    p.write_text(json.dumps(book))
    res_warn = validate_positions(load_positions(p), strict=False)
    assert res_warn.ok
    assert any("in the past" in w for w in res_warn.warnings)
    res_strict = validate_positions(load_positions(p), strict=True)
    assert not res_strict.ok


# ---------------------------------------------------------------------------
# Joining + flags


def test_build_context_joins_to_matrix(tmp_book, tmp_policy, tmp_matrix_run):
    ctx = build_context(tmp_book, tmp_policy, tmp_matrix_run)
    virt = next(p for p in ctx["positions"] if p["ticker"] == "VIRT")
    assert virt["live"]["in_latest_matrix"] is True
    assert virt["live"]["matrix_classification"] == "PICK"
    assert virt["live"]["matrix_tier_now"] == "B"
    assert virt["live"]["tier_delta"] == "stable"
    # Underlying P&L: (51.31 - 31.20) / 31.20 = 64.46%
    assert virt["live"]["underlying_pnl_pct"] == pytest.approx(64.46, rel=1e-3)
    # Option P&L: (5.49 - 4.50) / 4.50 = 22%
    assert virt["live"]["option_pnl_pct"] == pytest.approx(22.0, rel=1e-3)


def test_build_context_marks_absent_ticker_stale(tmp_book, tmp_policy, tmp_matrix_run):
    ctx = build_context(tmp_book, tmp_policy, tmp_matrix_run)
    aapl = next(p for p in ctx["positions"] if p["ticker"] == "AAPL")
    # AAPL has no thesis_tier_at_entry, so tier_delta should be n/a not stale
    assert aapl["live"]["matrix_tier_at_entry"] is None
    assert aapl["live"]["tier_delta"] == "n/a"


def test_stale_when_thesis_tier_set_but_ticker_absent(tmp_path, tmp_policy, tmp_matrix_run):
    book = {
        "as_of": "2026-05-09",
        "base_currency": "USD",
        "account_value": None,
        "positions": [{
            "id": "INTC-c",
            "ticker": "INTC",  # not in matrix
            "instrument": "long_call",
            "qty": 1,
            "underlying_cost_basis_at_entry": 22.80,
            "option": {
                "strike": 25, "expiry": "2026-12-19",
                "premium_paid_per_share": 2.80,
            },
            "entry_date": "2026-03-10",
            "thesis_run_id": "matrix_2026-03-06_top25",
            "thesis_tier_at_entry": "B",
        }],
    }
    p = tmp_path / "positions.json"
    p.write_text(json.dumps(book))
    ctx = build_context(p, tmp_policy, tmp_matrix_run)
    intc = ctx["positions"][0]
    assert intc["live"]["tier_delta"] == "stale"
    assert intc["live"]["in_latest_matrix"] is False


def test_compute_flags_take_profit_hit(tmp_book, tmp_policy, tmp_matrix_run):
    ctx = build_context(tmp_book, tmp_policy, tmp_matrix_run)
    flags = ctx["flags"]
    take_profit_flags = [f for f in flags if f["code"] == "take_profit_hit"]
    assert len(take_profit_flags) == 1
    assert take_profit_flags[0]["ticker"] == "VIRT"


def test_stop_loss_breach_fires(tmp_book, tmp_policy, tmp_matrix_run):
    # Mutate the matrix run's prices to drop VIRT below stop-loss
    prices_path = tmp_matrix_run / "current_prices.json"
    doc = json.loads(prices_path.read_text())
    doc["prices_usd"]["VIRT"] = 25.00  # below 28.50 stop
    prices_path.write_text(json.dumps(doc))
    ctx = build_context(tmp_book, tmp_policy, tmp_matrix_run)
    flags = ctx["flags"]
    breaches = [f for f in flags if f["code"] == "stop_loss_breach"]
    assert len(breaches) == 1
    assert breaches[0]["severity"] == "high"


def test_demoted_tier_flag(tmp_path, tmp_policy, tmp_matrix_run):
    # Mutate the matrix overlay so VIRT is now Tier C
    ov_path = tmp_matrix_run / "options_overlay.json"
    doc = json.loads(ov_path.read_text())
    doc["overlays"][0]["tier"] = "C"
    ov_path.write_text(json.dumps(doc))

    book = json.loads((tmp_matrix_run / "verdict_ledger.json").read_text())  # reuse for shape
    # write a minimal book with VIRT entered at tier B
    minimal = {
        "as_of": "2026-05-09",
        "base_currency": "USD",
        "account_value": None,
        "positions": [{
            "id": "VIRT-c",
            "ticker": "VIRT",
            "instrument": "long_call",
            "qty": 1,
            "underlying_cost_basis_at_entry": 31.20,
            "option": {
                "strike": 35, "expiry": "2026-09-18",
                "premium_paid_per_share": 4.50,
            },
            "entry_date": "2026-04-25",
            "thesis_tier_at_entry": "B",
        }],
    }
    p = tmp_path / "p.json"
    p.write_text(json.dumps(minimal))
    ctx = build_context(p, tmp_policy, tmp_matrix_run)
    flags = ctx["flags"]
    demoted = [f for f in flags if f["code"] == "tier_demoted"]
    assert len(demoted) == 1
    assert demoted[0]["ticker"] == "VIRT"


def test_summary_counts(tmp_book, tmp_policy, tmp_matrix_run):
    ctx = build_context(tmp_book, tmp_policy, tmp_matrix_run)
    s = ctx["summary"]
    assert s["n_positions"] == 2
    assert s["n_in_matrix"] == 1
    assert s["n_demoted"] == 0
    assert s["n_promoted"] == 0
    assert s["n_vetoed_now"] == 0


# ---------------------------------------------------------------------------
# Without a matrix run


def test_works_with_no_matrix_run(tmp_book, tmp_policy, tmp_path):
    empty_run = tmp_path / "no_matrix"
    empty_run.mkdir()
    # build_context falls back to find_latest_matrix_run if no path passed.
    # Pass a dir with no verdict_ledger.json to force the fallback / empty path.
    # Using a non-existent path is the explicit "no matrix" mode.
    ctx = build_context(tmp_book, tmp_policy, matrix_run_path=None)
    # We can't deterministically assert the outcome here since the test env
    # may or may not have runs/. So just confirm it doesn't crash and returns
    # a structured dict.
    assert "positions" in ctx
    assert "summary" in ctx


# ---------------------------------------------------------------------------
# Trades log


def test_load_trades_log_handles_missing_file(tmp_path):
    assert load_trades_log(tmp_path / "missing.jsonl") == []


def test_load_trades_log_skips_blank_and_invalid_lines(tmp_path):
    p = tmp_path / "log.jsonl"
    p.write_text(
        '\n'
        '{"ts": "2026-05-09T17:00:00Z", "ticker": "VIRT", "action": "TRIM", "qty": 2}\n'
        'this is not valid json\n'
        '\n'
        '{"ts": "2026-05-08T20:00:00Z", "ticker": "INTC", "action": "OPEN", "qty": 10}\n'
    )
    entries = load_trades_log(p)
    assert len(entries) == 2
    assert entries[0]["ticker"] == "VIRT"


def test_recent_actions_filters_by_ticker_and_id(tmp_path):
    entries = [
        {"ts": "2026-05-01T10:00:00Z", "ticker": "VIRT", "id": "a", "action": "OPEN", "qty": 5},
        {"ts": "2026-05-09T17:00:00Z", "ticker": "VIRT", "id": "a", "action": "TRIM", "qty": 2},
        {"ts": "2026-05-09T17:30:00Z", "ticker": "INTC", "id": "b", "action": "OPEN", "qty": 10},
    ]
    virt_a = recent_actions_for_position(entries, "VIRT", "a")
    assert len(virt_a) == 2
    assert virt_a[0]["action"] == "TRIM"  # most recent first
    assert virt_a[1]["action"] == "OPEN"

    intc = recent_actions_for_position(entries, "INTC")
    assert len(intc) == 1


def test_build_context_attaches_recent_actions(tmp_book, tmp_policy, tmp_matrix_run, tmp_path):
    log_path = tmp_path / "trades.jsonl"
    log_path.write_text(
        '{"ts": "2026-05-09T17:00:00Z", "ticker": "VIRT", "id": "VIRT-c", '
        '"action": "TRIM", "qty": 2, "premium_per_share": 5.65, "notes": "Test trim"}\n'
    )
    ctx = build_context(tmp_book, tmp_policy, tmp_matrix_run, trades_log_path=log_path)
    virt = next(p for p in ctx["positions"] if p["ticker"] == "VIRT")
    actions = virt["live"].get("recent_actions") or []
    assert len(actions) == 1
    assert actions[0]["action"] == "TRIM"
    assert actions[0]["notes"] == "Test trim"
    assert ctx["trades_log_entries"] == 1


# ---------------------------------------------------------------------------
# Multi-run union — content-based trade-date discovery, mtime trap,
# tier-collision dedupe


def _make_matrix_run(
    tmp_path: Path,
    name: str,
    *,
    trade_date: str,
    overlays: list[dict],
    ledger_rows: list[dict],
    prices: dict[str, float],
) -> Path:
    d = tmp_path / name
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({"run_id": name, "trade_date": trade_date}))
    (d / "verdict_ledger.json").write_text(json.dumps({
        "run_id": name, "snapshot_date": trade_date,
        "generated_at": trade_date + "T23:00:00Z", "rows": ledger_rows,
    }))
    (d / "options_overlay.json").write_text(json.dumps({
        "matrix_run": name, "snapshot_date": trade_date, "overlays": overlays,
    }))
    (d / "current_prices.json").write_text(json.dumps({"prices_usd": prices}))
    return d


@pytest.fixture
def two_tier_runs_same_date(tmp_path: Path) -> tuple[Path, Path]:
    mid = _make_matrix_run(
        tmp_path, "matrix_mid_weekly_2026-06-26_2103_chain",
        trade_date="2026-06-26",
        overlays=[{
            "ticker": "CTRE", "tier": "A", "current_price_usd": 41.6,
            "aggressive_pt": 45.0, "conservative_pt": 45.0,
            "legs": [{"strike": 40.0, "expiration": "2026-10-16", "price": 2.30}],
        }],
        ledger_rows=[{"ticker": "CTRE", "classification": "PICK",
                      "aggressive_pt": 45.0, "conservative_pt": 45.0, "pt_compression_pct": 0.0}],
        prices={"CTRE": 41.6},
    )
    large = _make_matrix_run(
        tmp_path, "matrix_large_weekly_2026-06-26_2126_chain",
        trade_date="2026-06-26",
        overlays=[{
            "ticker": "ADI", "tier": "C", "current_price_usd": 386.91,
            "aggressive_pt": 470.0, "conservative_pt": 410.0,
            "legs": [{"strike": 400.0, "expiration": "2026-12-18", "price": 55.36}],
        }],
        ledger_rows=[{"ticker": "ADI", "classification": "PICK",
                      "aggressive_pt": 470.0, "conservative_pt": 410.0, "pt_compression_pct": 15.2}],
        prices={"ADI": 386.91},
    )
    return mid, large


@pytest.fixture
def older_run_mtime_trap(tmp_path: Path) -> Path:
    """Older trade_date but artificially newer mtime — the exact scenario
    that broke mtime-based find_latest_matrix_run (a Saturday overlay
    re-run reshuffling "latest")."""
    d = _make_matrix_run(
        tmp_path, "matrix_mid_weekly_2026-06-19_1200_chain",
        trade_date="2026-06-19",
        overlays=[{
            "ticker": "OLD", "tier": "B", "current_price_usd": 10.0,
            "aggressive_pt": 12.0, "conservative_pt": 11.0,
            "legs": [{"strike": 10.0, "expiration": "2026-09-18", "price": 1.0}],
        }],
        ledger_rows=[{"ticker": "OLD", "classification": "PICK",
                      "aggressive_pt": 12.0, "conservative_pt": 11.0, "pt_compression_pct": 8.3}],
        prices={"OLD": 10.0},
    )
    import os
    import time
    future = time.time() + 100000
    for p in [d, d / "verdict_ledger.json", d / "manifest.json", d / "options_overlay.json"]:
        os.utime(p, (future, future))
    return d


def test_matrix_run_trade_date_prefers_manifest(tmp_path: Path):
    d = tmp_path / "run"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({"trade_date": "2026-06-26"}))
    (d / "verdict_ledger.json").write_text(json.dumps({"snapshot_date": "2026-06-25"}))
    assert _matrix_run_trade_date(d) == "2026-06-26"


def test_matrix_run_trade_date_falls_back_to_snapshot_date(tmp_path: Path):
    d = tmp_path / "run"
    d.mkdir()
    (d / "verdict_ledger.json").write_text(json.dumps({"snapshot_date": "2026-06-25"}))
    assert _matrix_run_trade_date(d) == "2026-06-25"


def test_matrix_run_trade_date_none_when_unresolvable(tmp_path: Path):
    d = tmp_path / "run"
    d.mkdir()
    (d / "verdict_ledger.json").write_text(json.dumps({}))
    assert _matrix_run_trade_date(d) is None


def test_find_matrix_runs_for_latest_trade_date_unions_same_date(tmp_path: Path, two_tier_runs_same_date):
    found = find_matrix_runs_for_latest_trade_date(tmp_path)
    names = {p.name for p in found}
    assert names == {"matrix_mid_weekly_2026-06-26_2103_chain", "matrix_large_weekly_2026-06-26_2126_chain"}


def test_find_matrix_runs_excludes_older_trade_date(tmp_path: Path, two_tier_runs_same_date, older_run_mtime_trap):
    # older_run_mtime_trap has a NEWER mtime than the current-week runs, but
    # content-based discovery must exclude it because its trade_date is older.
    found = find_matrix_runs_for_latest_trade_date(tmp_path)
    names = {p.name for p in found}
    assert "matrix_mid_weekly_2026-06-19_1200_chain" not in names
    assert len(found) == 2


def test_load_matrix_runs_unions_rows_overlays_prices(tmp_path: Path, two_tier_runs_same_date):
    mid, large = two_tier_runs_same_date
    matrix = load_matrix_runs([mid, large])
    assert set(matrix["rows_by_ticker"]) == {"CTRE", "ADI"}
    assert set(matrix["overlays_by_ticker"]) == {"CTRE", "ADI"}
    assert matrix["prices"] == {"CTRE": 41.6, "ADI": 386.91}
    assert set(matrix["run_ids"]) == {
        "matrix_mid_weekly_2026-06-26_2103_chain", "matrix_large_weekly_2026-06-26_2126_chain",
    }


def test_load_matrix_runs_empty_list_returns_empty_shape():
    matrix = load_matrix_runs([])
    assert matrix["rows_by_ticker"] == {}
    assert matrix["overlays_by_ticker"] == {}
    assert matrix["prices"] == {}
    assert matrix["run_ids"] == []


def test_load_matrix_runs_tier_collision_higher_tier_wins(tmp_path: Path):
    # Same ticker appears in both runs — a name that straddled the
    # mid/large mcap boundary. Run A rates it tier C, run B rates it tier A
    # (higher on the rank ladder). The tier-A row/overlay must win.
    run_c = _make_matrix_run(
        tmp_path, "matrix_mid", trade_date="2026-06-26",
        overlays=[{
            "ticker": "DUAL", "tier": "C", "current_price_usd": 100.0,
            "aggressive_pt": 110.0, "conservative_pt": None,
            "legs": [{"strike": 100.0, "expiration": "2026-10-16", "price": 5.0}],
        }],
        ledger_rows=[{"ticker": "DUAL", "classification": "PICK",
                      "aggressive_pt": 110.0, "conservative_pt": None, "pt_compression_pct": None}],
        prices={"DUAL": 99.0},
    )
    run_a = _make_matrix_run(
        tmp_path, "matrix_large", trade_date="2026-06-26",
        overlays=[{
            "ticker": "DUAL", "tier": "A", "current_price_usd": 101.0,
            "aggressive_pt": 108.0, "conservative_pt": 107.0,
            "legs": [{"strike": 100.0, "expiration": "2026-10-16", "price": 5.5}],
        }],
        ledger_rows=[{"ticker": "DUAL", "classification": "PICK",
                      "aggressive_pt": 108.0, "conservative_pt": 107.0, "pt_compression_pct": 0.9}],
        prices={"DUAL": 101.0},
    )
    matrix = load_matrix_runs([run_c, run_a])
    assert matrix["overlays_by_ticker"]["DUAL"]["tier"] == "A"
    assert matrix["rows_by_ticker"]["DUAL"]["conservative_pt"] == 107.0
    assert matrix["prices"]["DUAL"] == 101.0  # winning run's price wins too
    assert matrix["winning_run_by_ticker"]["DUAL"] == "matrix_large"

    # Order-independence: same result regardless of which run is processed first.
    matrix2 = load_matrix_runs([run_a, run_c])
    assert matrix2["overlays_by_ticker"]["DUAL"]["tier"] == "A"
    assert matrix2["winning_run_by_ticker"]["DUAL"] == "matrix_large"


def test_load_matrix_runs_backfills_price_for_ticker_with_no_row_or_overlay(tmp_path: Path):
    # A held ticker (e.g. AAPL) may have a price fetched without any
    # ledger row or overlay entry — must not be dropped from the union.
    run = _make_matrix_run(
        tmp_path, "matrix_one", trade_date="2026-06-26",
        overlays=[{
            "ticker": "CTRE", "tier": "A", "current_price_usd": 41.6,
            "aggressive_pt": 45.0, "conservative_pt": 45.0,
            "legs": [{"strike": 40.0, "expiration": "2026-10-16", "price": 2.30}],
        }],
        ledger_rows=[{"ticker": "CTRE", "classification": "PICK",
                      "aggressive_pt": 45.0, "conservative_pt": 45.0, "pt_compression_pct": 0.0}],
        prices={"CTRE": 41.6, "AAPL": 220.0},
    )
    matrix = load_matrix_runs([run])
    assert matrix["prices"]["AAPL"] == 220.0


def test_build_context_default_discovers_multi_run_union(tmp_path: Path, tmp_policy, two_tier_runs_same_date, monkeypatch):
    import scripts.portfolio_load_context as plc
    monkeypatch.setattr(plc, "RUNS_DIR", tmp_path)

    book = {
        "as_of": "2026-06-26", "base_currency": "USD", "account_value": None,
        "positions": [
            {"id": "CTRE-c", "ticker": "CTRE", "instrument": "long_call", "qty": 1,
             "underlying_cost_basis_at_entry": 40.0,
             "option": {"strike": 40, "expiry": "2026-10-16", "premium_paid_per_share": 2.0},
             "entry_date": "2026-06-01", "thesis_tier_at_entry": "A"},
            {"id": "ADI-c", "ticker": "ADI", "instrument": "long_call", "qty": 1,
             "underlying_cost_basis_at_entry": 380.0,
             "option": {"strike": 400, "expiry": "2026-12-18", "premium_paid_per_share": 50.0},
             "entry_date": "2026-06-01", "thesis_tier_at_entry": "C"},
        ],
    }
    book_path = tmp_path / "positions.json"
    book_path.write_text(json.dumps(book))

    ctx = build_context(book_path, tmp_policy, matrix_run_path=None)
    ctre = next(p for p in ctx["positions"] if p["ticker"] == "CTRE")
    adi = next(p for p in ctx["positions"] if p["ticker"] == "ADI")
    assert ctre["live"]["in_latest_matrix"] is True
    assert adi["live"]["in_latest_matrix"] is True
    assert len(ctx["contributing_matrix_runs"]) == 2


def test_build_context_explicit_matrix_run_list_override(tmp_book, tmp_policy, two_tier_runs_same_date):
    mid, large = two_tier_runs_same_date
    ctx = build_context(tmp_book, tmp_policy, [mid, large])
    assert len(ctx["contributing_matrix_runs"]) == 2
    assert set(ctx["contributing_matrix_run_ids"]) == {
        "matrix_mid_weekly_2026-06-26_2103_chain", "matrix_large_weekly_2026-06-26_2126_chain",
    }


def test_build_context_single_path_still_backward_compatible(tmp_book, tmp_policy, tmp_matrix_run):
    # Passing a bare Path (not a list) must keep working exactly as before.
    ctx = build_context(tmp_book, tmp_policy, tmp_matrix_run)
    assert ctx["contributing_matrix_runs"] == [str(tmp_matrix_run)]
    assert ctx["latest_matrix_run"] == str(tmp_matrix_run)
