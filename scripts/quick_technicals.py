#!/usr/bin/env python3
"""Fast technicals lookup for a single ticker — OHLCV + technical indicators.
Wraps the market dataflow without running the full pipeline.

Usage:
    .venv/bin/python scripts/quick_technicals.py NVDA
    .venv/bin/python scripts/quick_technicals.py INTC --days 60
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timedelta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker")
    parser.add_argument("--days", type=int, default=30,
                        help="Window in days (default 30)")
    parser.add_argument("--end-date", default=None,
                        help="End date YYYY-MM-DD (defaults to today)")
    parser.add_argument("--no-indicators", action="store_true",
                        help="Skip technical indicators section")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(".env")
    except Exception:
        pass
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

    from tradingagents.dataflows.polygon_finance import get_stock_data, get_indicators

    ticker = args.ticker.upper()
    end = args.end_date or datetime.utcnow().strftime("%Y-%m-%d")
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=args.days)).strftime("%Y-%m-%d")

    print(f"# Technicals · {ticker} · {start} → {end}\n")
    print("## OHLCV\n")
    print(get_stock_data(ticker, start, end))
    if not args.no_indicators:
        # Same panel the market_analyst typically requests
        for ind in ("rsi", "macd", "close_50_sma", "close_200_sma", "boll"):
            print(f"\n## {ind.upper()}\n")
            try:
                print(get_indicators(ticker, ind, end, args.days))
            except Exception as exc:  # noqa: BLE001
                print(f"_({ind} failed: {exc})_")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
