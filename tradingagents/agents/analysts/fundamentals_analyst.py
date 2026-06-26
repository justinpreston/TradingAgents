import os

from langchain_core.messages import ToolMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_income_statement,
    get_instrument_context_from_state,
    get_language_instruction,
)
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.tool_errors import (
    build_data_gaps_section,
    extract_tool_errors,
)


def _insider_txns_disabled() -> bool:
    """Whether to omit get_insider_transactions from the fundamentals analyst.

    Set ``TRADINGAGENTS_DISABLE_INSIDER_TXNS=1`` (or ``true`` / ``yes``) to
    drop the insider-transactions tool binding and its system-message
    sentence. Useful for ablation studies that need to isolate the impact
    of the insider data stream on agent verdicts, and as a temporary
    escape hatch when the upstream vendor chain is degraded.
    """
    val = os.environ.get("TRADINGAGENTS_DISABLE_INSIDER_TXNS", "")
    return val.strip().lower() in {"1", "true", "yes", "on"}


def create_fundamentals_analyst(llm):
    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)

        insider_disabled = _insider_txns_disabled()

        if insider_disabled:
            tools = [
                get_fundamentals,
                get_balance_sheet,
                get_cashflow,
                get_income_statement,
            ]
            tool_doc = (
                " Use the available tools: `get_fundamentals` for comprehensive"
                " company analysis, `get_balance_sheet`, `get_cashflow`, and"
                " `get_income_statement` for specific financial statements."
            )
        else:
            tools = [
                get_fundamentals,
                get_balance_sheet,
                get_cashflow,
                get_income_statement,
                get_insider_transactions,
            ]
            tool_doc = (
                " Use the available tools: `get_fundamentals` for comprehensive"
                " company analysis, `get_balance_sheet`, `get_cashflow`, and"
                " `get_income_statement` for specific financial statements, and"
                " `get_insider_transactions` to surface SEC Form-4 buying/selling"
                " by officers and directors (a strong signal for entry timing —"
                " clusters of insider buying are bullish, persistent insider"
                " selling is a yellow flag)."
            )

        system_message = (
            "You are a researcher tasked with analyzing fundamental information over the past week about a company. Please write a comprehensive report of the company's fundamental information such as financial documents, company profile, basic company financials, and company financial history to gain a full view of the company's fundamental information to inform traders. Make sure to include as much detail as possible. Provide specific, actionable insights with supporting evidence to help traders make informed decisions."
            + " Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."
            + tool_doc
            + get_language_instruction(),
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)

        result = chain.invoke(state["messages"])

        report = ""

        if len(result.tool_calls) == 0:
            report = result.content

        # Grounded-pipeline guardrail (see news_analyst.py for full rationale).
        tool_errors = []
        for msg in state.get("messages", []):
            if isinstance(msg, ToolMessage):
                tool_errors.extend(extract_tool_errors(getattr(msg, "content", "")))
        if report and tool_errors:
            report = build_data_gaps_section(tool_errors) + "\n" + report

        return {
            "messages": [result],
            "fundamentals_report": report,
        }

    return fundamentals_analyst_node
