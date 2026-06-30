"""Tests for scripts/export_lean_signals.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.export_lean_signals import build_manifest, main


@pytest.fixture
def policy() -> dict:
    return {
        "tier_sizing": {
            "A": {"starter_pct": 0.04, "max_pct": 0.06},
            "B": {"starter_pct": 0.04, "max_pct": 0.08},
            "C": {"starter_pct": 0.02, "max_pct": 0.04},
        },
        "options_defaults": {"delta_target": 0.55},
        "exit_defaults": {"stop_loss_premium_pct": -0.35, "time_stop_dte": 14},
    }


@pytest.fixture
def synthetic_runs(tmp_path: Path) -> tuple[Path, Path]:
    run1 = tmp_path / "matrix_one"
    run2 = tmp_path / "matrix_two"
    run1.mkdir()
    run2.mkdir()
    base = {
        "snapshot_date": "2026-05-08",
        "long_call_delta_target": 0.55,
        "overlays": [
            {
                "ticker": "CTRE",
                "tier": "A",
                "current_price_usd": 41.6,
                "aggressive_pt": 45.0,
                "conservative_pt": 45.0,
                "legs": [{
                    "symbol": "O:CTRE261016C00040000",
                    "type": "call",
                    "strike": 40.0,
                    "expiration": "2026-10-16",
                    "delta": 0.63,
                    "price": 2.30,
                    "open_interest": 72,
                    "iv": 0.23,
                }],
                "liquidity_warnings": ["OI below preferred 100"],
                "earnings_note": "earnings before expiry",
            },
            {
                "ticker": "VETO",
                "tier": "VETO",
                "current_price_usd": 10.0,
                "aggressive_pt": 12.0,
                "conservative_pt": 11.0,
                "legs": [{"price": 1.0}],
            },
        ],
    }
    second = {
        "snapshot_date": "2026-05-08",
        "long_call_delta_target": 0.55,
        "overlays": [
            {
                "ticker": "ADI",
                "tier": "C",
                "current_price_usd": 386.91,
                "aggressive_pt": 470.0,
                "conservative_pt": 410.0,
                "legs": [{
                    "symbol": "O:ADI261218C00400000",
                    "type": "call",
                    "strike": 400.0,
                    "expiration": "2026-12-18",
                    "delta": 0.54,
                    "price": 55.36,
                    "open_interest": 156,
                    "iv": 0.54,
                }],
            },
            {"ticker": "DASH", "tier": "—", "legs": [{"price": 1.0}]},
        ],
    }
    (run1 / "options_overlay.json").write_text(json.dumps(base))
    (run2 / "options_overlay.json").write_text(json.dumps(second))
    return run1, run2


def test_tier_rule_mapping_and_c_cons_pt_nulling(policy, synthetic_runs):
    manifest = build_manifest(list(synthetic_runs), policy, generated_at="2026-05-08T12:00:00+00:00")
    by_ticker = {s["ticker"]: s for s in manifest["signals"]}

    assert by_ticker["CTRE"]["exits"]["rule"] == "tier_a_take_profit"
    assert by_ticker["ADI"]["exits"]["rule"] == "tier_c_trim"
    assert by_ticker["ADI"]["exits"]["cons_pt"] is None


def test_size_slippage_approval_id_and_notes(policy, synthetic_runs):
    manifest = build_manifest(list(synthetic_runs), policy, slippage_pct=0.05)
    ctre = next(s for s in manifest["signals"] if s["ticker"] == "CTRE")

    assert ctre["id"] == "CTRE-2026-05-08"
    assert ctre["entry"]["size_pct"] == 0.04
    assert ctre["entry"]["max_size_pct"] == 0.06
    assert ctre["entry"]["max_premium_per_share"] == pytest.approx(2.415)
    assert ctre["approved"] is False
    assert "OI below preferred 100" in ctre["notes"]
    assert "earnings before expiry" in ctre["notes"]


def test_multi_run_merge_and_skipping_non_picks(policy, synthetic_runs):
    manifest = build_manifest(list(synthetic_runs), policy)

    assert manifest["measure_date"] == "2026-05-08"
    assert len(manifest["signals"]) == 2
    assert {s["ticker"] for s in manifest["signals"]} == {"CTRE", "ADI"}
    assert "matrix_one" in manifest["source_run"]
    assert "matrix_two" in manifest["source_run"]


def test_policy_exit_defaults_and_approve_all(policy, synthetic_runs):
    manifest = build_manifest(list(synthetic_runs), policy, approve_all=True)
    signal = manifest["signals"][0]

    assert signal["approved"] is True
    assert signal["exits"]["stop_loss_premium_pct"] == -0.40  # build_manifest literal default
    assert signal["exits"]["time_stop_dte"] == 21


def test_cli_reads_policy_exit_defaults(tmp_path: Path, policy, synthetic_runs):
    policy_path = tmp_path / "policy.json"
    output = tmp_path / "signals.json"
    policy_path.write_text(json.dumps(policy))

    rc = main([
        "--matrix-run", str(synthetic_runs[0]),
        "--policy-file", str(policy_path),
        "--output", str(output),
    ])

    assert rc == 0
    doc = json.loads(output.read_text())
    assert doc["signals"][0]["exits"]["stop_loss_premium_pct"] == -0.35
    assert doc["signals"][0]["exits"]["time_stop_dte"] == 14


def test_cli_override_stop_time_and_approve_all(tmp_path: Path, policy, synthetic_runs):
    policy_path = tmp_path / "policy.json"
    output = tmp_path / "signals.json"
    policy_path.write_text(json.dumps(policy))

    rc = main([
        "--matrix-run", str(synthetic_runs[0]),
        "--policy-file", str(policy_path),
        "--output", str(output),
        "--stop-loss-pct", "-0.5",
        "--time-stop-dte", "10",
        "--approve-all",
    ])

    assert rc == 0
    signal = json.loads(output.read_text())["signals"][0]
    assert signal["approved"] is True
    assert signal["exits"]["stop_loss_premium_pct"] == -0.5
    assert signal["exits"]["time_stop_dte"] == 10


def test_real_2026_06_26_overlays_if_present():
    runs = [
        Path("runs/matrix_mid_weekly_2026-06-26_2103_chain"),
        Path("runs/matrix_large_weekly_2026-06-26_2126_chain"),
        Path("runs/matrix_mega_weekly_2026-06-26_2146_chain"),
    ]
    if not all((run / "options_overlay.json").exists() for run in runs):
        pytest.skip("real 2026-06-26 overlays are not present")
    policy_path = Path("runs/portfolio/policy.json")
    if not policy_path.exists():
        pytest.skip("real portfolio policy is not present")

    manifest = build_manifest(runs, json.loads(policy_path.read_text()))
    assert {s["ticker"] for s in manifest["signals"]} == {"NUE", "KLIC", "ADI"}
