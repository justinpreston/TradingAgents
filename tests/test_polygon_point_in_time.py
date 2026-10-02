"""Polygon vendor conforms to the router's point-in-time contract (upstream v0.5.x).

The tool layer clamps dates to the run's trade date and the router calls
``get_insider_transactions(ticker, trade_date)``; Polygon must accept that call,
drop later rows, and not stamp the wall clock into tool output.
"""
from __future__ import annotations

from unittest import mock

import pytest

from tradingagents.dataflows.vendors.polygon import finance as pf
from tradingagents.dataflows.vendors.polygon import news as pn

pytestmark = pytest.mark.unit

_ROWS = [
    {"filing_date": "2024-05-01", "executive": "EARLY FILER", "transaction_type": "S"},
    {"filing_date": "2024-06-15", "executive": "LATE FILER", "transaction_type": "P"},
]


def test_insider_accepts_the_router_call_and_drops_later_filings():
    with mock.patch.object(pn, "paginated_results", return_value=list(_ROWS)):
        out = pn.get_insider_transactions("HXL", "2024-05-10")
    assert "EARLY FILER" in out
    assert "LATE FILER" not in out


def test_insider_without_a_date_keeps_every_row():
    with mock.patch.object(pn, "paginated_results", return_value=list(_ROWS)):
        out = pn.get_insider_transactions("HXL")
    assert "EARLY FILER" in out and "LATE FILER" in out


def test_router_passes_the_trade_date_through_to_polygon():
    from tradingagents.dataflows import router

    with mock.patch.object(pn, "paginated_results", return_value=list(_ROWS)), \
         mock.patch.dict(router.VENDOR_METHODS["get_insider_transactions"],
                         {"polygon": pn.get_insider_transactions}), \
         mock.patch.object(router, "get_vendor", return_value="polygon"):
        out = router.route_to_vendor("get_insider_transactions", "HXL", "2024-05-10")
    assert "EARLY FILER" in out and "LATE FILER" not in out


def test_no_wall_clock_stamp_in_polygon_tool_output():
    import inspect
    assert "Data retrieved on" not in inspect.getsource(pf)


def test_polygon_errors_never_carry_the_api_key(monkeypatch):
    """requests quotes the full URL (with ?apiKey=...) in connection errors; that
    text reaches tool output, prompts, state files and logs, so it is scrubbed."""
    import requests

    from tradingagents.dataflows.vendors.polygon import common

    monkeypatch.setenv("POLYGON_API_KEY", "sekret-key-123")

    def _boom(url, params=None, timeout=None, **k):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}?apiKey={params['apiKey']}")

    monkeypatch.setattr(common.requests, "get", _boom)
    monkeypatch.setattr(common.time, "sleep", lambda s: None)
    with pytest.raises(common.PolygonError) as exc:
        common._make_request("/v2/aggs/ticker/AAPL/prev", max_attempts=1)
    assert "sekret-key-123" not in str(exc.value)
    assert "***" in str(exc.value)
