"""Tests for scripts/approve_lean_signal.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.approve_lean_signal import (
    SignalsFileError,
    append_audit_record,
    load_signals_file,
    main,
    set_approval,
    write_signals_file,
)


@pytest.fixture
def signals_doc() -> dict:
    return {
        "schema_version": "1.0",
        "generated_at": "2026-06-28T11:07:25-04:00",
        "source_run": "runs/matrix_mid_weekly_2026-06-26_2103_chain",
        "measure_date": "2026-06-27",
        "signals": [
            {
                "id": "NUE-2026-06-27",
                "ticker": "NUE",
                "tier": "A",
                "classification": "PICK",
                "underlying_ref_price": 239.78,
                "option": {
                    "right": "call", "strike": 240, "expiry": "2026-12-18",
                    "target_delta": 0.55, "occ_symbol": "O:NUE261218C00240000",
                    "ref_premium_per_share": 26.37,
                },
                "entry": {"max_premium_per_share": 27.6885, "size_pct": 0.04, "max_size_pct": 0.06},
                "exits": {
                    "cons_pt": 274.0, "aggr_pt": 274.0, "rule": "tier_a_take_profit",
                    "stop_loss_premium_pct": -0.4, "time_stop_dte": 21,
                },
                "approved": False,
                "notes": "",
            },
            {
                "id": "ADI-2026-06-27",
                "ticker": "ADI",
                "tier": "C",
                "classification": "PICK",
                "underlying_ref_price": 386.91,
                "option": {
                    "right": "call", "strike": 400, "expiry": "2026-12-18",
                    "target_delta": 0.55, "occ_symbol": "O:ADI261218C00400000",
                    "ref_premium_per_share": 55.36,
                },
                "entry": {"max_premium_per_share": 58.128, "size_pct": 0.02, "max_size_pct": 0.04},
                "exits": {
                    "cons_pt": None, "aggr_pt": 470.0, "rule": "tier_c_trim",
                    "stop_loss_premium_pct": -0.4, "time_stop_dte": 21,
                },
                "approved": False,
                "notes": "",
            },
        ],
    }


@pytest.fixture
def signals_file(tmp_path: Path, signals_doc: dict) -> Path:
    p = tmp_path / "signals.json"
    p.write_text(json.dumps(signals_doc, indent=2) + "\n")
    return p


# ---------------------------------------------------------------------------
# load_signals_file


def test_load_signals_file_missing_raises(tmp_path: Path):
    with pytest.raises(SignalsFileError, match="not found"):
        load_signals_file(tmp_path / "does_not_exist.json")


def test_load_signals_file_malformed_json_raises(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json")
    with pytest.raises(SignalsFileError, match="not valid JSON"):
        load_signals_file(p)


def test_load_signals_file_wrong_shape_raises(tmp_path: Path):
    p = tmp_path / "wrong.json"
    p.write_text(json.dumps({"foo": "bar"}))
    with pytest.raises(SignalsFileError, match="signals"):
        load_signals_file(p)


def test_load_signals_file_ok(signals_file: Path):
    doc = load_signals_file(signals_file)
    assert len(doc["signals"]) == 2


# ---------------------------------------------------------------------------
# set_approval


def test_set_approval_flips_true(signals_doc: dict):
    doc, applied, unknown = set_approval(signals_doc, ["NUE-2026-06-27"], True)
    assert applied == ["NUE-2026-06-27"]
    assert unknown == []
    nue = next(s for s in doc["signals"] if s["id"] == "NUE-2026-06-27")
    assert nue["approved"] is True
    adi = next(s for s in doc["signals"] if s["id"] == "ADI-2026-06-27")
    assert adi["approved"] is False  # untouched


def test_set_approval_flips_false(signals_doc: dict):
    signals_doc["signals"][0]["approved"] = True
    doc, applied, _ = set_approval(signals_doc, ["NUE-2026-06-27"], False)
    assert doc["signals"][0]["approved"] is False


def test_set_approval_unknown_id_raises_with_helpful_list(signals_doc: dict):
    with pytest.raises(SignalsFileError) as exc_info:
        set_approval(signals_doc, ["TYPO-2026-06-27"], True)
    msg = str(exc_info.value)
    assert "TYPO-2026-06-27" in msg
    assert "NUE-2026-06-27" in msg  # available ids listed
    assert "ADI-2026-06-27" in msg


def test_set_approval_partial_unknown_batch_applies_nothing(signals_doc: dict):
    # If one id in a multi-id batch is unknown, refuse the whole batch rather
    # than partially applying (caller might assume all-or-nothing).
    with pytest.raises(SignalsFileError):
        set_approval(signals_doc, ["NUE-2026-06-27", "BOGUS-2026-06-27"], True)
    nue = next(s for s in signals_doc["signals"] if s["id"] == "NUE-2026-06-27")
    assert nue["approved"] is False  # untouched despite being valid


# ---------------------------------------------------------------------------
# write_signals_file preserves key order / indent


def test_write_signals_file_preserves_key_order(tmp_path: Path, signals_doc: dict):
    out = tmp_path / "out.json"
    write_signals_file(out, signals_doc)
    text = out.read_text()
    # Top-level key order preserved
    assert text.index('"schema_version"') < text.index('"generated_at"')
    assert text.index('"generated_at"') < text.index('"measure_date"')
    assert text.index('"measure_date"') < text.index('"signals"')
    # 2-space indent convention
    assert '{\n  "schema_version"' in text
    # Round-trips
    reloaded = json.loads(text)
    assert reloaded == signals_doc


# ---------------------------------------------------------------------------
# append_audit_record


def test_append_audit_record_writes_jsonl(tmp_path: Path):
    log = tmp_path / "approvals_log.jsonl"
    append_audit_record(log, ids=["NUE-2026-06-27", "ADI-2026-06-27"], action="approve", ts="2026-06-28T12:00:00Z")
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 2
    rec0 = json.loads(lines[0])
    assert rec0 == {
        "ts": "2026-06-28T12:00:00Z",
        "id": "NUE-2026-06-27",
        "action": "approve",
        "source": "approve_lean_signal",
    }


def test_append_audit_record_creates_parent_dir(tmp_path: Path):
    log = tmp_path / "nested" / "dir" / "approvals_log.jsonl"
    append_audit_record(log, ids=["X-2026-01-01"], action="approve")
    assert log.exists()


# ---------------------------------------------------------------------------
# main() CLI


def test_main_approve_flips_and_logs(tmp_path: Path, signals_file: Path):
    log = tmp_path / "approvals_log.jsonl"
    rc = main([
        "--id", "NUE-2026-06-27",
        "--signals-file", str(signals_file),
        "--approvals-log", str(log),
    ])
    assert rc == 0
    doc = json.loads(signals_file.read_text())
    nue = next(s for s in doc["signals"] if s["id"] == "NUE-2026-06-27")
    assert nue["approved"] is True
    adi = next(s for s in doc["signals"] if s["id"] == "ADI-2026-06-27")
    assert adi["approved"] is False

    audit = [json.loads(l) for l in log.read_text().strip().splitlines()]
    assert len(audit) == 1
    assert audit[0]["id"] == "NUE-2026-06-27"
    assert audit[0]["action"] == "approve"


def test_main_unapprove(tmp_path: Path, signals_file: Path):
    log = tmp_path / "approvals_log.jsonl"
    # First approve...
    main(["--id", "NUE-2026-06-27", "--signals-file", str(signals_file), "--approvals-log", str(log)])
    # ...then revoke.
    rc = main([
        "--id", "NUE-2026-06-27", "--unapprove",
        "--signals-file", str(signals_file), "--approvals-log", str(log),
    ])
    assert rc == 0
    doc = json.loads(signals_file.read_text())
    nue = next(s for s in doc["signals"] if s["id"] == "NUE-2026-06-27")
    assert nue["approved"] is False

    audit = [json.loads(l) for l in log.read_text().strip().splitlines()]
    assert len(audit) == 2
    assert audit[1]["action"] == "unapprove"


def test_main_multiple_ids_repeatable_flag(tmp_path: Path, signals_file: Path):
    log = tmp_path / "approvals_log.jsonl"
    rc = main([
        "--id", "NUE-2026-06-27",
        "--id", "ADI-2026-06-27",
        "--signals-file", str(signals_file),
        "--approvals-log", str(log),
    ])
    assert rc == 0
    doc = json.loads(signals_file.read_text())
    assert all(s["approved"] for s in doc["signals"])
    audit = [json.loads(l) for l in log.read_text().strip().splitlines()]
    assert len(audit) == 2


def test_main_unknown_id_errors_and_does_not_write(tmp_path: Path, signals_file: Path, capsys):
    original = signals_file.read_text()
    rc = main([
        "--id", "BOGUS-2026-06-27",
        "--signals-file", str(signals_file),
    ])
    assert rc == 2
    assert signals_file.read_text() == original  # unchanged
    err = capsys.readouterr().err
    assert "BOGUS-2026-06-27" in err
    assert "NUE-2026-06-27" in err  # helpful id listing


def test_main_missing_file_errors(tmp_path: Path, capsys):
    rc = main([
        "--id", "X-2026-01-01",
        "--signals-file", str(tmp_path / "missing.json"),
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err


def test_main_list_shows_all_signals(signals_file: Path, capsys):
    rc = main(["--list", "--signals-file", str(signals_file)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "NUE-2026-06-27" in out
    assert "ADI-2026-06-27" in out
    assert "0/2 approved" in out


def test_main_no_ids_and_no_list_errors(signals_file: Path, capsys):
    rc = main(["--signals-file", str(signals_file)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--id" in err
