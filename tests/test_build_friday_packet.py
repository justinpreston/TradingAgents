"""Tests for scripts/build_friday_packet.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.build_friday_packet import (
    build_packet,
    build_tickets,
    fetch_live_prices,
    find_latest_trade_date,
    find_matrix_runs_for_trade_date,
    load_run_artifacts,
    overlay_health,
    render_html,
    render_markdown,
    write_packet,
)


# ---------------------------------------------------------------------------
# Fixtures


def _write_run(
    runs_dir: Path,
    name: str,
    *,
    trade_date: str,
    overlays: list[dict],
    ledger_rows: list[dict],
    overlay_generated_at: str | None = "2026-06-26T23:10:00Z",
    include_overlay: bool = True,
) -> Path:
    d = runs_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({"run_id": name, "trade_date": trade_date}))
    (d / "verdict_ledger.json").write_text(json.dumps({
        "run_id": name,
        "snapshot_date": trade_date,
        "generated_at": trade_date + "T23:00:00Z",
        "rows": ledger_rows,
    }))
    if include_overlay:
        doc = {
            "matrix_run": name,
            "snapshot_date": trade_date,
            "overlays": overlays,
        }
        if overlay_generated_at is not None:
            doc["generated_at"] = overlay_generated_at
        (d / "options_overlay.json").write_text(json.dumps(doc))
    (d / "current_prices.json").write_text(json.dumps({"prices_usd": {}}))
    return d


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "runs"
    d.mkdir()
    return d


@pytest.fixture
def two_tier_runs(runs_dir: Path) -> tuple[Path, Path]:
    mid = _write_run(
        runs_dir,
        "matrix_mid_weekly_2026-06-26_2103_chain",
        trade_date="2026-06-26",
        overlays=[
            {
                "ticker": "CTRE", "tier": "A", "current_price_usd": 41.6,
                "aggressive_pt": 45.0, "conservative_pt": 45.0,
                "legs": [{
                    "symbol": "O:CTRE261016C00040000", "strike": 40.0,
                    "expiration": "2026-10-16", "price": 2.30, "open_interest": 72,
                }],
                "liquidity_warnings": ["OI below preferred 100"],
            },
            {
                "ticker": "VIRT", "tier": "B", "current_price_usd": 51.31,
                "aggressive_pt": 62.0, "conservative_pt": 55.0,
                "legs": [{
                    "symbol": "O:VIRT260918C00050000", "strike": 50.0,
                    "expiration": "2026-09-18", "price": 4.20, "open_interest": 800,
                }],
            },
        ],
        ledger_rows=[
            {"ticker": "CTRE", "classification": "PICK", "current_price": 41.6,
             "aggressive_pt": 45.0, "conservative_pt": 45.0, "pt_compression_pct": 0.0,
             "aggressive_executive_summary": "Healthcare REIT tailwind."},
            {"ticker": "VIRT", "classification": "PICK", "current_price": 51.31,
             "aggressive_pt": 62.0, "conservative_pt": 55.0, "pt_compression_pct": 12.7,
             "aggressive_executive_summary": "Market-maker volume tailwind."},
        ],
    )
    large = _write_run(
        runs_dir,
        "matrix_large_weekly_2026-06-26_2126_chain",
        trade_date="2026-06-26",
        overlays=[
            {
                "ticker": "ADI", "tier": "C", "current_price_usd": 386.91,
                "aggressive_pt": 470.0, "conservative_pt": 410.0,
                "legs": [{
                    "symbol": "O:ADI261218C00400000", "strike": 400.0,
                    "expiration": "2026-12-18", "price": 55.36, "open_interest": 156,
                }],
            },
        ],
        ledger_rows=[
            {"ticker": "ADI", "classification": "PICK", "current_price": 386.91,
             "aggressive_pt": 470.0, "conservative_pt": 410.0, "pt_compression_pct": 15.2,
             "aggressive_executive_summary": "Analog chip demand recovery."},
        ],
    )
    return mid, large


@pytest.fixture
def older_run(runs_dir: Path) -> Path:
    """An older-dated run that must be excluded from trade-date discovery,
    with mtime deliberately bumped newer than the current-week runs (the
    mtime trap)."""
    d = _write_run(
        runs_dir,
        "matrix_mid_weekly_2026-06-19_1200_chain",
        trade_date="2026-06-19",
        overlays=[{
            "ticker": "OLD", "tier": "B", "current_price_usd": 10.0,
            "aggressive_pt": 12.0, "conservative_pt": 11.0,
            "legs": [{"symbol": "O:OLD", "strike": 10.0, "expiration": "2026-09-18", "price": 1.0}],
        }],
        ledger_rows=[{"ticker": "OLD", "classification": "PICK", "current_price": 10.0,
                      "aggressive_pt": 12.0, "conservative_pt": 11.0, "pt_compression_pct": 8.3}],
    )
    import os
    import time
    future = time.time() + 100000
    os.utime(d / "verdict_ledger.json", (future, future))
    os.utime(d, (future, future))
    return d


@pytest.fixture
def policy_file(tmp_path: Path) -> Path:
    policy = {
        "tier_sizing": {
            "A": {"starter_pct": 0.04, "max_pct": 0.06},
            "B": {"starter_pct": 0.04, "max_pct": 0.08},
            "C": {"starter_pct": 0.02, "max_pct": 0.04},
        },
        "options_defaults": {"strategy": "long_call", "delta_target": 0.55, "min_open_interest": 100},
        "roll_defaults": {"dte_threshold": 60},
        "exit_defaults": {},
    }
    p = tmp_path / "policy.json"
    p.write_text(json.dumps(policy))
    return p


@pytest.fixture
def positions_file(tmp_path: Path) -> Path:
    book = {
        "as_of": "2026-06-26",
        "base_currency": "USD",
        "account_value": 100000.0,
        "positions": [
            {
                "id": "VIRT-c", "ticker": "VIRT", "instrument": "long_call", "qty": 5,
                "underlying_cost_basis_at_entry": 31.20,
                "option": {"strike": 35.0, "expiry": "2026-09-18",
                           "premium_paid_per_share": 4.50, "current_mark_per_share": 5.49},
                "entry_date": "2026-04-25", "thesis_tier_at_entry": "B", "sector": "Financials",
            },
        ],
    }
    p = tmp_path / "positions.json"
    p.write_text(json.dumps(book))
    return p


# ---------------------------------------------------------------------------
# Run discovery — content-based, mtime trap


def test_find_matrix_runs_for_trade_date_unions_same_date_runs(runs_dir, two_tier_runs):
    found = find_matrix_runs_for_trade_date("2026-06-26", runs_dir)
    assert len(found) == 2
    names = {p.name for p in found}
    assert names == {"matrix_mid_weekly_2026-06-26_2103_chain", "matrix_large_weekly_2026-06-26_2126_chain"}


def test_find_matrix_runs_excludes_other_dates(runs_dir, two_tier_runs, older_run):
    found = find_matrix_runs_for_trade_date("2026-06-26", runs_dir)
    names = {p.name for p in found}
    assert "matrix_mid_weekly_2026-06-19_1200_chain" not in names


def test_find_latest_trade_date_ignores_mtime_trap(runs_dir, two_tier_runs, older_run):
    # older_run has a NEWER mtime than the current-week runs but an OLDER
    # trade_date. Content-based discovery must still pick 2026-06-26.
    latest = find_latest_trade_date(runs_dir)
    assert latest == "2026-06-26"


# ---------------------------------------------------------------------------
# overlay_health — red banner triggers


def test_overlay_health_clean_run_has_no_problems(runs_dir, two_tier_runs):
    mid, _ = two_tier_runs
    artifacts = load_run_artifacts(mid)
    assert overlay_health(artifacts) == []


def test_overlay_health_missing_overlay_with_picks_flags_problem(runs_dir):
    d = _write_run(
        runs_dir, "matrix_broken", trade_date="2026-06-26",
        overlays=[], ledger_rows=[{"ticker": "X", "classification": "PICK",
                                    "aggressive_pt": 10, "conservative_pt": 9, "pt_compression_pct": 1.0}],
        include_overlay=False,
    )
    artifacts = load_run_artifacts(d)
    problems = overlay_health(artifacts)
    assert len(problems) == 1
    assert "MISSING" in problems[0]


def test_overlay_health_zero_strategies_despite_picks_flags_problem(runs_dir):
    d = _write_run(
        runs_dir, "matrix_zero_strats", trade_date="2026-06-26",
        overlays=[{"ticker": "X", "tier": "A", "legs": []}],
        ledger_rows=[{"ticker": "X", "classification": "PICK",
                      "aggressive_pt": 10, "conservative_pt": 9, "pt_compression_pct": 1.0}],
    )
    artifacts = load_run_artifacts(d)
    problems = overlay_health(artifacts)
    assert any("ZERO strategies" in p for p in problems)


def test_overlay_health_error_row_flags_problem(runs_dir):
    d = _write_run(
        runs_dir, "matrix_errored_row", trade_date="2026-06-26",
        overlays=[{"ticker": "X", "tier": "A", "legs": [{"price": 1.0}], "error": "chain fetch failed"}],
        ledger_rows=[{"ticker": "X", "classification": "PICK",
                      "aggressive_pt": 10, "conservative_pt": 9, "pt_compression_pct": 1.0}],
    )
    artifacts = load_run_artifacts(d)
    problems = overlay_health(artifacts)
    assert any("chain fetch failed" in p for p in problems)


# ---------------------------------------------------------------------------
# build_tickets — ranking, math, flags


def test_build_tickets_ranks_b_then_a_then_c(runs_dir, two_tier_runs, policy_file):
    mid, large = two_tier_runs
    artifacts = [load_run_artifacts(mid), load_run_artifacts(large)]
    policy = json.loads(policy_file.read_text())
    tickets = build_tickets(
        artifacts, "2026-06-26",
        account_value=100000.0, policy=policy, live_prices={}, skip_live=True,
    )
    assert [t["tier"] for t in tickets] == ["B", "A", "C"]
    assert [t["ticker"] for t in tickets] == ["VIRT", "CTRE", "ADI"]


def test_build_tickets_signal_id_matches_export_lean_signals_convention(runs_dir, two_tier_runs, policy_file):
    mid, large = two_tier_runs
    artifacts = [load_run_artifacts(mid), load_run_artifacts(large)]
    policy = json.loads(policy_file.read_text())
    tickets = build_tickets(artifacts, "2026-06-26", account_value=None, policy=policy,
                             live_prices={}, skip_live=True)
    virt = next(t for t in tickets if t["ticker"] == "VIRT")
    assert virt["signal_id"] == "VIRT-2026-06-26"


def test_build_tickets_qty_math_uses_per_share_premium_times_100(runs_dir, two_tier_runs, policy_file):
    """Per-share vs per-contract invariant: qty = floor(account_value * starter_pct / (premium_per_share * 100))."""
    mid, large = two_tier_runs
    artifacts = [load_run_artifacts(mid), load_run_artifacts(large)]
    policy = json.loads(policy_file.read_text())
    tickets = build_tickets(
        artifacts, "2026-06-26",
        account_value=100000.0, policy=policy, live_prices={}, skip_live=True,
    )
    virt = next(t for t in tickets if t["ticker"] == "VIRT")
    # VIRT: ref_premium_per_share = 4.20, tier B starter_pct = 0.04
    # qty = floor(100000 * 0.04 / (4.20 * 100)) = floor(4000 / 420) = floor(9.523) = 9
    assert virt["ref_premium_per_share"] == 4.20
    assert virt["qty"] == 9
    # Limit price = ref_premium * 1.05
    assert virt["limit_price_per_share"] == pytest.approx(4.41)


def test_build_tickets_qty_omitted_without_account_value(runs_dir, two_tier_runs, policy_file):
    mid, large = two_tier_runs
    artifacts = [load_run_artifacts(mid), load_run_artifacts(large)]
    policy = json.loads(policy_file.read_text())
    tickets = build_tickets(artifacts, "2026-06-26", account_value=None, policy=policy,
                             live_prices={}, skip_live=True)
    assert all(t["qty"] is None for t in tickets)


def test_build_tickets_thin_chain_flag(runs_dir, two_tier_runs, policy_file):
    mid, large = two_tier_runs
    artifacts = [load_run_artifacts(mid), load_run_artifacts(large)]
    policy = json.loads(policy_file.read_text())
    tickets = build_tickets(artifacts, "2026-06-26", account_value=None, policy=policy,
                             live_prices={}, skip_live=True)
    ctre = next(t for t in tickets if t["ticker"] == "CTRE")
    assert any("thin chain" in f for f in ctre["liquidity_flags"])


def test_build_tickets_c_tier_has_no_cons_pt(runs_dir, two_tier_runs, policy_file):
    mid, large = two_tier_runs
    artifacts = [load_run_artifacts(mid), load_run_artifacts(large)]
    policy = json.loads(policy_file.read_text())
    tickets = build_tickets(artifacts, "2026-06-26", account_value=None, policy=policy,
                             live_prices={}, skip_live=True)
    adi = next(t for t in tickets if t["ticker"] == "ADI")
    assert adi["cons_pt"] is None
    assert adi["exit_rule"] == "tier_c_trim"


# ---------------------------------------------------------------------------
# Gap% math and --skip-live


def test_gap_pct_math_and_warning(runs_dir, two_tier_runs, policy_file):
    mid, large = two_tier_runs
    artifacts = [load_run_artifacts(mid), load_run_artifacts(large)]
    policy = json.loads(policy_file.read_text())
    # VIRT anchor 51.31, live 55.0 → gap = (55.0-51.31)/51.31*100 = 7.19% > 3% warn threshold
    tickets = build_tickets(
        artifacts, "2026-06-26",
        account_value=None, policy=policy,
        live_prices={"VIRT": 55.0, "CTRE": 41.8, "ADI": 386.0},
        skip_live=False,
    )
    virt = next(t for t in tickets if t["ticker"] == "VIRT")
    assert virt["gap_pct"] == pytest.approx(7.19, abs=0.01)
    assert virt["gap_warn"] is True

    ctre = next(t for t in tickets if t["ticker"] == "CTRE")
    assert ctre["gap_warn"] is False  # (41.8-41.6)/41.6 = 0.48%, under threshold


def test_skip_live_omits_live_price_and_gap(runs_dir, two_tier_runs, policy_file):
    mid, large = two_tier_runs
    artifacts = [load_run_artifacts(mid), load_run_artifacts(large)]
    policy = json.loads(policy_file.read_text())
    tickets = build_tickets(artifacts, "2026-06-26", account_value=None, policy=policy,
                             live_prices={}, skip_live=True)
    for t in tickets:
        assert t["live_price"] is None
        assert t["gap_pct"] is None
        assert t["stale_warning"] is not None


def test_fetch_failure_yields_stale_warning_not_crash(runs_dir, two_tier_runs, policy_file):
    mid, large = two_tier_runs
    artifacts = [load_run_artifacts(mid), load_run_artifacts(large)]
    policy = json.loads(policy_file.read_text())
    # live_prices dict missing a ticker simulates a fetch failure for that ticker.
    tickets = build_tickets(artifacts, "2026-06-26", account_value=None, policy=policy,
                             live_prices={"VIRT": 51.31}, skip_live=False)
    ctre = next(t for t in tickets if t["ticker"] == "CTRE")
    assert ctre["live_price"] is None
    assert ctre["stale_warning"] is not None
    assert "stale" in ctre["stale_warning"] or "failed" in ctre["stale_warning"]


def test_fetch_live_prices_handles_request_errors_gracefully(monkeypatch):
    import tradingagents.dataflows.polygon_common as pc

    def boom(*args, **kwargs):
        raise pc.PolygonError("simulated failure")

    monkeypatch.setattr(pc, "_make_request", boom)
    monkeypatch.setattr("scripts.build_friday_packet.time.sleep", lambda s: None)
    prices = fetch_live_prices(["AAPL"], pace=0, retries=1)
    assert prices == {}


def test_fetch_live_prices_returns_close_price(monkeypatch):
    import tradingagents.dataflows.polygon_common as pc

    def fake_request(path, params=None, **kwargs):
        return {"results": [{"c": 123.45}]}

    monkeypatch.setattr(pc, "_make_request", fake_request)
    monkeypatch.setattr("scripts.build_friday_packet.time.sleep", lambda s: None)
    prices = fetch_live_prices(["AAPL"], pace=0)
    assert prices == {"AAPL": 123.45}


# ---------------------------------------------------------------------------
# Red-banner integration via build_packet (offline)


def test_build_packet_clean_run_has_no_banner_problems(runs_dir, two_tier_runs, policy_file):
    packet = build_packet(
        trade_date="2026-06-26", runs_dir=runs_dir,
        positions_path=None, policy_path=policy_file, skip_live=True,
    )
    assert packet["banner_problems"] == []
    assert len(packet["tickets"]) == 3


def test_build_packet_red_banner_on_missing_overlay(runs_dir, two_tier_runs, policy_file):
    _write_run(
        runs_dir, "matrix_mega_broken", trade_date="2026-06-26",
        overlays=[], ledger_rows=[{"ticker": "Z", "classification": "PICK",
                                    "aggressive_pt": 10, "conservative_pt": 9, "pt_compression_pct": 1.0}],
        include_overlay=False,
    )
    packet = build_packet(
        trade_date="2026-06-26", runs_dir=runs_dir,
        positions_path=None, policy_path=policy_file, skip_live=True,
    )
    assert len(packet["banner_problems"]) == 1
    assert "matrix_mega_broken" in packet["banner_problems"][0]


def test_build_packet_no_matrix_runs_raises(tmp_path, policy_file):
    empty_runs = tmp_path / "empty_runs"
    empty_runs.mkdir()
    with pytest.raises(ValueError, match="no matrix runs"):
        build_packet(
            trade_date="2026-06-26", runs_dir=empty_runs,
            positions_path=None, policy_path=policy_file, skip_live=True,
        )


def test_build_packet_defaults_to_latest_trade_date(runs_dir, two_tier_runs, older_run, policy_file):
    packet = build_packet(
        trade_date=None, runs_dir=runs_dir,
        positions_path=None, policy_path=policy_file, skip_live=True,
    )
    assert packet["trade_date"] == "2026-06-26"


def test_build_packet_macro_regime_not_built_message(runs_dir, two_tier_runs, policy_file):
    packet = build_packet(
        trade_date="2026-06-26", runs_dir=runs_dir,
        positions_path=None, policy_path=policy_file, skip_live=True,
    )
    assert packet["macro_snapshot"] is None


def test_build_packet_macro_regime_loaded_when_present(runs_dir, two_tier_runs, policy_file):
    (runs_dir / "macro_2026-06-26.json").write_text(json.dumps({"regime": "defensive"}))
    packet = build_packet(
        trade_date="2026-06-26", runs_dir=runs_dir,
        positions_path=None, policy_path=policy_file, skip_live=True,
    )
    assert packet["macro_snapshot"]["regime"] == "defensive"


def test_build_packet_with_positions_includes_portfolio_ctx(runs_dir, two_tier_runs, policy_file, positions_file):
    packet = build_packet(
        trade_date="2026-06-26", runs_dir=runs_dir,
        positions_path=positions_file, policy_path=policy_file, skip_live=True,
    )
    assert packet["portfolio_ctx"] is not None
    virt = next(p for p in packet["portfolio_ctx"]["positions"] if p["ticker"] == "VIRT")
    assert virt["live"]["in_latest_matrix"] is True


# ---------------------------------------------------------------------------
# Packet never writes outside its own output dir


def test_write_packet_only_touches_output_dir(runs_dir, two_tier_runs, policy_file, tmp_path):
    packet = build_packet(
        trade_date="2026-06-26", runs_dir=runs_dir,
        positions_path=None, policy_path=policy_file, skip_live=True,
    )
    out_dir = tmp_path / "packet_output"
    before = {p for p in runs_dir.rglob("*")}
    md_path, html_path = write_packet(packet, out_dir)
    after = {p for p in runs_dir.rglob("*")}
    assert before == after  # nothing in runs_dir was touched
    assert md_path.exists()
    assert html_path.exists()
    assert md_path.parent == out_dir
    assert html_path.parent == out_dir


# ---------------------------------------------------------------------------
# render_markdown / render_html smoke


def test_render_markdown_contains_approval_checklist_commands(runs_dir, two_tier_runs, policy_file):
    packet = build_packet(
        trade_date="2026-06-26", runs_dir=runs_dir,
        positions_path=None, policy_path=policy_file, skip_live=True,
    )
    md = render_markdown(
        trade_date=packet["trade_date"], run_artifacts=packet["run_artifacts"],
        tickets=packet["tickets"], banner_problems=packet["banner_problems"],
        macro_snapshot=packet["macro_snapshot"], skip_live=packet["skip_live"],
        portfolio_ctx=packet["portfolio_ctx"], generated_at=packet["generated_at"],
    )
    assert "approve_lean_signal.py --id VIRT-2026-06-26" in md
    assert "approve_lean_signal.py --id CTRE-2026-06-26" in md
    assert "approve_lean_signal.py --id ADI-2026-06-26" in md
    assert "build_options_overlay.py" in md  # refresh command present


def test_render_markdown_shows_red_banner_text(runs_dir, two_tier_runs, policy_file):
    _write_run(
        runs_dir, "matrix_mega_broken", trade_date="2026-06-26",
        overlays=[], ledger_rows=[{"ticker": "Z", "classification": "PICK",
                                    "aggressive_pt": 10, "conservative_pt": 9, "pt_compression_pct": 1.0}],
        include_overlay=False,
    )
    packet = build_packet(
        trade_date="2026-06-26", runs_dir=runs_dir,
        positions_path=None, policy_path=policy_file, skip_live=True,
    )
    md = render_markdown(
        trade_date=packet["trade_date"], run_artifacts=packet["run_artifacts"],
        tickets=packet["tickets"], banner_problems=packet["banner_problems"],
        macro_snapshot=packet["macro_snapshot"], skip_live=packet["skip_live"],
        portfolio_ctx=packet["portfolio_ctx"], generated_at=packet["generated_at"],
    )
    assert "DATA QUALITY ISSUE" in md


def test_render_html_is_self_contained_and_escapes(runs_dir, two_tier_runs, policy_file):
    packet = build_packet(
        trade_date="2026-06-26", runs_dir=runs_dir,
        positions_path=None, policy_path=policy_file, skip_live=True,
    )
    doc = render_html(
        trade_date=packet["trade_date"], run_artifacts=packet["run_artifacts"],
        tickets=packet["tickets"], banner_problems=packet["banner_problems"],
        macro_snapshot=packet["macro_snapshot"], skip_live=packet["skip_live"],
        portfolio_ctx=packet["portfolio_ctx"], generated_at=packet["generated_at"],
    )
    assert doc.startswith("<!DOCTYPE html>")
    assert "<script src=" not in doc  # no external assets
    assert "<link " not in doc
    assert "VIRT" in doc
