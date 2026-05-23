# Portfolio Market-Awareness Scripts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build three scripts that give the `/portfolio` skill real-time market awareness: theme momentum, short interest/volume, and live position greeks.

**Architecture:** Each script is a standalone CLI in `scripts/` that reads Polygon (or yfinance for indices) and writes JSON + optional markdown to stdout. They follow the same pattern as `build_options_overlay.py` and `build_macro_snapshot.py`: thin CLI wrapper over pure functions, fail-soft on any single ticker, mock-friendly via `_make_request`. Each has a companion dataflow module in `tradingagents/dataflows/` for the Polygon/yfinance calls, and tests that mock all network I/O.

**Tech Stack:** Polygon REST API (`_make_request` from `polygon_common.py`), yfinance (for index tickers VIX/SPY), argparse, pytest with `unittest.mock.patch`.

---

## File Map

### Script 1: Theme Momentum
- Create: `tradingagents/dataflows/theme_momentum.py` — pure functions for pulling daily bars and computing relative strength
- Create: `scripts/build_theme_momentum.py` — CLI wrapper
- Create: `tests/test_theme_momentum.py` — unit tests

### Script 2: Short Interest / Volume
- Create: `tradingagents/dataflows/polygon_shorts.py` — Polygon `/stocks/v1/short-interest` and `/stocks/v1/short-volume` wrappers
- Create: `scripts/build_short_interest.py` — CLI wrapper
- Create: `tests/test_short_interest.py` — unit tests

### Script 3: Live Position Greeks
- Create: `scripts/build_position_greeks.py` — reads positions.json, pulls Polygon options snapshots, computes aggregate portfolio greeks
- Create: `tests/test_position_greeks.py` — unit tests

---

## Task 1: Theme Momentum — dataflow module

**Files:**
- Create: `tradingagents/dataflows/theme_momentum.py`
- Test: `tests/test_theme_momentum.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_theme_momentum.py
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
        # ticker up 10%, benchmark up 5%
        rs = relative_strength(10.0, 5.0)
        assert rs == pytest.approx(5.0, abs=0.01)

    def test_none_inputs(self):
        assert relative_strength(None, 5.0) is None
        assert relative_strength(10.0, None) is None


class TestBuildThemeSnapshot:
    @patch("tradingagents.dataflows.theme_momentum.fetch_daily_closes")
    def test_snapshot_structure(self, mock_fetch):
        # All tickers return same series
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_theme_momentum.py -x -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tradingagents.dataflows.theme_momentum'`

- [ ] **Step 3: Write the implementation**

```python
# tradingagents/dataflows/theme_momentum.py
"""Theme/basket momentum — relative strength of a ticker group vs a benchmark.

Pulls daily close bars from Polygon (stocks/ETFs) or yfinance (indices
like SPY, ^VIX) and computes 5d/20d/60d returns plus relative strength
vs a benchmark (default SPY).

Designed for the portfolio skill's market-awareness layer: "is the AI
stack still ripping or rolling over?"

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
    """Return a list of daily close prices, oldest first.

    Uses Polygon /v2/aggs for stocks/ETFs. Returns [] on any error.
    """
    end = as_of or date.today()
    start = end - timedelta(days=int(lookback_days * 1.5))  # calendar days buffer
    path = f"/v2/aggs/ticker/{ticker}/range/1/day/{start.isoformat()}/{end.isoformat()}"
    try:
        data = _make_request(path, {"adjusted": "true", "sort": "asc", "limit": "50000"})
    except PolygonError:
        return []
    results = data.get("results") or []
    return [bar["c"] for bar in results if "c" in bar]


def compute_returns(closes: list[float]) -> dict[str, float | None]:
    """Compute 5d, 20d, 60d, and total returns from a close series.

    Returns dict with keys: 5d_pct, 20d_pct, 60d_pct, total_pct.
    None for any window that doesn't have enough data.
    """
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
    """Build a momentum snapshot for a basket of tickers vs a benchmark.

    Returns::

        {
            "as_of": "2026-05-09",
            "lookback_days": 90,
            "benchmark": {"ticker": "SPY", "returns": {...}},
            "basket": [
                {"ticker": "NVDA", "returns": {...}, "vs_benchmark": {...}},
                ...
            ],
            "basket_avg": {"5d_pct": ..., "20d_pct": ..., "60d_pct": ...},
        }
    """
    ref_date = as_of or date.today()

    # Benchmark
    bench_closes = fetch_daily_closes(benchmark, lookback_days, ref_date)
    bench_returns = compute_returns(bench_closes)

    # Basket
    basket_entries = []
    for ticker in basket:
        closes = fetch_daily_closes(ticker, lookback_days, ref_date)
        returns = compute_returns(closes)
        vs = {}
        for key in ("5d_pct", "20d_pct", "60d_pct"):
            vs[key] = relative_strength(returns.get(key), bench_returns.get(key))
        basket_entries.append({
            "ticker": ticker,
            "returns": returns,
            "vs_benchmark": vs,
        })

    # Basket average
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_theme_momentum.py -x -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add tradingagents/dataflows/theme_momentum.py tests/test_theme_momentum.py
git commit -m "feat(dataflows): add theme_momentum module with relative strength vs benchmark"
```

---

## Task 2: Theme Momentum — CLI script

**Files:**
- Create: `scripts/build_theme_momentum.py`

- [ ] **Step 1: Write the CLI script**

```python
#!/usr/bin/env python3
"""Build a theme/basket momentum snapshot.

Computes 5d/20d/60d returns for a basket of tickers and their relative
strength vs a benchmark (default SPY). Designed for the portfolio skill
to answer "is the AI stack still ripping or rolling over?"

Usage::

    # AI stack vs SPY
    .venv/bin/python scripts/build_theme_momentum.py \
        --tickers NVDA INTC GOOG MSFT MU BE --benchmark SPY

    # From positions file
    .venv/bin/python scripts/build_theme_momentum.py --from-positions

    # Sector ETFs
    .venv/bin/python scripts/build_theme_momentum.py \
        --tickers SMH SOXX XLK --benchmark SPY

    # JSON output
    .venv/bin/python scripts/build_theme_momentum.py \
        --tickers NVDA MU --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from tradingagents.dataflows.theme_momentum import build_theme_snapshot  # noqa: E402


def render_markdown(snap: dict) -> str:
    lines = [f"# Theme momentum — {snap['as_of']}", ""]
    bench = snap["benchmark"]
    br = bench["returns"]
    lines.append(f"**Benchmark:** {bench['ticker']}  "
                 f"5d {_fmt(br.get('5d_pct'))} · "
                 f"20d {_fmt(br.get('20d_pct'))} · "
                 f"60d {_fmt(br.get('60d_pct'))}")
    lines.append("")
    lines.append("| Ticker | 5d | 20d | 60d | vs bench 5d | vs bench 20d | vs bench 60d |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for e in snap["basket"]:
        r = e["returns"]
        v = e["vs_benchmark"]
        lines.append(
            f"| {e['ticker']} | {_fmt(r.get('5d_pct'))} | {_fmt(r.get('20d_pct'))} | "
            f"{_fmt(r.get('60d_pct'))} | {_fmt(v.get('5d_pct'))} | "
            f"{_fmt(v.get('20d_pct'))} | {_fmt(v.get('60d_pct'))} |"
        )
    avg = snap.get("basket_avg", {})
    lines.append("")
    lines.append(f"**Basket avg:** 5d {_fmt(avg.get('5d_pct'))} · "
                 f"20d {_fmt(avg.get('20d_pct'))} · "
                 f"60d {_fmt(avg.get('60d_pct'))}")
    return "\n".join(lines)


def _fmt(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:+.1f}%"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tickers", nargs="+", default=None,
                   help="Basket tickers (space-separated)")
    p.add_argument("--from-positions", action="store_true",
                   help="Read tickers from runs/portfolio/positions.json")
    p.add_argument("--benchmark", default="SPY", help="Benchmark ticker (default SPY)")
    p.add_argument("--lookback", type=int, default=90, help="Lookback days (default 90)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    tickers = args.tickers
    if args.from_positions:
        pos_file = REPO_ROOT / "runs" / "portfolio" / "positions.json"
        pos = json.loads(pos_file.read_text())
        tickers = list({p["ticker"] for p in pos.get("positions", [])})
        tickers.sort()

    if not tickers:
        print("No tickers specified. Use --tickers or --from-positions.", file=sys.stderr)
        return 1

    snap = build_theme_snapshot(tickers, args.benchmark, args.lookback)

    if args.json:
        print(json.dumps(snap, indent=2, default=str))
    else:
        print(render_markdown(snap))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Smoke test with live data**

Run: `.venv/bin/python scripts/build_theme_momentum.py --tickers NVDA MU INTC GOOG MSFT --benchmark SPY`
Expected: markdown table with 5d/20d/60d returns and vs-benchmark columns

- [ ] **Step 3: Commit**

```bash
git add scripts/build_theme_momentum.py
git commit -m "feat(scripts): add build_theme_momentum CLI for basket vs benchmark"
```

---

## Task 3: Short Interest / Volume — dataflow module

**Files:**
- Create: `tradingagents/dataflows/polygon_shorts.py`
- Create: `tests/test_short_interest.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_short_interest.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_short_interest.py -x -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

```python
# tradingagents/dataflows/polygon_shorts.py
"""Polygon short interest and short volume data.

Wraps the Stocks Developer endpoints:
- /stocks/v1/short-interest — biweekly FINRA short interest
- /stocks/v1/short-volume — daily short volume with venue breakdown

Fail-soft: returns None per ticker on any error rather than raising.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .polygon_common import _make_request, PolygonError


def get_short_interest(ticker: str, limit: int = 1) -> dict[str, Any] | None:
    """Fetch the most recent short interest report for a ticker.

    Returns the latest FINRA settlement with short_interest,
    days_to_cover, avg_daily_volume. Returns None on error or no data.
    """
    try:
        data = _make_request(
            "/stocks/v1/short-interest",
            {"ticker": ticker, "limit": str(limit), "sort": "settlement_date.desc"},
        )
    except PolygonError:
        return None
    results = data.get("results") or []
    return results[0] if results else None


def get_short_volume(ticker: str, limit: int = 1) -> dict[str, Any] | None:
    """Fetch the most recent daily short volume for a ticker.

    Returns short_volume, total_volume, short_volume_ratio, and
    venue breakdown. Returns None on error or no data.
    """
    try:
        data = _make_request(
            "/stocks/v1/short-volume",
            {"ticker": ticker, "limit": str(limit), "sort": "date.desc"},
        )
    except PolygonError:
        return None
    results = data.get("results") or []
    return results[0] if results else None


def build_shorts_snapshot(
    tickers: list[str],
    as_of: date | None = None,
) -> dict[str, Any]:
    """Build a combined short interest + volume snapshot for a list of tickers.

    Returns::

        {
            "as_of": "2026-05-09",
            "tickers": [
                {
                    "ticker": "NVDA",
                    "short_interest": {...} | None,
                    "short_volume": {...} | None,
                },
                ...
            ]
        }
    """
    ref_date = as_of or date.today()
    entries = []
    for ticker in tickers:
        entries.append({
            "ticker": ticker,
            "short_interest": get_short_interest(ticker),
            "short_volume": get_short_volume(ticker),
        })
    return {"as_of": ref_date.isoformat(), "tickers": entries}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_short_interest.py -x -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add tradingagents/dataflows/polygon_shorts.py tests/test_short_interest.py
git commit -m "feat(dataflows): add polygon_shorts module for short interest and volume"
```

---

## Task 4: Short Interest / Volume — CLI script

**Files:**
- Create: `scripts/build_short_interest.py`

- [ ] **Step 1: Write the CLI script**

```python
#!/usr/bin/env python3
"""Build short interest and short volume snapshot for held tickers.

Pulls Polygon /stocks/v1/short-interest (biweekly FINRA) and
/stocks/v1/short-volume (daily) for each ticker. Designed for
the portfolio skill to surface squeeze potential and sentiment.

Usage::

    .venv/bin/python scripts/build_short_interest.py --tickers NVDA MU INTC

    # From positions file
    .venv/bin/python scripts/build_short_interest.py --from-positions

    # JSON output
    .venv/bin/python scripts/build_short_interest.py --from-positions --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from tradingagents.dataflows.polygon_shorts import build_shorts_snapshot  # noqa: E402


def render_markdown(snap: dict) -> str:
    lines = [f"# Short interest / volume — {snap['as_of']}", ""]
    lines.append("| Ticker | Short Interest | Days to Cover | Avg Daily Vol | Short Vol Ratio | Short Vol Date |")
    lines.append("|---|---:|---:|---:|---:|---|")
    for e in snap["tickers"]:
        si = e.get("short_interest") or {}
        sv = e.get("short_volume") or {}
        lines.append(
            f"| {e['ticker']} "
            f"| {_fmtint(si.get('short_interest'))} "
            f"| {_fmtf(si.get('days_to_cover'))} "
            f"| {_fmtint(si.get('avg_daily_volume'))} "
            f"| {_fmtf(sv.get('short_volume_ratio'), suffix='%')} "
            f"| {sv.get('date', '—')} |"
        )
    return "\n".join(lines)


def _fmtint(v) -> str:
    if v is None:
        return "—"
    return f"{int(v):,}"


def _fmtf(v, suffix: str = "") -> str:
    if v is None:
        return "—"
    return f"{v:.2f}{suffix}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tickers", nargs="+", default=None)
    p.add_argument("--from-positions", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    tickers = args.tickers
    if args.from_positions:
        pos_file = REPO_ROOT / "runs" / "portfolio" / "positions.json"
        pos = json.loads(pos_file.read_text())
        tickers = list({p["ticker"] for p in pos.get("positions", [])})
        tickers.sort()

    if not tickers:
        print("No tickers specified. Use --tickers or --from-positions.", file=sys.stderr)
        return 1

    snap = build_shorts_snapshot(tickers)

    if args.json:
        print(json.dumps(snap, indent=2, default=str))
    else:
        print(render_markdown(snap))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Smoke test with live data**

Run: `.venv/bin/python scripts/build_short_interest.py --tickers NVDA MU INTC`
Expected: markdown table with short interest, days to cover, short volume ratio

- [ ] **Step 3: Commit**

```bash
git add scripts/build_short_interest.py
git commit -m "feat(scripts): add build_short_interest CLI for short interest and volume"
```

---

## Task 5: Live Position Greeks — tests and implementation

**Files:**
- Create: `scripts/build_position_greeks.py`
- Create: `tests/test_position_greeks.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_position_greeks.py
"""Position greeks tests — mocks Polygon options snapshot."""

from __future__ import annotations

from unittest.mock import patch

import pytest

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
        # delta: 3*100*0.67 + 5*100*0.50 = 201 + 250 = 451
        assert agg["total_delta"] == pytest.approx(451.0, abs=0.1)
        # theta: 3*100*(-0.07) + 5*100*(-0.05) = -21 + -25 = -46
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_position_greeks.py -x -q`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Write the implementation**

```python
#!/usr/bin/env python3
"""Build live portfolio greeks from Polygon options snapshots.

Reads positions.json, pulls live greeks/IV/OI for each options position
from Polygon's /v3/snapshot/options/ endpoint, and computes aggregate
portfolio-level delta, gamma, theta, vega exposure.

Usage::

    .venv/bin/python scripts/build_position_greeks.py
    .venv/bin/python scripts/build_position_greeks.py --json
    .venv/bin/python scripts/build_position_greeks.py --ticker NVDA
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from tradingagents.dataflows.polygon_common import _make_request, PolygonError  # noqa: E402

DEFAULT_POSITIONS = REPO_ROOT / "runs" / "portfolio" / "positions.json"


def fetch_contract_snapshot(
    underlying: str,
    strike: float,
    expiry: str,
    contract_type: str = "call",
) -> dict[str, Any] | None:
    """Fetch a single contract snapshot from Polygon options chain.

    Returns the snapshot dict with greeks, IV, OI, or None if not found.
    """
    try:
        data = _make_request(
            f"/v3/snapshot/options/{underlying}",
            {
                "strike_price": str(strike),
                "expiration_date": expiry,
                "contract_type": contract_type,
                "limit": "5",
            },
        )
    except PolygonError:
        return None
    results = data.get("results") or []
    if not results:
        return None
    # Return the closest match (usually exact)
    for r in results:
        d = r.get("details", {})
        if d.get("strike_price") == strike and d.get("expiration_date") == expiry:
            return r
    return results[0]


def aggregate_greeks(positions: list[dict[str, Any]]) -> dict[str, float]:
    """Compute aggregate portfolio greeks weighted by qty * 100 (shares per contract).

    Returns total_delta, total_gamma, total_theta_per_day, total_vega.
    """
    total_delta = 0.0
    total_gamma = 0.0
    total_theta = 0.0
    total_vega = 0.0
    for p in positions:
        qty = p.get("qty", 0)
        g = p.get("greeks") or {}
        multiplier = qty * 100
        total_delta += multiplier * (g.get("delta") or 0)
        total_gamma += multiplier * (g.get("gamma") or 0)
        total_theta += multiplier * (g.get("theta") or 0)
        total_vega += multiplier * (g.get("vega") or 0)
    return {
        "total_delta": round(total_delta, 2),
        "total_gamma": round(total_gamma, 4),
        "total_theta_per_day": round(total_theta, 2),
        "total_vega": round(total_vega, 2),
    }


def build_portfolio_greeks(
    positions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build live greeks for all options positions.

    Skips equity positions. Returns per-position greeks + aggregate.
    """
    option_positions = []
    for pos in positions:
        if pos.get("instrument") == "equity" or not pos.get("option"):
            continue
        opt = pos["option"]
        contract_type = "call" if "call" in pos.get("instrument", "") else "put"
        snap = fetch_contract_snapshot(
            pos["ticker"],
            opt["strike"],
            opt["expiry"],
            contract_type,
        )
        entry = {
            "id": pos.get("id"),
            "ticker": pos["ticker"],
            "instrument": pos["instrument"],
            "qty": pos.get("qty", 0),
            "strike": opt["strike"],
            "expiry": opt["expiry"],
        }
        if snap:
            g = snap.get("greeks") or {}
            entry["live_greeks"] = g
            entry["greeks"] = g  # also used by aggregate_greeks
            entry["implied_volatility"] = snap.get("implied_volatility")
            entry["open_interest"] = snap.get("open_interest")
            entry["underlying_price"] = (snap.get("underlying_asset") or {}).get("price")
            entry["mark"] = (snap.get("day") or {}).get("close")
            entry["break_even"] = snap.get("break_even_price")
        else:
            entry["live_greeks"] = None
            entry["greeks"] = {}
            entry["implied_volatility"] = None
            entry["open_interest"] = None
            entry["underlying_price"] = None
            entry["mark"] = None
            entry["break_even"] = None
        option_positions.append(entry)

    agg = aggregate_greeks(option_positions)
    return {
        "as_of": date.today().isoformat(),
        "positions": option_positions,
        "aggregate": agg,
    }


def render_markdown(result: dict) -> str:
    lines = [f"# Portfolio greeks — {result['as_of']}", ""]
    agg = result["aggregate"]
    lines.append("## Aggregate exposure")
    lines.append(f"- **Total delta:** {agg['total_delta']:,.0f} shares equivalent")
    lines.append(f"- **Total gamma:** {agg['total_gamma']:,.4f}")
    lines.append(f"- **Total theta:** ${agg['total_theta_per_day']:,.2f}/day")
    lines.append(f"- **Total vega:** {agg['total_vega']:,.2f}")
    lines.append("")
    lines.append("## Per-position")
    lines.append("| Ticker | Strike | Expiry | Qty | Delta | Gamma | Theta | Vega | IV | OI | Underlying | Mark |")
    lines.append("|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for p in result["positions"]:
        g = p.get("live_greeks") or {}
        lines.append(
            f"| {p['ticker']} | {p['strike']} | {p['expiry']} | {p['qty']} "
            f"| {_fmtf(g.get('delta'))} | {_fmtf(g.get('gamma'), 4)} "
            f"| {_fmtf(g.get('theta'))} | {_fmtf(g.get('vega'))} "
            f"| {_fmtpct(p.get('implied_volatility'))} | {_fmtint(p.get('open_interest'))} "
            f"| {_fmtprice(p.get('underlying_price'))} | {_fmtprice(p.get('mark'))} |"
        )
    return "\n".join(lines)


def _fmtf(v, prec: int = 3) -> str:
    return f"{v:.{prec}f}" if v is not None else "—"


def _fmtpct(v) -> str:
    return f"{v*100:.1f}%" if v is not None else "—"


def _fmtint(v) -> str:
    return f"{int(v):,}" if v is not None else "—"


def _fmtprice(v) -> str:
    return f"${v:.2f}" if v is not None else "—"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--positions-file", type=Path, default=DEFAULT_POSITIONS)
    p.add_argument("--ticker", type=str, default=None,
                   help="Limit to one ticker (case-insensitive)")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    pos_data = json.loads(args.positions_file.read_text())
    positions = pos_data.get("positions", [])

    if args.ticker:
        positions = [p for p in positions if p["ticker"].upper() == args.ticker.upper()]

    result = build_portfolio_greeks(positions)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(render_markdown(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_position_greeks.py -x -q`
Expected: all pass

- [ ] **Step 5: Smoke test with live data**

Run: `.venv/bin/python scripts/build_position_greeks.py`
Expected: markdown table showing live greeks per options position + aggregate delta/theta/vega

- [ ] **Step 6: Commit**

```bash
git add scripts/build_position_greeks.py tests/test_position_greeks.py
git commit -m "feat(scripts): add build_position_greeks for live portfolio greeks from Polygon"
```

---

## Task 6: Run full test suite

- [ ] **Step 1: Run all tests**

Run: `.venv/bin/python -m pytest tests/ -x -q`
Expected: all existing tests still pass + new tests pass

- [ ] **Step 2: Smoke test all three scripts end-to-end**

```bash
.venv/bin/python scripts/build_theme_momentum.py --from-positions --benchmark SPY
.venv/bin/python scripts/build_short_interest.py --from-positions
.venv/bin/python scripts/build_position_greeks.py
```

Expected: all three produce markdown output without errors

- [ ] **Step 3: Final commit if any fixups needed**

```bash
git add -A
git commit -m "fix: address any test/lint issues from market-awareness scripts"
```
