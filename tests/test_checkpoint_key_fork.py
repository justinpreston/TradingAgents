"""Fork: matrix cells for one ticker and date must never share a checkpoint.

The matrix runs the same ticker/date as an aggressive cell and a conservative
cell (and A/B tests run other models alongside), with checkpoints in a shared
per-ticker store. On 2026-10-02 a conservative cell resumed another run's
state and finished in 31s without its own analysis. Upstream v0.5.2 hashes the
config into the checkpoint key; this pins that the fork's own knobs count.
"""
from __future__ import annotations

import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

pytestmark = pytest.mark.unit


def _signature(**config):
    graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
    graph.config = {**DEFAULT_CONFIG, **config}
    graph.selected_analysts = ["market", "social", "news", "fundamentals"]
    return graph._run_signature("stock")


def test_risk_profile_changes_the_checkpoint_key():
    assert _signature(risk_profile="aggressive") != _signature(risk_profile="conservative")


def test_persona_models_change_the_checkpoint_key():
    old = {"portfolio_manager": "gpt-5.5", "bull_researcher": "claude-opus-4.8"}
    new = {"portfolio_manager": "gpt-6.1-sol", "bull_researcher": "claude-opus-5.5"}
    assert _signature(persona_models=old) != _signature(persona_models=new)


def test_the_same_settings_keep_the_same_key():
    assert _signature(risk_profile="aggressive") == _signature(risk_profile="aggressive")


def test_persona_models_build_their_own_clients(tmp_path, monkeypatch):
    """Upstream v0.6.0 (#1440) moved the tier clients to create_tier_client; the
    fork's persona overrides still build theirs on llm_provider / backend_url."""
    from tradingagents.graph import trading_graph

    made = []

    class _Client:
        def __init__(self, model):
            self.model = model

        def get_llm(self):
            return f"llm:{self.model}"

    monkeypatch.setattr(trading_graph, "create_tier_client",
                        lambda config, tier, **k: _Client(config[f"{tier}_think_llm"]))
    monkeypatch.setattr(trading_graph, "create_llm_client",
                        lambda provider, model, base_url=None, **k: made.append((provider, model)) or _Client(model))
    cfg = {**DEFAULT_CONFIG, "results_dir": str(tmp_path / "r"), "data_cache_dir": str(tmp_path / "c"),
           "memory_log_path": str(tmp_path / "log.md"), "llm_provider": "openai",
           "deep_think_llm": "claude-sonnet-5.5", "quick_think_llm": "gpt-5.5",
           "persona_models": {"bull_researcher": "claude-sonnet-5.5", "bear_researcher": "gpt-5.4"}}
    graph = TradingAgentsGraph(config=cfg)
    assert made == [("openai", "gpt-5.4")]
    assert graph.graph_setup.persona_llms == {"bull_researcher": "llm:claude-sonnet-5.5",
                                              "bear_researcher": "llm:gpt-5.4"}
