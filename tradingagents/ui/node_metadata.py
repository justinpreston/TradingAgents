"""Node-name → phase / display-label mapping for the progress dashboard.

The graph (see ``tradingagents/graph/setup.py``) registers a number of
nodes that aren't useful to surface in a user-facing progress UI:

* ``tools_market`` / ``tools_news`` / etc. — these are ``ToolNode``
  invocations triggered by the conditional edge from each analyst. They
  are surfaced separately via the LangChain tool callback (``on_tool_call``)
  rather than as discrete progress steps.
* ``Msg Clear *`` — pure plumbing nodes that wipe message history before
  the next phase. These are hidden entirely.

What we DO show as progress steps is the human-readable list below.
:data:`NODE_DISPLAY_ORDER` is also used as the denominator for the
"X / N" progress bar.
"""

from __future__ import annotations

from typing import Dict, List, Set


# ---- Phase definitions ------------------------------------------------------

# Phase keys are short ALL CAPS tokens that fit nicely in a status column.
ANALYSTS = "ANALYSTS"
DEBATE = "DEBATE"
SYNTHESIS = "SYNTHESIS"
RISK = "RISK"
DECISION = "DECISION"

PHASE_LABELS: Dict[str, str] = {
    ANALYSTS: "Analysts",
    DEBATE: "Bull/Bear",
    SYNTHESIS: "Synthesis",
    RISK: "Risk",
    DECISION: "Decision",
}


# ---- Visible node ordering --------------------------------------------------

# This is the canonical "user-facing" order. The dashboard advances the
# progress bar each time one of these nodes emits an update event. Hidden
# nodes (tools_*, Msg Clear *) do not advance the bar.
NODE_DISPLAY_ORDER: List[str] = [
    "Market Analyst",
    "Social Analyst",
    "News Analyst",
    "Fundamentals Analyst",
    "Bull Researcher",
    "Bear Researcher",
    "Research Manager",
    "Trader",
    "Aggressive Analyst",
    "Neutral Analyst",
    "Conservative Analyst",
    "Portfolio Manager",
]

PHASE_FOR_NODE: Dict[str, str] = {
    "Market Analyst": ANALYSTS,
    "Social Analyst": ANALYSTS,
    "News Analyst": ANALYSTS,
    "Fundamentals Analyst": ANALYSTS,
    "Bull Researcher": DEBATE,
    "Bear Researcher": DEBATE,
    "Research Manager": SYNTHESIS,
    "Trader": SYNTHESIS,
    "Aggressive Analyst": RISK,
    "Neutral Analyst": RISK,
    "Conservative Analyst": RISK,
    "Portfolio Manager": DECISION,
}


# ---- Hidden / informational nodes ------------------------------------------

# Nodes whose updates we silently consume (don't advance the bar, don't log
# to the event tail). Membership tested via prefix on the second set.
_HIDDEN_PREFIXES: Set[str] = {
    "Msg Clear ",
    "tools_",
}

# Tool nodes — surfaced as sub-events ("uses tools: …") rather than steps.
TOOL_NODE_PREFIX = "tools_"


# ---- Final decision nodes --------------------------------------------------

# When one of these fires, the dashboard treats it as the "decision was
# made" event for the ticker — not strictly necessary because we also use
# the run-complete event, but useful for highlighting the moment in the
# event tail.
DECISION_NODES: Set[str] = {"Portfolio Manager"}


def is_visible_node(node_name: str) -> bool:
    """Return ``True`` if ``node_name`` should advance the progress bar."""
    return node_name in PHASE_FOR_NODE


def is_hidden_node(node_name: str) -> bool:
    """Return ``True`` for plumbing nodes (Msg Clear *, tools_*)."""
    for prefix in _HIDDEN_PREFIXES:
        if node_name.startswith(prefix):
            return True
    return False


def phase_label(phase: str) -> str:
    """Return the short user-facing label for a phase token."""
    return PHASE_LABELS.get(phase, phase)


def phase_for_node(node_name: str) -> str:
    """Best-effort phase lookup; returns empty string for hidden/unknown."""
    return PHASE_FOR_NODE.get(node_name, "")
