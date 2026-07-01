#!/usr/bin/env python3
"""Fast fundamentals lookup for a single ticker — wraps the fundamentals
dataflow + insider transactions without running the full pipeline.

Usage:
    .venv/bin/python scripts/quick_fundamentals.py NVDA
    .venv/bin/python scripts/quick_fundamentals.py INTC --no-insider
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ticker")
    parser.add_argument("--date", default=None,
                        help="Point-in-time date YYYY-MM-DD (defaults to today)")
    parser.add_argument("--no-insider", action="store_true",
                        help="Skip insider transactions section")
    args = parser.parse_args()

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    try:
        from scripts._env import load_repo_env
        load_repo_env()
    except Exception:
        pass

    from tradingagents.dataflows.polygon_finance import get_fundamentals

    ticker = args.ticker.upper()
    curr_date = args.date or datetime.utcnow().strftime("%Y-%m-%d")

    print(f"# Fundamentals · {ticker} · point-in-time {curr_date}\n")
    print(get_fundamentals(ticker, curr_date))

    if not args.no_insider:
        # Insider transactions route to yfinance/AV per default_config.tool_vendors
        from tradingagents.dataflows.interface import route_to_vendor
        print("\n## Insider transactions (last quarter)\n")
        try:
            print(route_to_vendor("get_insider_transactions", ticker))
        except Exception as exc:  # noqa: BLE001
            print(f"_(insider lookup failed: {exc})_")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
