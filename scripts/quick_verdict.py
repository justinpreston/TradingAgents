#!/usr/bin/env python3
"""Fast dual-frame verdict on a single ticker — shells out to
run_copilot_matrix.py for one ticker, parses verdict_ledger.json, prints a
plain-English summary with Rating, PTs, compression, tier, and a one-line
ticket. This is the "ask a question, get a verdict" entry point.

Usage:
    .venv/bin/python scripts/quick_verdict.py NVDA
    .venv/bin/python scripts/quick_verdict.py INTC --max-parallel 2
    .venv/bin/python scripts/quick_verdict.py CRWV --no-chronos --no-iv-surface

This is slower than the other quick_* scripts (~3-5 min) because it runs the
full per-ticker analysis under both aggressive AND conservative personas.
Use the other quick_* scripts for raw data; use this for a verdict.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
from datetime import datetime

from tradingagents.tiers import tier_for_row


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _tier(row: dict) -> str:
    """Canonical tier rule. Delegates to tradingagents.tiers.tier_for_row.
    Note: treats conservative_pt == "" the same as None — preserved here."""
    if row.get("conservative_pt") == "":
        row = {**row, "conservative_pt": None}
    return tier_for_row(row, suspect_caps_a=False)


def _fmt(v, prefix: str = "$") -> str:
    if v in (None, ""):
        return "—"
    try:
        return f"{prefix}{float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ticker")
    parser.add_argument("--max-parallel", type=int, default=2)
    parser.add_argument("--no-chronos", action="store_true",
                        help="Skip Chronos overlay (faster)")
    parser.add_argument("--no-iv-surface", action="store_true",
                        help="Skip IV-surface scoring (faster)")
    parser.add_argument("--date", default=None,
                        help="Trade date YYYY-MM-DD (defaults to today)")
    parser.add_argument("--keep-run", action="store_true",
                        help="Don't delete the matrix run dir after parsing")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON output instead of markdown")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except Exception:
        pass

    ticker = args.ticker.upper()
    trade_date = args.date or datetime.utcnow().strftime("%Y-%m-%d")
    run_id = f"quick_verdict_{ticker}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

    cmd = [
        sys.executable, "run_copilot_matrix.py",
        "--tickers", ticker,
        "--profiles", "aggressive", "conservative",
        "--max-parallel", str(args.max_parallel),
        "--stop-on-overweight", "0",
        "--date", trade_date,
        "--run-id", run_id,
        "--no-dashboard",
    ]
    if args.no_chronos:
        cmd.append("--no-chronos")
    if args.no_iv_surface:
        cmd.append("--no-iv-surface")

    print(f"# Quick verdict · {ticker} · {trade_date}", file=sys.stderr)
    print(f"# Running: {' '.join(cmd)}\n", file=sys.stderr)

    try:
        subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            check=True,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as exc:
        print(f"_(matrix runner failed exit={exc.returncode})_", file=sys.stderr)
        return exc.returncode

    run_dir = REPO_ROOT / "runs" / run_id
    ledger_path = run_dir / "verdict_ledger.json"
    if not ledger_path.exists():
        print(f"_(no verdict_ledger.json at {ledger_path})_", file=sys.stderr)
        return 2
    rows = json.loads(ledger_path.read_text())
    row = next((r for r in rows if (r.get("ticker") or "").upper() == ticker), None)
    if row is None:
        print(f"_(no row for {ticker} in ledger)_", file=sys.stderr)
        return 3

    tier = _tier(row)
    classification = row.get("classification") or "—"
    compression = row.get("pt_compression_pct")
    agg_pt = row.get("aggressive_pt")
    cons_pt = row.get("conservative_pt")
    price = row.get("current_price")
    agg_rating = row.get("aggressive_rating")
    cons_rating = row.get("conservative_rating")

    payload = {
        "ticker": ticker,
        "trade_date": trade_date,
        "classification": classification,
        "tier": tier,
        "current_price": price,
        "aggressive_rating": agg_rating,
        "aggressive_pt": agg_pt,
        "conservative_rating": cons_rating,
        "conservative_pt": cons_pt,
        "pt_compression_pct": compression,
        "run_dir": str(run_dir),
    }

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(f"## {ticker} · {tier} · {classification}\n")
        print(f"- **Current price**: {_fmt(price)}")
        print(f"- **Aggressive**: {agg_rating or '—'} · PT {_fmt(agg_pt)}")
        print(f"- **Conservative**: {cons_rating or '—'} · PT {_fmt(cons_pt)}")
        if compression is not None:
            print(f"- **PT compression**: {compression:.1f}% (A < 5%)")
        if classification == "PICK" and price and agg_pt:
            upside = (float(agg_pt) - float(price)) / float(price) * 100
            print(f"- **Implied upside**: {upside:+.1f}%")
        print(f"\n**Run dir**: `{run_dir.relative_to(REPO_ROOT)}`")
        per_ticker = run_dir / "per_ticker" / f"{ticker}.md"
        if per_ticker.exists():
            print(f"**Drilldown**: `{per_ticker.relative_to(REPO_ROOT)}`")

    if not args.keep_run:
        # Default: keep — verdict runs are cheap to inspect and the indexer
        # will pick them up. Only print the path.
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
