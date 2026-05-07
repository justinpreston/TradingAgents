"""Tests for tradingagents.dataflows.volatility_context."""
from __future__ import annotations

import math
import random
from datetime import date

import pytest

from tradingagents.dataflows import volatility_context as vc


def _fake_bars(n: int, *, seed: int = 0,
               start_price: float = 100.0,
               sigma: float = 0.20) -> list[dict]:
    """Generate ``n`` synthetic daily bars with a given annualized vol."""
    rng = random.Random(seed)
    daily_sigma = sigma / math.sqrt(vc._TRADING_DAYS_PER_YEAR)
    bars = []
    p = start_price
    for i in range(n):
        r = rng.gauss(0, daily_sigma)
        p = p * math.exp(r)
        bars.append({"c": p, "t": i})
    return bars


@pytest.fixture(autouse=True)
def _clear_cache():
    vc.reset_cache()
    yield
    vc.reset_cache()


def test_compute_log_returns_basic():
    closes = [100.0, 110.0, 99.0]
    out = vc._compute_log_returns(closes)
    assert len(out) == 2
    assert math.isclose(out[0], math.log(110.0 / 100.0), rel_tol=1e-9)
    assert math.isclose(out[1], math.log(99.0 / 110.0), rel_tol=1e-9)


def test_compute_log_returns_skips_zero_or_negative():
    closes = [100.0, 0.0, 105.0, -1.0, 110.0]
    out = vc._compute_log_returns(closes)
    # All adjacent pairs touch a non-positive value, so none are valid.
    assert out == []
    # Sanity: a clean sequence interspersed with one zero should still yield
    # the valid neighboring pair.
    out2 = vc._compute_log_returns([100.0, 110.0, 0.0, 120.0, 130.0])
    assert len(out2) == 2  # (100,110) and (120,130)


def test_rolling_realized_vol_window():
    log_returns = [0.01] * 60
    series = vc._rolling_realized_vol(log_returns, window=30)
    assert len(series) == 31  # n - window + 1
    # All-equal returns → zero std
    assert all(math.isclose(v, 0.0, abs_tol=1e-9) for v in series)


def test_rolling_realized_vol_short_history_returns_empty():
    log_returns = [0.01] * 5
    assert vc._rolling_realized_vol(log_returns, window=30) == []


def test_rank_pct_clamps_and_orders():
    history = [0.10, 0.20, 0.30, 0.40, 0.50]
    rank, pct = vc._rank_pct(0.30, history)
    # (0.30 - 0.10) / (0.50 - 0.10) * 100 = 50.0
    assert math.isclose(rank, 50.0, abs_tol=1e-6)
    # 2 of 5 strictly below 0.30
    assert math.isclose(pct, 40.0, abs_tol=1e-6)


def test_rank_pct_at_extremes():
    history = [0.1, 0.2, 0.3]
    assert vc._rank_pct(0.05, history)[0] == 0.0
    assert vc._rank_pct(1.0, history)[0] == 100.0


def test_rank_pct_constant_history_returns_50():
    rank, _ = vc._rank_pct(0.2, [0.1, 0.1, 0.1])
    assert rank == 50.0


def test_backdrop_favorable_when_low_hv_and_low_premium():
    assert vc._backdrop_label(20.0, 1.10, False) == "favorable"


def test_backdrop_unfavorable_when_premium_elevated():
    assert vc._backdrop_label(20.0, 1.60, False) == "unfavorable"


def test_backdrop_unfavorable_when_hv_elevated():
    assert vc._backdrop_label(80.0, 1.10, False) == "unfavorable"


def test_backdrop_mixed_default():
    assert vc._backdrop_label(50.0, 1.30, False) == "mixed"


def test_backdrop_earnings_window_trumps_favorable():
    # Even with favorable HV/IV signature, an earnings warning forces "mixed"
    assert vc._backdrop_label(20.0, 1.10, True) == "mixed"


def test_compute_vol_context_fetch_error(monkeypatch):
    def fake_fetch(t, today, lookback_days, max_retries=3):
        return ([], "boom")

    monkeypatch.setattr(vc, "_fetch_daily_aggs", fake_fetch)
    out = vc.compute_vol_context("FAKE", today=date(2026, 5, 6))
    assert out["status"] == "fetch_error"
    assert out["long_call_backdrop"] == "mixed"


def test_compute_vol_context_insufficient_history(monkeypatch):
    bars = _fake_bars(40, seed=1)  # < 60 needed for a useful RV history

    def fake_fetch(t, today, lookback_days, max_retries=3):
        return (bars, None)

    monkeypatch.setattr(vc, "_fetch_daily_aggs", fake_fetch)
    out = vc.compute_vol_context("FAKE", today=date(2026, 5, 6))
    assert out["status"] == "insufficient_history"


def test_compute_vol_context_ok_with_iv(monkeypatch):
    # 300 bars of sigma=0.30 vol → enough for hv + 252-day rank
    bars = _fake_bars(320, seed=42, sigma=0.30)

    def fake_fetch(t, today, lookback_days, max_retries=3):
        return (bars, None)

    monkeypatch.setattr(vc, "_fetch_daily_aggs", fake_fetch)
    out = vc.compute_vol_context(
        "FAKE", today=date(2026, 5, 6), current_iv=0.45
    )
    assert out["status"] == "ok"
    assert out["hv_30d"] > 0.0
    assert 0.0 <= out["hv_rank_252d"] <= 100.0
    assert out["current_iv"] == 0.45
    assert out["iv_rv_ratio"] > 1.0  # 0.45 IV vs ~0.30 RV
    assert out["long_call_backdrop"] in {"favorable", "mixed", "unfavorable"}


def test_compute_vol_context_caches_per_ticker(monkeypatch):
    calls = {"n": 0}
    bars = _fake_bars(320, seed=7)

    def fake_fetch(t, today, lookback_days, max_retries=3):
        calls["n"] += 1
        return (bars, None)

    monkeypatch.setattr(vc, "_fetch_daily_aggs", fake_fetch)
    today = date(2026, 5, 6)
    vc.compute_vol_context("FAKE", today=today)
    vc.compute_vol_context("FAKE", today=today)
    vc.compute_vol_context("FAKE", today=today, current_iv=0.5)
    # 3 calls, but only 1 fetch (others reuse cache)
    assert calls["n"] == 1


def test_render_vol_advisory_returns_none_on_error():
    assert vc.render_vol_advisory({"status": "fetch_error"}) is None
    assert vc.render_vol_advisory({"status": "insufficient_history"}) is None


def test_render_vol_advisory_includes_hv_and_iv_rv():
    out = vc.render_vol_advisory({
        "status": "ok",
        "hv_30d": 0.30,
        "hv_rank_252d": 25.0,
        "iv_rv_ratio": 1.20,
        "long_call_backdrop": "favorable",
    })
    assert out is not None
    assert "favorable" in out
    assert "HV rank 25" in out
    assert "1.20×" in out


def test_render_vol_advisory_falls_back_to_hv_when_no_iv_ratio():
    out = vc.render_vol_advisory({
        "status": "ok",
        "hv_30d": 0.40,
        "hv_rank_252d": 60.0,
        "long_call_backdrop": "mixed",
    })
    assert out is not None
    assert "30d HV" in out
    assert "mixed" in out
