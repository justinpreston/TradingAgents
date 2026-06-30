"""Regression tests for TradingAgentsGraph._create_tool_nodes wiring.

Each analyst ToolNode must contain the same tool callables that the
corresponding analyst factory binds to its LLM. A mismatch (a missing
entry, or a stale entry on the wrong analyst) causes LangGraph to error
out at runtime with "<tool> is not a valid tool, try one of …" — which
the agent silently retries 3-6× before giving up, and writes hedging
caveats into the report. See commit history for the original incident
where ``get_insider_transactions`` was wired to the ``news`` ToolNode but
bound to the ``fundamentals`` analyst.
"""

from __future__ import annotations

from langgraph.prebuilt import ToolNode

from tradingagents.agents.utils.agent_utils import (
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_global_news,
    get_income_statement,
    get_indicators,
    get_insider_transactions,
    get_macro_indicators,
    get_news,
    get_prediction_markets,
    get_stock_data,
    get_verified_market_snapshot,
)
from tradingagents.graph.trading_graph import TradingAgentsGraph


def _tool_nodes() -> dict[str, ToolNode]:
    """Build the tool-node mapping without going through __init__.

    The method has no self.* dependencies in its body, so this is safe
    and avoids spinning up LLM clients, memory logs, or configs.
    """
    bare = TradingAgentsGraph.__new__(TradingAgentsGraph)
    return bare._create_tool_nodes()


def _names(node: ToolNode) -> set[str]:
    # ``ToolNode`` stores tools as ``{name: BaseTool}`` on ``tools_by_name``.
    return set(node.tools_by_name.keys())


def test_fundamentals_node_contains_insider_transactions():
    """The fundamentals analyst binds get_insider_transactions, so the
    fundamentals ToolNode must list it. This is the regression case."""
    nodes = _tool_nodes()
    assert get_insider_transactions.name in _names(nodes["fundamentals"])


def test_news_node_does_not_contain_insider_transactions():
    """news_analyst does not bind insider transactions, so leaving it on
    the news ToolNode is dead code at best and confusing at worst."""
    nodes = _tool_nodes()
    assert get_insider_transactions.name not in _names(nodes["news"])


def test_each_analyst_node_has_expected_tools():
    """Pin the exact tool sets per analyst so future moves are obvious in
    the diff. If you add or remove a tool here, you must also update the
    matching analyst factory's ``tools = [...]`` list (and vice-versa)."""
    nodes = _tool_nodes()

    assert _names(nodes["market"]) == {
        get_stock_data.name,
        get_indicators.name,
        get_verified_market_snapshot.name,
    }
    assert _names(nodes["social"]) == {get_news.name}
    assert _names(nodes["news"]) == {
        get_news.name,
        get_global_news.name,
        get_macro_indicators.name,
        get_prediction_markets.name,
    }
    assert _names(nodes["fundamentals"]) == {
        get_fundamentals.name,
        get_balance_sheet.name,
        get_cashflow.name,
        get_income_statement.name,
        get_insider_transactions.name,
    }
