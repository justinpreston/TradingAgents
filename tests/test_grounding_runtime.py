"""Tests for the runtime grounding assertion."""

from __future__ import annotations

import logging

from tradingagents.grounding.runtime import (
    _count_tool_calls_in_messages,
    assert_analyst_grounding,
)


class _FakeMsg:
    """Minimal AIMessage-like shape for testing."""

    def __init__(self, tool_calls=None, additional_kwargs=None):
        if tool_calls is not None:
            self.tool_calls = tool_calls
        if additional_kwargs is not None:
            self.additional_kwargs = additional_kwargs


def test_count_zero_when_no_messages():
    assert _count_tool_calls_in_messages([]) == 0


def test_count_modern_tool_calls():
    msgs = [_FakeMsg(tool_calls=[{"name": "get_news", "args": {}}])]
    assert _count_tool_calls_in_messages(msgs) == 1


def test_count_legacy_tool_calls():
    msgs = [
        _FakeMsg(additional_kwargs={"tool_calls": [{"id": "1"}, {"id": "2"}]}),
    ]
    assert _count_tool_calls_in_messages(msgs) == 2


def test_assert_warns_on_grounded_report_with_no_tool_calls(caplog):
    """Tool-bound analyst produced a long report with zero tool calls."""
    state = {
        "messages": [],
        "market_report": "## Technicals\n" + ("Detailed technical analysis. " * 50),
        "news_report": "",
        "fundamentals_report": "",
    }
    with caplog.at_level(logging.WARNING, logger="tradingagents.grounding.runtime"):
        warnings = assert_analyst_grounding(state, ticker="TEST")
    assert len(warnings) == 1
    assert "market_analyst" in warnings[0]
    assert "ZERO tool calls" in warnings[0]


def test_assert_quiet_when_tools_were_called():
    state = {
        "messages": [_FakeMsg(tool_calls=[{"name": "get_stock_data"}])],
        "market_report": "## Technicals\n" + ("Detailed technical analysis. " * 50),
        "news_report": "",
        "fundamentals_report": "",
    }
    assert assert_analyst_grounding(state, ticker="TEST") == []


def test_assert_quiet_when_report_is_empty():
    state = {
        "messages": [],
        "market_report": "",
        "news_report": "",
        "fundamentals_report": "",
    }
    assert assert_analyst_grounding(state, ticker="TEST") == []


def test_assert_quiet_when_report_too_short_to_judge():
    """A very short report could legitimately be a "no data" placeholder."""
    state = {
        "messages": [],
        "market_report": "No data available.",
        "news_report": "",
        "fundamentals_report": "",
    }
    assert assert_analyst_grounding(state, ticker="TEST") == []
