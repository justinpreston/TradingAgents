"""Declarative grounding contract for TradingAgents nodes.

The pipeline executes LLM nodes that produce content consumed by
downstream nodes (and ultimately by humans risking real money). Some
nodes have grounding tools attached (market_analyst.bind_tools, etc.);
others are explicitly *ungrounded by design* (bull_researcher operates
on the analyst reports as text — that's the intended architecture).

This module is the single place that distinguishes "grounded" from
"deliberately ungrounded" and pairs every ungrounded node with:

1. A documented **rationale** — why is this node allowed to operate
   without tools? What upstream content is it grounded on?
2. A **risk note** — what fabrication modes is this node susceptible
   to, and what guardrail in the pipeline catches them?

A CI test (``tests/test_grounding_requirements.py``) enforces that
**every** module in ``tradingagents/agents/`` is either:

  (a) bound to a non-empty tool list inside the module body, OR
  (b) listed here in ``UNGROUNDED_BY_DESIGN`` with a rationale.

This prevents a future agent from being added with no tools and no
documented rationale — exactly the failure mode that produced the old
``social_media_analyst`` fabrication.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class UngroundedRationale:
    """Documented exemption: why a node is allowed to skip tool binding."""

    module: str
    rationale: str
    risk_note: str
    mitigation: str


UNGROUNDED_BY_DESIGN: Dict[str, UngroundedRationale] = {
    "tradingagents.agents.researchers.bull_researcher": UngroundedRationale(
        module="tradingagents.agents.researchers.bull_researcher",
        rationale=(
            "Operates on the four analyst reports (market/news/fundamentals/sentiment) "
            "as text. By design it has no independent data tools — it's a synthesis "
            "node, not an analyst."
        ),
        risk_note=(
            "Under persona pressure ('build an evidence-based case'), can confabulate "
            "specific numerics — analyst-PT mentions, YoY percentages, earnings beats."
        ),
        mitigation=(
            "scripts/grounding_audit.py scans the debate history for unsourced numeric "
            "claim shapes and surfaces high counts as ELEVATED/HIGH risk."
        ),
    ),
    "tradingagents.agents.researchers.bear_researcher": UngroundedRationale(
        module="tradingagents.agents.researchers.bear_researcher",
        rationale=(
            "Symmetric counterpart to bull_researcher. Same synthesis-not-analyst "
            "architecture — operates on the four analyst reports as text."
        ),
        risk_note=(
            "Same as bull_researcher — specifically vulnerable to invented adverse "
            "events (lawsuits, downgrades, customer losses, fabricated bear-case "
            "numerics)."
        ),
        mitigation=(
            "Caught by the same scripts/grounding_audit.py numeric-claim regex pass "
            "as bull_researcher."
        ),
    ),
    "tradingagents.agents.risk_mgmt.aggressive_debator": UngroundedRationale(
        module="tradingagents.agents.risk_mgmt.aggressive_debator",
        rationale=(
            "Risk-perspective synthesis over the upstream analyst reports + research "
            "manager plan. No new data sources — this is structured deliberation."
        ),
        risk_note="Same persona-pressure fabrication risk as the researchers.",
        mitigation="Captured by the grounding_audit numeric-claim regex pass.",
    ),
    "tradingagents.agents.risk_mgmt.conservative_debator": UngroundedRationale(
        module="tradingagents.agents.risk_mgmt.conservative_debator",
        rationale=(
            "Symmetric to aggressive_debator — synthesizes risk perspective from "
            "upstream analyst reports and the research manager plan."
        ),
        risk_note=(
            "Same persona-pressure fabrication risk as aggressive_debator; may "
            "invent specific numeric claims to bolster the conservative case."
        ),
        mitigation=(
            "Caught by the same scripts/grounding_audit.py numeric-claim regex pass."
        ),
    ),
    "tradingagents.agents.risk_mgmt.neutral_debator": UngroundedRationale(
        module="tradingagents.agents.risk_mgmt.neutral_debator",
        rationale=(
            "Symmetric to aggressive_debator — synthesizes the neutral risk "
            "perspective from upstream analyst reports."
        ),
        risk_note=(
            "Same persona-pressure fabrication risk — may invent specific "
            "numerics when arguing the middle position."
        ),
        mitigation=(
            "Caught by the same scripts/grounding_audit.py numeric-claim regex pass."
        ),
    ),
    "tradingagents.agents.managers.research_manager": UngroundedRationale(
        module="tradingagents.agents.managers.research_manager",
        rationale=(
            "Produces the structured ResearchPlan synthesis (rating + entry strategy + "
            "thesis) from the bull/bear debate. Structured output via Pydantic schema."
        ),
        risk_note=(
            "Optional ``entry_price`` field is an ungrounded float. The Trader (and "
            "downstream sizing logic) should validate this against current_price."
        ),
        mitigation=(
            "build_run_accounting.py + grounding_audit.py compare LLM-emitted PT/entry "
            "numbers against the Polygon current price; flagged when >50% off."
        ),
    ),
    "tradingagents.agents.managers.portfolio_manager": UngroundedRationale(
        module="tradingagents.agents.managers.portfolio_manager",
        rationale=(
            "Produces the final PortfolioDecision (rating + executive summary + "
            "investment thesis) using structured output."
        ),
        risk_note=(
            "Optional ``price_target`` field is an ungrounded float — this is the most "
            "consequential hallucination in the system because it directly feeds the "
            "Tier A/B/C classification rule via pt_compression_pct."
        ),
        mitigation=(
            "PT sanity-check in build_run_accounting.py blocks Tier A when any PT is "
            "more than ±50% from current price. grounding_audit.py additionally "
            "compares conservative_pt against yfinance analyst consensus when "
            "available and flags >20% divergence."
        ),
    ),
    "tradingagents.agents.trader.trader": UngroundedRationale(
        module="tradingagents.agents.trader.trader",
        rationale=(
            "Turns the research_manager's investment plan into a TraderProposal. "
            "Structured output; consumes only the upstream plan + analyst reports."
        ),
        risk_note=(
            "Optional ``entry_price`` and ``stop_loss`` floats are ungrounded — no "
            "ATR or support-level tool is bound."
        ),
        mitigation=(
            "These fields are not consumed by the options overlay or sizing logic "
            "(which use current_price directly). They appear in the final report only "
            "as advisory anchors. Treat as ungrounded text annotations."
        ),
    ),
    "tradingagents.agents.analysts.sentiment_analyst": UngroundedRationale(
        module="tradingagents.agents.analysts.sentiment_analyst",
        rationale=(
            "Pre-fetches news + StockTwits + Reddit data before LLM invocation and "
            "injects them as structured prompt blocks. Does not bind tools because "
            "the data is already in the prompt from turn 0 — but the data IS grounded."
        ),
        risk_note=(
            "When all three sources return placeholders (illiquid mid-caps, "
            "rate-limited Reddit, empty StockTwits), the LLM may confabulate "
            "sentiment from training-data memory of the ticker."
        ),
        mitigation=(
            "Hard-block in sentiment_analyst_node: when informative_chars < 400 "
            "across all three sources, the LLM is NOT invoked. A literal "
            "'INSUFFICIENT SENTIMENT DATA' report is written instead."
        ),
    ),
}


def is_ungrounded_by_design(module_name: str) -> bool:
    """Return True if ``module_name`` is documented as ungrounded by design."""
    return module_name in UNGROUNDED_BY_DESIGN


def get_rationale(module_name: str) -> UngroundedRationale | None:
    """Return the documented rationale for ``module_name``, or None."""
    return UNGROUNDED_BY_DESIGN.get(module_name)
