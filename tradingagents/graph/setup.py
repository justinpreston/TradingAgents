# TradingAgents/graph/setup.py

from typing import Any, Dict, Optional
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents import (
    create_aggressive_debator,
    create_bear_researcher,
    create_bull_researcher,
    create_conservative_debator,
    create_fundamentals_analyst,
    create_market_analyst,
    create_msg_delete,
    create_neutral_debator,
    create_news_analyst,
    create_portfolio_manager,
    create_research_manager,
    create_sentiment_analyst,
    create_trader,
)
from tradingagents.agents.utils.agent_states import AgentState

from .analyst_execution import build_analyst_execution_plan
from .conditional_logic import ConditionalLogic

# Every target a shared conditional router can return. Each edge driven by the
# router maps all of them, so a fall-through return (e.g. under prompt/i18n/
# refactor drift in the speaker labels) can never hit a missing path_map entry
# and crash LangGraph mid-run (#1088).
DEBATE_PATH_MAP = {
    "Bull Researcher": "Bull Researcher",
    "Bear Researcher": "Bear Researcher",
    "Research Manager": "Research Manager",
}
RISK_ANALYSIS_PATH_MAP = {
    "Aggressive Analyst": "Aggressive Analyst",
    "Conservative Analyst": "Conservative Analyst",
    "Neutral Analyst": "Neutral Analyst",
    "Portfolio Manager": "Portfolio Manager",
}


class GraphSetup:
    """Handles the setup and configuration of the agent graph."""

    def __init__(
        self,
        quick_thinking_llm: Any,
        deep_thinking_llm: Any,
        tool_nodes: dict[str, ToolNode],
        conditional_logic: ConditionalLogic,
        persona_llms: Optional[Dict[str, Any]] = None,
        risk_profile: Optional[str] = None,
        analyst_concurrency_limit: int = 1,
    ):
        """Initialize with required components.

        Args:
            persona_llms: Optional mapping from persona role name to a
                pre-built LLM client. Roles not present in the dict fall
                back to ``quick_thinking_llm`` or ``deep_thinking_llm`` as
                they have historically. Recognised roles:
                    market_analyst, social_analyst, news_analyst,
                    fundamentals_analyst, bull_researcher, bear_researcher,
                    research_manager, trader, aggressive_analyst,
                    neutral_analyst, conservative_analyst, portfolio_manager.
            risk_profile: Optional risk-tolerance profile that drives a PM
                prompt addendum. One of "aggressive", "conservative",
                "neutral", or None. Default behavior (no addendum) is
                preserved when None or "neutral".
            analyst_concurrency_limit: Upstream analyst-execution planner
                knob that controls how many analyst nodes can run in
                parallel. Default 1 (sequential) preserves prior behavior.
        """
        self.quick_thinking_llm = quick_thinking_llm
        self.deep_thinking_llm = deep_thinking_llm
        self.tool_nodes = tool_nodes
        self.conditional_logic = conditional_logic
        self.persona_llms = persona_llms or {}
        self.risk_profile = risk_profile
        self.analyst_concurrency_limit = analyst_concurrency_limit

    def _llm(self, role: str, default: Any) -> Any:
        """Return the LLM bound to ``role``, falling back to ``default``."""
        return self.persona_llms.get(role, default)

    def setup_graph(
        self, selected_analysts=("market", "social", "news", "fundamentals")
    ):
        """Set up and compile the agent workflow graph.

        Args:
            selected_analysts (list): List of analyst types to include. Options are:
                - "market": Market analyst
                - "social": Social media analyst
                - "news": News analyst
                - "fundamentals": Fundamentals analyst
        """
        plan = build_analyst_execution_plan(selected_analysts)

        analyst_factories = {
            "market": lambda: create_market_analyst(
                self._llm("market_analyst", self.quick_thinking_llm)
            ),
            "social": lambda: create_sentiment_analyst(
                self._llm("social_analyst", self.quick_thinking_llm)
            ),
            "news": lambda: create_news_analyst(
                self._llm("news_analyst", self.quick_thinking_llm)
            ),
            "fundamentals": lambda: create_fundamentals_analyst(
                self._llm("fundamentals_analyst", self.quick_thinking_llm)
            ),
        }

        # Create researcher and manager nodes
        bull_researcher_node = create_bull_researcher(
            self._llm("bull_researcher", self.quick_thinking_llm)
        )
        bear_researcher_node = create_bear_researcher(
            self._llm("bear_researcher", self.quick_thinking_llm)
        )
        research_manager_node = create_research_manager(
            self._llm("research_manager", self.deep_thinking_llm),
            risk_profile=self.risk_profile,
        )
        trader_node = create_trader(
            self._llm("trader", self.quick_thinking_llm)
        )

        # Create risk analysis nodes
        aggressive_analyst = create_aggressive_debator(
            self._llm("aggressive_analyst", self.quick_thinking_llm)
        )
        neutral_analyst = create_neutral_debator(
            self._llm("neutral_analyst", self.quick_thinking_llm)
        )
        conservative_analyst = create_conservative_debator(
            self._llm("conservative_analyst", self.quick_thinking_llm)
        )
        portfolio_manager_node = create_portfolio_manager(
            self._llm("portfolio_manager", self.deep_thinking_llm),
            risk_profile=self.risk_profile,
        )

        # Create workflow
        workflow = StateGraph(AgentState)

        # Add analyst nodes to the graph
        for spec in plan.specs:
            workflow.add_node(spec.agent_node, analyst_factories[spec.key]())
            workflow.add_node(spec.clear_node, create_msg_delete())
            workflow.add_node(spec.tool_node, self.tool_nodes[spec.key])

        # Add other nodes
        workflow.add_node("Bull Researcher", bull_researcher_node)
        workflow.add_node("Bear Researcher", bear_researcher_node)
        workflow.add_node("Research Manager", research_manager_node)
        workflow.add_node("Trader", trader_node)
        workflow.add_node("Aggressive Analyst", aggressive_analyst)
        workflow.add_node("Neutral Analyst", neutral_analyst)
        workflow.add_node("Conservative Analyst", conservative_analyst)
        workflow.add_node("Portfolio Manager", portfolio_manager_node)

        # Define edges
        # Start with the first analyst
        workflow.add_edge(START, plan.specs[0].agent_node)

        # Connect analysts in sequence
        for i, spec in enumerate(plan.specs):
            current_analyst = spec.agent_node
            current_tools = spec.tool_node
            current_clear = spec.clear_node

            # Add conditional edges for current analyst
            workflow.add_conditional_edges(
                current_analyst,
                getattr(self.conditional_logic, f"should_continue_{spec.key}"),
                [current_tools, current_clear],
            )
            workflow.add_edge(current_tools, current_analyst)

            # Connect to next analyst or to Bull Researcher if this is the last analyst
            if i < len(plan.specs) - 1:
                workflow.add_edge(current_clear, plan.specs[i + 1].agent_node)
            else:
                workflow.add_edge(current_clear, "Bull Researcher")

        # Both research-debate edges share the complete DEBATE_PATH_MAP (#1088).
        for debate_node in ("Bull Researcher", "Bear Researcher"):
            workflow.add_conditional_edges(
                debate_node,
                self.conditional_logic.should_continue_debate,
                DEBATE_PATH_MAP,
            )
        workflow.add_edge("Research Manager", "Trader")
        workflow.add_edge("Trader", "Aggressive Analyst")
        # All three risk edges share the complete RISK_ANALYSIS_PATH_MAP (#1088).
        for risk_node in ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst"):
            workflow.add_conditional_edges(
                risk_node,
                self.conditional_logic.should_continue_risk_analysis,
                RISK_ANALYSIS_PATH_MAP,
            )

        workflow.add_edge("Portfolio Manager", END)

        return workflow
