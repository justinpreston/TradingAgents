"""Tests for tradingagents/dataflows/polygon_bars.py — the shared daily-bar
fetch + OCC-ticker helpers extracted from scripts/backtest_picks.py and
scripts/backtest_exit_rules.py.

No live network calls — polygon_common._make_request is mocked directly.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tradingagents.dataflows.polygon_bars import fetch_daily_bars, occ_ticker
from tradingagents.dataflows.polygon_common import (
    PolygonAuthError,
    PolygonError,
    PolygonRateLimitError,
)


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")


# ---------------------------------------------------------------------------
# occ_ticker
# ---------------------------------------------------------------------------

def test_occ_ticker_basic():
    assert occ_ticker("AAPL", "2026-01-16", 150.0) == "O:AAPL260116C00150000"


def test_occ_ticker_put():
    assert occ_ticker("AAPL", "2026-01-16", 150.0, "P") == "O:AAPL260116P00150000"


def test_occ_ticker_fractional_strike():
    assert occ_ticker("TSLA", "2026-06-19", 12.5) == "O:TSLA260619C00012500"


# ---------------------------------------------------------------------------
# fetch_daily_bars
# ---------------------------------------------------------------------------

def test_fetch_daily_bars_success():
    with patch(
        "tradingagents.dataflows.polygon_bars._make_request",
        return_value={"results": [{"t": 1, "o": 1, "h": 2, "l": 0.5, "c": 1.5}]},
    ) as mock_req:
        bars, err = fetch_daily_bars("AAPL", "2026-01-01", "2026-01-31")
    assert err is None
    assert bars == [{"t": 1, "o": 1, "h": 2, "l": 0.5, "c": 1.5}]
    mock_req.assert_called_once()
    path = mock_req.call_args[0][0]
    assert path == "/v2/aggs/ticker/AAPL/range/1/day/2026-01-01/2026-01-31"


def test_fetch_daily_bars_empty_results():
    with patch("tradingagents.dataflows.polygon_bars._make_request", return_value={"results": []}):
        bars, err = fetch_daily_bars("AAPL", "2026-01-01", "2026-01-31")
    assert err is None
    assert bars == []


def test_fetch_daily_bars_missing_results_key():
    with patch("tradingagents.dataflows.polygon_bars._make_request", return_value={}):
        bars, err = fetch_daily_bars("AAPL", "2026-01-01", "2026-01-31")
    assert err is None
    assert bars == []


def test_fetch_daily_bars_auth_error_403():
    with patch(
        "tradingagents.dataflows.polygon_bars._make_request",
        side_effect=PolygonAuthError("Polygon auth failed for /x: 403 NOT_AUTHORIZED"),
    ):
        bars, err = fetch_daily_bars("AAPL", "2026-01-01", "2026-01-31")
    assert bars is None
    assert err == "403"


def test_fetch_daily_bars_auth_error_401():
    with patch(
        "tradingagents.dataflows.polygon_bars._make_request",
        side_effect=PolygonAuthError("Polygon auth failed for /x: 401 unauthorized"),
    ):
        bars, err = fetch_daily_bars("AAPL", "2026-01-01", "2026-01-31")
    assert bars is None
    assert err == "401"


def test_fetch_daily_bars_rate_limit_exhausted():
    with patch(
        "tradingagents.dataflows.polygon_bars._make_request",
        side_effect=PolygonRateLimitError("rate limit hit after 6 attempts"),
    ):
        bars, err = fetch_daily_bars("AAPL", "2026-01-01", "2026-01-31")
    assert bars is None
    assert err is not None and "rate_limit" in err


def test_fetch_daily_bars_generic_error():
    with patch(
        "tradingagents.dataflows.polygon_bars._make_request",
        side_effect=PolygonError("transient 500"),
    ):
        bars, err = fetch_daily_bars("AAPL", "2026-01-01", "2026-01-31")
    assert bars is None
    assert err == "transient 500"


# ---------------------------------------------------------------------------
# backtest_picks.py / backtest_exit_rules.py wrapper contracts (byte-shape
# preservation across the polygon_common consolidation)
# ---------------------------------------------------------------------------

def test_backtest_picks_daily_bars_contract():
    from scripts.backtest_picks import _daily_bars

    with patch(
        "scripts.backtest_picks.fetch_daily_bars",
        return_value=([{"c": 1.0}], None),
    ):
        bars, err = _daily_bars("AAPL", "2026-01-01", "2026-01-31", "unused-key", 0.1)
    assert bars == [{"c": 1.0}]
    assert err is None


def test_backtest_picks_daily_bars_auth_error_contract():
    """Old contract: (None, 403) as an int auth code."""
    from scripts.backtest_picks import _daily_bars

    with patch("scripts.backtest_picks.fetch_daily_bars", return_value=(None, "403")):
        bars, err = _daily_bars("AAPL", "2026-01-01", "2026-01-31", "unused-key", 0.1)
    assert bars is None
    assert err == 403
    assert isinstance(err, int)


def test_backtest_exit_rules_bars_contract():
    """Old contract: bars-or-None, no auth info surfaced."""
    from scripts.backtest_exit_rules import _bars

    with patch("scripts.backtest_exit_rules.fetch_daily_bars", return_value=([{"c": 2.0}], None)):
        bars = _bars("AAPL", "2026-01-01", "2026-01-31", "unused-key", 0.1)
    assert bars == [{"c": 2.0}]


def test_backtest_exit_rules_bars_none_on_error():
    from scripts.backtest_exit_rules import _bars

    with patch("scripts.backtest_exit_rules.fetch_daily_bars", return_value=(None, "403")):
        bars = _bars("AAPL", "2026-01-01", "2026-01-31", "unused-key", 0.1)
    assert bars is None


def test_backtest_picks_occ_ticker_delegates():
    from scripts.backtest_picks import _occ_ticker
    assert _occ_ticker("AAPL", "2026-01-16", 150.0) == "O:AAPL260116C00150000"


def test_backtest_exit_rules_occ_ticker_delegates():
    from scripts.backtest_exit_rules import _occ_ticker
    assert _occ_ticker("AAPL", "2026-01-16", 150.0) == "O:AAPL260116C00150000"
