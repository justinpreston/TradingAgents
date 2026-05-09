"""Polygon short interest and short volume data.

Wraps the Stocks Developer endpoints:
- /stocks/v1/short-interest — biweekly FINRA short interest
- /stocks/v1/short-volume — daily short volume with venue breakdown

Fail-soft: returns None per ticker on any error rather than raising.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .polygon_common import _make_request, PolygonError


def get_short_interest(ticker: str, limit: int = 1) -> dict[str, Any] | None:
    """Fetch the most recent short interest report for a ticker."""
    try:
        data = _make_request(
            "/stocks/v1/short-interest",
            {"ticker": ticker, "limit": str(limit), "sort": "settlement_date.desc"},
        )
    except PolygonError:
        return None
    results = data.get("results") or []
    return results[0] if results else None


def get_short_volume(ticker: str, limit: int = 1) -> dict[str, Any] | None:
    """Fetch the most recent daily short volume for a ticker."""
    try:
        data = _make_request(
            "/stocks/v1/short-volume",
            {"ticker": ticker, "limit": str(limit), "sort": "date.desc"},
        )
    except PolygonError:
        return None
    results = data.get("results") or []
    return results[0] if results else None


def build_shorts_snapshot(
    tickers: list[str],
    as_of: date | None = None,
) -> dict[str, Any]:
    """Build a combined short interest + volume snapshot for a list of tickers."""
    ref_date = as_of or date.today()
    entries = []
    for ticker in tickers:
        entries.append({
            "ticker": ticker,
            "short_interest": get_short_interest(ticker),
            "short_volume": get_short_volume(ticker),
        })
    return {"as_of": ref_date.isoformat(), "tickers": entries}
