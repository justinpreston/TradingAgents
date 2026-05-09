"""Short interest / short volume tests — mocks all Polygon calls."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tradingagents.dataflows.polygon_shorts import (
    get_short_interest,
    get_short_volume,
    build_shorts_snapshot,
)
from tradingagents.dataflows.polygon_common import PolygonNotFoundError


class TestGetShortInterest:
    @patch("tradingagents.dataflows.polygon_shorts._make_request")
    def test_returns_latest_entry(self, mock_req):
        mock_req.return_value = {
            "status": "OK",
            "results": [
                {
                    "ticker": "NVDA",
                    "settlement_date": "2026-04-15",
                    "short_interest": 283335701,
                    "avg_daily_volume": 144451756,
                    "days_to_cover": 1.96,
                }
            ],
        }
        si = get_short_interest("NVDA")
        assert si["short_interest"] == 283335701
        assert si["days_to_cover"] == 1.96
        assert si["ticker"] == "NVDA"

    @patch("tradingagents.dataflows.polygon_shorts._make_request")
    def test_returns_none_on_404(self, mock_req):
        mock_req.side_effect = PolygonNotFoundError("404")
        assert get_short_interest("FAKE") is None

    @patch("tradingagents.dataflows.polygon_shorts._make_request")
    def test_returns_none_on_empty_results(self, mock_req):
        mock_req.return_value = {"status": "OK", "results": []}
        assert get_short_interest("NVDA") is None


class TestGetShortVolume:
    @patch("tradingagents.dataflows.polygon_shorts._make_request")
    def test_returns_latest_entry(self, mock_req):
        mock_req.return_value = {
            "status": "OK",
            "results": [
                {
                    "ticker": "NVDA",
                    "date": "2026-05-08",
                    "total_volume": 58902391.0,
                    "short_volume": 22848771.9,
                    "short_volume_ratio": 38.79,
                }
            ],
        }
        sv = get_short_volume("NVDA")
        assert sv["short_volume_ratio"] == 38.79

    @patch("tradingagents.dataflows.polygon_shorts._make_request")
    def test_returns_none_on_error(self, mock_req):
        mock_req.side_effect = PolygonNotFoundError("404")
        assert get_short_volume("FAKE") is None


class TestBuildShortsSnapshot:
    @patch("tradingagents.dataflows.polygon_shorts.get_short_volume")
    @patch("tradingagents.dataflows.polygon_shorts.get_short_interest")
    def test_snapshot_structure(self, mock_si, mock_sv):
        mock_si.return_value = {
            "ticker": "NVDA", "short_interest": 283335701,
            "days_to_cover": 1.96, "settlement_date": "2026-04-15",
            "avg_daily_volume": 144451756,
        }
        mock_sv.return_value = {
            "ticker": "NVDA", "date": "2026-05-08",
            "short_volume_ratio": 38.79,
            "total_volume": 58902391.0, "short_volume": 22848771.9,
        }
        snap = build_shorts_snapshot(["NVDA"])
        assert len(snap["tickers"]) == 1
        entry = snap["tickers"][0]
        assert entry["ticker"] == "NVDA"
        assert entry["short_interest"]["days_to_cover"] == 1.96
        assert entry["short_volume"]["short_volume_ratio"] == 38.79

    @patch("tradingagents.dataflows.polygon_shorts.get_short_volume")
    @patch("tradingagents.dataflows.polygon_shorts.get_short_interest")
    def test_skips_missing_tickers(self, mock_si, mock_sv):
        mock_si.return_value = None
        mock_sv.return_value = None
        snap = build_shorts_snapshot(["FAKE"])
        assert len(snap["tickers"]) == 1
        assert snap["tickers"][0]["short_interest"] is None
