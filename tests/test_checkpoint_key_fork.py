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
