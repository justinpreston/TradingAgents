"""The PM prompt must ask for a price target (fork: tiers depend on it).

Upstream v0.5.x added an explicit ``## Output`` list to the PM prompt naming only
Rating / Executive Summary / Investment Thesis. The schema's optional
``price_target`` then went almost always unfilled (conservative persona: 0/17
cells in the 2026-09-27 A/B vs 59% historically), which forces every PICK into
Tier C because tier derivation keys on ``conservative_pt``.
"""
from __future__ import annotations

import pytest

from tradingagents.agents.managers import portfolio_manager as pm

pytestmark = pytest.mark.unit


def _prompt_seen_by_llm():
    captured = {}

    class _LLM:
        def with_structured_output(self, *a, **k):
            return self

        def invoke(self, prompt, *a, **k):
            captured["prompt"] = prompt if isinstance(prompt, str) else str(prompt)
            raise RuntimeError("stop after capturing the prompt")

    node = pm.create_portfolio_manager(_LLM())
    state = {
        "company_of_interest": "NUE", "trade_date": "2026-09-25",
        "risk_debate_state": {"history": "", "aggressive_history": "", "conservative_history": "",
                              "neutral_history": "", "current_aggressive_response": "",
                              "current_conservative_response": "", "current_neutral_response": "",
                              "judge_decision": "", "count": 0, "latest_speaker": ""},
        "market_report": "", "sentiment_report": "", "news_report": "", "fundamentals_report": "",
        "investment_plan": "", "trader_investment_plan": "", "past_context": "", "messages": [],
    }
    try:
        node(state)
    except Exception:
        pass
    return captured.get("prompt", "")


def test_output_section_names_price_target_and_horizon():
    prompt = _prompt_seen_by_llm()
    assert "## Output" in prompt
    assert "**Price Target**" in prompt
    assert "**Time Horizon**" in prompt
