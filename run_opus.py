"""Run TradingAgents using Anthropic Claude Opus 4.7 as the LLM backend.

Setup:
    1. Get your Anthropic API key from https://console.anthropic.com/settings/keys
    2. Add it to .env as: ANTHROPIC_API_KEY=sk-ant-...
    3. python run_opus.py

Cost note: Claude Opus 4.7 is premium-tier. A full multi-agent run
(market + news + social + fundamentals analysts → bull/bear debate →
trader → risk debate → portfolio manager) is roughly 30-50 LLM calls
with significant context accumulation. Budget accordingly.
"""

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts._env import load_repo_env  # noqa: E402

from tradingagents.default_config import DEFAULT_CONFIG  # noqa: E402
from tradingagents.graph.trading_graph import TradingAgentsGraph  # noqa: E402

load_repo_env()

if not os.environ.get("ANTHROPIC_API_KEY"):
    raise SystemExit(
        "Missing ANTHROPIC_API_KEY. Get a key at "
        "https://console.anthropic.com/settings/keys and add it to .env "
        "as ANTHROPIC_API_KEY=sk-ant-..."
    )

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "anthropic"
# Opus 4.7 for both slots - premium intelligence end-to-end.
config["quick_think_llm"] = "claude-opus-4-7"
config["deep_think_llm"] = "claude-opus-4-7"
config["max_debate_rounds"] = 1
# Anthropic-specific: dial reasoning effort. Options: "low", "medium", "high".
config["anthropic_effort"] = "high"

if __name__ == "__main__":
    ta = TradingAgentsGraph(debug=True, config=config)
    _, decision = ta.propagate("NVDA", "2024-05-10")
    print(decision)
