"""Tests for the earnings calendar enrichment surface (Tier-1 item A).

Covers the three layers the user-facing flow depends on:
1. ``tradingagents.dataflows.earnings_calendar`` — fail-soft, cached
2. ``tradingagents.dataflows.news_enrichment_loader`` — env-var prefix
3. ``scripts.build_options_overlay`` — tenor adjustment when expiry sits
   on top of an earnings print

Tests mock ``yf.Ticker`` rather than hit the network, so they are
deterministic in CI and on a flaky-network laptop.
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pandas as pd
import pytest

from tradingagents.dataflows import earnings_calendar as ec
from tradingagents.dataflows import news_enrichment_loader as nel


# ──────────────────────────────────────────────────────────────────────
# earnings_calendar dataflow
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_caches():
    ec.reset_cache()
    nel.reset_cache()
    # Ensure env vars from other tests don't leak in.
    os.environ.pop(nel.EARNINGS_ENV_VAR, None)
    os.environ.pop(nel.ENRICHMENT_ENV_VAR, None)
    yield
    ec.reset_cache()
    nel.reset_cache()


def _mock_ticker_with_calendar(next_date: date | None = None,
                               history_rows: list[dict] | None = None):
    """Build a SimpleNamespace that mimics yfinance.Ticker shape.

    yfinance returns ``calendar`` as either a dict (older) or DataFrame
    (newer). We use the dict shape because it's the more common path in
    the wild and the loader handles both.
    """
    cal: dict | None = None
    if next_date:
        cal = {"Earnings Date": [next_date]}
    history_df: pd.DataFrame | None = None
    if history_rows:
        history_df = pd.DataFrame(history_rows).set_index("quarter")
    return SimpleNamespace(
        calendar=cal,
        earnings_dates=None,
        earnings_history=history_df,
    )


def test_get_next_earnings_returns_none_when_yf_missing():
    with mock.patch.object(ec, "yf", None):
        assert ec.get_next_earnings("AAPL") is None


def test_get_next_earnings_fail_soft_on_vendor_exception():
    with mock.patch.object(ec, "yf") as yfmock:
        yfmock.Ticker.side_effect = RuntimeError("network down")
        assert ec.get_next_earnings("AAPL") is None


def test_get_next_earnings_returns_payload_when_present():
    today = date(2026, 5, 6)
    next_date = today + timedelta(days=14)
    with mock.patch.object(ec, "yf") as yfmock:
        yfmock.Ticker.return_value = _mock_ticker_with_calendar(next_date=next_date)
        result = ec.get_next_earnings("AAPL", today=today)
    assert result is not None
    assert result["date"] == next_date.isoformat()
    assert result["days_away"] == 14
    assert result["source"] == "yfinance"
    assert result["confirmed"] is True


def test_get_next_earnings_skips_past_dates():
    today = date(2026, 5, 6)
    past = today - timedelta(days=3)
    with mock.patch.object(ec, "yf") as yfmock:
        yfmock.Ticker.return_value = _mock_ticker_with_calendar(next_date=past)
        assert ec.get_next_earnings("AAPL", today=today) is None


def test_cache_uses_false_sentinel_for_negative_results():
    """Confirms the False sentinel logic — first miss caches, second
    call short-circuits without re-hitting the vendor."""
    with mock.patch.object(ec, "yf") as yfmock:
        yfmock.Ticker.side_effect = RuntimeError("boom")
        first = ec.get_next_earnings("XYZ")
        second = ec.get_next_earnings("XYZ")
    assert first is None and second is None
    # vendor was hit exactly once — second call short-circuited via cache
    assert yfmock.Ticker.call_count == 1


def test_beat_rate_requires_min_four_matched_quarters():
    history = [
        {"eps_estimate": 1.0, "eps_actual": 1.2, "surprise_pct": 20.0, "quarter": "q1"},
        {"eps_estimate": 1.5, "eps_actual": 1.4, "surprise_pct": -6.7, "quarter": "q2"},
        {"eps_estimate": None, "eps_actual": 1.6, "surprise_pct": None, "quarter": "q3"},
    ]
    assert ec.beat_rate(history) is None  # only 2 matched
    history.extend([
        {"eps_estimate": 2.0, "eps_actual": 2.1, "surprise_pct": 5.0, "quarter": "q4"},
        {"eps_estimate": 2.5, "eps_actual": 2.6, "surprise_pct": 4.0, "quarter": "q5"},
    ])
    # 4 matched quarters, 3 beats → 75%
    assert ec.beat_rate(history) == 75.0


def test_is_earnings_window_boundaries():
    today = date(2026, 5, 6)
    next_e = today + timedelta(days=10)
    with mock.patch.object(ec, "yf") as yfmock:
        yfmock.Ticker.return_value = _mock_ticker_with_calendar(next_date=next_e)
        # Expiry well after earnings, far outside window
        in_w, info = ec.is_earnings_window("AAPL", next_e + timedelta(days=21),
                                          window_trading_days=5, today=today)
        assert info is not None
        assert in_w is False

        # Expiry on the earnings date — definitely in window
        ec.reset_cache()
        yfmock.Ticker.return_value = _mock_ticker_with_calendar(next_date=next_e)
        in_w, _ = ec.is_earnings_window("AAPL", next_e,
                                        window_trading_days=5, today=today)
        assert in_w is True


def test_build_summary_combines_next_and_history():
    today = date(2026, 5, 6)
    next_e = today + timedelta(days=21)
    history = [
        {"epsEstimate": 1.0, "epsActual": 1.1, "quarter": pd.Timestamp("2024-03-31")},
        {"epsEstimate": 1.2, "epsActual": 1.3, "quarter": pd.Timestamp("2024-06-30")},
        {"epsEstimate": 1.4, "epsActual": 1.5, "quarter": pd.Timestamp("2024-09-30")},
        {"epsEstimate": 1.6, "epsActual": 1.7, "quarter": pd.Timestamp("2024-12-31")},
    ]
    df = pd.DataFrame(history).set_index("quarter")

    with mock.patch.object(ec, "yf") as yfmock:
        ticker_obj = SimpleNamespace(
            calendar={"Earnings Date": [next_e]},
            earnings_dates=None,
            earnings_history=df,
        )
        yfmock.Ticker.return_value = ticker_obj
        summary = ec.build_summary("AAPL", today=today)

    assert summary is not None
    assert summary["ticker"] == "AAPL"
    assert summary["next_earnings"]["date"] == next_e.isoformat()
    assert summary["beat_rate_pct"] == 100.0
    assert len(summary["history"]) == 4


# ──────────────────────────────────────────────────────────────────────
# news_enrichment_loader — earnings env-var path
# ──────────────────────────────────────────────────────────────────────


def test_loader_returns_empty_string_with_no_envvars():
    assert nel.build_enrichment_prefix("AAPL") == ""


def test_loader_renders_earnings_only_prefix(tmp_path: Path):
    payload = {
        "schema_version": 1,
        "tickers": [
            {
                "ticker": "AAPL",
                "next_earnings": {
                    "date": "2026-08-01",
                    "days_away": 87,
                    "trading_days_away": 62,
                    "source": "yfinance",
                    "confirmed": True,
                },
                "beat_rate_pct": 75.0,
            }
        ],
    }
    p = tmp_path / "earnings_calendar.json"
    p.write_text(json.dumps(payload))
    os.environ[nel.EARNINGS_ENV_VAR] = str(p)
    nel.reset_cache()
    text = nel.build_enrichment_prefix("AAPL")
    assert "Next earnings: 2026-08-01" in text
    assert "in 87 days" in text
    assert "8Q beat rate 75%" in text


def test_loader_combines_news_and_earnings(tmp_path: Path):
    news_payload = {
        "schema_version": 1,
        "lookback_days": 30,
        "tickers": [{
            "ticker": "AAPL",
            "lookback_days": 30,
            "sentiment": {"finbert": {"scorer": "FinBERT", "aggregate": 0.12,
                                       "n_headlines": 50, "trigger_terms": []}},
            "themes": [{"label": "earnings", "confidence": 0.45}],
        }],
    }
    earn_payload = {
        "schema_version": 1,
        "tickers": [{
            "ticker": "AAPL",
            "next_earnings": {"date": "2026-08-01", "days_away": 87,
                              "trading_days_away": 62, "source": "yfinance",
                              "confirmed": True},
        }],
    }
    np_path = tmp_path / "news_enrichment.json"
    np_path.write_text(json.dumps(news_payload))
    ep_path = tmp_path / "earnings_calendar.json"
    ep_path.write_text(json.dumps(earn_payload))
    os.environ[nel.ENRICHMENT_ENV_VAR] = str(np_path)
    os.environ[nel.EARNINGS_ENV_VAR] = str(ep_path)
    nel.reset_cache()
    text = nel.build_enrichment_prefix("AAPL")
    assert "FinBERT sentiment +0.12" in text
    assert "Top themes: earnings" in text
    assert "Next earnings: 2026-08-01" in text


# ──────────────────────────────────────────────────────────────────────
# build_options_overlay — earnings-aware tenor adjustment
# ──────────────────────────────────────────────────────────────────────


def _stub_contract(strike: float, exp: date, *, today: date,
                   delta: float = 0.55, mark: float = 5.0,
                   open_interest: int = 500) -> dict:
    """Polygon-shaped snapshot dict that ``_build_strategy`` expects."""
    return {
        "details": {
            "ticker": f"O:TEST{exp.strftime('%y%m%d')}C{int(strike*1000):08d}",
            "strike_price": strike,
            "expiration_date": exp.isoformat(),
            "contract_type": "call",
        },
        "greeks": {"delta": delta, "theta": -0.02, "vega": 0.10},
        "implied_volatility": 0.30,
        "open_interest": open_interest,
        "last_quote": {"bid": mark - 0.05, "ask": mark + 0.05},
        "last_trade": {"price": mark},
    }


def test_overlay_pushes_expiry_past_earnings_when_long_horizon():
    """When target horizon is ≥60 DTE and the chosen expiry is within
    ±7 calendar days of earnings, swap to the next expiry past earnings."""
    from scripts import build_options_overlay as bo

    today = date(2026, 5, 6)
    earnings = today + timedelta(days=120)
    near_exp = earnings + timedelta(days=3)
    safe_exp = earnings + timedelta(days=20)

    row = {
        "ticker": "TEST",
        "current_price_usd": 100.0,
        "aggressive_pt": 130.0,
        "conservative_pt": 110.0,
        "aggressive_horizon": "12 months",
        "aggressive_rating": "Buy",
        "conservative_rating": "Hold",
        "classification": "PICK",
        "pt_compression_pct": 8.0,
    }

    chain = [
        _stub_contract(95.0, near_exp, today=today, delta=0.62, mark=10.0),
        _stub_contract(100.0, near_exp, today=today, delta=0.55, mark=8.0),
        _stub_contract(105.0, near_exp, today=today, delta=0.45, mark=6.0),
        _stub_contract(95.0, safe_exp, today=today, delta=0.63, mark=10.5),
        _stub_contract(100.0, safe_exp, today=today, delta=0.55, mark=8.5),
        _stub_contract(105.0, safe_exp, today=today, delta=0.46, mark=6.5),
    ]

    earnings_info = {
        "date": earnings.isoformat(),
        "days_away": 120,
        "trading_days_away": 86,
        "source": "yfinance",
        "confirmed": True,
    }

    strat = bo._build_strategy(
        row, chain, today, risk_free=0.045, min_oi=50,
        strategy_mode="long-call", long_call_delta=0.55,
        earnings_info=earnings_info,
    )

    assert strat is not None
    assert strat["expiration"] == safe_exp.isoformat(), (
        "Should have pushed expiry past earnings, but landed on "
        f"{strat['expiration']}"
    )
    assert strat.get("earnings_note"), "earnings_note should be populated"
    assert "earnings" in strat["earnings_note"].lower()


def test_overlay_does_not_push_for_short_horizon():
    """Short horizons can legitimately straddle earnings (catalyst trade).
    Threshold is 90 DTE — '1-3 months' (target_dte=75) stays put."""
    from scripts import build_options_overlay as bo

    today = date(2026, 5, 6)
    earnings = today + timedelta(days=40)
    near_exp = earnings + timedelta(days=3)  # 43 DTE total

    row = {
        "ticker": "TEST",
        "current_price_usd": 100.0,
        "aggressive_pt": 130.0,
        "conservative_pt": 110.0,
        "aggressive_horizon": "1-3 months",  # target_dte=75, below 90 threshold
        "aggressive_rating": "Buy",
        "conservative_rating": "Hold",
        "classification": "PICK",
        "pt_compression_pct": 8.0,
    }

    chain = [
        _stub_contract(95.0, near_exp, today=today, delta=0.62, mark=8.0),
        _stub_contract(100.0, near_exp, today=today, delta=0.55, mark=6.0),
        _stub_contract(105.0, near_exp, today=today, delta=0.45, mark=4.0),
    ]

    earnings_info = {
        "date": earnings.isoformat(),
        "days_away": 40,
        "trading_days_away": 28,
        "source": "yfinance",
        "confirmed": True,
    }

    strat = bo._build_strategy(
        row, chain, today, risk_free=0.045, min_oi=50,
        strategy_mode="long-call", long_call_delta=0.55,
        earnings_info=earnings_info,
    )

    assert strat is not None
    assert strat["expiration"] == near_exp.isoformat()
    assert strat.get("earnings_note") is None  # no push, no note


def test_overlay_no_earnings_info_is_passthrough():
    """When earnings_info is None, behavior must be identical to before."""
    from scripts import build_options_overlay as bo

    today = date(2026, 5, 6)
    exp = today + timedelta(days=180)

    row = {
        "ticker": "TEST",
        "current_price_usd": 100.0,
        "aggressive_pt": 130.0,
        "conservative_pt": 110.0,
        "aggressive_horizon": "12 months",
        "aggressive_rating": "Buy",
        "conservative_rating": "Hold",
        "classification": "PICK",
        "pt_compression_pct": 8.0,
    }

    chain = [
        _stub_contract(95.0, exp, today=today, delta=0.62, mark=10.0),
        _stub_contract(100.0, exp, today=today, delta=0.55, mark=8.0),
        _stub_contract(105.0, exp, today=today, delta=0.45, mark=6.0),
    ]

    strat = bo._build_strategy(
        row, chain, today, risk_free=0.045, min_oi=50,
        strategy_mode="long-call", long_call_delta=0.55,
        earnings_info=None,
    )
    assert strat is not None
    assert strat.get("earnings_note") is None


def test_overlay_warns_when_no_safe_expiry_after_earnings():
    """Edge case — earnings sits right before the longest available
    expiry. Code should not crash and should preserve the original
    expiry while warning."""
    from scripts import build_options_overlay as bo

    today = date(2026, 5, 6)
    earnings = today + timedelta(days=120)
    near_exp = earnings + timedelta(days=3)  # only this one available

    row = {
        "ticker": "TEST",
        "current_price_usd": 100.0,
        "aggressive_pt": 130.0,
        "conservative_pt": 110.0,
        "aggressive_horizon": "12 months",
        "aggressive_rating": "Buy",
        "conservative_rating": "Hold",
        "classification": "PICK",
        "pt_compression_pct": 8.0,
    }

    chain = [
        _stub_contract(95.0, near_exp, today=today, delta=0.62, mark=10.0),
        _stub_contract(100.0, near_exp, today=today, delta=0.55, mark=8.0),
        _stub_contract(105.0, near_exp, today=today, delta=0.45, mark=6.0),
    ]

    earnings_info = {
        "date": earnings.isoformat(),
        "days_away": 120,
        "trading_days_away": 86,
        "source": "yfinance",
        "confirmed": True,
    }

    strat = bo._build_strategy(
        row, chain, today, risk_free=0.045, min_oi=50,
        strategy_mode="long-call", long_call_delta=0.55,
        earnings_info=earnings_info,
    )
    assert strat is not None
    assert strat["expiration"] == near_exp.isoformat()
    assert strat.get("earnings_note"), "should warn that no safe expiry exists"


# ──────────────────────────────────────────────────────────────────────
# CLI builder (build_earnings_calendar.py)
# ──────────────────────────────────────────────────────────────────────


def test_builder_writes_json_with_dedup(tmp_path: Path, monkeypatch):
    from scripts import build_earnings_calendar as bec

    today = date(2026, 5, 6)

    def fake_summary(ticker: str, *, today=None):
        return {
            "ticker": ticker.upper(),
            "fetched_at": "2026-05-06T12:00:00Z",
            "next_earnings": {
                "date": (today + timedelta(days=14)).isoformat() if today else "2026-05-20",
                "days_away": 14,
                "trading_days_away": 10,
                "source": "yfinance",
                "confirmed": True,
            },
            "beat_rate_pct": 80.0,
        }

    monkeypatch.setattr(bec, "build_summary", fake_summary)
    monkeypatch.setattr(bec, "reset_cache", lambda: None)

    out = tmp_path / "earnings_calendar.json"
    rc = bec._write_calendar(
        tickers=["AAPL", "AAPL", "msft"],  # dup + casing
        out_path=out,
        max_history_quarters=8,
        verbose=False,
        today=today,
        source_run="test",
    )
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["schema_version"] == 1
    assert data["n_tickers"] == 2
    assert {row["ticker"] for row in data["tickers"]} == {"AAPL", "MSFT"}
    assert data["n_with_next_earnings"] == 2
