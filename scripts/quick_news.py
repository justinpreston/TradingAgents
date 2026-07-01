#!/usr/bin/env python3
"""Fast news lookup for a single ticker — wraps the news dataflow without
running the full LangGraph pipeline.

Usage:
    .venv/bin/python scripts/quick_news.py NVDA
    .venv/bin/python scripts/quick_news.py INTC --days 14
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime, timedelta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker")
    parser.add_argument("--days", type=int, default=7,
                        help="Lookback window in days (default 7)")
    parser.add_argument("--end-date", default=None,
                        help="End date YYYY-MM-DD (defaults to today)")
    args = parser.parse_args()

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    try:
        from scripts._env import load_repo_env
        load_repo_env()
    except Exception:
        pass

    from tradingagents.dataflows.polygon_news import get_news
    end = args.end_date or datetime.utcnow().strftime("%Y-%m-%d")
    start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=args.days)).strftime("%Y-%m-%d")

    ticker = args.ticker.upper()
    print(f"# News · {ticker} · {start} → {end}\n")
    print(get_news(ticker, start, end))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
