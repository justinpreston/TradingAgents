"""Structured tool-error marker for the grounded pipeline.

Background
----------
Dataflow tools historically returned a free-text string like
``"Error fetching news for NVDA: HTTPError 429"`` when the vendor call
failed. The LLM agent that consumed those tool outputs had nothing
forcing it to surface the failure — under prompt pressure to "write a
comprehensive report" it could silently confabulate content from its
training-data prior instead.

This module introduces a single sentinel that downstream nodes can
detect reliably and surface as an explicit Data Gap rather than letting
the LLM paper over it. Both new emissions and legacy ``Error fetching``
phrasing are recognized so we can migrate dataflows incrementally
without losing detection coverage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List

# Canonical machine-readable marker. Picked to be obvious in logs, easy
# to grep, and unlikely to collide with legitimate report content.
TOOL_ERROR_OPEN = "[[TOOL_ERROR:"
TOOL_ERROR_CLOSE = "]]"

_MARKER_RE = re.compile(
    r"\[\[TOOL_ERROR:\s*(?P<body>[^\]]+?)\s*\]\]",
    re.DOTALL,
)

_LEGACY_PATTERNS = (
    re.compile(r"^Error fetching [^:]+: .+$", re.MULTILINE),
    re.compile(r"^Error retrieving [^:]+: .+$", re.MULTILINE),
    re.compile(r"^Error: .+$", re.MULTILINE),
)


@dataclass(frozen=True)
class ToolError:
    """One detected tool-failure occurrence inside a string."""

    body: str
    source: str  # "marker" | "legacy"

    def __str__(self) -> str:  # noqa: D401 - dataclass display
        return self.body


def format_tool_error(tool_name: str, target: str | None, exc: BaseException | str) -> str:
    """Return the canonical sentinel string for a tool failure.

    ``target`` may be a ticker symbol, a date range string, or any
    identifying context for the failed call. ``exc`` is stringified so
    callers can pass either an exception object or a pre-built message.
    """
    target_part = f" for {target}" if target else ""
    body = f"{tool_name}{target_part}: {exc}"
    # Replace any newlines or stray brackets in the message that would
    # break the closing marker detection. Compact but readable.
    body = body.replace("\n", " ").replace("]]", "]")
    return f"{TOOL_ERROR_OPEN} {body} {TOOL_ERROR_CLOSE}"


def extract_tool_errors(text: str | None) -> List[ToolError]:
    """Find every tool-error occurrence in ``text``.

    Detects both the new ``[[TOOL_ERROR: ...]]`` marker and the legacy
    ``Error fetching`` / ``Error retrieving`` phrasing used by older
    dataflow sites.
    """
    if not text:
        return []
    errors: List[ToolError] = []
    seen: set[str] = set()

    for match in _MARKER_RE.finditer(text):
        body = match.group("body").strip()
        if body not in seen:
            errors.append(ToolError(body=body, source="marker"))
            seen.add(body)

    for pattern in _LEGACY_PATTERNS:
        for match in pattern.finditer(text):
            body = match.group(0).strip()
            if body not in seen:
                errors.append(ToolError(body=body, source="legacy"))
                seen.add(body)

    return errors


def has_tool_errors(text: str | None) -> bool:
    """Cheap predicate — useful in tight loops or assertions."""
    if not text:
        return False
    if TOOL_ERROR_OPEN in text:
        return True
    for pattern in _LEGACY_PATTERNS:
        if pattern.search(text):
            return True
    return False


def build_data_gaps_section(errors: Iterable[ToolError]) -> str:
    """Format a Markdown ``## Data Gaps`` block to prepend to a report.

    Returns the empty string when there are no errors so callers can
    use it unconditionally.
    """
    items = list(errors)
    if not items:
        return ""
    lines = [
        "## ⚠️ Data Gaps",
        "",
        "_The following tool calls failed during this analysis. The report below should be read as partial — any specific numerical claim that depends on these data sources is unsupported._",
        "",
    ]
    for err in items:
        lines.append(f"- {err.body}")
    lines.append("")
    return "\n".join(lines)
