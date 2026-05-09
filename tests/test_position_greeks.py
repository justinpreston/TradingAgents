"""Position greeks tests — mocks Polygon options snapshot."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure repo root is on path so scripts/ is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.build_position_greeks import (
    fetch_contract_snapshot,
    build_portfolio_greeks,
    aggregate_greeks,
)


MOCK_SNAPSHOT = {
    "results": [
        {
            "details": {
                "contract_type": "call",
                "strike_price": 200.0,
                "expiration_date": "2027-01-15",
                "ticker": "O:NVDA270115C00200000",
            },
            "greeks": {
                "delta": 0.67,
                "gamma": 0.0046,
                "theta": -0.068,
                "vega": 0.65,
            },
            "implied_volatility": 0.445,
            "open_interest": 89753,
            "underlying_asset": {"price": 215.05, "ticker": "NVDA"},
            "break_even_price": 214.88,
            "day": {"close": 41.21, "volume": 1234},
        }
    ]
}


class TestFetchContractSnapshot:
    @patch("scripts.build_position_greeks._make_request")
    def test_returns_matched_contract(self, mock_req):
        mock_req.return_value = MOCK_SNAPSHOT
        result = fetch_contract_snapshot("NVDA", 200.0, "2027-01-15", "call")
        assert result is not None
        assert result["greeks"]["delta"] == 0.67
        assert result["implied_volatility"] == 0.445

    @patch("scripts.build_position_greeks._make_request")
    def test_returns_none_on_no_match(self, mock_req):
        mock_req.return_value = {"results": []}
        result = fetch_contract_snapshot("NVDA", 999.0, "2027-01-15", "call")
        assert result is None


class TestAggregateGreeks:
    def test_sums_weighted_by_qty(self):
        positions = [
            {"qty": 3, "greeks": {"delta": 0.67, "gamma": 0.005, "theta": -0.07, "vega": 0.65}},
            {"qty": 5, "greeks": {"delta": 0.50, "gamma": 0.003, "theta": -0.05, "vega": 0.40}},
        ]
        agg = aggregate_greeks(positions)
        assert agg["total_delta"] == pytest.approx(451.0, abs=0.1)
        assert agg["total_theta_per_day"] == pytest.approx(-46.0, abs=0.1)

    def test_empty_positions(self):
        agg = aggregate_greeks([])
        assert agg["total_delta"] == 0.0


class TestBuildPortfolioGreeks:
    @patch("scripts.build_position_greeks.fetch_contract_snapshot")
    def test_builds_from_positions(self, mock_fetch):
        mock_fetch.return_value = MOCK_SNAPSHOT["results"][0]
        positions = [
            {
                "id": "NVDA-test",
                "ticker": "NVDA",
                "instrument": "long_call",
                "qty": 3,
                "option": {"strike": 200.0, "expiry": "2027-01-15"},
            }
        ]
        result = build_portfolio_greeks(positions)
        assert len(result["positions"]) == 1
        assert result["positions"][0]["live_greeks"]["delta"] == 0.67
        assert result["aggregate"]["total_delta"] == pytest.approx(201.0, abs=0.1)

    @patch("scripts.build_position_greeks.fetch_contract_snapshot")
    def test_skips_equity_positions(self, mock_fetch):
        positions = [
            {"id": "MSFT-eq", "ticker": "MSFT", "instrument": "equity", "qty": 100, "option": None}
        ]
        result = build_portfolio_greeks(positions)
        assert len(result["positions"]) == 0
        mock_fetch.assert_not_called()
