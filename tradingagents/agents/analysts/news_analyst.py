from langchain_core.messages import ToolMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_global_news,
    get_language_instruction,
    get_news,
)
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.news_enrichment_loader import (
    build_enrichment_prefix,
)
from tradingagents.dataflows.tool_errors import (
    build_data_gaps_section,
    extract_tool_errors,
)


def create_news_analyst(llm):
    def news_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]
        asset_type = state.get("asset_type", "stock")
        asset_label = "company" if asset_type == "stock" else "asset"
        instrument_context = get_instrument_context_from_state(state)

        tools = [
            get_news,
            get_global_news,
        ]

        # Pre-computed news enrichment context (FinBERT polarity + theme tags)
        # is opt-in via env var. When set, prepend it to the system message so
        # the analyst starts with verified ground truth before tool calls.
        enrichment_prefix = build_enrichment_prefix(ticker)

        system_message = (
            enrichment_prefix
            + f"You are a news researcher tasked with analyzing recent news and trends over the past week. Please write a comprehensive report of the current state of the world that is relevant for trading and macroeconomics. Use the available tools: get_news(query, start_date, end_date) for {asset_label}-specific or targeted news searches, and get_global_news(curr_date, look_back_days, limit) for broader macroeconomic news. Provide specific, actionable insights with supporting evidence to help traders make informed decisions."
            + """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
            + get_language_instruction()
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
                    " You have access to the following tools: {tool_names}.\n{system_message}"
                    "For your reference, the current date is {current_date}. {instrument_context}",
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

        # Grounded-pipeline guardrail: scan the tool-message history for any
        # TOOL_ERROR markers (or legacy "Error fetching" / "Error retrieving"
        # phrasing). If present, prepend an explicit "Data Gaps" section to
        # the report. The LLM has already written its summary by this point;
        # surfacing the gaps in-band ensures any downstream consumer (the
        # research manager, the grounding audit) can see that part of the
        # report is unsupported instead of fabricated.
        tool_errors = []
        for msg in state.get("messages", []):
            if isinstance(msg, ToolMessage):
                tool_errors.extend(extract_tool_errors(getattr(msg, "content", "")))
        if report and tool_errors:
            gaps_block = build_data_gaps_section(tool_errors)
            report = gaps_block + "\n" + report

        return {
            "messages": [result],
            "news_report": report,
        }

    return news_analyst_node
