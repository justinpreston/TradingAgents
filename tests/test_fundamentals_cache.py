"""Tests for the disk-cache layer in :mod:`tradingagents.screener.fundamentals`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from tradingagents.dataflows._disk_cache import DiskCache
from tradingagents.screener import fundamentals
from tradingagents.screener.fundamentals import (
    FundamentalSignals,
    _signals_from_dict,
    compute_fundamental_signals,
)


def _make_reports(revenues: list[float], cogs: list[float]) -> list[dict]:
    quarter_ends = [
        "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
        "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31",
    ]
    return [
        {
            "period_of_report_date": quarter_ends[i],
            "financials": {
                "income_statement": {
                    "revenues": {"value": r * 1_000_000},
                    "cost_of_revenue": {"value": c * 1_000_000},
                    "operating_income_loss": {"value": (r - c - 10) * 1_000_000},
                }
            },
        }
        for i, (r, c) in enumerate(zip(revenues, cogs))
    ]


@pytest.fixture
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DiskCache:
    """Replace the module-level cache with a tmp-rooted one."""
    cache = DiskCache("fundamentals", ttl_seconds=fundamentals._FUNDAMENTALS_CACHE_TTL_S, cache_root=tmp_path)
    monkeypatch.setattr(fundamentals, "_FUNDAMENTALS_CACHE", cache)
    return cache


def test_cache_miss_calls_polygon_then_caches(isolated_cache: DiskCache) -> None:
    reports = _make_reports([80, 82, 85, 90, 95, 100, 110, 125],
                            [50, 51, 52, 54, 55, 56, 58, 60])
    fetch_count = {"n": 0}

    def fake_fetch(ticker: str, *, max_quarters: int = 8) -> list[dict]:
        fetch_count["n"] += 1
        return reports

    with patch.object(fundamentals, "fetch_quarterly_financials", side_effect=fake_fetch):
        sig1 = compute_fundamental_signals("AAPL")
        assert fetch_count["n"] == 1
        assert sig1.revenue_yoy_accelerating

        # Second call — should hit cache, no new fetch
        sig2 = compute_fundamental_signals("AAPL")
        assert fetch_count["n"] == 1
        assert sig2.revenue_yoy_accelerating
        assert sig2.fundamental_score == sig1.fundamental_score


def test_cache_persists_insufficient_data_too(isolated_cache: DiskCache) -> None:
    """Tickers with too few filings should also be cached so we don't refetch every run."""
    fetch_count = {"n": 0}

    def fake_fetch(ticker: str, *, max_quarters: int = 8) -> list[dict]:
        fetch_count["n"] += 1
        return []  # no data — typical for ADRs / IPOs

    with patch.object(fundamentals, "fetch_quarterly_financials", side_effect=fake_fetch):
        sig1 = compute_fundamental_signals("ADR_X")
        assert "insufficient_data" in sig1.flags
        assert fetch_count["n"] == 1

        sig2 = compute_fundamental_signals("ADR_X")
        assert "insufficient_data" in sig2.flags
        assert fetch_count["n"] == 1, "cache miss on insufficient_data ticker"


def test_use_cache_false_bypasses(isolated_cache: DiskCache) -> None:
    reports = _make_reports([100, 110, 120, 130, 140, 150, 160, 170],
                            [50, 55, 60, 60, 65, 70, 75, 80])
    fetch_count = {"n": 0}

    def fake_fetch(ticker: str, *, max_quarters: int = 8) -> list[dict]:
        fetch_count["n"] += 1
        return reports

    with patch.object(fundamentals, "fetch_quarterly_financials", side_effect=fake_fetch):
        compute_fundamental_signals("MSFT")
        assert fetch_count["n"] == 1

        # use_cache=False forces a fresh fetch
        compute_fundamental_signals("MSFT", use_cache=False)
        assert fetch_count["n"] == 2


def test_caller_supplied_reports_does_not_persist(isolated_cache: DiskCache) -> None:
    """Tests that pass synthetic reports must not poison the shared cache."""
    reports = _make_reports([80, 82, 85, 90, 95, 100, 110, 125],
                            [50, 51, 52, 54, 55, 56, 58, 60])

    compute_fundamental_signals("FAKE_X", reports=reports)
    # Cache file should not exist
    assert isolated_cache.get("FAKE_X") is None


def test_signals_from_dict_tolerates_unknown_keys() -> None:
    """A stale cache entry from before a field was added must still load."""
    payload = {
        "ticker": "AAPL",
        "fundamental_score": 75.0,
        "future_field_we_havent_added_yet": "ignored",
    }
    sig = _signals_from_dict(payload)
    assert sig.ticker == "AAPL"
    assert sig.fundamental_score == 75.0
    # Defaults filled in
    assert sig.quarters_available == 0


def test_signals_from_dict_tolerates_missing_keys() -> None:
    sig = _signals_from_dict({"ticker": "T"})
    assert sig.ticker == "T"
    assert sig.fundamental_score == 0.0
    assert sig.flags == []
