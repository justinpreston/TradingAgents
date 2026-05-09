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
