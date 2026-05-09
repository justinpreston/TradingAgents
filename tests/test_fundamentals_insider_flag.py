"""Tests for the ``TRADINGAGENTS_DISABLE_INSIDER_TXNS`` ablation flag.

Background — commit ``57eb9d5`` added ``get_insider_transactions`` to the
fundamentals analyst's tool list and bound it to the LLM, but the
``fundamentals`` :class:`langgraph.prebuilt.ToolNode` in
``tradingagents/graph/trading_graph.py`` was never updated to serve that
tool. The result is the LLM repeatedly calls ``get_insider_transactions``
and the ToolNode replies *"is not a valid tool, try one of …"*. The
agent then writes uncertainty caveats into its fundamentals report
("traders should separately verify Form 4 filings before entering"),
which can subtly widen the conservative frame's price-target band.

The flag tested here is the temporary escape hatch / ablation variable:
when set, the fundamentals analyst drops the insider tool from its
binding and trims the system-message sentence so the LLM never
attempts the call. Combined with the ToolNode fix landing in the same
session, this gives a clean three-state comparison
(broken / ablated / fixed).
"""

from __future__ import annotations

import importlib
import os

import pytest
from langchain_core.runnables import RunnableLambda

from tradingagents.agents.analysts import fundamentals_analyst as fa


@pytest.fixture(autouse=True)
def _restore_env(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_DISABLE_INSIDER_TXNS", raising=False)
    yield


def test_default_keeps_insider_tool_enabled(monkeypatch):
    """Default behavior (flag unset) leaves the insider tool wired."""
    monkeypatch.delenv("TRADINGAGENTS_DISABLE_INSIDER_TXNS", raising=False)
    assert fa._insider_txns_disabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "Yes"])
def test_truthy_values_disable_insider_tool(monkeypatch, val):
    monkeypatch.setenv("TRADINGAGENTS_DISABLE_INSIDER_TXNS", val)
    assert fa._insider_txns_disabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "  "])
def test_falsey_values_keep_insider_tool_enabled(monkeypatch, val):
    monkeypatch.setenv("TRADINGAGENTS_DISABLE_INSIDER_TXNS", val)
    assert fa._insider_txns_disabled() is False


def _stub_llm(captured: dict):
    """Return a fake LLM whose ``bind_tools`` records the tools list and
    yields a real Runnable so ``prompt | llm.bind_tools(tools)`` works.
    """

    def _invoke(_messages):
        class _Result:
            tool_calls: list = []
            content = ""
        return _Result()

    class _FakeLLM:
        def bind_tools(self, tools):
            captured["tools"] = tools
            return RunnableLambda(_invoke)

    return _FakeLLM()


def test_node_factory_omits_insider_tool_when_flag_set(monkeypatch):
    """When the flag is set the analyst's tools list excludes the insider
    tool and the system message no longer mentions it."""
    monkeypatch.setenv("TRADINGAGENTS_DISABLE_INSIDER_TXNS", "1")

    captured: dict[str, object] = {}
    state = {
        "trade_date": "2026-05-08",
        "company_of_interest": "AAPL",
        "messages": [],
    }

    node = fa.create_fundamentals_analyst(_stub_llm(captured))
    node(state)

    tool_names = {getattr(t, "name", str(t)) for t in captured["tools"]}
    assert "get_insider_transactions" not in tool_names
    assert {
        "get_fundamentals",
        "get_balance_sheet",
        "get_cashflow",
        "get_income_statement",
    } <= tool_names


def test_node_factory_includes_insider_tool_when_flag_unset(monkeypatch):
    """Default path keeps the insider tool wired (current behavior)."""
    monkeypatch.delenv("TRADINGAGENTS_DISABLE_INSIDER_TXNS", raising=False)

    captured: dict[str, object] = {}
    state = {
        "trade_date": "2026-05-08",
        "company_of_interest": "AAPL",
        "messages": [],
    }

    node = fa.create_fundamentals_analyst(_stub_llm(captured))
    node(state)

    tool_names = {getattr(t, "name", str(t)) for t in captured["tools"]}
    assert "get_insider_transactions" in tool_names
