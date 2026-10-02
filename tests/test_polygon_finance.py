"""Polygon vendor module tests.

Mocks :func:`tradingagents.dataflows.vendors.polygon.common._make_request` and
:func:`paginated_results` so no live network calls are made. Validates:

* End-to-end ``get_fundamentals`` integration with mocked Polygon endpoints and
  a mocked SEC EDGAR snapshot (the retired Polygon financials endpoint is gone)
* Vendor router fallback when Polygon raises
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tradingagents.dataflows.errors import NoMarketDataError, VendorUnavailableError
from tradingagents.dataflows.vendors.polygon import finance as pf
from tradingagents.dataflows.vendors.polygon.common import (
    PolygonError,
    PolygonNotFoundError,
    PolygonRateLimitError,
)


# ---------------------------------------------------------------------------
# get_fundamentals end-to-end (mocked HTTP)
# ---------------------------------------------------------------------------


_NVDA_TICKER_REF = {
    "results": {
        "name": "Nvidia Corp",
        "market_cap": 2247000000000.0,
        "share_class_shares_outstanding": 2500000000,
        "weighted_shares_outstanding": 2495000000,
        "sic_description": "SEMICONDUCTORS & RELATED DEVICES",
        "primary_exchange": "XNAS",
    }
}

_NVDA_BARS = {
    "results": [
        {"t": 1715040000000, "o": 89.48, "h": 91.19, "l": 89.42, "c": 90.41, "v": 325721020},
        {"t": 1715126400000, "o": 90.53, "h": 91.07, "l": 88.23, "c": 88.75, "v": 378012680},
        {"t": 1715212800000, "o": 90.30, "h": 91.40, "l": 89.23, "c": 89.88, "v": 335325410},
    ]
}


# What sec_edgar.ttm_snapshot returns for NVDA as of 2024-05-10 (the EDGAR parsing
# itself is tested in test_edgar_fundamentals.py).
_NVDA_SNAPSHOT = {
    "revenue": 60922000000.0,
    "gross_profit": 44301000000.0,
    "net_income": 29760000000.0,
    "eps": 11.93,
    "cash": 7280000000.0,
    "total_assets": 65728000000.0,
    "equity": 42978000000.0,
    "long_term_debt": 8459000000.0,
    "latest_period_end": "2024-01-28",
    "latest_filed": "2024-02-21",
}


class TestGetFundamentalsIntegration:
    """Full get_fundamentals call with mocked Polygon endpoints and EDGAR snapshot."""

    def _route(self, endpoint, params=None):
        """Mock dispatcher: route calls based on URL prefix."""
        if endpoint.startswith("/v3/reference/tickers/"):
            return _NVDA_TICKER_REF
        if endpoint.startswith("/v2/aggs/ticker/"):
            return _NVDA_BARS
        raise AssertionError(f"Unexpected endpoint: {endpoint}")

    def test_get_fundamentals_returns_correct_market_cap_for_nvda(self):
        with patch.object(pf, "_make_request", side_effect=self._route), \
             patch.object(pf.sec_edgar, "ttm_snapshot", return_value=_NVDA_SNAPSHOT) as snap:
            report = pf.get_fundamentals("NVDA", "2024-05-10")

        # Critical: market cap must be in trillions, not buggy $224B
        assert "$2.25T" in report or "$2.24T" in report
        assert "Nvidia" in report
        assert "SEMICONDUCTORS" in report.upper()
        # PIT date appears in header, and EDGAR was asked for that date
        assert "2024-05-10" in report
        snap.assert_called_once_with("NVDA", "2024-05-10")

    def test_filing_derived_fields_come_from_the_edgar_snapshot(self):
        with patch.object(pf, "_make_request", side_effect=self._route), \
             patch.object(pf.sec_edgar, "ttm_snapshot", return_value=_NVDA_SNAPSHOT):
            report = pf.get_fundamentals("NVDA", "2024-05-10")
        assert "Revenue (TTM): $60.92B" in report
        assert "Gross Margin (TTM): 72.7%" in report
        assert "Net Income (TTM): $29.76B" in report
        assert "EPS (TTM): 11.93" in report
        assert "Total Assets (latest): $65.73B" in report
        assert "Most recent fiscal period: 2024-01-28 (filed 2024-02-21)" in report
        # Fields the filer did not tag are omitted, never invented.
        assert "Operating Income (TTM)" not in report
        assert "Operating Cash Flow (TTM)" not in report

    def test_no_edgar_coverage_falls_through_to_the_next_vendor(self):
        """An ADR or new IPO has no us-gaap facts: the overview must not be served
        half-empty from Polygon alone, so the router moves on to yfinance."""
        from tradingagents.dataflows import router

        with patch.object(pf.sec_edgar, "ttm_snapshot", side_effect=NoMarketDataError("TEVA", "TEVA", "no facts")), \
             patch.object(pf, "_make_request", side_effect=AssertionError("Polygon must not be called")), \
             patch.object(router, "get_vendor", return_value="polygon,yfinance"), \
             patch.dict(router.VENDOR_METHODS["get_fundamentals"],
                        {"polygon": pf.get_fundamentals, "yfinance": lambda *a, **k: "YF OVERVIEW"}):
            assert router.route_to_vendor("get_fundamentals", "TEVA", "2024-05-10") == "YF OVERVIEW"

    def test_edgar_outage_falls_through_to_the_next_vendor(self):
        from tradingagents.dataflows import router

        with patch.object(pf.sec_edgar, "ttm_snapshot", side_effect=VendorUnavailableError("503")), \
             patch.object(router, "get_vendor", return_value="polygon,yfinance"), \
             patch.dict(router.VENDOR_METHODS["get_fundamentals"],
                        {"polygon": pf.get_fundamentals, "yfinance": lambda *a, **k: "YF OVERVIEW"}):
            assert router.route_to_vendor("get_fundamentals", "NVDA", "2024-05-10") == "YF OVERVIEW"

    def test_no_code_path_calls_the_retired_financials_endpoint(self):
        """Sunset 2026-10-09 (410 GONE in the brownout): no source file may pass the
        endpoint as a string literal. Docstrings may still name it in backticks."""
        from pathlib import Path

        root = Path(pf.__file__).resolve().parents[3]
        offenders = [
            str(path) for folder in (root / "tradingagents", root / "scripts")
            for path in folder.rglob("*.py")
            if '"/vX/reference/financials"' in path.read_text(encoding="utf-8")
            or "'/vX/reference/financials'" in path.read_text(encoding="utf-8")
        ]
        assert offenders == []


# ---------------------------------------------------------------------------
# get_news / get_global_news / get_insider_transactions
# ---------------------------------------------------------------------------


class TestPolygonNews:
    def test_get_news_returns_formatted_string(self):
        sample = [
            {
                "published_utc": "2024-05-09T14:30:00Z",
                "title": "NVDA up on AI demand",
                "publisher": {"name": "Reuters"},
                "article_url": "https://example.com/1",
                "description": "NVIDIA shares rose ahead of earnings.",
            }
        ]
        from tradingagents.dataflows.vendors.polygon import news as pn
        with patch.object(pn, "paginated_results", return_value=sample):
            out = pn.get_news("NVDA", "2024-05-01", "2024-05-10")
        assert "NVDA up on AI demand" in out
        assert "Reuters" in out

    def test_get_news_handles_no_results(self):
        from tradingagents.dataflows.vendors.polygon import news as pn
        with patch.object(pn, "paginated_results", side_effect=PolygonNotFoundError("none")):
            out = pn.get_news("NVDA", "2024-05-01", "2024-05-10")
        assert "No news found" in out

    def test_get_insider_transactions_falls_through_when_all_endpoints_404(self):
        """Polygon does not currently expose an insider transactions
        endpoint on the public REST surface — every documented spelling
        returns 404. After probing all configured paths, the function
        must raise :class:`PolygonError` so the vendor router transparently
        falls through to alpha_vantage / yfinance.
        """
        from tradingagents.dataflows.vendors.polygon import news as pn
        # All configured probes raise PolygonNotFoundError → final raise.
        with patch.object(
            pn,
            "paginated_results",
            side_effect=PolygonNotFoundError("404 page not found"),
        ) as mock_paginate:
            with pytest.raises(PolygonError):
                pn.get_insider_transactions("NVDA")
        # Should have probed every spelling, not just the first.
        assert mock_paginate.call_count == len(pn._INSIDER_TXN_ENDPOINTS)
        called_paths = [c.args[0] for c in mock_paginate.call_args_list]
        assert called_paths == list(pn._INSIDER_TXN_ENDPOINTS)

    def test_get_insider_transactions_returns_formatted_on_first_200(self):
        """If any probe spelling returns 200 (e.g. Polygon ships the
        endpoint, or a future plan upgrade entitles us), the formatter
        must produce a usable digest and the remaining probes are
        skipped — no wasted round-trips."""
        from tradingagents.dataflows.vendors.polygon import news as pn
        sample = [
            {
                "transaction_date": "2026-04-23",
                "executive": "PAREKH, KEVAN",
                "executive_title": "SVP, CFO",
                "acquisition_or_disposal": "D",
                "shares": 1234,
                "price_per_share": 175.42,
            }
        ]
        with patch.object(
            pn, "paginated_results", return_value=sample
        ) as mock_paginate:
            out = pn.get_insider_transactions("AAPL")
        assert mock_paginate.call_count == 1  # short-circuit on first 200
        assert "Insider transactions for AAPL" in out
        assert "PAREKH, KEVAN" in out
        assert "SVP, CFO" in out

    def test_get_insider_transactions_empty_results_returns_friendly_message(self):
        from tradingagents.dataflows.vendors.polygon import news as pn
        with patch.object(pn, "paginated_results", return_value=[]):
            out = pn.get_insider_transactions("XYZ")
        assert "No insider transactions found" in out

    def test_get_insider_transactions_recovers_when_first_endpoint_404s(self):
        """If the first probe 404s but a later one succeeds (defends
        against URL drift across Polygon API versions), the function
        must return the successful payload."""
        from tradingagents.dataflows.vendors.polygon import news as pn
        sample = [{"transaction_date": "2026-04-23", "executive": "DOE, JANE"}]
        side_effects = [
            PolygonNotFoundError("404"),  # first endpoint missing
            sample,                       # second endpoint returns
        ]
        # Ensure the probe list has at least 2 entries so this test is
        # meaningful — gate explicitly so future trims don't silently
        # turn this into a no-op.
        assert len(pn._INSIDER_TXN_ENDPOINTS) >= 2
        with patch.object(
            pn, "paginated_results", side_effect=side_effects
        ):
            out = pn.get_insider_transactions("XYZ")
        assert "DOE, JANE" in out


# ---------------------------------------------------------------------------
# Vendor router fallback
# ---------------------------------------------------------------------------


class TestVendorRouterFallback:
    """Confirm route_to_vendor falls back from Polygon to yfinance on
    PolygonError, and from any vendor missing API key to the next."""

    def test_polygon_error_triggers_fallback_to_yfinance(self):
        from tradingagents.dataflows import router as interface
        def boom(*args, **kwargs):
            raise PolygonRateLimitError("simulated 429")

        sentinel = "FALLBACK_TO_YFINANCE_REPORT"
        # Patch the configured chain to be exactly polygon→yfinance so we
        # can reason about the fallback deterministically (avoids ordering
        # dependencies on alpha_vantage, which sits between them in the
        # registration order and would otherwise be tried second).
        with patch.object(interface, "get_vendor", return_value="polygon,yfinance"), \
             patch.dict(
                 interface.VENDOR_METHODS["get_fundamentals"],
                 {
                     "polygon": boom,
                     "yfinance": lambda *a, **kw: sentinel,
                 },
             ):
            result = interface.route_to_vendor(
                "get_fundamentals", "NVDA", "2024-05-10"
            )
            assert result == sentinel

    def test_missing_api_key_value_error_triggers_fallback(self):
        from tradingagents.dataflows import router as interface
        def missing_key(*args, **kwargs):
            raise ValueError("ALPHA_VANTAGE_API_KEY environment variable is not set.")

        sentinel = "FALLBACK_AFTER_MISSING_KEY"
        with patch.object(interface, "get_vendor", return_value="polygon,yfinance"), \
             patch.dict(
                 interface.VENDOR_METHODS["get_fundamentals"],
                 {
                     "polygon": missing_key,
                     "yfinance": lambda *a, **kw: sentinel,
                 },
             ):
            result = interface.route_to_vendor(
                "get_fundamentals", "NVDA", "2024-05-10"
            )
            assert result == sentinel

    def test_unrelated_value_error_does_not_swallow(self):
        """ValueError that isn't about API keys (e.g. bad input args) must
        propagate — we only fall through on missing-key signals."""
        from tradingagents.dataflows import router as interface
        def bad_input(*args, **kwargs):
            raise ValueError("ticker must be a non-empty string")

        with patch.object(interface, "get_vendor", return_value="polygon,yfinance,alpha_vantage"), \
             patch.dict(
                 interface.VENDOR_METHODS["get_fundamentals"],
                 {
                     "polygon": bad_input,
                     "yfinance": bad_input,
                     "alpha_vantage": bad_input,
                 },
             ):
            with pytest.raises(ValueError, match="non-empty string"):
                interface.route_to_vendor(
                    "get_fundamentals", "NVDA", "2024-05-10"
                )
