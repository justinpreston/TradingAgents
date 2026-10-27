"""LangChain callback handler that drives the progress dashboard.

Wraps a :class:`tradingagents.ui.ProgressListener` and forwards two kinds
of events:

* ``on_tool_start``  → ``listener.on_tool_call(...)``
* ``on_llm_end``     → ``listener.on_llm_tokens(...)``

Token usage is normalized across the three message shapes we see in this
codebase:

1. ``AIMessage.usage_metadata`` (preferred — populated by
   ``langchain-anthropic`` and modern ``langchain-openai``).
2. ``message.response_metadata["token_usage"]`` (older OpenAI shape).
3. ``LLMResult.llm_output["token_usage"]`` (some providers / models).

The handler also keeps a small local stats counter so multi-ticker runs
can show running totals without the dashboard re-summing on every paint.

Thread-safety: a single ``threading.Lock`` guards all mutable state. We
expect callbacks to fire from the main thread in practice, but
LangChain's batch APIs and some agent frameworks invoke them from worker
threads, so the lock is cheap insurance.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Mapping
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult

from .progress_listener import NoOpListener, ProgressListener


def _extract_token_usage(response: LLMResult) -> dict[str, Any] | None:
    """Return ``{"input_tokens": int, "output_tokens": int, "model": str|None}``
    or ``None`` if the response carries no usable token metadata.
    """

    # 1) AIMessage.usage_metadata — the modern, normalized shape.
    try:
        generation = response.generations[0][0]
    except (IndexError, TypeError):
        generation = None

    model: str | None = None
    if generation is not None:
        message = getattr(generation, "message", None)
        if isinstance(message, AIMessage):
            usage = getattr(message, "usage_metadata", None)
            if usage:
                model = (
                    message.response_metadata.get("model_name")
                    or message.response_metadata.get("model")
                    if isinstance(message.response_metadata, Mapping)
                    else None
                )
                return {
                    "input_tokens": int(usage.get("input_tokens", 0) or 0),
                    "output_tokens": int(usage.get("output_tokens", 0) or 0),
                    "model": model,
                }

            # 2) older OpenAI: message.response_metadata.token_usage
            rmeta = getattr(message, "response_metadata", None)
            if isinstance(rmeta, Mapping):
                token_usage = rmeta.get("token_usage") or {}
                if token_usage:
                    return {
                        "input_tokens": int(
                            token_usage.get("prompt_tokens")
                            or token_usage.get("input_tokens")
                            or 0
                        ),
                        "output_tokens": int(
                            token_usage.get("completion_tokens")
                            or token_usage.get("output_tokens")
                            or 0
                        ),
                        "model": rmeta.get("model_name") or rmeta.get("model"),
                    }

    # 3) LLMResult.llm_output.token_usage — last-resort.
    llm_output = response.llm_output or {}
    if isinstance(llm_output, Mapping):
        token_usage = llm_output.get("token_usage") or {}
        if token_usage:
            return {
                "input_tokens": int(
                    token_usage.get("prompt_tokens")
                    or token_usage.get("input_tokens")
                    or 0
                ),
                "output_tokens": int(
                    token_usage.get("completion_tokens")
                    or token_usage.get("output_tokens")
                    or 0
                ),
                "model": llm_output.get("model_name") or llm_output.get("model"),
            }

    return None


class TokenToolCallbackHandler(BaseCallbackHandler):
    """Forward LLM and tool callbacks to a :class:`ProgressListener`.

    The listener is assumed to track per-ticker state. The handler holds a
    mutable ``current_ticker`` slot that the run loop must update before
    each ticker; the trading runner does this in
    :func:`TradingAgentsGraph._run_graph`.
    """

    def __init__(self, listener: ProgressListener | None = None) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._listener: ProgressListener = listener or NoOpListener()
        self.current_ticker: str | None = None

        # local stats — useful for tests / ad-hoc inspection
        self.llm_calls: int = 0
        self.tool_calls: int = 0
        self.tokens_in: int = 0
        self.tokens_out: int = 0

    # ---- API ---------------------------------------------------------

    def set_ticker(self, ticker: str | None) -> None:
        """Set the ticker that subsequent callbacks should be attributed to."""
        with self._lock:
            self.current_ticker = ticker

    def get_stats(self) -> dict[str, int]:
        """Return cumulative stats since handler construction."""
        with self._lock:
            return {
                "llm_calls": self.llm_calls,
                "tool_calls": self.tool_calls,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
            }

    # ---- LangChain callbacks ----------------------------------------

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        **kwargs: Any,
    ) -> None:
        with self._lock:
            self.llm_calls += 1

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        **kwargs: Any,
    ) -> None:
        with self._lock:
            self.llm_calls += 1

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        usage = _extract_token_usage(response)
        if usage is None:
            return
        with self._lock:
            self.tokens_in += usage["input_tokens"]
            self.tokens_out += usage["output_tokens"]
            ticker = self.current_ticker
        # Never let dashboard rendering break the run.
        with contextlib.suppress(Exception):
            self._listener.on_llm_tokens(
                ticker or "",
                usage["input_tokens"],
                usage["output_tokens"],
                usage["model"],
            )

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        tool_name = ""
        if isinstance(serialized, Mapping):
            tool_name = serialized.get("name") or ""
        if not tool_name:
            tool_name = kwargs.get("name") or ""
        with self._lock:
            self.tool_calls += 1
            ticker = self.current_ticker
        with contextlib.suppress(Exception):
            self._listener.on_tool_call(ticker or "", tool_name)
