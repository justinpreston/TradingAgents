"""Run TradingAgents using GitHub Copilot's Claude Opus 4.7 (xhigh reasoning).

This uses the Copilot Chat API (api.githubcopilot.com) with a GitHub OAuth
token. Requires an active Copilot subscription (Pro / Business / Enterprise).

Auth (in priority order):
    1. GITHUB_TOKEN env var
    2. `gh auth token` from the GitHub CLI

Setup:
    gh auth login        (one-time)
    python run_copilot_opus.py
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
        "No GitHub token available. Run `gh auth login` (Copilot subscription required)."
    )


os.environ["GITHUB_TOKEN"] = _resolve_github_token()

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "copilot"
# Opus 4.7 with extra-high reasoning effort (model id baked in by Copilot).
# Other Copilot families: claude-opus-4.7, claude-opus-4.7-high, claude-sonnet-4.6
config["quick_think_llm"] = "claude-opus-4.8"
config["deep_think_llm"] = "claude-opus-4.8"
config["max_debate_rounds"] = 1

if __name__ == "__main__":
    ta = TradingAgentsGraph(debug=True, config=config)
    _, decision = ta.propagate("NVDA", "2024-05-10")
    print(decision)
