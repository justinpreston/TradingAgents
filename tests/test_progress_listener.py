"""Tests for the live progress UX module (``tradingagents.ui``)."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from tradingagents.ui import (
    NoOpListener,
    ProgressListener,
    RunDashboard,
    TokenToolCallbackHandler,
)
from tradingagents.ui.callbacks import _extract_token_usage
from tradingagents.ui.node_metadata import (
    NODE_DISPLAY_ORDER,
    PHASE_FOR_NODE,
    is_hidden_node,
    is_visible_node,
    phase_label,
)


# ---------------------------------------------------------------------------
# Recording listener used to assert event ordering in graph-level tests.
# ---------------------------------------------------------------------------


class RecordingListener:
    """In-memory ``ProgressListener`` that captures every event."""

    def __init__(self) -> None:
        self.events: List[Tuple[str, tuple, dict]] = []

    def on_run_start(self, ticker: str, trade_date: str) -> None:
        self.events.append(("on_run_start", (ticker, trade_date), {}))

    def on_node_complete(
        self, ticker: str, node_name: str, delta: Mapping[str, Any]
    ) -> None:
        self.events.append(("on_node_complete", (ticker, node_name), dict(delta)))

    def on_run_complete(
        self,
        ticker: str,
        signal: str,
        elapsed_s: float,
        final_state: Mapping[str, Any],
    ) -> None:
        self.events.append(
            ("on_run_complete", (ticker, signal), {"elapsed": elapsed_s})
        )

    def on_run_error(self, ticker: str, exc: BaseException) -> None:
        self.events.append(("on_run_error", (ticker, type(exc).__name__), {}))

    def on_tool_call(self, ticker: str, tool_name: str) -> None:
        self.events.append(("on_tool_call", (ticker, tool_name), {}))

    def on_llm_tokens(
        self, ticker: str, in_tokens: int, out_tokens: int, model: Optional[str]
    ) -> None:
        self.events.append(
            ("on_llm_tokens", (ticker, in_tokens, out_tokens, model), {})
        )

    def names(self) -> List[str]:
        return [name for (name, _, _) in self.events]


# ---------------------------------------------------------------------------
# node_metadata
# ---------------------------------------------------------------------------


class TestNodeMetadata:
    def test_display_order_matches_phase_map(self) -> None:
        assert set(NODE_DISPLAY_ORDER) == set(PHASE_FOR_NODE)
        # 12 visible nodes (4 analysts + 2 debate + 1 RM + 1 trader + 3 risk + 1 PM)
        assert len(NODE_DISPLAY_ORDER) == 12

    @pytest.mark.parametrize(
        "node",
        ["Market Analyst", "Bear Researcher", "Portfolio Manager"],
    )
    def test_visible_nodes(self, node: str) -> None:
        assert is_visible_node(node) is True
        assert is_hidden_node(node) is False

    @pytest.mark.parametrize(
        "node",
        ["tools_market", "tools_news", "Msg Clear Fundamentals"],
    )
    def test_hidden_nodes(self, node: str) -> None:
        assert is_hidden_node(node) is True
        assert is_visible_node(node) is False

    def test_phase_labels(self) -> None:
        assert phase_label("ANALYSTS") == "Analysts"
        assert phase_label("DECISION") == "Decision"
        # unknown phase tokens echo through unchanged
        assert phase_label("OTHER") == "OTHER"


# ---------------------------------------------------------------------------
# Token extraction shim
# ---------------------------------------------------------------------------


def _make_llm_result_with_usage_metadata(
    in_tokens: int = 100, out_tokens: int = 50, model: str = "gpt-5.5"
) -> LLMResult:
    msg = AIMessage(
        content="hi",
        usage_metadata={
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
            "total_tokens": in_tokens + out_tokens,
        },
        response_metadata={"model_name": model},
    )
    return LLMResult(generations=[[ChatGeneration(message=msg)]])


def _make_llm_result_with_response_metadata(
    in_tokens: int = 200, out_tokens: int = 75, model: str = "gpt-4o"
) -> LLMResult:
    msg = AIMessage(
        content="hi",
        response_metadata={
            "token_usage": {
                "prompt_tokens": in_tokens,
                "completion_tokens": out_tokens,
            },
            "model_name": model,
        },
    )
    return LLMResult(generations=[[ChatGeneration(message=msg)]])


def _make_llm_result_with_llm_output(
    in_tokens: int = 300, out_tokens: int = 90, model: str = "claude-opus-4.7"
) -> LLMResult:
    msg = AIMessage(content="hi")
    return LLMResult(
        generations=[[ChatGeneration(message=msg)]],
        llm_output={
            "token_usage": {
                "prompt_tokens": in_tokens,
                "completion_tokens": out_tokens,
            },
            "model_name": model,
        },
    )


class TestTokenExtraction:
    def test_usage_metadata_path(self) -> None:
        result = _make_llm_result_with_usage_metadata(100, 50, "gpt-5.5")
        usage = _extract_token_usage(result)
        assert usage == {
            "input_tokens": 100,
            "output_tokens": 50,
            "model": "gpt-5.5",
        }

    def test_response_metadata_path(self) -> None:
        result = _make_llm_result_with_response_metadata(200, 75, "gpt-4o")
        usage = _extract_token_usage(result)
        assert usage == {
            "input_tokens": 200,
            "output_tokens": 75,
            "model": "gpt-4o",
        }

    def test_llm_output_path(self) -> None:
        result = _make_llm_result_with_llm_output(300, 90, "claude-opus-4.7")
        usage = _extract_token_usage(result)
        assert usage == {
            "input_tokens": 300,
            "output_tokens": 90,
            "model": "claude-opus-4.7",
        }

    def test_no_usage_returns_none(self) -> None:
        msg = AIMessage(content="no metadata here")
        result = LLMResult(generations=[[ChatGeneration(message=msg)]])
        assert _extract_token_usage(result) is None

    def test_empty_generations_returns_none(self) -> None:
        result = LLMResult(generations=[])
        assert _extract_token_usage(result) is None


# ---------------------------------------------------------------------------
# TokenToolCallbackHandler
# ---------------------------------------------------------------------------


class TestTokenToolCallbackHandler:
    def test_llm_end_forwards_to_listener(self) -> None:
        listener = RecordingListener()
        handler = TokenToolCallbackHandler(listener=listener)
        handler.set_ticker("NVDA")

        result = _make_llm_result_with_usage_metadata(100, 50, "gpt-5.5")
        handler.on_llm_end(result)

        assert listener.events == [
            ("on_llm_tokens", ("NVDA", 100, 50, "gpt-5.5"), {}),
        ]
        assert handler.get_stats() == {
            "llm_calls": 0,  # on_llm_start was not called
            "tool_calls": 0,
            "tokens_in": 100,
            "tokens_out": 50,
        }

    def test_llm_start_increments_counter(self) -> None:
        handler = TokenToolCallbackHandler()
        handler.on_llm_start({"name": "x"}, ["prompt"])
        handler.on_chat_model_start({"name": "x"}, [[]])
        assert handler.get_stats()["llm_calls"] == 2

    def test_tool_start_forwards_and_increments(self) -> None:
        listener = RecordingListener()
        handler = TokenToolCallbackHandler(listener=listener)
        handler.set_ticker("AAPL")
        handler.on_tool_start({"name": "get_news"}, "{}")

        assert listener.events == [
            ("on_tool_call", ("AAPL", "get_news"), {}),
        ]
        assert handler.get_stats()["tool_calls"] == 1

    def test_tool_start_handles_missing_name(self) -> None:
        listener = RecordingListener()
        handler = TokenToolCallbackHandler(listener=listener)
        handler.set_ticker("MSFT")
        handler.on_tool_start({}, "{}")
        assert listener.events == [("on_tool_call", ("MSFT", ""), {})]

    def test_no_listener_uses_noop(self) -> None:
        handler = TokenToolCallbackHandler()
        handler.set_ticker("GOOGL")
        # Should not raise even though there's no real sink.
        result = _make_llm_result_with_usage_metadata(10, 5)
        handler.on_llm_end(result)
        handler.on_tool_start({"name": "any"}, "{}")
        stats = handler.get_stats()
        assert stats["tokens_in"] == 10
        assert stats["tokens_out"] == 5
        assert stats["tool_calls"] == 1

    def test_listener_exception_does_not_propagate(self) -> None:
        broken = MagicMock(spec=ProgressListener)
        broken.on_llm_tokens.side_effect = RuntimeError("boom")
        broken.on_tool_call.side_effect = RuntimeError("boom")

        handler = TokenToolCallbackHandler(listener=broken)
        handler.set_ticker("X")
        # Neither should raise.
        handler.on_llm_end(_make_llm_result_with_usage_metadata(1, 1))
        handler.on_tool_start({"name": "t"}, "{}")
        # Stats still updated.
        assert handler.get_stats()["tokens_in"] == 1
        assert handler.get_stats()["tool_calls"] == 1


# ---------------------------------------------------------------------------
# RunDashboard — state-only tests (no Live rendering)
# ---------------------------------------------------------------------------


class _SilentDashboard(RunDashboard):
    """Dashboard subclass that suppresses Live rendering for tests.

    We still want to exercise the listener bookkeeping (state map, event tail,
    counters) but don't want to actually paint to a TTY.
    """

    def _refresh(self) -> None:  # type: ignore[override]
        return None


def _make_dashboard(tickers: List[str]) -> _SilentDashboard:
    from rich.console import Console

    return _SilentDashboard(
        tickers=tickers,
        trade_date="2024-05-10",
        run_id="test_run",
        console=Console(file=open("/dev/null", "w"), force_terminal=False),
    )


class TestRunDashboardStateTransitions:
    def test_initial_state_all_pending(self) -> None:
        dash = _make_dashboard(["NVDA", "AAPL"])
        states = dash._states  # type: ignore[attr-defined]
        assert states["NVDA"].status == "pending"
        assert states["AAPL"].status == "pending"
        assert dash._active is None  # type: ignore[attr-defined]

    def test_run_start_marks_running_and_sets_active(self) -> None:
        dash = _make_dashboard(["NVDA"])
        dash.on_run_start("NVDA", "2024-05-10")
        state = dash._states["NVDA"]  # type: ignore[attr-defined]
        assert state.status == "running"
        assert state.started_at is not None
        assert dash._active == "NVDA"  # type: ignore[attr-defined]

    def test_node_complete_advances_progress(self) -> None:
        dash = _make_dashboard(["NVDA"])
        dash.on_run_start("NVDA", "2024-05-10")
        dash.on_node_complete("NVDA", "Market Analyst", {})
        state = dash._states["NVDA"]  # type: ignore[attr-defined]
        assert state.nodes_completed == 1
        assert state.current_node == "Market Analyst"
        assert state.phase == "ANALYSTS"

    def test_hidden_nodes_do_not_advance(self) -> None:
        dash = _make_dashboard(["NVDA"])
        dash.on_run_start("NVDA", "2024-05-10")
        dash.on_node_complete("NVDA", "tools_market", {})
        dash.on_node_complete("NVDA", "Msg Clear Market", {})
        state = dash._states["NVDA"]  # type: ignore[attr-defined]
        assert state.nodes_completed == 0

    def test_idempotent_node_replay(self) -> None:
        # Checkpoint resume can replay completed nodes — must not double-count.
        dash = _make_dashboard(["NVDA"])
        dash.on_run_start("NVDA", "2024-05-10")
        dash.on_node_complete("NVDA", "Market Analyst", {})
        dash.on_node_complete("NVDA", "Market Analyst", {})
        state = dash._states["NVDA"]  # type: ignore[attr-defined]
        assert state.nodes_completed == 1

    def test_run_complete_records_signal(self) -> None:
        dash = _make_dashboard(["NVDA"])
        dash.on_run_start("NVDA", "2024-05-10")
        dash.on_run_complete("NVDA", "BUY", 12.5, {"final_trade_decision": "BUY"})
        state = dash._states["NVDA"]  # type: ignore[attr-defined]
        assert state.status == "done"
        assert state.signal == "BUY"
        assert state.finished_at is not None

    def test_run_error_records_error(self) -> None:
        dash = _make_dashboard(["NVDA"])
        dash.on_run_start("NVDA", "2024-05-10")
        dash.on_run_error("NVDA", ValueError("nope"))
        state = dash._states["NVDA"]  # type: ignore[attr-defined]
        assert state.status == "error"
        assert state.error is not None
        assert "ValueError" in state.error

    def test_token_accumulation(self) -> None:
        dash = _make_dashboard(["NVDA"])
        dash.on_run_start("NVDA", "2024-05-10")
        dash.on_llm_tokens("NVDA", 100, 50, "gpt-5.5")
        dash.on_llm_tokens("NVDA", 200, 80, "gpt-5.5")
        state = dash._states["NVDA"]  # type: ignore[attr-defined]
        assert state.tokens_in == 300
        assert state.tokens_out == 130
        assert state.llm_calls == 2

    def test_tool_calls_counted(self) -> None:
        dash = _make_dashboard(["NVDA"])
        dash.on_run_start("NVDA", "2024-05-10")
        dash.on_tool_call("NVDA", "get_news")
        dash.on_tool_call("NVDA", "get_stock_data")
        state = dash._states["NVDA"]  # type: ignore[attr-defined]
        assert state.tool_calls == 2

    def test_multi_ticker_isolation(self) -> None:
        dash = _make_dashboard(["NVDA", "AAPL"])
        dash.on_run_start("NVDA", "2024-05-10")
        dash.on_node_complete("NVDA", "Market Analyst", {})
        dash.on_run_complete("NVDA", "BUY", 5.0, {})

        dash.on_run_start("AAPL", "2024-05-10")
        dash.on_node_complete("AAPL", "Market Analyst", {})
        dash.on_node_complete("AAPL", "Social Analyst", {})

        nvda = dash._states["NVDA"]  # type: ignore[attr-defined]
        aapl = dash._states["AAPL"]  # type: ignore[attr-defined]
        assert nvda.status == "done"
        assert nvda.nodes_completed == 1
        assert aapl.status == "running"
        assert aapl.nodes_completed == 2

    def test_event_tail_capped(self) -> None:
        dash = _make_dashboard(["NVDA"])
        dash.on_run_start("NVDA", "2024-05-10")
        for _ in range(20):
            dash.on_node_complete("NVDA", "Market Analyst", {})  # idempotent
            dash.on_tool_call("NVDA", "get_news")
        # tail should be capped
        assert len(dash._events) == dash.EVENT_TAIL_LEN  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# NoOpListener
# ---------------------------------------------------------------------------


class TestNoOpListener:
    def test_satisfies_protocol(self) -> None:
        assert isinstance(NoOpListener(), ProgressListener)

    def test_methods_return_none(self) -> None:
        listener = NoOpListener()
        assert listener.on_run_start("NVDA", "2024-05-10") is None
        assert listener.on_node_complete("NVDA", "Market Analyst", {}) is None
        assert listener.on_run_complete("NVDA", "BUY", 1.0, {}) is None
        assert listener.on_run_error("NVDA", RuntimeError("x")) is None
        assert listener.on_tool_call("NVDA", "get_news") is None
        assert listener.on_llm_tokens("NVDA", 1, 1, "x") is None


# ---------------------------------------------------------------------------
# Integration: callback handler -> dashboard
# ---------------------------------------------------------------------------


class TestCallbackToDashboardWiring:
    def test_handler_drives_dashboard_state(self) -> None:
        dash = _make_dashboard(["NVDA"])
        dash.on_run_start("NVDA", "2024-05-10")

        handler = TokenToolCallbackHandler(listener=dash)
        handler.set_ticker("NVDA")

        # Simulate a real LLM completion event.
        result = _make_llm_result_with_usage_metadata(150, 60, "gpt-5.5")
        handler.on_llm_end(result)
        # Simulate a tool invocation.
        handler.on_tool_start({"name": "get_indicators"}, "{}")

        state = dash._states["NVDA"]  # type: ignore[attr-defined]
        assert state.tokens_in == 150
        assert state.tokens_out == 60
        assert state.tool_calls == 1
        assert state.llm_calls == 1
