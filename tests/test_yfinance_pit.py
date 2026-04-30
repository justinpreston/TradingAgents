"""Point-in-time correctness tests for yfinance fundamentals.

yfinance's ``Ticker.info`` always returns *live* snapshot values (current
market cap, TTM ratios, 52-week ranges) regardless of any historical date
supplied by the caller. ``get_fundamentals`` must therefore omit those
fields when ``curr_date`` is in the past, while preserving the prior
behaviour for live (None / today) calls.

These tests mock ``yfinance.Ticker`` so they do not hit the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.dataflows import y_finance


# A representative subset of the live-snapshot field labels the function
# is expected to *suppress* on historical calls. (Picked to span price-
# derived, TTM, and rolling-window categories.)
_LIVE_FIELD_LABELS = (
    "Market Cap",
    "PE Ratio (TTM)",
    "Forward PE",
    "Price to Book",
    "52 Week High",
    "50 Day Average",
    "200 Day Average",
    "Dividend Yield",
    "Revenue (TTM)",
    "EBITDA",
    "Free Cash Flow",
    "EPS (TTM)",
)

_STABLE_FIELD_LABELS = ("Name", "Sector", "Industry", "Beta")


def _fake_info():
    """Realistic-shaped yfinance Ticker.info payload (NVDA-like, April 2026)."""
    return {
        "longName": "NVIDIA Corporation",
        "sector": "Technology",
        "industry": "Semiconductors",
        "marketCap": 5_090_000_000_000,
        "trailingPE": 45.6,
        "forwardPE": 18.6,
        "pegRatio": 1.2,
        "priceToBook": 38.0,
        "trailingEps": 4.90,
        "forwardEps": 11.24,
        "dividendYield": 0.0002,
        "beta": 2.34,
        "fiftyTwoWeekHigh": 216.83,
        "fiftyTwoWeekLow": 104.08,
        "fiftyDayAverage": 186.23,
        "twoHundredDayAverage": 183.35,
        "totalRevenue": 215_940_000_000,
        "grossProfits": 156_000_000_000,
        "ebitda": 130_000_000_000,
        "netIncomeToCommon": 90_000_000_000,
        "profitMargins": 0.42,
        "operatingMargins": 0.55,
        "returnOnEquity": 0.95,
        "returnOnAssets": 0.45,
        "debtToEquity": 22.0,
        "currentRatio": 4.5,
        "bookValue": 2.5,
        "freeCashflow": 58_100_000_000,
    }


def _patch_ticker(info):
    """Patch yf.Ticker so that .info returns ``info`` and yf_retry passes through."""
    fake_ticker = MagicMock()
    fake_ticker.info = info
    return patch.object(y_finance.yf, "Ticker", return_value=fake_ticker)


@pytest.mark.unit
def test_historical_curr_date_omits_live_snapshot_fields():
    """A past curr_date must drop look-ahead live fields (Market Cap, P/E, etc.)."""
    past = "2024-05-10"
    with _patch_ticker(_fake_info()):
        out = y_finance.get_fundamentals("NVDA", curr_date=past)

    # Live snapshot fields must NOT appear in the body.
    for label in _LIVE_FIELD_LABELS:
        assert f"{label}:" not in out, (
            f"{label!r} leaked into historical fundamentals output: "
            f"this is the look-ahead bug ({label} from yfinance.Ticker.info "
            f"always reflects today's value, not {past})."
        )

    # Stable structural fields must still be present.
    for label in _STABLE_FIELD_LABELS:
        assert f"{label}:" in out, f"stable field {label!r} missing from output"

    # Header must explicitly call out the point-in-time mode and the omission,
    # so downstream agents can reason about the absent data instead of silently
    # filling the gap from the live LLM context.
    assert "Point-in-time mode" in out
    assert past in out
    assert "look-ahead bias" in out


@pytest.mark.unit
def test_no_curr_date_preserves_live_snapshot_fields():
    """Backwards compatibility: curr_date=None returns the full live snapshot."""
    with _patch_ticker(_fake_info()):
        out = y_finance.get_fundamentals("NVDA", curr_date=None)

    for label in _LIVE_FIELD_LABELS + _STABLE_FIELD_LABELS:
        assert f"{label}:" in out, f"{label!r} missing from live (no-curr_date) output"

    assert "Point-in-time mode" not in out


@pytest.mark.unit
def test_today_preserves_live_snapshot_fields():
    """curr_date == today is a *live* call and must not trip the historical guard."""
    today = datetime.now().date().strftime("%Y-%m-%d")
    with _patch_ticker(_fake_info()):
        out = y_finance.get_fundamentals("NVDA", curr_date=today)

    assert "Market Cap:" in out
    assert "Point-in-time mode" not in out


@pytest.mark.unit
def test_future_curr_date_treated_as_live():
    """A curr_date in the future is degenerate — fall back to live snapshot rather
    than silently returning an empty 'point-in-time' view."""
    future = (datetime.now().date() + timedelta(days=30)).strftime("%Y-%m-%d")
    with _patch_ticker(_fake_info()):
        out = y_finance.get_fundamentals("NVDA", curr_date=future)

    assert "Market Cap:" in out
    assert "Point-in-time mode" not in out


@pytest.mark.unit
def test_malformed_curr_date_treated_as_live():
    """Invalid curr_date strings must not silently suppress fields — fall back to live."""
    with _patch_ticker(_fake_info()):
        out = y_finance.get_fundamentals("NVDA", curr_date="not-a-date")

    assert "Market Cap:" in out
    assert "Point-in-time mode" not in out


@pytest.mark.unit
def test_empty_info_payload():
    """A None / empty info dict still produces a clean error string, not a crash."""
    with _patch_ticker({}):
        out = y_finance.get_fundamentals("NVDA", curr_date="2024-05-10")

    assert "No fundamentals data found" in out
