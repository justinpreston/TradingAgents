"""Tests for portfolio_check.py and portfolio_allocation.py and portfolio_trade_tickets.py."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from scripts.portfolio_load_context import build_context
from scripts.portfolio_check import recommend_for_position
from scripts.portfolio_allocation import (
    aggregate_exposures,
    cash_buffer_status,
    compute_breaches,
    simulate_candidate,
    _deployed_capital_for_position,
    _market_value_for_position,
)
from scripts.portfolio_trade_tickets import build_queue


@pytest.fixture
def tmp_book(tmp_path: Path) -> Path:
    book = {
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
                "stop_loss_underlying": 165.00,
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
        "max_thematic_basket_pct": 0.25,
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
        "thematic_baskets": {"gov_stake": ["INTC", "MP"]},
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
        "rows": [
            {
                "ticker": "VIRT", "classification": "PICK",
                "aggressive_pt": 58.0, "conservative_pt": 55.0, "pt_compression_pct": 5.2,
                "aggressive_rating": "Overweight",
            },
            {
                "ticker": "CTRE", "classification": "PICK",
                "aggressive_pt": 45.0, "conservative_pt": 45.0, "pt_compression_pct": 0.0,
                "aggressive_rating": "Overweight",
            },
            {
                "ticker": "ADI", "classification": "VETOED",
                "aggressive_pt": 420.0, "conservative_pt": 398.0, "pt_compression_pct": 5.5,
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
                    "type": "call", "strike": 50, "expiration": "2026-09-18",
                    "delta": 0.62, "iv": 0.30, "open_interest": 800,
                }],
                "net_debit_per_share": 4.20, "net_debit_per_contract": 420.0,
                "breakeven_underlying": 54.20, "breakeven_pct_from_current": 5.6,
            },
            {
                "ticker": "CTRE", "tier": "A", "current_price_usd": 41.60,
                "aggressive_pt": 45.0, "conservative_pt": 45.0,
                "legs": [{
                    "type": "call", "strike": 40, "expiration": "2026-10-16",
                    "delta": 0.63, "iv": 0.23, "open_interest": 72,
                }],
                "net_debit_per_share": 2.30, "net_debit_per_contract": 230.0,
                "breakeven_underlying": 42.30, "breakeven_pct_from_current": 1.68,
            },
        ],
    }))
    (run / "current_prices.json").write_text(json.dumps({
        "prices_usd": {"VIRT": 51.31, "AAPL": 220.00, "CTRE": 41.60, "ADI": 416.52},
    }))
    return run


# ---------------------------------------------------------------------------
# portfolio_check.recommend_for_position


def test_recommend_take_profit_hit_yields_TRIM(tmp_book, tmp_policy, tmp_matrix_run):
    ctx = build_context(tmp_book, tmp_policy, tmp_matrix_run)
    virt = next(p for p in ctx["positions"] if p["ticker"] == "VIRT")
    ticket = recommend_for_position(virt, ctx["policy"], date(2026, 5, 9))
    assert ticket["recommendation"] == "TRIM"
    assert ticket["qty_to_act"] == 2  # 5 // 3 with rounding = 2
    assert ticket["qty_to_keep"] == 3
    assert "take_profit_hit" in ticket["tags"]
    # New: should produce a plain-English spoken summary
    assert "spoken_summary" in ticket
    assert "VIRT" in ticket["spoken_summary"]
    assert "Sell" in ticket["spoken_summary"] or "trim" in ticket["spoken_summary"].lower()


def test_recommend_stop_loss_yields_EXIT(tmp_book, tmp_policy, tmp_matrix_run):
    # Drop VIRT below stop loss
    prices_path = tmp_matrix_run / "current_prices.json"
    doc = json.loads(prices_path.read_text())
    doc["prices_usd"]["VIRT"] = 25.00
    prices_path.write_text(json.dumps(doc))
    ctx = build_context(tmp_book, tmp_policy, tmp_matrix_run)
    virt = next(p for p in ctx["positions"] if p["ticker"] == "VIRT")
    ticket = recommend_for_position(virt, ctx["policy"], date(2026, 5, 9))
    assert ticket["recommendation"] == "EXIT"
    assert ticket["qty_to_act"] == 5
    assert "stop_loss_breach" in ticket["tags"]


def test_recommend_VETOED_yields_EXIT(tmp_path, tmp_policy, tmp_matrix_run):
    book = {
        "as_of": "2026-05-09",
        "base_currency": "USD",
        "account_value": 100000.0,
        "positions": [{
            "id": "ADI-c",
            "ticker": "ADI",
            "instrument": "long_call",
            "qty": 3,
            "underlying_cost_basis_at_entry": 380.00,
            "option": {
                "strike": 400, "expiry": "2026-12-19",
                "premium_paid_per_share": 12.0,
            },
            "entry_date": "2026-04-15",
            "thesis_tier_at_entry": "B",
        }],
    }
    p = tmp_path / "positions.json"
    p.write_text(json.dumps(book))
    ctx = build_context(p, tmp_policy, tmp_matrix_run)
    ticket = recommend_for_position(ctx["positions"][0], ctx["policy"], date(2026, 5, 9))
    assert ticket["recommendation"] == "EXIT"
    assert "matrix_veto" in ticket["tags"]


def test_recommend_HOLD_for_stable_position(tmp_book, tmp_policy, tmp_matrix_run):
    # Pull take-profit further away so VIRT stays HOLD
    book = json.loads(tmp_book.read_text())
    book["positions"][0]["take_profit_underlying"] = 100.0  # never hit
    tmp_book.write_text(json.dumps(book))
    ctx = build_context(tmp_book, tmp_policy, tmp_matrix_run)
    virt = next(p for p in ctx["positions"] if p["ticker"] == "VIRT")
    ticket = recommend_for_position(virt, ctx["policy"], date(2026, 5, 9))
    assert ticket["recommendation"] == "HOLD"


def test_recommend_includes_alternative_for_ROLL(tmp_book, tmp_policy, tmp_matrix_run):
    # Bump DTE threshold so 132 < threshold, making VIRT armed for roll
    book = json.loads(tmp_book.read_text())
    # Disable take_profit so roll trigger fires before TP rule
    book["positions"][0]["take_profit_underlying"] = None
    book["positions"][0]["roll_trigger_dte"] = 200  # arms even with 132 DTE
    tmp_book.write_text(json.dumps(book))
    ctx = build_context(tmp_book, tmp_policy, tmp_matrix_run)
    virt = next(p for p in ctx["positions"] if p["ticker"] == "VIRT")
    ticket = recommend_for_position(virt, ctx["policy"], date(2026, 5, 9))
    assert ticket["recommendation"] == "ROLL"
    assert ticket["alternative_contract"] is not None
    assert ticket["alternative_contract"]["strike"] == 50


# ---------------------------------------------------------------------------
# portfolio_allocation


def test_market_value_options_uses_current_mark(tmp_book):
    book = json.loads(tmp_book.read_text())
    pos = dict(book["positions"][0])
    pos["live"] = {"current_underlying_price": 51.31}
    # 5 contracts × 100 × $5.49 mark = $2,745
    assert _market_value_for_position(pos) == pytest.approx(2745.0)


def test_deployed_capital_options_uses_premium_paid(tmp_book):
    book = json.loads(tmp_book.read_text())
    pos = book["positions"][0]
    # 5 contracts × 100 × $4.50 paid = $2,250
    assert _deployed_capital_for_position(pos) == pytest.approx(2250.0)


def test_aggregate_exposures_by_sector(tmp_book, tmp_policy, tmp_matrix_run):
    ctx = build_context(tmp_book, tmp_policy, tmp_matrix_run)
    exp = aggregate_exposures(ctx["positions"], ctx["policy"], use_mtm=True)
    # AAPL 50 × $220 = $11,000 (Tech), VIRT options = $2,745 (Financials)
    assert exp["by_sector"]["Technology"] == pytest.approx(11000.0)
    assert exp["by_sector"]["Financials"] == pytest.approx(2745.0)


def test_compute_breaches_max_single_name(tmp_book, tmp_policy, tmp_matrix_run):
    ctx = build_context(tmp_book, tmp_policy, tmp_matrix_run)
    exp = aggregate_exposures(ctx["positions"], ctx["policy"], use_mtm=True)
    # AAPL = $11,000 / $100,000 = 11% > 8% cap
    breaches = compute_breaches(exp, ctx["policy"], 100000.0)
    aapl_breach = [b for b in breaches if b["key"] == "AAPL"]
    assert len(aapl_breach) == 1
    assert aapl_breach[0]["kind"] == "max_single_name"


def test_cash_buffer_status_above_target(tmp_book, tmp_policy, tmp_matrix_run):
    ctx = build_context(tmp_book, tmp_policy, tmp_matrix_run)
    exp = aggregate_exposures(ctx["positions"], ctx["policy"], use_mtm=True)
    cash = cash_buffer_status(exp, ctx["policy"], 100000.0)
    assert cash["below_target"] is False
    assert cash["cash_pct"] > 20.0  # only $13,745 deployed


def test_simulate_candidate_returns_specific_contract(tmp_book, tmp_policy, tmp_matrix_run):
    ctx = build_context(tmp_book, tmp_policy, tmp_matrix_run)
    sim = simulate_candidate(ctx, "CTRE", 100000.0)
    assert "error" not in sim
    assert sim["tier"] == "A"
    assert sim["contract"]["strike"] == 40
    assert sim["contract"]["premium_per_share"] == 2.30
    assert sim["contract"]["premium_per_contract_usd"] == 230.0
    # 4% × $100k = $4,000 / $230 = 17 contracts
    assert sim["suggested_qty_contracts"] == 17
    # 17 × $230 = $3,910
    assert sim["contract"]["premium_total_usd"] == pytest.approx(3910.0)


def test_simulate_candidate_unknown_ticker(tmp_book, tmp_policy, tmp_matrix_run):
    ctx = build_context(tmp_book, tmp_policy, tmp_matrix_run)
    sim = simulate_candidate(ctx, "UNKNOWN", 100000.0)
    assert "error" in sim


# ---------------------------------------------------------------------------
# portfolio_trade_tickets


def test_build_queue_orders_exits_before_opens(tmp_book, tmp_policy, tmp_matrix_run):
    queue = build_queue(tmp_book, tmp_policy, tmp_matrix_run, include_new_picks=True, max_new=2)
    actions = [t["action"] for t in queue["tickets"]]
    # TRIM should appear before OPEN_NEW
    if "TRIM" in actions and "OPEN_NEW" in actions:
        assert actions.index("TRIM") < actions.index("OPEN_NEW")


def test_build_queue_skips_HOLD(tmp_book, tmp_policy, tmp_matrix_run):
    # Disable triggers so VIRT becomes HOLD
    book = json.loads(tmp_book.read_text())
    book["positions"][0]["take_profit_underlying"] = 999
    book["positions"][0]["stop_loss_underlying"] = 1
    tmp_book.write_text(json.dumps(book))
    queue = build_queue(tmp_book, tmp_policy, tmp_matrix_run, include_new_picks=False)
    assert all(t["action"] != "HOLD" for t in queue["tickets"])


def test_build_queue_emits_specific_contract_for_new_pick(tmp_book, tmp_policy, tmp_matrix_run):
    queue = build_queue(tmp_book, tmp_policy, tmp_matrix_run, include_new_picks=True, max_new=2)
    new_picks = [t for t in queue["tickets"] if t["action"] == "OPEN_NEW"]
    assert len(new_picks) > 0
    pick = new_picks[0]
    assert pick["contract"]["strike"] is not None
    assert pick["contract"]["expiry"] is not None
    assert pick["contract"]["premium_per_share"] is not None
    assert pick["qty"] is not None
    assert pick["qty"] > 0


def test_build_queue_warns_on_low_liquidity(tmp_book, tmp_policy, tmp_matrix_run):
    # CTRE overlay has OI=72 < min_open_interest=100, so should get a low_liquidity tag
    queue = build_queue(tmp_book, tmp_policy, tmp_matrix_run, include_new_picks=True, max_new=2)
    ctre = next((t for t in queue["tickets"] if t["ticker"] == "CTRE"), None)
    assert ctre is not None
    assert "low_liquidity" in ctre["tags"]
    assert any("Thin" in r or "thin" in r or "liquidity" in r.lower() for r in ctre["rationale"])


def test_build_queue_emits_spoken_summary(tmp_book, tmp_policy, tmp_matrix_run):
    queue = build_queue(tmp_book, tmp_policy, tmp_matrix_run, include_new_picks=True, max_new=1)
    for t in queue["tickets"]:
        assert "spoken_summary" in t
        assert isinstance(t["spoken_summary"], str)
        assert len(t["spoken_summary"]) > 10


def test_open_new_spoken_summary_is_specific(tmp_book, tmp_policy, tmp_matrix_run):
    queue = build_queue(tmp_book, tmp_policy, tmp_matrix_run, include_new_picks=True, max_new=2)
    new_picks = [t for t in queue["tickets"] if t["action"] == "OPEN_NEW"]
    assert len(new_picks) > 0
    # Should mention "Buy", a strike, and an expiry
    spoken = new_picks[0]["spoken_summary"]
    assert "Buy" in spoken
    assert "C exp" in spoken  # e.g. "40C exp 2026-..."
    assert "$" in spoken
