"""Progress listener Protocol and a no-op default.

The graph fires a small set of well-defined events that a UI (or any other
sink — JSON logger, websocket bridge, etc.) can subscribe to. Keeping this
surface small lets us swap the dashboard implementation without touching
the graph.

Events
------

``on_run_start(ticker, trade_date)``
    Called once per ticker, before the graph starts streaming.

``on_node_complete(ticker, node_name, delta)``
    Called when a single graph node finishes. ``node_name`` is the raw name
    registered with the workflow (e.g. ``"Market Analyst"``,
    ``"tools_market"``, ``"Msg Clear Market"``). ``delta`` is the partial
    state update emitted by langgraph's ``stream_mode="updates"``.

``on_run_complete(ticker, signal, elapsed_s, final_state)``
    Called when a ticker run finishes successfully.

``on_run_error(ticker, exc)``
    Called when a ticker run raises.

``on_tool_call(ticker, tool_name)``
    Called by :class:`TokenToolCallbackHandler` once per tool invocation.

``on_llm_tokens(ticker, in_tokens, out_tokens, model)``
    Called by :class:`TokenToolCallbackHandler` once per LLM completion.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol, runtime_checkable


@runtime_checkable
class ProgressListener(Protocol):
    """Sink for run events."""

    def on_run_start(self, ticker: str, trade_date: str) -> None: ...

    def on_node_complete(
        self,
        ticker: str,
        node_name: str,
        delta: Mapping[str, Any],
    ) -> None: ...

    def on_run_complete(
        self,
        ticker: str,
        signal: str,
        elapsed_s: float,
        final_state: Mapping[str, Any],
    ) -> None: ...

    def on_run_error(self, ticker: str, exc: BaseException) -> None: ...

    def on_tool_call(self, ticker: str, tool_name: str) -> None: ...

    def on_llm_tokens(
        self,
        ticker: str,
        in_tokens: int,
        out_tokens: int,
        model: Optional[str],
    ) -> None: ...


class NoOpListener:
    """Default listener that swallows every event.

    Use this when no UI is desired so call-sites don't need ``None`` checks.
    """

    def on_run_start(self, ticker: str, trade_date: str) -> None:  # noqa: D401
        return None

    def on_node_complete(
        self,
        ticker: str,
        node_name: str,
        delta: Mapping[str, Any],
    ) -> None:
        return None

    def on_run_complete(
        self,
        ticker: str,
        signal: str,
        elapsed_s: float,
        final_state: Mapping[str, Any],
    ) -> None:
        return None

    def on_run_error(self, ticker: str, exc: BaseException) -> None:
        return None

    def on_tool_call(self, ticker: str, tool_name: str) -> None:
        return None

    def on_llm_tokens(
        self,
        ticker: str,
        in_tokens: int,
        out_tokens: int,
        model: Optional[str],
    ) -> None:
        return None
