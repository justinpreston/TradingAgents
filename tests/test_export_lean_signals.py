"""Tests for scripts/export_lean_signals.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.export_lean_signals import build_manifest, load_existing_signals, main, write_manifest


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


# ---------------------------------------------------------------------------
# Mixed snapshot_date: warn (default) vs --strict


@pytest.fixture
def mismatched_date_runs(tmp_path: Path) -> tuple[Path, Path]:
    run1 = tmp_path / "matrix_a"
    run2 = tmp_path / "matrix_b"
    run1.mkdir()
    run2.mkdir()
    (run1 / "options_overlay.json").write_text(json.dumps({
        "snapshot_date": "2026-05-08",
        "overlays": [{
            "ticker": "AAA", "tier": "A", "current_price_usd": 10.0,
            "aggressive_pt": 12.0, "conservative_pt": 12.0,
            "legs": [{"symbol": "O:AAA", "strike": 10, "expiration": "2026-10-16", "price": 1.0}],
        }],
    }))
    (run2 / "options_overlay.json").write_text(json.dumps({
        "snapshot_date": "2026-05-09",  # different date — a Saturday overlay re-run
        "overlays": [{
            "ticker": "BBB", "tier": "B", "current_price_usd": 20.0,
            "aggressive_pt": 25.0, "conservative_pt": 22.0,
            "legs": [{"symbol": "O:BBB", "strike": 20, "expiration": "2026-10-16", "price": 2.0}],
        }],
    }))
    return run1, run2


def test_mismatched_snapshot_date_warns_by_default(policy, mismatched_date_runs):
    manifest = build_manifest(list(mismatched_date_runs), policy)
    assert len(manifest["signals"]) == 2  # both runs still contribute
    assert manifest["measure_date"] == "2026-05-09"  # most recent of the two
    assert any("snapshot_date" in w for w in manifest["warnings"])


def test_mismatched_snapshot_date_raises_with_strict(policy, mismatched_date_runs):
    with pytest.raises(ValueError, match="snapshot_date"):
        build_manifest(list(mismatched_date_runs), policy, strict=True)


def test_matching_snapshot_date_has_no_warning(policy, synthetic_runs):
    manifest = build_manifest(list(synthetic_runs), policy)
    assert manifest["warnings"] == []


# ---------------------------------------------------------------------------
# Missing legs: skip with warning, export healthy rows


@pytest.fixture
def missing_legs_run(tmp_path: Path) -> Path:
    run = tmp_path / "matrix_missing_legs"
    run.mkdir()
    (run / "options_overlay.json").write_text(json.dumps({
        "snapshot_date": "2026-05-08",
        "overlays": [
            {
                "ticker": "GOOD", "tier": "A", "current_price_usd": 41.6,
                "aggressive_pt": 45.0, "conservative_pt": 45.0,
                "legs": [{"symbol": "O:GOOD", "strike": 40.0, "expiration": "2026-10-16", "price": 2.30}],
            },
            {
                "ticker": "NOLEGS", "tier": "B", "current_price_usd": 50.0,
                "aggressive_pt": 60.0, "conservative_pt": 55.0,
                "legs": [],  # simulates a broken overlay row
            },
        ],
    }))
    return run


def test_missing_legs_row_skipped_with_warning(policy, missing_legs_run):
    manifest = build_manifest([missing_legs_run], policy)
    assert {s["ticker"] for s in manifest["signals"]} == {"GOOD"}
    assert any("NOLEGS" in w and "no option legs" in w for w in manifest["warnings"])


def test_missing_legs_row_does_not_raise(policy, missing_legs_run):
    # Old behavior raised ValueError here; new behavior must not.
    manifest = build_manifest([missing_legs_run], policy)
    assert manifest is not None


# ---------------------------------------------------------------------------
# Approval carry-forward on regeneration


def test_carry_forward_preserves_approved_true(policy, synthetic_runs):
    first = build_manifest(list(synthetic_runs), policy)
    ctre = next(s for s in first["signals"] if s["ticker"] == "CTRE")
    ctre["approved"] = True
    ctre["notes_user"] = "Confirmed fill, sizing up"
    existing = {s["id"]: s for s in first["signals"]}

    second = build_manifest(list(synthetic_runs), policy, existing_signals=existing)
    ctre2 = next(s for s in second["signals"] if s["ticker"] == "CTRE")
    assert ctre2["approved"] is True
    assert ctre2["notes_user"] == "Confirmed fill, sizing up"
    # Untouched signal stays unapproved.
    adi2 = next(s for s in second["signals"] if s["ticker"] == "ADI")
    assert adi2["approved"] is False


def test_carry_forward_new_signal_defaults_unapproved(policy, synthetic_runs):
    first = build_manifest(list(synthetic_runs), policy)
    existing = {s["id"]: s for s in first["signals"]}
    # Re-export against the same runs: same ids as before, nothing new.
    second = build_manifest(list(synthetic_runs), policy, existing_signals=existing)
    assert second["n_new_signals"] == 0
    assert second["n_carried_forward_approvals"] == 0  # none were approved yet


def test_carry_forward_genuinely_new_ticker_counts_as_new(policy, synthetic_runs, missing_legs_run):
    first = build_manifest(list(synthetic_runs), policy)
    existing = {s["id"]: s for s in first["signals"]}
    # Second export adds a brand-new run (GOOD ticker) not present in `existing`.
    second = build_manifest(list(synthetic_runs) + [missing_legs_run], policy, existing_signals=existing)
    assert second["n_new_signals"] == 1
    good = next(s for s in second["signals"] if s["ticker"] == "GOOD")
    assert good["approved"] is False


def test_carry_forward_counts_new_vs_carried(policy, synthetic_runs):
    first = build_manifest(list(synthetic_runs), policy)
    for s in first["signals"]:
        s["approved"] = True
    existing = {s["id"]: s for s in first["signals"]}
    second = build_manifest(list(synthetic_runs), policy, existing_signals=existing)
    assert second["n_carried_forward_approvals"] == len(second["signals"])
    assert second["n_new_signals"] == 0


def test_approve_all_overrides_carry_forward(policy, synthetic_runs):
    # If a prior run had approved:false but --approve-all is passed this
    # time, approve_all should still win (it's an explicit override).
    first = build_manifest(list(synthetic_runs), policy)
    existing = {s["id"]: s for s in first["signals"]}
    second = build_manifest(list(synthetic_runs), policy, existing_signals=existing, approve_all=True)
    assert all(s["approved"] for s in second["signals"])


def test_write_manifest_strips_transient_keys(tmp_path: Path, policy, synthetic_runs):
    manifest = build_manifest(list(synthetic_runs), policy)
    out = tmp_path / "signals.json"
    write_manifest(out, manifest)
    doc = json.loads(out.read_text())
    assert "warnings" not in doc
    assert "n_new_signals" not in doc
    assert "n_carried_forward_approvals" not in doc
    assert set(doc.keys()) == {"schema_version", "generated_at", "source_run", "measure_date", "signals"}


def test_load_existing_signals_missing_file_returns_empty(tmp_path: Path):
    assert load_existing_signals(tmp_path / "nope.json") == {}


def test_load_existing_signals_malformed_returns_empty(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("not json")
    assert load_existing_signals(p) == {}


def test_cli_regeneration_carries_forward_approval(tmp_path: Path, policy, synthetic_runs):
    policy_path = tmp_path / "policy.json"
    output = tmp_path / "signals.json"
    policy_path.write_text(json.dumps(policy))

    # First export + manual approval (simulating approve_lean_signal.py).
    rc = main([
        "--matrix-run", str(synthetic_runs[0]),
        "--matrix-run", str(synthetic_runs[1]),
        "--policy-file", str(policy_path),
        "--output", str(output),
    ])
    assert rc == 0
    doc = json.loads(output.read_text())
    for s in doc["signals"]:
        if s["ticker"] == "CTRE":
            s["approved"] = True
    output.write_text(json.dumps(doc, indent=2) + "\n")

    # Re-export (Friday refresh) — approval should survive.
    rc = main([
        "--matrix-run", str(synthetic_runs[0]),
        "--matrix-run", str(synthetic_runs[1]),
        "--policy-file", str(policy_path),
        "--output", str(output),
    ])
    assert rc == 0
    doc2 = json.loads(output.read_text())
    ctre = next(s for s in doc2["signals"] if s["ticker"] == "CTRE")
    assert ctre["approved"] is True
    adi = next(s for s in doc2["signals"] if s["ticker"] == "ADI")
    assert adi["approved"] is False


def test_cli_strict_flag_raises_on_mismatched_dates(tmp_path: Path, policy, mismatched_date_runs):
    policy_path = tmp_path / "policy.json"
    output = tmp_path / "signals.json"
    policy_path.write_text(json.dumps(policy))

    # build_manifest raises ValueError under --strict; main() does not catch
    # it (matches the script's pre-existing behavior for its other
    # ValueError-raising paths, e.g. missing --matrix-run).
    with pytest.raises(ValueError, match="snapshot_date"):
        main([
            "--matrix-run", str(mismatched_date_runs[0]),
            "--matrix-run", str(mismatched_date_runs[1]),
            "--policy-file", str(policy_path),
            "--output", str(output),
            "--strict",
        ])


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
