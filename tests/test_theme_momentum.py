"""Theme momentum tests — mocks all network I/O."""

from __future__ import annotations

from unittest.mock import patch, MagicMock
from datetime import date

import pytest

from tradingagents.dataflows.theme_momentum import (
    fetch_daily_closes,
    compute_returns,
    relative_strength,
    build_theme_snapshot,
)


def _mock_bars(ticker: str, n: int = 60) -> list[dict]:
    """Generate n synthetic daily bars with price = 100 + i."""
    return [
        {"t": 1714500000000 + i * 86400000, "c": 100.0 + i}
        for i in range(n)
    ]


class TestFetchDailyCloses:
    @patch("tradingagents.dataflows.theme_momentum._make_request")
    def test_returns_close_series(self, mock_req):
        mock_req.return_value = {"results": _mock_bars("NVDA", 5)}
        closes = fetch_daily_closes("NVDA", lookback_days=10)
        assert len(closes) == 5
        assert closes[0] == 100.0
        assert closes[-1] == 104.0

    @patch("tradingagents.dataflows.theme_momentum._make_request")
    def test_empty_results_returns_empty(self, mock_req):
        mock_req.return_value = {"results": []}
        closes = fetch_daily_closes("NVDA", lookback_days=10)
        assert closes == []


class TestComputeReturns:
    def test_simple_returns(self):
        closes = [100.0, 105.0, 110.0]
        r = compute_returns(closes)
        assert r["5d_pct"] is None  # not enough data
        assert r["total_pct"] == pytest.approx(10.0, abs=0.01)

    def test_enough_data_for_5d(self):
        closes = list(range(100, 107))  # 7 data points
        r = compute_returns(closes)
        # 5d return = (106 - 101) / 101
        assert r["5d_pct"] == pytest.approx(4.95, abs=0.1)

    def test_empty_closes(self):
        r = compute_returns([])
        assert r["total_pct"] is None


class TestRelativeStrength:
    def test_positive_relative_strength(self):
        rs = relative_strength(10.0, 5.0)
        assert rs == pytest.approx(5.0, abs=0.01)

    def test_none_inputs(self):
        assert relative_strength(None, 5.0) is None
        assert relative_strength(10.0, None) is None


class TestBuildThemeSnapshot:
    @patch("tradingagents.dataflows.theme_momentum.fetch_daily_closes")
    def test_snapshot_structure(self, mock_fetch):
        mock_fetch.return_value = [100.0 + i for i in range(62)]
        snap = build_theme_snapshot(
            basket=["NVDA", "INTC"],
            benchmark="SPY",
            lookback_days=90,
        )
        assert "basket" in snap
        assert "benchmark" in snap
        assert len(snap["basket"]) == 2
        assert snap["benchmark"]["ticker"] == "SPY"
        for entry in snap["basket"]:
            assert "ticker" in entry
            assert "returns" in entry
            assert "vs_benchmark" in entry
