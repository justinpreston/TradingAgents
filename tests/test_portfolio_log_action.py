"""Tests for portfolio_log_action.py."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.portfolio_log_action import main as log_main


def test_log_action_writes_jsonl_entry(tmp_path: Path):
    log = tmp_path / "trades.jsonl"
    rc = log_main([
        "--ticker", "VIRT",
        "--action", "TRIM",
        "--id", "VIRT-c",
        "--qty", "2",
        "--premium", "5.65",
        "--underlying", "51.40",
        "--notes", "Took half profits",
        "--log-file", str(log),
    ])
    assert rc == 0
    assert log.exists()
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["ticker"] == "VIRT"
    assert entry["action"] == "TRIM"
    assert entry["qty"] == 2.0
    assert entry["notes"] == "Took half profits"
    assert "ts" in entry


def test_log_action_appends(tmp_path: Path):
    log = tmp_path / "trades.jsonl"
    log_main(["--ticker", "VIRT", "--action", "OPEN", "--qty", "5", "--log-file", str(log)])
    log_main(["--ticker", "VIRT", "--action", "TRIM", "--qty", "2", "--log-file", str(log)])
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["action"] == "OPEN"
    assert json.loads(lines[1])["action"] == "TRIM"


def test_log_action_uppercases_ticker_and_action(tmp_path: Path):
    log = tmp_path / "trades.jsonl"
    log_main(["--ticker", "virt", "--action", "TRIM", "--qty", "1", "--log-file", str(log)])
    entry = json.loads(log.read_text().strip())
    assert entry["ticker"] == "VIRT"
    assert entry["action"] == "TRIM"


def test_log_action_note_only_no_qty(tmp_path: Path):
    log = tmp_path / "trades.jsonl"
    rc = log_main([
        "--ticker", "INTC",
        "--action", "NOTE",
        "--notes", "Earnings 7/24",
        "--log-file", str(log),
    ])
    assert rc == 0
    entry = json.loads(log.read_text().strip())
    assert entry["action"] == "NOTE"
    assert "qty" not in entry  # None values are stripped
    assert entry["notes"] == "Earnings 7/24"


def test_log_action_creates_parent_dir(tmp_path: Path):
    log = tmp_path / "nested" / "deeper" / "trades.jsonl"
    rc = log_main(["--ticker", "X", "--action", "OPEN", "--qty", "1", "--log-file", str(log)])
    assert rc == 0
    assert log.exists()
