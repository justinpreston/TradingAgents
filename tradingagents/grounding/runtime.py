"""Runtime grounding assertions.

After a graph run completes, we inspect the message history and analyst
reports to detect cases where a *tool-bound* analyst (market / news /
fundamentals) produced a report without ever calling a tool. That's
the runtime fingerprint of a fabricated analyst output — the LLM
walked past the grounding tools and emitted prose from memory.

The check is **warning-only** by design:

* It never crashes a live run. Real-money pipelines must not abort on
  a meta-check failure — the downstream grounding_audit.py scoring
  catches the same content fingerprints with a richer signal.
* It logs to stderr (via ``logging``) so the warning shows up in the
  matrix-cell logs and in any aggregated run report.

The signal it emits is the strongest possible proof that an analyst
report is ungrounded: **zero AIMessage tool_calls in the entire run**
for an analyst that has tools bound.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


# (analyst report key in final state, expected to call tools, label)
# Sentiment analyst is deliberately omitted — it pre-fetches data into
# the prompt rather than binding tools (see grounding/__init__.py).
_TOOL_BOUND_ANALYSTS: Tuple[Tuple[str, str], ...] = (
    ("market_report", "market_analyst"),
    ("news_report", "news_analyst"),
    ("fundamentals_report", "fundamentals_analyst"),
)


def _count_tool_calls_in_messages(messages: List[Any]) -> int:
    """Return the total number of tool calls across all AIMessages.

    LangChain's AIMessage exposes ``tool_calls`` as a list of dicts.
    Older shapes may use ``additional_kwargs['tool_calls']``. We
    accept both.
    """
    count = 0
    for msg in messages:
        # tool_calls attribute (LangChain ≥ 0.1)
        tcs = getattr(msg, "tool_calls", None)
        if isinstance(tcs, list):
            count += len(tcs)
            continue
        # additional_kwargs.tool_calls (legacy)
        ak = getattr(msg, "additional_kwargs", None)
        if isinstance(ak, dict):
            legacy = ak.get("tool_calls")
            if isinstance(legacy, list):
                count += len(legacy)
    return count


def assert_analyst_grounding(final_state: Dict[str, Any], ticker: str = "?") -> List[str]:
    """Inspect ``final_state`` and warn for any tool-bound analyst that
    produced a non-empty report without making any tool calls.

    Returns the list of warning strings emitted (also logged to stderr
    via ``logger.warning``). Empty list = all clear.
    """
    warnings: List[str] = []
    messages = final_state.get("messages") or []
    total_tool_calls = _count_tool_calls_in_messages(messages)

    for report_key, label in _TOOL_BOUND_ANALYSTS:
        report = final_state.get(report_key)
        if not report or not isinstance(report, str):
            # No report produced — separate failure mode, not this one.
            continue
        if report.strip() == "":
            continue
        # If the FULL run made zero tool calls AND this analyst produced
        # a non-trivial report, something is wrong. We can't perfectly
        # attribute a tool call to a specific analyst from the merged
        # message stream, but if total == 0 then by definition no
        # analyst called a tool, which means this report is ungrounded.
        if total_tool_calls == 0 and len(report) > 200:
            msg = (
                f"GROUNDING WARNING [{ticker}]: {label} produced a "
                f"{len(report)}-char report but the run made ZERO tool calls. "
                "Report is likely ungrounded (LLM walked past its tools)."
            )
            warnings.append(msg)
            logger.warning(msg)

    return warnings
