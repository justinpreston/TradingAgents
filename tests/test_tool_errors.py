"""Tests for the TOOL_ERROR sentinel module."""

from __future__ import annotations

from tradingagents.dataflows.tool_errors import (
    build_data_gaps_section,
    extract_tool_errors,
    format_tool_error,
    has_tool_errors,
)


def test_format_tool_error_basic():
    msg = format_tool_error("get_news", "AAPL", "rate limited")
    assert msg.startswith("[[TOOL_ERROR:")
    assert msg.endswith("]]")
    assert "get_news" in msg
    assert "AAPL" in msg
    assert "rate limited" in msg


def test_format_tool_error_strips_close_marker():
    """Embedded ']]' in the message must not break the closing marker."""
    msg = format_tool_error("get_news", "AAPL", "got nested ]] bracket]] in msg")
    # Should still close cleanly
    assert msg.count("]]") == 1
    assert msg.endswith("]]")


def test_format_tool_error_strips_newlines():
    msg = format_tool_error("get_news", "AAPL", "first line\nsecond line\nthird")
    assert "\n" not in msg
    assert "first line" in msg
    assert "second line" in msg


def test_extract_tool_errors_marker_path():
    text = "Some preamble.\n" + format_tool_error("get_news", "AAPL", "rate limited") + "\nMore prose."
    errors = extract_tool_errors(text)
    assert len(errors) == 1
    assert "get_news" in errors[0].body
    assert "AAPL" in errors[0].body
    assert "rate limited" in errors[0].body
    assert errors[0].source == "marker"


def test_extract_tool_errors_legacy_pattern():
    text = "Some preamble.\nError fetching balance sheet: HTTPError 429\nMore prose."
    errors = extract_tool_errors(text)
    assert len(errors) >= 1
    assert any(e.source == "legacy" for e in errors)


def test_extract_tool_errors_dedups():
    a = format_tool_error("get_news", "AAPL", "rate limited")
    text = "\n".join([a, a, "filler", a])
    errors = extract_tool_errors(text)
    # Same tool+target+message → dedup
    assert len(errors) == 1


def test_has_tool_errors():
    clean = "The market opened higher today on positive earnings."
    dirty = "Stock data unavailable. " + format_tool_error("get_stock_data", "ABC", "404")
    assert not has_tool_errors(clean)
    assert has_tool_errors(dirty)


def test_build_data_gaps_section_empty():
    section = build_data_gaps_section([])
    assert section == ""


def test_build_data_gaps_section_renders():
    err = format_tool_error("get_news", "AAPL", "Polygon 429")
    errors = extract_tool_errors(err)
    section = build_data_gaps_section(errors)
    assert "Data Gaps" in section
    assert "get_news" in section
    assert "AAPL" in section
    assert "Polygon 429" in section
