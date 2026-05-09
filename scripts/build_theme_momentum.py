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
