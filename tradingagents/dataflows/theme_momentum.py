"""Theme/basket momentum — relative strength of a ticker group vs a benchmark.

Pulls daily close bars from Polygon (stocks/ETFs) and computes 5d/20d/60d
returns plus relative strength vs a benchmark (default SPY).

All public functions are fail-soft: a missing ticker returns None fields
rather than raising. Network I/O is mockable via _make_request.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .polygon_common import _make_request, PolygonError


def fetch_daily_closes(
    ticker: str,
    lookback_days: int = 90,
    as_of: date | None = None,
) -> list[float]:
    """Return a list of daily close prices, oldest first."""
    end = as_of or date.today()
    start = end - timedelta(days=int(lookback_days * 1.5))
    path = f"/v2/aggs/ticker/{ticker}/range/1/day/{start.isoformat()}/{end.isoformat()}"
    try:
        data = _make_request(path, {"adjusted": "true", "sort": "asc", "limit": "50000"})
    except PolygonError:
        return []
    results = data.get("results") or []
    return [bar["c"] for bar in results if "c" in bar]


def compute_returns(closes: list[float]) -> dict[str, float | None]:
    """Compute 5d, 20d, 60d, and total returns from a close series."""
    if len(closes) < 2:
        return {"5d_pct": None, "20d_pct": None, "60d_pct": None, "total_pct": None}
    total = (closes[-1] / closes[0] - 1) * 100

    def _ret(n: int) -> float | None:
        if len(closes) <= n:
            return None
        return (closes[-1] / closes[-n - 1] - 1) * 100

    return {
        "5d_pct": _ret(5),
        "20d_pct": _ret(20),
        "60d_pct": _ret(60),
        "total_pct": round(total, 2),
    }


def relative_strength(
    ticker_return: float | None,
    benchmark_return: float | None,
) -> float | None:
    """Excess return of ticker over benchmark. None if either is missing."""
    if ticker_return is None or benchmark_return is None:
        return None
    return round(ticker_return - benchmark_return, 2)


def build_theme_snapshot(
    basket: list[str],
    benchmark: str = "SPY",
    lookback_days: int = 90,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Build a momentum snapshot for a basket of tickers vs a benchmark."""
    ref_date = as_of or date.today()
    bench_closes = fetch_daily_closes(benchmark, lookback_days, ref_date)
    bench_returns = compute_returns(bench_closes)

    basket_entries = []
    for ticker in basket:
        closes = fetch_daily_closes(ticker, lookback_days, ref_date)
        returns = compute_returns(closes)
        vs = {}
        for key in ("5d_pct", "20d_pct", "60d_pct"):
            vs[key] = relative_strength(returns.get(key), bench_returns.get(key))
        basket_entries.append({"ticker": ticker, "returns": returns, "vs_benchmark": vs})

    avg = {}
    for key in ("5d_pct", "20d_pct", "60d_pct"):
        vals = [e["returns"][key] for e in basket_entries if e["returns"][key] is not None]
        avg[key] = round(sum(vals) / len(vals), 2) if vals else None

    return {
        "as_of": ref_date.isoformat(),
        "lookback_days": lookback_days,
        "benchmark": {"ticker": benchmark, "returns": bench_returns},
        "basket": basket_entries,
        "basket_avg": avg,
    }
