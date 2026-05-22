"""Tests for the analyst PT consensus fetcher."""

from __future__ import annotations

from tradingagents.dataflows.analyst_pt_consensus import (
    AnalystPriceTargets,
    fetch_analyst_pt_consensus,
)


def test_analyst_price_targets_is_populated_when_mean_present():
    tgt = AnalystPriceTargets(
        ticker="AAPL",
        current=190.0,
        high=240.0,
        low=160.0,
        mean=200.0,
        median=205.0,
        number_of_analysts=35,
        source="yfinance",
    )
    assert tgt.is_populated()


def test_analyst_price_targets_is_unpopulated_when_all_targets_none():
    tgt = AnalystPriceTargets(
        ticker="XYZ",
        current=None,
        high=None,
        low=None,
        mean=None,
        median=None,
        number_of_analysts=None,
        source="unavailable",
    )
    assert not tgt.is_populated()


def test_as_dict_round_trip():
    tgt = AnalystPriceTargets(
        ticker="MSFT",
        current=440.0,
        high=500.0,
        low=400.0,
        mean=450.0,
        median=445.0,
        number_of_analysts=42,
        source="yfinance",
    )
    d = tgt.as_dict()
    assert d["ticker"] == "MSFT"
    assert d["mean"] == 450.0
    assert d["number_of_analysts"] == 42
    assert d["source"] == "yfinance"


def test_fetch_returns_unpopulated_on_bogus_ticker():
    """A clearly invalid ticker must not raise — fail-soft is required."""
    tgt = fetch_analyst_pt_consensus("ZZZZZZNOTAREALTICKER", use_cache=False)
    assert isinstance(tgt, AnalystPriceTargets)
    assert tgt.ticker == "ZZZZZZNOTAREALTICKER"
    # Contract: never raises. Either unpopulated or backstopped from info.
    assert tgt.source in {"yfinance", "unavailable"}
