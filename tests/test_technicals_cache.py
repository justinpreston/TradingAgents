"""Tests for the disk-cache layer in :mod:`tradingagents.screener.technicals`."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from tradingagents.dataflows._disk_cache import DiskCache
from tradingagents.screener import technicals
from tradingagents.screener.technicals import (
    TechnicalSignals,
    _signals_from_dict,
    compute_technical_signals,
)


def _synthetic_bars(n: int = 260, start: float = 100.0, step: float = 0.3) -> list[dict]:
    """Generate n strictly-uptrending bars — enough history for all signals."""
    return [
        {
            "t": i,
            "o": start + i * step,
            "h": start + i * step + 1.0,
            "l": start + i * step - 1.0,
            "c": start + i * step,
            "v": 1_000_000 + i * 1000,
        }
        for i in range(n)
    ]


@pytest.fixture
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DiskCache:
    cache = DiskCache("technicals", cache_root=tmp_path)
    monkeypatch.setattr(technicals, "_TECHNICALS_CACHE", cache)
    return cache


def test_cache_miss_then_hit(isolated_cache: DiskCache) -> None:
    bars = _synthetic_bars()
    end = date(2026, 5, 8)
    fetch_count = {"n": 0}

    def fake_fetch(ticker: str, *, end_date: date | None = None, lookback_days: int = 380) -> list[dict]:
        fetch_count["n"] += 1
        return bars

    with patch.object(technicals, "fetch_daily_bars", side_effect=fake_fetch):
        sig1 = compute_technical_signals("AAPL", end_date=end)
        assert fetch_count["n"] == 1

        sig2 = compute_technical_signals("AAPL", end_date=end)
        assert fetch_count["n"] == 1  # cache hit
        assert sig2.technical_score == sig1.technical_score
        assert sig2.bars_count == sig1.bars_count


def test_cache_keyed_on_end_date(isolated_cache: DiskCache) -> None:
    """Same ticker on different dates should miss cache (refetch)."""
    bars = _synthetic_bars()
    fetch_count = {"n": 0}

    def fake_fetch(ticker: str, *, end_date: date | None = None, lookback_days: int = 380) -> list[dict]:
        fetch_count["n"] += 1
        return bars

    with patch.object(technicals, "fetch_daily_bars", side_effect=fake_fetch):
        compute_technical_signals("AAPL", end_date=date(2026, 5, 1))
        compute_technical_signals("AAPL", end_date=date(2026, 5, 8))
        assert fetch_count["n"] == 2  # different dates → no shared cache


def test_use_cache_false_bypasses(isolated_cache: DiskCache) -> None:
    bars = _synthetic_bars()
    end = date(2026, 5, 8)
    fetch_count = {"n": 0}

    def fake_fetch(ticker: str, *, end_date: date | None = None, lookback_days: int = 380) -> list[dict]:
        fetch_count["n"] += 1
        return bars

    with patch.object(technicals, "fetch_daily_bars", side_effect=fake_fetch):
        compute_technical_signals("MSFT", end_date=end)
        assert fetch_count["n"] == 1

        compute_technical_signals("MSFT", end_date=end, use_cache=False)
        assert fetch_count["n"] == 2  # bypassed


def test_caller_supplied_bars_does_not_persist(isolated_cache: DiskCache) -> None:
    """Tests that pass synthetic bars must not poison the shared cache."""
    bars = _synthetic_bars()
    end = date(2026, 5, 8)

    compute_technical_signals("FAKE_X", end_date=end, bars=bars)
    cache_key = f"FAKE_X_{end.isoformat()}"
    assert isolated_cache.get(cache_key) is None


def test_insufficient_history_is_cached(isolated_cache: DiskCache) -> None:
    """Tickers with <200 bars should also be cached so we don't refetch every run."""
    short_bars = _synthetic_bars(n=50)
    end = date(2026, 5, 8)
    fetch_count = {"n": 0}

    def fake_fetch(ticker: str, *, end_date: date | None = None, lookback_days: int = 380) -> list[dict]:
        fetch_count["n"] += 1
        return short_bars

    with patch.object(technicals, "fetch_daily_bars", side_effect=fake_fetch):
        sig1 = compute_technical_signals("IPO_X", end_date=end)
        assert "insufficient_history" in sig1.flags
        assert fetch_count["n"] == 1

        sig2 = compute_technical_signals("IPO_X", end_date=end)
        assert "insufficient_history" in sig2.flags
        assert fetch_count["n"] == 1  # cached


def test_signals_from_dict_tolerates_unknown_keys() -> None:
    payload = {
        "ticker": "AAPL",
        "technical_score": 75.0,
        "future_field_we_havent_added_yet": "ignored",
    }
    sig = _signals_from_dict(payload)
    assert sig.ticker == "AAPL"
    assert sig.technical_score == 75.0
    assert sig.bars_count == 0  # default


def test_signals_from_dict_tolerates_missing_keys() -> None:
    sig = _signals_from_dict({"ticker": "T"})
    assert sig.ticker == "T"
    assert sig.technical_score == 0.0
    assert sig.flags == []
