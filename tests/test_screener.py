"""Smoke tests for the early-cycle screener — pure logic only, no API calls."""

from __future__ import annotations

import math

from tradingagents.screener.fundamentals import (
    FundamentalSignals,
    compute_fundamental_signals,
)
from tradingagents.screener.score import (
    build_candidate,
    composite_score,
    rank_candidates,
)
from tradingagents.screener.technicals import (
    TechnicalSignals,
    _moving_average,
    _rsi,
    compute_technical_signals,
)
from tradingagents.screener.universe import (
    UniverseEntry,
    _is_common_stock_shape,
)


def _synthetic_bars(n: int, start_price: float = 100.0, drift: float = 0.001, vol: float = 0.01):
    """Generate ``n`` synthetic daily bars with mild upward drift."""
    bars = []
    price = start_price
    for i in range(n):
        # Deterministic pseudo-noise via sine
        noise = math.sin(i * 0.7) * vol
        price = price * (1 + drift + noise)
        h = price * 1.005
        l = price * 0.995
        bars.append({"o": price, "h": h, "l": l, "c": price, "v": 1_000_000 + (i * 1_000)})
    return bars


def test_common_stock_shape():
    assert _is_common_stock_shape("MSFT")
    assert _is_common_stock_shape("AAPL")
    assert not _is_common_stock_shape("BRK.B")  # preferred class
    assert not _is_common_stock_shape("ABCDEF")  # too long
    assert not _is_common_stock_shape("ABCDU")  # unit
    assert not _is_common_stock_shape("ABCDW")  # warrant
    assert not _is_common_stock_shape("")


def test_moving_average():
    assert _moving_average([1, 2, 3, 4, 5], 5) == 3.0
    assert _moving_average([1, 2, 3], 5) == 0.0  # insufficient
    assert _moving_average([10, 20, 30], 2) == 25.0  # last 2 only


def test_rsi_extremes():
    # Monotonic up → RSI = 100
    up = list(range(1, 50))
    assert _rsi([float(x) for x in up], 14) > 99.0
    # Monotonic down → RSI = 0
    down = list(range(50, 1, -1))
    assert _rsi([float(x) for x in down], 14) < 1.0


def test_compute_technical_signals_with_bars():
    bars = _synthetic_bars(280, start_price=50.0, drift=0.003)
    sig = compute_technical_signals("FAKE", bars=bars)
    assert sig.bars_count == 280
    assert sig.last_close > 50.0
    assert sig.ma_50 > 0
    assert sig.ma_200 > 0
    assert sig.rsi_14 > 0
    assert sig.high_252 >= sig.last_close * 0.99
    # With consistent uptrend, 50d > 200d should hold
    assert sig.ma_50 > sig.ma_200
    assert sig.ma_stack_bullish
    assert 0 <= sig.technical_score <= 100


def test_compute_technical_signals_insufficient():
    bars = _synthetic_bars(50)  # < 200
    sig = compute_technical_signals("FAKE", bars=bars)
    assert "insufficient_history" in sig.flags
    assert sig.technical_score == 0


def test_compute_fundamental_re_acceleration():
    """Synthesize 8 quarterly reports with re-accelerating revenue."""
    revenues_by_q = [80, 82, 85, 90, 95, 100, 110, 125]  # last 4 YoY: +18.75, +21.95, +29.41, +38.89
    cogs_by_q = [50, 51, 52, 54, 55, 56, 58, 60]  # gross margins improving
    reports = []
    # Use strictly increasing dates so the orchestrator sorts them correctly
    quarter_ends = [
        "2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31",
        "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
    ]
    for i, (r, c) in enumerate(zip(revenues_by_q, cogs_by_q)):
        reports.append({
            "period_of_report_date": quarter_ends[i],
            "financials": {
                "income_statement": {
                    "revenues": {"value": r * 1_000_000},
                    "cost_of_revenue": {"value": c * 1_000_000},
                    "operating_income_loss": {"value": (r - c - 10) * 1_000_000},
                }
            },
        })
    sig = compute_fundamental_signals("FAKE", reports=reports)
    assert sig.quarters_available == 8
    assert sig.revenue_yoy_accelerating, f"yoy={sig.revenue_yoy}"
    assert sig.revenue_growth_strong, f"latest yoy={sig.revenue_yoy[-1] if sig.revenue_yoy else None}"
    assert sig.gross_margin_expanding, f"gm={sig.gross_margin_quarterly}"
    assert sig.fundamental_score >= 70, f"score={sig.fundamental_score}"


def test_compute_fundamental_insufficient():
    sig = compute_fundamental_signals("FAKE", reports=[])
    assert "insufficient_data" in sig.flags
    assert sig.fundamental_score == 0


def test_composite_score_weighting():
    tech = TechnicalSignals(ticker="X", technical_score=80.0)
    fund = FundamentalSignals(ticker="X", fundamental_score=60.0)
    s = composite_score(tech, fund, technical_weight=0.5, fundamental_weight=0.5)
    assert s == 70.0


def test_composite_score_insufficient_fundamentals():
    tech = TechnicalSignals(ticker="X", technical_score=80.0)
    fund = FundamentalSignals(ticker="X", flags=["insufficient_data"])
    s = composite_score(tech, fund)
    # 15% penalty applied to tech-only path
    assert abs(s - 80.0 * 0.85) < 0.01


def test_rank_candidates_assigns_rank():
    universe = UniverseEntry(ticker="A", market_cap=1e10)
    tech_a = TechnicalSignals(ticker="A", technical_score=90.0)
    fund_a = FundamentalSignals(ticker="A", fundamental_score=80.0)
    tech_b = TechnicalSignals(ticker="B", technical_score=50.0)
    fund_b = FundamentalSignals(ticker="B", fundamental_score=50.0)
    universe_b = UniverseEntry(ticker="B", market_cap=1e10)

    cands = [
        build_candidate(universe, tech_a, fund_a),
        build_candidate(universe_b, tech_b, fund_b),
    ]
    ranked = rank_candidates(cands)
    assert ranked[0].ticker == "A"
    assert ranked[0].rank == 1
    assert ranked[1].ticker == "B"
    assert ranked[1].rank == 2
    assert ranked[0].composite_score > ranked[1].composite_score


if __name__ == "__main__":
    import sys
    funcs = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for f in funcs:
        try:
            f()
            print(f"  ✓ {f.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {f.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {f.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
