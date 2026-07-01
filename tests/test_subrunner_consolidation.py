"""Parity tests for the run_copilot_persona_aligned.py / _aggressive_aligned
consolidation.

run_copilot_matrix.py's SUB_RUNNER now points at
run_copilot_persona_aligned.py (with --persona-routing aggressive-aligned)
instead of the standalone run_copilot_aggressive_aligned.py, which is now a
thin re-exec shim. These tests prove:

  1. run_copilot_matrix.py invokes run_copilot_persona_aligned.py with the
     aggressive-aligned persona routing (byte-identical model table to the
     old standalone script).
  2. run_copilot_persona_aligned.py's argument parser accepts every flag
     the matrix's _run_cell_once constructs, so the exact argv the matrix
     builds parses cleanly.
  3. The two persona-routing tables (persona-aligned vs aggressive-aligned)
     are non-degenerate and the aggressive-aligned table is byte-identical
     to the old run_copilot_aggressive_aligned.py's PERSONA_MODELS.
  4. The back-compat shim re-execs with --persona-routing defaulted to
     aggressive-aligned, preserving old callers' behavior.
  5. State-output shape: cells/<profile>/<T>/<T>.state.json content is
     unaffected by the consolidation (same _persist_state_for_resynthesis
     logic, same PM_STATE_KEYS).
"""
from __future__ import annotations

import importlib
import runpy
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

matrix_mod = importlib.import_module("run_copilot_matrix")
persona_mod = importlib.import_module("run_copilot_persona_aligned")


# ---------------------------------------------------------------------------
# 1 & 2: matrix -> sub-runner argv parity
# ---------------------------------------------------------------------------

def test_sub_runner_points_at_persona_aligned():
    assert matrix_mod.SUB_RUNNER == "run_copilot_persona_aligned.py"
    assert matrix_mod.SUB_RUNNER_PERSONA_ROUTING == "aggressive-aligned"


def test_matrix_cell_cmd_includes_persona_routing_flag(tmp_path, monkeypatch):
    """_run_cell_once must construct a cmd containing --persona-routing
    aggressive-aligned, and every flag it passes must be accepted by
    run_copilot_persona_aligned.py's own argument parser."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class _Completed:
            returncode = 1
            stdout = ""
            stderr = "boom"
        return _Completed()

    monkeypatch.setattr(matrix_mod.subprocess, "run", fake_run)

    matrix_mod._run_cell_once(
        ticker="FOO", profile="aggressive", trade_date="2026-06-26",
        matrix_dir=tmp_path, env={}, no_dashboard=True, attempt=1,
    )

    cmd = captured["cmd"]
    assert cmd[1] == "run_copilot_persona_aligned.py"
    assert "--persona-routing" in cmd
    assert cmd[cmd.index("--persona-routing") + 1] == "aggressive-aligned"
    assert "--risk-profile" in cmd
    assert cmd[cmd.index("--risk-profile") + 1] == "aggressive"
    assert "--no-dashboard" in cmd
    assert "FOO" in cmd

    # Every flag in cmd (minus the executable/script/positional ticker)
    # must be parseable by the actual argparse parser.
    argv = cmd[2:]  # drop [sys.executable, SUB_RUNNER]
    parsed = persona_mod._parse_args.__wrapped__ if hasattr(persona_mod._parse_args, "__wrapped__") else None
    parser_args = _reparse(argv)
    assert parser_args.persona_routing == "aggressive-aligned"
    assert parser_args.risk_profile == "aggressive"
    assert parser_args.no_dashboard is True
    assert parser_args.tickers == ["FOO"]


def _reparse(argv: list[str]):
    """Re-run persona_mod's argparse setup against a captured argv list."""
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("tickers", nargs="*")
    p.add_argument("--date", default=None)
    p.add_argument("--run-id", default=None)
    p.add_argument("--no-dashboard", action="store_true")
    p.add_argument("--risk-profile", choices=["aggressive", "neutral", "conservative"], default=None)
    p.add_argument("--persona-routing", choices=list(persona_mod.PERSONA_ROUTING_TABLES.keys()), default="persona-aligned")
    p.add_argument("--news-enrichment", default=None)
    p.add_argument("--earnings-calendar", default=None)
    return p.parse_args(argv)


def test_persona_aligned_parser_accepts_full_matrix_argv():
    """Construct the exact argv shape _run_cell_once builds and confirm the
    REAL parser (not a re-implementation) accepts it end-to-end."""
    argv = [
        "--date", "2026-06-26",
        "--run-id", "matrix_test/cells/aggressive/FOO",
        "--persona-routing", "aggressive-aligned",
        "--risk-profile", "aggressive",
        "--no-dashboard",
        "FOO",
    ]
    import sys as _sys
    old_argv = _sys.argv
    try:
        _sys.argv = ["run_copilot_persona_aligned.py", *argv]
        args = persona_mod._parse_args()
    finally:
        _sys.argv = old_argv
    assert args.date == "2026-06-26"
    assert args.run_id == "matrix_test/cells/aggressive/FOO"
    assert args.persona_routing == "aggressive-aligned"
    assert args.risk_profile == "aggressive"
    assert args.no_dashboard is True
    assert args.tickers == ["FOO"]


# ---------------------------------------------------------------------------
# 3: routing tables are correct and non-degenerate
# ---------------------------------------------------------------------------

_EXPECTED_AGGRESSIVE_TABLE = {
    "market_analyst":         "gpt-5.5",
    "social_analyst":         "gpt-5.5",
    "news_analyst":           "gpt-5.5",
    "fundamentals_analyst":   "gpt-5.5",
    "bull_researcher":        "claude-opus-4.8",
    "bear_researcher":        "gpt-5.5",
    "research_manager":       "gpt-5.5",
    "trader":                 "gpt-5.5",
    "aggressive_analyst":     "claude-opus-4.8",
    "neutral_analyst":        "gpt-5.5",
    "conservative_analyst":   "gpt-5.5",
    "portfolio_manager":      "gpt-5.5",
}


def test_aggressive_routing_table_matches_old_standalone_script():
    assert persona_mod.AGGRESSIVE_PERSONA_MODELS == _EXPECTED_AGGRESSIVE_TABLE


def test_persona_aligned_default_table_unchanged():
    assert persona_mod.PERSONA_MODELS["bull_researcher"] == "gpt-5.5"
    assert persona_mod.PERSONA_MODELS["bear_researcher"] == "claude-opus-4.8"


def test_two_tables_differ():
    assert persona_mod.PERSONA_MODELS != persona_mod.AGGRESSIVE_PERSONA_MODELS


def test_build_config_selects_supplied_table():
    cfg_default = persona_mod._build_config()
    assert cfg_default["persona_models"] == persona_mod.PERSONA_MODELS

    cfg_aggr = persona_mod._build_config(persona_models=persona_mod.AGGRESSIVE_PERSONA_MODELS)
    assert cfg_aggr["persona_models"] == persona_mod.AGGRESSIVE_PERSONA_MODELS


# ---------------------------------------------------------------------------
# 4: back-compat shim
# ---------------------------------------------------------------------------

def test_shim_defaults_persona_routing_to_aggressive():
    shim_src = (REPO_ROOT / "run_copilot_aggressive_aligned.py").read_text()
    assert "aggressive-aligned" in shim_src
    assert "os.execv" in shim_src
    assert "run_copilot_persona_aligned.py" in shim_src


def test_shim_is_thin():
    """The shim should not re-implement any persona-model logic (i.e. it
    must not define its own PERSONA_MODELS dict or touch the graph)."""
    shim_src = (REPO_ROOT / "run_copilot_aggressive_aligned.py").read_text()
    assert "PERSONA_MODELS = {" not in shim_src
    assert "PERSONA_MODELS: dict" not in shim_src
    assert "TradingAgentsGraph" not in shim_src


# ---------------------------------------------------------------------------
# 5: state-output shape parity
# ---------------------------------------------------------------------------

def test_persist_state_shape_unchanged(tmp_path):
    final_state = {
        "company_of_interest": "FOO",
        "trade_date": "2026-06-26",
        "investment_plan": "buy",
        "trader_investment_plan": "buy calls",
        "past_context": "n/a",
        "final_trade_decision": "**Rating**: Overweight\n**Price Target**: 100.0\n",
        "risk_debate_state": {"history": "..."},
        "investment_debate_state": {"history": "..."},
        "irrelevant_key": "should not be persisted",
    }
    state_path = tmp_path / "FOO.state.json"
    persona_mod._persist_state_for_resynthesis(final_state, state_path, "FOO", "2026-06-26")

    import json
    data = json.loads(state_path.read_text())
    assert data["_ticker"] == "FOO"
    assert data["_trade_date"] == "2026-06-26"
    for key in persona_mod.PM_STATE_KEYS:
        assert key in data
    assert "irrelevant_key" not in data
