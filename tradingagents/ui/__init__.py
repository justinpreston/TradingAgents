"""Live progress UX for TradingAgents runs.

Public surface:

* :class:`ProgressListener` — Protocol any dashboard / sink can implement.
* :class:`NoOpListener`     — default no-op that the graph can use when no
  listener is supplied (so call-sites don't have to ``None``-check).
* :class:`TokenToolCallbackHandler` — LangChain ``BaseCallbackHandler`` that
  forwards LLM token usage and tool calls to a :class:`ProgressListener`.
* :class:`RunDashboard` — multi-ticker terminal UI driven by
  :class:`rich.live.Live`. Implements :class:`ProgressListener`.
"""

from .progress_listener import NoOpListener, ProgressListener
from .callbacks import TokenToolCallbackHandler
from .dashboard import RunDashboard
from .node_metadata import (
    DECISION_NODES,
    NODE_DISPLAY_ORDER,
    PHASE_FOR_NODE,
    is_visible_node,
    phase_label,
)

__all__ = [
    "DECISION_NODES",
    "NODE_DISPLAY_ORDER",
    "NoOpListener",
    "PHASE_FOR_NODE",
    "ProgressListener",
    "RunDashboard",
    "TokenToolCallbackHandler",
    "is_visible_node",
    "phase_label",
]
