"""build_run_accounting._parse_state reads free-text PM price targets."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "build_run_accounting",
    Path(__file__).resolve().parent.parent / "scripts" / "build_run_accounting.py",
)
accounting = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(accounting)


@pytest.mark.parametrize("line, expected", [
    ("**Price Target**: 425.0", 425.0),
    ("**Price Target**: $170. This is the resistance zone.", 170.0),
    ("**Price Target**: $1,234.50 over 6 months", 1234.5),
])
def test_parse_state_price_target(tmp_path, line, expected):
    p = tmp_path / "X.state.json"
    p.write_text(json.dumps({"final_trade_decision": f"**Rating**: Hold\n{line}\n"}))
    assert accounting._parse_state(p)["price_target"] == expected
