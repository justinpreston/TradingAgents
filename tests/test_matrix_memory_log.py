"""Matrix cells write their own memory log, one per profile.

On the global log the matrix lost every Stage-B conservative decision: the log
keeps one entry per (ticker, date), so the aggressive entry Stage A wrote first
blocked it. And since v0.6.0 every cell's Memory Log step settles every ticker
on the log it shares, including ad-hoc and CLI runs.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

from tradingagents.memory.log import TradingMemoryLog

matrix_mod = importlib.import_module("run_copilot_matrix")
CellResult = matrix_mod.CellResult


def test_each_profile_gets_its_own_log(tmp_path):
    env = {"GITHUB_TOKEN": "t"}
    aggr = matrix_mod._stage_env(env, tmp_path, "aggressive")
    cons = matrix_mod._stage_env(env, tmp_path, "conservative")
    assert aggr[matrix_mod.MEMORY_LOG_ENV] == str(tmp_path / "aggressive.md")
    assert cons[matrix_mod.MEMORY_LOG_ENV] == str(tmp_path / "conservative.md")
    assert matrix_mod.MEMORY_LOG_ENV not in env  # the shared env is not mutated


def test_shared_log_leaves_the_inherited_env_alone():
    env = {"GITHUB_TOKEN": "t", matrix_mod.MEMORY_LOG_ENV: "/elsewhere.md"}
    assert matrix_mod._stage_env(env, None, "aggressive") is env


def test_one_log_drops_the_second_profiles_decision(tmp_path):
    """Why the logs are split: a shared log keeps only the first decision."""
    shared = TradingMemoryLog({"memory_log_path": str(tmp_path / "shared.md")})
    shared.store_decision("ABC", "2026-10-30", "Rating: Overweight", rating="Overweight")
    shared.store_decision("ABC", "2026-10-30", "Rating: Underweight", rating="Underweight")
    assert [e["rating"] for e in shared.load_entries()] == ["Overweight"]

    for profile, rating in (("aggressive", "Overweight"), ("conservative", "Underweight")):
        path = matrix_mod._memory_log_path(tmp_path, profile)
        log = TradingMemoryLog({"memory_log_path": str(path)})
        log.store_decision("ABC", "2026-10-30", f"Rating: {rating}", rating=rating)
        assert [e["rating"] for e in log.load_entries()] == [rating]


def _run_main(monkeypatch, tmp_path, *extra):
    seen = {}

    def fake_stage(profile, tickers, env, **kw):
        seen[profile] = env.get(matrix_mod.MEMORY_LOG_ENV)
        rating = "Overweight" if profile == "aggressive" else "Hold"
        return {t: CellResult(ticker=t, profile=profile, status="ok", rating=rating) for t in tickers}

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(matrix_mod.MEMORY_LOG_ENV, raising=False)
    monkeypatch.setattr(matrix_mod, "_run_stage", fake_stage)
    monkeypatch.setattr(matrix_mod, "_resolve_github_token", lambda: "t")
    monkeypatch.setattr(sys, "argv", [
        "run_copilot_matrix.py", "--tickers", "ABC", "--date", "2026-10-23",
        "--run-id", "m", "--no-auto-report", *extra,
    ])
    assert matrix_mod.main() == 0
    return seen


def test_main_points_each_stage_at_its_profile_log(monkeypatch, tmp_path):
    logs = tmp_path / "logs"
    seen = _run_main(monkeypatch, tmp_path, "--memory-log-dir", str(logs))
    assert seen == {"aggressive": str(logs / "aggressive.md"),
                    "conservative": str(logs / "conservative.md")}


def test_main_shared_memory_log_opts_out(monkeypatch, tmp_path):
    seen = _run_main(monkeypatch, tmp_path, "--shared-memory-log")
    assert seen == {"aggressive": None, "conservative": None}


def test_default_dir_is_under_the_tradingagents_home():
    assert Path.home() / ".tradingagents" / "memory" / "matrix" == matrix_mod.DEFAULT_MEMORY_LOG_DIR
