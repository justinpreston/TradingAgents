"""Portfolio Manager: synthesises the risk-analyst debate into the final decision.

Uses LangChain's ``with_structured_output`` so the LLM produces a typed
``PortfolioDecision`` directly, in a single call.  The result is rendered
back to markdown for storage in ``final_trade_decision`` so memory log,
CLI display, and saved reports continue to consume the same shape they do
today.  When a provider does not expose structured output, the agent falls
back gracefully to free-text generation.
"""

from __future__ import annotations

from tradingagents.agents.schemas import PortfolioDecision, render_pm_decision
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_language_instruction,
)
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)


RISK_PROFILE_ADDENDA: dict = {
    "aggressive": (
        "**Investor Risk Profile: Aggressive Growth.**\n"
        "The portfolio's mandate is aggressive capital appreciation, not capital preservation. "
        "Apply this disposition when synthesising:\n"
        "- When the bull case identifies a coherent fundamental or technical growth signal "
        "AND the bear case has provided a well-defined stop or invalidation level, prefer "
        "**Buy** or **Overweight** over Hold.\n"
        "- Treat 'extended valuation', 'near resistance', or 'overbought RSI' alone as "
        "**insufficient** to downgrade — these are entry-timing concerns, not exit triggers, "
        "and are addressed via staged sizing and stops rather than rating downgrades.\n"
        "- Reserve **Underweight** or **Sell** for cases where the bear identifies concrete "
        "deterioration in the underlying operating engine (e.g., margin compression, demand "
        "destruction, deteriorating cash conversion, accounting concerns, broken trend on volume).\n"
        "- Position sizing should express conviction in the upside, not absence of risk: "
        "use stops, staged entry, and core/trade splits to manage risk while remaining exposed.\n"
        "- The investor's precept: 'It's not how much you make, it's how much you keep' — "
        "translates here to disciplined stops, NOT to defaulting to Hold/Underweight when "
        "growth signals are intact."
    ),
    "conservative": (
        "**Investor Risk Profile: Conservative.**\n"
        "The portfolio's mandate is capital preservation with steady income. "
        "Apply this disposition when synthesising:\n"
        "- When the bear case identifies any risk to the underlying operating engine, "
        "valuation, or technical structure, prefer **Hold** or **Underweight** over Buy/Overweight.\n"
        "- Require multiple corroborating bull signals (earnings momentum AND margin expansion "
        "AND favorable technicals AND reasonable valuation) before upgrading to Overweight or Buy.\n"
        "- Treat ambiguous or 'mixed' setups as **Hold** by default.\n"
        "- Prefer larger margins of safety; require confirmed catalysts before adding."
    ),
    "neutral": None,
}


def _format_risk_profile(risk_profile):
    if risk_profile in (None, "", "neutral"):
        return ""
    addendum = RISK_PROFILE_ADDENDA.get(risk_profile)
    if not addendum:
        return ""
    return f"\n---\n\n{addendum}\n"


def create_portfolio_manager(llm, risk_profile: str | None = None):
    structured_llm = bind_structured(llm, PortfolioDecision, "Portfolio Manager")
    risk_profile_block = _format_risk_profile(risk_profile)

    def portfolio_manager_node(state) -> dict:
        instrument_context = build_instrument_context(state["company_of_interest"])

        history = state["risk_debate_state"]["history"]
        risk_debate_state = state["risk_debate_state"]
        research_plan = state["investment_plan"]
        trader_plan = state["trader_investment_plan"]

        past_context = state.get("past_context", "")
        lessons_line = (
            f"- Lessons from prior decisions and outcomes:\n{past_context}\n"
            if past_context
            else ""
        )

        prompt = f"""As the Portfolio Manager, synthesize the risk analysts' debate and deliver the final trading decision.

{instrument_context}

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction to enter or add to position
- **Overweight**: Favorable outlook, gradually increase exposure
- **Hold**: Maintain current position, no action needed
- **Underweight**: Reduce exposure, take partial profits
- **Sell**: Exit position or avoid entry
{risk_profile_block}
**Context:**
- Research Manager's investment plan: **{research_plan}**
- Trader's transaction proposal: **{trader_plan}**
{lessons_line}
**Risk Analysts Debate History:**
{history}

---

Be decisive and ground every conclusion in specific evidence from the analysts.{get_language_instruction()}"""

        final_trade_decision = invoke_structured_or_freetext(
            structured_llm,
            llm,
            prompt,
            render_pm_decision,
            "Portfolio Manager",
        )

        new_risk_debate_state = {
            "judge_decision": final_trade_decision,
            "history": risk_debate_state["history"],
            "aggressive_history": risk_debate_state["aggressive_history"],
            "conservative_history": risk_debate_state["conservative_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_aggressive_response": risk_debate_state["current_aggressive_response"],
            "current_conservative_response": risk_debate_state["current_conservative_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": final_trade_decision,
        }

    return portfolio_manager_node
