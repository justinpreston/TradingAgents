"""Sentiment analyst — multi-source sentiment analysis for a target ticker.

Previously named ``social_media_analyst``. Renamed and redesigned because
the old version had a prompt that demanded social-media analysis but the
only tool available was Yahoo Finance news — which led LLMs to fabricate
Reddit/X/StockTwits content under prompt pressure (verified live).

The redesigned agent pre-fetches three complementary data sources before
the LLM is invoked and injects them into the prompt as structured blocks:

  1. News headlines     — Yahoo Finance (institutional framing)
  2. StockTwits messages — retail-trader posts indexed by cashtag, with
                           user-labeled Bullish/Bearish sentiment tags
  3. Reddit posts        — r/wallstreetbets, r/stocks, r/investing

The agent does not use tool-calling; the data is in the prompt from
turn 0. Output uses the structured-output pattern (json_schema for
OpenAI/xAI, response_schema for Gemini, tool-use for Anthropic), falling
back to free-text generation for providers that lack native support, so
the sentiment header (band + score + confidence) is deterministic across
runs and providers instead of free-form per-model prose.

**Grounding hard-block (fabrication prevention).** When all three
fetchers return placeholders / empty results, the LLM is *not* invoked
— a literal ``INSUFFICIENT SENTIMENT DATA`` report is written instead.
Without this guard, a thin-coverage ticker (illiquid mid-cap with no
StockTwits cashtag and zero Reddit mentions) would still pass three
empty blocks to the LLM, which under persona pressure can confabulate
sentiment from its training-data prior.

See: https://github.com/TauricResearch/TradingAgents/issues/557
See: https://github.com/TauricResearch/TradingAgents/issues/796
"""

from datetime import datetime, timedelta

from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.schemas import SentimentReport, render_sentiment_report
from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_news,
)
from tradingagents.agents.utils.structured import (
    bind_structured,
    invoke_structured_or_freetext,
)
from tradingagents.dataflows.reddit import fetch_reddit_posts
from tradingagents.dataflows.stocktwits import fetch_stocktwits_messages
from tradingagents.dataflows.tool_errors import has_tool_errors


# Minimum *informative* characters across all three blocks below which
# the LLM is bypassed entirely. Empirically calibrated so that one
# real headline (~150 chars) + a thin StockTwits/Reddit response will
# still clear the bar, but three placeholder strings (each ~50 chars)
# definitively will not.
_MIN_INFORMATIVE_CHARS = 400


def _seven_days_back(trade_date: str) -> str:
    return (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")


def create_sentiment_analyst(llm):
    """Create a sentiment analyst node for the trading graph.

    Pre-fetches news + StockTwits + Reddit data, injects them into the
    prompt as structured blocks, and produces a deterministic sentiment
    report via structured output (with a free-text fallback for providers
    that do not support it).
    """
    structured_llm = bind_structured(llm, SentimentReport, "Sentiment Analyst")

    def sentiment_analyst_node(state):
        ticker = state["company_of_interest"]
        end_date = state["trade_date"]
        start_date = _seven_days_back(end_date)
        instrument_context = get_instrument_context_from_state(state)

        # Pre-fetch all three sources. Each fetcher degrades gracefully and
        # returns a string (no exceptions surface from here), so the LLM
        # always sees something — either real data or a clear placeholder.
        news_block = get_news.func(ticker, start_date, end_date)
        stocktwits_block = fetch_stocktwits_messages(ticker, limit=30)
        reddit_block = fetch_reddit_posts(ticker)

        # Grounding hard-block. If every source came back as a placeholder
        # or a tool error, invoking the LLM would invite it to confabulate
        # sentiment from training-data memory of this ticker. Short-circuit
        # to an explicit "insufficient data" report so downstream nodes
        # (and the grounding audit) see a clean signal of the gap.
        informative_chars = _informative_chars(news_block, stocktwits_block, reddit_block)
        if informative_chars < _MIN_INFORMATIVE_CHARS:
            insufficient_report = _build_insufficient_data_report(
                ticker=ticker,
                start_date=start_date,
                end_date=end_date,
                news_block=news_block,
                stocktwits_block=stocktwits_block,
                reddit_block=reddit_block,
                informative_chars=informative_chars,
            )
            return {
                "messages": [AIMessage(content=insufficient_report)],
                "sentiment_report": insufficient_report,
            }

        system_message = _build_system_message(
            ticker=ticker,
            start_date=start_date,
            end_date=end_date,
            news_block=news_block,
            stocktwits_block=stocktwits_block,
            reddit_block=reddit_block,
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    "\n{system_message}\n"
                    "For your reference, the current date is {current_date}. {instrument_context}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(current_date=end_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        # Format the template into a concrete message list so the structured
        # and free-text paths receive the same input. No bind_tools — the
        # data is already in the prompt.
        formatted_messages = prompt.format_messages(messages=state["messages"])

        report_text = invoke_structured_or_freetext(
            structured_llm,
            llm,
            formatted_messages,
            render_sentiment_report,
            "Sentiment Analyst",
        )

        return {
            "messages": [AIMessage(content=report_text)],
            "sentiment_report": report_text,
        }

    return sentiment_analyst_node


def _build_system_message(
    *,
    ticker: str,
    start_date: str,
    end_date: str,
    news_block: str,
    stocktwits_block: str,
    reddit_block: str,
) -> str:
    """Assemble the sentiment-analyst system message with structured data blocks."""
    return f"""You are a financial market sentiment analyst. Your task is to produce a comprehensive sentiment report for {ticker} covering the period from {start_date} to {end_date}, drawing on three complementary data sources that have already been collected for you.

## Data sources (pre-fetched, in this prompt)

### News headlines — Yahoo Finance, past 7 days
Institutional framing. Fact-driven, slower-moving signal.

<start_of_news>
{news_block}
<end_of_news>

### StockTwits messages — retail-trader social platform indexed by cashtag
Fast-moving signal. Each message carries a user-labeled sentiment tag (Bullish / Bearish / no-label) plus the message body.

<start_of_stocktwits>
{stocktwits_block}
<end_of_stocktwits>

### Reddit posts — r/wallstreetbets, r/stocks, r/investing (past 7 days)
Community discussion. Engagement signal via upvote score and comment count. Subreddit character matters (r/wallstreetbets is often contrarian/exuberant; r/stocks more measured; r/investing longer-term).

<start_of_reddit>
{reddit_block}
<end_of_reddit>

## How to analyze this data (best practices)

1. **Read the StockTwits Bullish/Bearish ratio as a leading retail-sentiment signal.** A 70/30 bullish/bearish split is moderately bullish; ≥90/10 may indicate over-extension and contrarian risk; 50/50 is uncertainty. Sample size matters — base rates on the actual message count, not percentages alone.

2. **Look for cross-source divergences.** If news framing is bearish but StockTwits is overwhelmingly bullish, that mismatch is itself a signal — it can mean retail is leaning into a thesis the news flow hasn't caught up to (or vice versa, that retail is chasing while institutions are cautious).

3. **Weight Reddit posts by engagement.** A 400-upvote / 200-comment thread reflects community attention; a 3-upvote post is noise. Read the body excerpts for context — the title alone often misleads.

4. **Distinguish opinion from event.** A news headline ("Nvidia announces $500M Corning deal") is an event; a StockTwits post ("buying NVDA, this is going to moon") is opinion. Both are inputs but should be weighted differently in your conclusions.

5. **Identify recurring narrative themes.** What topic keeps coming up across sources? That's the dominant narrative driving current sentiment.

6. **Be honest about data limits.** If StockTwits returned only a handful of messages, or one or more sources returned an "<unavailable>" placeholder, the sentiment read is less robust — flag this explicitly in the `confidence` field and the narrative. If the sources are silent on a given subreddit, say so.

7. **Identify catalysts and risks** that emerge across sources — news of upcoming earnings, product launches, competitive threats, macro headlines, etc.

8. **Past sentiment is not predictive.** Frame your conclusions as signal for the trader to weigh alongside fundamentals and technicals, not as a price call.

## Output fields

Fill the following fields:

- **overall_band**: Exactly one of Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. Use Mixed when sources point in clearly different directions; Neutral only when all sources are genuinely silent.
- **overall_score**: A number from 0 (maximally bearish) to 10 (maximally bullish); 5 is neutral. Keep it consistent with overall_band.
- **confidence**: low / medium / high, based on data quality and sample size.
- **narrative**: Full source-by-source breakdown, divergences, dominant narrative themes, catalysts and risks, and a markdown summary table of key sentiment signals (direction, source, supporting evidence).

{get_language_instruction()}"""


# ---------------------------------------------------------------------------
# Grounding hard-block helpers
# ---------------------------------------------------------------------------
_PLACEHOLDER_MARKERS = (
    "<no Reddit posts found",
    "<no StockTwits messages found",
    "<stocktwits unavailable",
    "<no posts found mentioning",  # per-subreddit placeholder
    "No news found for",
    "[[TOOL_ERROR:",
)


def _informative_chars(*blocks: str) -> int:
    """Count characters across all blocks excluding pure-placeholder lines.

    A line counts toward the budget only if it contains content beyond
    the known placeholder markers. This ensures three "thin" blocks made
    entirely of placeholders evaluate to 0 even though each individual
    block has a non-trivial character count.
    """
    total = 0
    for block in blocks:
        if not block:
            continue
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if any(marker in stripped for marker in _PLACEHOLDER_MARKERS):
                continue
            total += len(stripped)
    return total


def _build_insufficient_data_report(
    *,
    ticker: str,
    start_date: str,
    end_date: str,
    news_block: str,
    stocktwits_block: str,
    reddit_block: str,
    informative_chars: int,
) -> str:
    """Produce the no-LLM short-circuit report for a data-empty cell."""
    has_news_error = has_tool_errors(news_block)
    has_st_error = has_tool_errors(stocktwits_block) or stocktwits_block.startswith("<stocktwits unavailable")
    has_reddit_error = has_tool_errors(reddit_block) or "no Reddit posts found" in reddit_block

    lines = [
        f"# Sentiment Report — {ticker} ({start_date} → {end_date})",
        "",
        "## ⚠️ INSUFFICIENT SENTIMENT DATA",
        "",
        "This report was generated **without invoking the language model** because every pre-fetched data source returned a placeholder, an empty result, or a tool error. Sentiment analysis under these conditions invites fabrication from the model's training-data memory rather than grounded analysis of current sources.",
        "",
        f"- Informative characters detected across all sources: **{informative_chars}** (threshold: {_MIN_INFORMATIVE_CHARS})",
        f"- News block: {'tool error' if has_news_error else ('no recent items' if 'No news found' in news_block else 'sparse')}",
        f"- StockTwits block: {'tool error / unavailable' if has_st_error else 'no recent messages'}",
        f"- Reddit block: {'tool error / no posts' if has_reddit_error else 'sparse'}",
        "",
        "## Implications for downstream consumers",
        "",
        "- Treat the absence of sentiment as **informational, not signal**. Do not infer bullish or bearish positioning.",
        "- Bull/bear researchers and risk debators should weight the fundamentals + market + news reports more heavily, and explicitly avoid claims that depend on sentiment data.",
        "- The grounding audit will flag this cell with `sentiment_grounding: INSUFFICIENT`.",
        "",
        "| Metric | Direction | Source | Evidence |",
        "|---|---|---|---|",
        "| Overall | — | (none) | Insufficient data; LLM bypassed |",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backwards-compatibility shim
# ---------------------------------------------------------------------------
def create_social_media_analyst(llm):
    """Deprecated alias for :func:`create_sentiment_analyst`.

    Kept so existing code that imports ``create_social_media_analyst``
    continues to work.

    .. deprecated::
        Import :func:`create_sentiment_analyst` directly instead.
    """
    import warnings
    warnings.warn(
        "create_social_media_analyst is deprecated and will be removed in a "
        "future version. Use create_sentiment_analyst instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return create_sentiment_analyst(llm)
