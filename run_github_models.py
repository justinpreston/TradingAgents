"""Run TradingAgents using GitHub Models as the LLM backend.

GitHub Models (https://github.com/marketplace/models) exposes an OpenAI-compatible
inference API. The "github" provider in tradingagents.llm_clients points the
OpenAI-compatible client at the GitHub Models endpoint and authenticates with
a GitHub token.

Auth (in priority order):
    1. GITHUB_TOKEN env var (loaded from .env if present)
    2. `gh auth token` from the GitHub CLI (uses your existing OAuth session)

Setup:
    Easiest:   gh auth login    (one-time, OAuth-based; no PAT needed)
    Then:      python run_github_models.py
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts._env import load_repo_env  # noqa: E402

from tradingagents.default_config import DEFAULT_CONFIG  # noqa: E402
from tradingagents.graph.trading_graph import TradingAgentsGraph  # noqa: E402

load_repo_env()


def _resolve_github_token() -> str:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    if shutil.which("gh"):
        try:
            token = subprocess.check_output(
                ["gh", "auth", "token"], stderr=subprocess.DEVNULL, text=True
            ).strip()
            if token:
                return token
        except subprocess.CalledProcessError:
            pass
    raise SystemExit(
        "No GitHub token available.\n"
        "  Easiest: run `gh auth login` (OAuth flow, no PAT needed).\n"
        "  Or set GITHUB_TOKEN=... in .env (fine-grained PAT with 'Models: read')."
    )


os.environ["GITHUB_TOKEN"] = _resolve_github_token()

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "github"
# GitHub Models free tier note: "low" tier models (gpt-4o-mini, gpt-4.1-mini)
# cap request bodies at ~8k tokens, which the multi-agent pipeline exceeds.
# Use "high" tier models (gpt-4.1, gpt-4o) for both quick + deep slots.
config["quick_think_llm"] = "openai/gpt-4.1"
config["deep_think_llm"] = "openai/gpt-4.1"
config["max_debate_rounds"] = 1

if __name__ == "__main__":
    ta = TradingAgentsGraph(debug=True, config=config)
    _, decision = ta.propagate("NVDA", "2024-05-10")
    print(decision)
